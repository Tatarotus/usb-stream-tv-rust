//! Fail-closed startup validation for the opt-in favorites mode.
//!
//! Nothing is inferred from file presence: every path, hash, identity and the
//! template geometry must be supplied explicitly. Any incoherence aborts
//! before `/dev/fuse` is mounted, so the fallback remains the untouched
//! single-Live path.

use std::collections::HashMap;
use std::fmt;
use std::fs::{self, File};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::favorites::layout::{self, Layout, LayoutError};
use crate::favorites::runtime::{FavoritesRuntime, RuntimeConfig};
use crate::vfs::ntfs_mft::{
    apply_fixup, find_attr, parse_record_meta, parse_runlist, parse_vbr, NtfsTemplate, ATTR_DATA,
    MFT_REC_IN_USE,
};

pub const MAX_LAYOUT_BYTES: u64 = 1024 * 1024;
pub const MAX_PACKAGE_BYTES: u64 = 1024 * 1024;
pub const MAX_MANIFEST_BYTES: u64 = 4 * 1024 * 1024;
pub const MAX_ARCHIVE_BYTES: u64 = 64 * 1024 * 1024;
pub const MAX_TOKEN_BYTES: u64 = 4096;
pub const MIN_TOKEN_LEN: usize = 32;
pub const MAX_TOKEN_LEN: usize = 512;
pub const MAX_SESSION_LEN: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartupError {
    MissingFlag(&'static str),
    UnknownFlag(String),
    BadFlag(&'static str),
    Io { path: String, error: String },
    TooLarge { path: String },
    Package(String),
    Manifest(String),
    Template(String),
    Runtime(String),
    Thread(String),
}

impl fmt::Display for StartupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingFlag(flag) => write!(f, "missing required flag --favorites-{flag}=VALUE"),
            Self::UnknownFlag(flag) => write!(f, "unknown flag {flag}"),
            Self::BadFlag(flag) => write!(f, "invalid value for --favorites-{flag}=VALUE"),
            Self::Io { path, error } => write!(f, "{path}: {error}"),
            Self::TooLarge { path } => write!(f, "{path}: file exceeds startup limit"),
            Self::Package(error) => write!(f, "package invalid: {error}"),
            Self::Manifest(error) => write!(f, "manifest invalid: {error}"),
            Self::Template(error) => write!(f, "template invalid: {error}"),
            Self::Runtime(error) => write!(f, "runtime invalid: {error}"),
            Self::Thread(error) => write!(f, "worker thread: {error}"),
        }
    }
}

impl std::error::Error for StartupError {}

impl From<LayoutError> for StartupError {
    fn from(value: LayoutError) -> Self {
        Self::Package(format!("{value:?}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FavoritesStartupOptions {
    pub layout_path: PathBuf,
    pub package_path: PathBuf,
    pub manifest_path: PathBuf,
    pub template_archive_path: PathBuf,
    pub template_image_path: PathBuf,
    pub session_id: String,
    pub media_address: String,
    pub token: String,
}

impl FavoritesStartupOptions {
    /// Parses only `--favorites-*` flags, all with `=VALUE`, leaving the three
    /// legacy positional arguments untouched. Returns `None` when no
    /// favorites flag is present.
    pub fn from_args(args: &[String]) -> Result<Option<Self>, StartupError> {
        let mut values: HashMap<&'static str, String> = HashMap::new();
        let mut enabled = false;
        for arg in args.iter().skip(1) {
            if !arg.starts_with("--favorites-") {
                continue;
            }
            enabled = true;
            let (name, value) = arg
                .split_once('=')
                .ok_or(StartupError::BadFlag("flag requires =VALUE"))?;
            let value = value.to_string();
            if value.is_empty() {
                return Err(StartupError::BadFlag("empty value"));
            }
            let key = match name {
                "--favorites-layout" => "layout",
                "--favorites-package" => "package",
                "--favorites-manifest" => "manifest",
                "--favorites-template-archive" => "template-archive",
                "--favorites-template-image" => "template-image",
                "--favorites-session" => "session",
                "--favorites-media" => "media",
                "--favorites-token-file" => "token-file",
                other => return Err(StartupError::UnknownFlag(other.to_string())),
            };
            if values.insert(key, value).is_some() {
                return Err(StartupError::BadFlag("duplicate flag"));
            }
        }
        if !enabled {
            return Ok(None);
        }
        let layout_path = take_path(&mut values, "layout")?;
        let package_path = take_path(&mut values, "package")?;
        let manifest_path = take_path(&mut values, "manifest")?;
        let template_archive_path = take_path(&mut values, "template-archive")?;
        let template_image_path = take_path(&mut values, "template-image")?;
        let session_id = values
            .remove("session")
            .ok_or(StartupError::MissingFlag("session"))?;
        let media_address = values
            .remove("media")
            .ok_or(StartupError::MissingFlag("media"))?;
        let token_path = take_path(&mut values, "token-file")?;
        if !values.is_empty() {
            return Err(StartupError::BadFlag("unconsumed"));
        }
        if !valid_session(&session_id) {
            return Err(StartupError::BadFlag("session"));
        }
        if !valid_media_address(&media_address) {
            return Err(StartupError::BadFlag("media"));
        }
        let token = read_token(&token_path)?;
        Ok(Some(Self {
            layout_path,
            package_path,
            manifest_path,
            template_archive_path,
            template_image_path,
            session_id,
            media_address,
            token,
        }))
    }
}

fn take_path(
    values: &mut HashMap<&'static str, String>,
    flag: &'static str,
) -> Result<PathBuf, StartupError> {
    values
        .remove(flag)
        .map(PathBuf::from)
        .ok_or(StartupError::MissingFlag(flag))
}

fn valid_session(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_SESSION_LEN
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

fn valid_media_address(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .bytes()
            .all(|byte| !byte.is_ascii_control() && !byte.is_ascii_whitespace())
        && value.rsplit_once(':').is_some_and(|(host, port)| {
            !host.is_empty() && port.parse::<u16>().is_ok_and(|number| number != 0)
        })
}

fn read_token(path: &Path) -> Result<String, StartupError> {
    let bytes = read_bounded(path, MAX_TOKEN_BYTES)?;
    let token = String::from_utf8(bytes)
        .map_err(|_| StartupError::BadFlag("token-file"))?
        .trim()
        .to_string();
    if token.len() < MIN_TOKEN_LEN || token.len() > MAX_TOKEN_LEN {
        return Err(StartupError::BadFlag("token-file"));
    }
    if token
        .bytes()
        .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
    {
        return Err(StartupError::BadFlag("token-file"));
    }
    Ok(token)
}

fn read_bounded(path: &Path, max: u64) -> Result<Vec<u8>, StartupError> {
    let metadata = fs::metadata(path).map_err(|error| StartupError::Io {
        path: path.display().to_string(),
        error: error.to_string(),
    })?;
    if !metadata.is_file() {
        return Err(StartupError::Io {
            path: path.display().to_string(),
            error: "not a regular file".into(),
        });
    }
    if metadata.len() > max {
        return Err(StartupError::TooLarge {
            path: path.display().to_string(),
        });
    }
    fs::read(path).map_err(|error| StartupError::Io {
        path: path.display().to_string(),
        error: error.to_string(),
    })
}

/// Validated startup bundle. The runtime is shared with FUSE; the template is
/// moved into `NtfsServer`.
pub struct FavoritesStartup {
    pub runtime: Arc<FavoritesRuntime>,
    pub template: NtfsTemplate,
    pub media_address: String,
    pub token: String,
    pub session_id: String,
    pub manifest_id: String,
    pub file_count: usize,
}

impl FavoritesStartup {
    pub fn load(options: FavoritesStartupOptions) -> Result<Self, StartupError> {
        let layout_bytes = read_bounded(&options.layout_path, MAX_LAYOUT_BYTES)?;
        let package_bytes = read_bounded(&options.package_path, MAX_PACKAGE_BYTES)?;
        let manifest_bytes = read_bounded(&options.manifest_path, MAX_MANIFEST_BYTES)?;
        let archive_bytes = read_bounded(&options.template_archive_path, MAX_ARCHIVE_BYTES)?;
        let layout = Layout::parse(&layout_bytes)?;
        let package = layout::Package::parse(&package_bytes)
            .map_err(|error| StartupError::Package(format!("{error:?}")))?;
        package
            .verify(&archive_bytes, &layout_bytes, &layout)
            .map_err(|error| StartupError::Package(format!("{error:?}")))?;
        if package.files.len() != layout.files.len() {
            return Err(StartupError::Package("file count mismatch".into()));
        }
        let manifest = layout::manifest_file_ids(&manifest_bytes)
            .map_err(|error| StartupError::Manifest(format!("{error:?}")))?;
        if manifest.files.len() != layout.files.len() {
            return Err(StartupError::Manifest("file count mismatch".into()));
        }
        if manifest.files.len() > super::manifest::MAX_MVP_ENTRIES {
            return Err(StartupError::Manifest("too many entries".into()));
        }
        let (map, files) = layout
            .build_runtime_files(&manifest.files)
            .map_err(|error| StartupError::Package(format!("{error:?}")))?;
        if files.is_empty() {
            return Err(StartupError::Manifest("no Live files".into()));
        }
        let mut template = NtfsTemplate::open(&options.template_image_path).map_err(|error| {
            StartupError::Template(format!(
                "{}: {error}",
                options.template_image_path.display()
            ))
        })?;
        verify_template_image(&options.template_image_path, &layout)?;
        template.randomize_serial();
        let runtime = FavoritesRuntime::new(
            map,
            files.clone(),
            RuntimeConfig::new(&options.session_id, &manifest.manifest_id),
        )
        .map_err(|error| StartupError::Runtime(format!("{error:?}")))?;
        Ok(Self {
            runtime: Arc::new(runtime),
            template,
            media_address: options.media_address,
            token: options.token,
            session_id: options.session_id,
            manifest_id: manifest.manifest_id,
            file_count: files.len(),
        })
    }
}

/// Binds the unpacked sparse image to the verified layout by reading the exact
/// MFT records (fixups applied) and comparing every `$DATA` run and size.
pub fn verify_template_image(path: &Path, layout: &Layout) -> Result<(), StartupError> {
    let metadata = fs::metadata(path).map_err(|error| StartupError::Io {
        path: path.display().to_string(),
        error: error.to_string(),
    })?;
    if metadata.len() != layout.volume_bytes {
        return Err(StartupError::Template(format!(
            "size {} != {}",
            metadata.len(),
            layout.volume_bytes
        )));
    }
    let file = File::open(path).map_err(|error| StartupError::Io {
        path: path.display().to_string(),
        error: error.to_string(),
    })?;
    let mut vbr_bytes = [0u8; 512];
    file.read_exact_at(&mut vbr_bytes, 0)
        .map_err(|error| StartupError::Template(format!("VBR: {error}")))?;
    let vbr =
        parse_vbr(&vbr_bytes).map_err(|error| StartupError::Template(format!("VBR: {error:?}")))?;
    if u64::from(vbr.cluster_size) != layout.cluster_size {
        return Err(StartupError::Template("cluster size mismatch".into()));
    }
    let declared = vbr
        .total_sectors
        .checked_add(1)
        .and_then(|sectors| sectors.checked_mul(u64::from(vbr.bytes_per_sector)))
        .ok_or_else(|| StartupError::Template("volume size overflow".into()))?;
    if declared != layout.volume_bytes {
        return Err(StartupError::Template("volume size mismatch".into()));
    }
    for entry in &layout.files {
        let offset = vbr
            .mft_lcn
            .checked_mul(u64::from(vbr.cluster_size))
            .and_then(|base| {
                entry
                    .mft_inode
                    .checked_mul(u64::from(vbr.mft_record_size))
                    .and_then(|inside| base.checked_add(inside))
            })
            .ok_or_else(|| StartupError::Template("MFT offset overflow".into()))?;
        let mut record = vec![0u8; vbr.mft_record_size as usize];
        file.read_exact_at(&mut record, offset)
            .map_err(|error| StartupError::Template(format!("MFT {}: {error}", entry.mft_inode)))?;
        if &record[..4] != b"FILE" {
            return Err(StartupError::Template(format!(
                "MFT {} has no FILE signature",
                entry.mft_inode
            )));
        }
        let meta = parse_record_meta(&record).map_err(|error| {
            StartupError::Template(format!("MFT {}: {error:?}", entry.mft_inode))
        })?;
        if meta.flags & MFT_REC_IN_USE == 0 {
            return Err(StartupError::Template(format!(
                "MFT {} is not in use",
                entry.mft_inode
            )));
        }
        apply_fixup(&mut record, 0x38).map_err(|error| {
            StartupError::Template(format!("MFT {} fixup: {error:?}", entry.mft_inode))
        })?;
        let data = find_attr(&record, meta.attrs_offset, ATTR_DATA)
            .map_err(|error| StartupError::Template(format!("MFT {}: {error:?}", entry.mft_inode)))?
            .ok_or_else(|| {
                StartupError::Template(format!("MFT {} has no $DATA", entry.mft_inode))
            })?;
        if data.real_size != entry.logical_size {
            return Err(StartupError::Template(format!(
                "MFT {} size {} != {}",
                entry.mft_inode, data.real_size, entry.logical_size
            )));
        }
        let run_start = data
            .offset
            .checked_add(usize::from(data.runlist_offset))
            .ok_or_else(|| StartupError::Template("runlist offset overflow".into()))?;
        let run_end = data
            .offset
            .checked_add(data.length as usize)
            .ok_or_else(|| StartupError::Template("runlist length overflow".into()))?;
        let run_bytes = record
            .get(run_start..run_end)
            .ok_or_else(|| StartupError::Template("runlist out of record".into()))?;
        let runs = parse_runlist(run_bytes);
        let actual: Vec<(u64, Option<u64>, u64)> = runs
            .iter()
            .map(|run| (run.vcn, run.lcn, run.length_clusters))
            .collect();
        let expected: Vec<(u64, Option<u64>, u64)> = entry
            .runs
            .iter()
            .map(|run| (run.vcn, run.lcn, run.length_clusters))
            .collect();
        if actual != expected {
            return Err(StartupError::Template(format!(
                "MFT {} runlist mismatch",
                entry.mft_inode
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::ntfs_mft::{synthesize_vbr, VbrInfo};

    fn args(values: &[&str]) -> Vec<String> {
        std::iter::once("fuse_ntfs".to_string())
            .chain(values.iter().map(|value| value.to_string()))
            .collect()
    }

    #[test]
    fn legacy_positionals_do_not_enable_favorites() {
        assert_eq!(
            FavoritesStartupOptions::from_args(&args(&["/mnt", "/pipe", "/template"])).unwrap(),
            None
        );
        assert_eq!(
            FavoritesStartupOptions::from_args(&args(&["--no-engine"])).unwrap(),
            None
        );
    }

    #[test]
    fn requires_the_complete_flag_set_with_equals_syntax() {
        let complete = [
            "--favorites-layout=/x/layout.json",
            "--favorites-package=/x/package.json",
            "--favorites-manifest=/x/manifest.json",
            "--favorites-template-archive=/x/t.gz",
            "--favorites-template-image=/x/t.bin",
            "--favorites-session=sess-1",
            "--favorites-media=127.0.0.1:9123",
            "--favorites-token-file=/x/token",
        ];
        let error = FavoritesStartupOptions::from_args(&args(&complete[..7])).unwrap_err();
        assert!(matches!(error, StartupError::MissingFlag("token-file")));
        let no_equals = ["--favorites-layout", "/x/layout.json"];
        assert!(FavoritesStartupOptions::from_args(&args(&no_equals)).is_err());
        let unknown = [complete[0], "--favorites-nope=1"];
        assert!(FavoritesStartupOptions::from_args(&args(&unknown)).is_err());
    }

    fn write_record(image: &File, offset: u64, runs: &[(u64, u64)], size: u64) {
        let mut record = vec![0u8; 1024];
        record[..4].copy_from_slice(b"FILE");
        record[4..6].copy_from_slice(&0x30u16.to_le_bytes());
        record[6..8].copy_from_slice(&3u16.to_le_bytes());
        record[0x10..0x12].copy_from_slice(&1u16.to_le_bytes());
        record[0x16..0x18].copy_from_slice(&MFT_REC_IN_USE.to_le_bytes());
        record[0x18..0x1c].copy_from_slice(&1024u32.to_le_bytes());
        record[0x14..0x16].copy_from_slice(&0x38u16.to_le_bytes());
        record[0x30..0x32].copy_from_slice(&1u16.to_le_bytes());
        for i in 1..3 {
            record[i * 512 - 2..i * 512].copy_from_slice(&1u16.to_le_bytes());
        }
        record[0x32..0x34].copy_from_slice(&0xAAAAu16.to_le_bytes());
        record[0x34..0x36].copy_from_slice(&0xBBBBu16.to_le_bytes());
        let attr = 0x38usize;
        record[attr..attr + 4].copy_from_slice(&ATTR_DATA.to_le_bytes());
        let runlist = encode_runs(runs);
        let length = 0x40 + runlist.len();
        record[attr + 4..attr + 8].copy_from_slice(&(length as u32).to_le_bytes());
        record[attr + 8] = 1;
        record[attr + 0x20..attr + 0x22].copy_from_slice(&0x40u16.to_le_bytes());
        record[attr + 0x30..attr + 0x38].copy_from_slice(&size.to_le_bytes());
        record[attr + 0x40..attr + 0x40 + runlist.len()].copy_from_slice(&runlist);
        record[attr + length..attr + length + 4].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        image.write_all_at(&record, offset).unwrap();
    }

    fn encode_runs(runs: &[(u64, u64)]) -> Vec<u8> {
        let mut out = Vec::new();
        let mut previous = 0i64;
        for (lcn, clusters) in runs {
            out.push(0x44);
            out.extend_from_slice(&(*clusters as u32).to_le_bytes());
            let delta = *lcn as i64 - previous;
            out.extend_from_slice(&(delta as i32).to_le_bytes());
            previous = *lcn as i64;
        }
        out.push(0);
        out
    }

    #[test]
    fn template_image_must_match_layout_runlists_and_size() {
        let directory = std::env::temp_dir().join(format!(
            "favorites-startup-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&directory).unwrap();
        let image_path = directory.join("template.bin");
        let volume_bytes = 128 * 1024 * 1024u64;
        let image = File::create(&image_path).unwrap();
        image.set_len(volume_bytes).unwrap();
        let mut vbr = [0u8; 512];
        let geometry = VbrInfo {
            bytes_per_sector: 512,
            sectors_per_cluster: 8,
            cluster_size: 4096,
            total_sectors: volume_bytes / 512 - 1,
            mft_lcn: 4,
            mft_mirror_lcn: 1000,
            mft_record_size: 1024,
            index_record_size: 4096,
            serial: 1,
        };
        synthesize_vbr(&mut vbr, &geometry, 1);
        image.write_all_at(&vbr, 0).unwrap();
        let mft = 4 * 4096u64;
        write_record(&image, mft + 66 * 1024, &[(100, 2)], 8192);
        let layout = Layout {
            schema_version: 1,
            cluster_size: 4096,
            volume_bytes,
            files: vec![crate::favorites::layout::LayoutFile {
                path: "/Favoritos/Abertos/SBT.trp".into(),
                mft_inode: 66,
                logical_size: 8192,
                runs: vec![crate::vfs::ntfs_mft::RunPiece {
                    vcn: 0,
                    length_clusters: 2,
                    lcn: Some(100),
                }],
            }],
        };
        verify_template_image(&image_path, &layout).unwrap();
        let mut wrong = layout.clone();
        wrong.files[0].runs[0].lcn = Some(101);
        assert!(verify_template_image(&image_path, &wrong).is_err());
        drop(image);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    #[ignore = "requires FAVORITES_TEMPLATE_IMAGE unpacked from the checked-in sparse package"]
    fn loads_the_real_package_and_template_image() {
        let directory =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/live-favorites-mvp");
        let image =
            std::env::var("FAVORITES_TEMPLATE_IMAGE").expect("set FAVORITES_TEMPLATE_IMAGE");
        let layout_bytes = fs::read(directory.join("favorites_layout.json")).unwrap();
        let layout = Layout::parse(&layout_bytes).unwrap();
        use crate::favorites::manifest::{entry as manifest_entry, CapacityPolicy, Manifest};

        let mut entries = Vec::new();
        for (index, file) in layout.files.iter().enumerate() {
            let path = file.path.trim_start_matches('/');
            let (folder, filename) = path.rsplit_once('/').unwrap();
            entries.push(
                manifest_entry(
                    66 + index as u32,
                    "live",
                    if index == 0 { "sbt" } else { "record" },
                    folder.split('/').map(str::to_string).collect(),
                    filename.to_string(),
                    file.logical_size,
                )
                .unwrap(),
            );
        }
        let manifest = Manifest::build(1, entries, &CapacityPolicy::mvp_two_channels()).unwrap();
        let manifest_id = manifest.manifest_id.clone();
        let manifest = manifest.to_json();
        let temp = std::env::temp_dir().join(format!(
            "favorites-real-startup-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&temp).unwrap();
        let manifest_path = temp.join("manifest.json");
        let token_path = temp.join("token");
        fs::write(&manifest_path, manifest).unwrap();
        fs::write(&token_path, "0123456789abcdef0123456789abcdef").unwrap();
        let options = FavoritesStartupOptions {
            layout_path: directory.join("favorites_layout.json"),
            package_path: directory.join("favorites_package.json"),
            manifest_path,
            template_archive_path: directory.join("favorites_template.sparse.gz"),
            template_image_path: PathBuf::from(image),
            session_id: "startup-session".into(),
            media_address: "127.0.0.1:9".into(),
            token: fs::read_to_string(&token_path).unwrap(),
        };
        let startup = FavoritesStartup::load(options).unwrap();
        assert_eq!(startup.file_count, 2);
        assert_eq!(startup.manifest_id, manifest_id);
        assert_eq!(
            startup.runtime.map().file_extents(66)[0].disk_start,
            4_195_432 * 4096
        );
        assert!(startup.runtime.bootstrap(66).is_some());
        let _ = fs::remove_dir_all(temp);
    }
}
