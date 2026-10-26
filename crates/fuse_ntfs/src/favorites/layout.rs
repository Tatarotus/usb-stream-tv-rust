//! Minimal, dependency-free JSON reader for the exported favorites package.
//!
//! The deployment package is produced offline by
//! `scripts/build_live_favorites_template.py`: a sparse NTFS template, a layout
//! JSON with the real runlists, and a package JSON binding both SHA-256
//! digests. This module validates those artifacts before the runtime is built.
//! It is not a general JSON library; it only accepts the values the package
//! schema needs and rejects malformed/ambiguous input.

use std::collections::{BTreeMap, HashMap};

use crate::favorites::digest::sha256_hex;
use crate::favorites::extents::{ExtentMap, MapError};
use crate::favorites::runtime::FavoriteFile;
use crate::vfs::ntfs_mft::RunPiece;

pub const LAYOUT_SCHEMA_VERSION: u64 = 1;
pub const PACKAGE_SCHEMA_VERSION: u64 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JsonError {
    UnexpectedEnd,
    Unexpected(char),
    Trailing,
    Depth,
    BadNumber,
    BadEscape,
    BadUnicode,
    Expected(&'static str),
    DuplicateKey(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Json {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Json>),
    Object(BTreeMap<String, Json>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(map) => map.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Number(value) => value.parse().ok(),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(values) => Some(values),
            _ => None,
        }
    }
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self {
            bytes,
            pos: 0,
            depth: 0,
        }
    }

    fn peek(&self) -> Option<char> {
        self.bytes.get(self.pos).map(|&b| b as char)
    }

    fn bump(&mut self) -> Option<char> {
        let value = self.peek()?;
        self.pos += 1;
        Some(value)
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(' ' | '\n' | '\r' | '\t')) {
            self.pos += 1;
        }
    }

    fn value(&mut self) -> Result<Json, JsonError> {
        self.skip_ws();
        self.depth += 1;
        if self.depth > 64 {
            return Err(JsonError::Depth);
        }
        let result = match self.peek() {
            Some('{') => self.object(),
            Some('[') => self.array(),
            Some('"') => self.string().map(Json::String),
            Some('t') => self.literal("true", Json::Bool(true)),
            Some('f') => self.literal("false", Json::Bool(false)),
            Some('n') => self.literal("null", Json::Null),
            Some('-') | Some('0'..='9') => self.number(),
            Some(ch) => Err(JsonError::Unexpected(ch)),
            None => Err(JsonError::UnexpectedEnd),
        };
        self.depth -= 1;
        result
    }

    fn literal(&mut self, text: &str, value: Json) -> Result<Json, JsonError> {
        if self.bytes[self.pos..].starts_with(text.as_bytes()) {
            self.pos += text.len();
            Ok(value)
        } else {
            Err(JsonError::Expected("literal"))
        }
    }

    fn number(&mut self) -> Result<Json, JsonError> {
        let start = self.pos;
        if self.peek() == Some('-') {
            self.pos += 1;
        }
        let mut digits = 0usize;
        while matches!(self.peek(), Some('0'..='9')) {
            self.pos += 1;
            digits += 1;
        }
        if self.peek() == Some('.') {
            self.pos += 1;
            while matches!(self.peek(), Some('0'..='9')) {
                self.pos += 1;
                digits += 1;
            }
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some('+' | '-')) {
                self.pos += 1;
            }
            while matches!(self.peek(), Some('0'..='9')) {
                self.pos += 1;
            }
        }
        if digits == 0 {
            return Err(JsonError::BadNumber);
        }
        let text =
            std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| JsonError::BadNumber)?;
        Ok(Json::Number(text.to_string()))
    }

    fn string(&mut self) -> Result<String, JsonError> {
        if self.bump() != Some('"') {
            return Err(JsonError::Expected("string"));
        }
        let mut out = String::new();
        loop {
            match self.bump().ok_or(JsonError::UnexpectedEnd)? {
                '"' => return Ok(out),
                '\\' => match self.bump().ok_or(JsonError::UnexpectedEnd)? {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'b' => out.push('\u{0008}'),
                    'f' => out.push('\u{000c}'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'u' => {
                        let mut code = 0u32;
                        for _ in 0..4 {
                            let ch = self.bump().ok_or(JsonError::BadUnicode)?;
                            code = (code << 4) | ch.to_digit(16).ok_or(JsonError::BadUnicode)?;
                        }
                        let ch = char::from_u32(code).ok_or(JsonError::BadUnicode)?;
                        out.push(ch);
                    }
                    _ => return Err(JsonError::BadEscape),
                },
                ch if (ch as u32) < 0x20 => return Err(JsonError::BadEscape),
                ch => out.push(ch),
            }
        }
    }

    fn array(&mut self) -> Result<Json, JsonError> {
        if self.bump() != Some('[') {
            return Err(JsonError::Expected("["));
        }
        let mut values = Vec::new();
        self.skip_ws();
        if self.peek() == Some(']') {
            self.pos += 1;
            return Ok(Json::Array(values));
        }
        loop {
            values.push(self.value()?);
            self.skip_ws();
            match self.bump() {
                Some(',') => continue,
                Some(']') => return Ok(Json::Array(values)),
                Some(ch) => return Err(JsonError::Unexpected(ch)),
                None => return Err(JsonError::UnexpectedEnd),
            }
        }
    }

    fn object(&mut self) -> Result<Json, JsonError> {
        if self.bump() != Some('{') {
            return Err(JsonError::Expected("{"));
        }
        let mut values = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some('}') {
            self.pos += 1;
            return Ok(Json::Object(values));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            if self.bump() != Some(':') {
                return Err(JsonError::Expected(":"));
            }
            let value = self.value()?;
            if values.insert(key.clone(), value).is_some() {
                return Err(JsonError::DuplicateKey(key));
            }
            self.skip_ws();
            match self.bump() {
                Some(',') => continue,
                Some('}') => return Ok(Json::Object(values)),
                Some(ch) => return Err(JsonError::Unexpected(ch)),
                None => return Err(JsonError::UnexpectedEnd),
            }
        }
    }
}

pub fn parse(bytes: &[u8]) -> Result<Json, JsonError> {
    let mut parser = Parser::new(bytes);
    let value = parser.value()?;
    parser.skip_ws();
    if parser.pos != bytes.len() {
        return Err(JsonError::Trailing);
    }
    Ok(value)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LayoutError {
    Json(JsonError),
    Schema { expected: u64, found: u64 },
    Missing(&'static str),
    Invalid(&'static str),
    UnsupportedClusterSize(u64),
    DuplicatePath(String),
    UnknownPath(String),
    MissingFileId(String),
    Extent(MapError),
    Empty,
    SizeMismatch { file_id: u32 },
    Manifest(String),
}

impl From<JsonError> for LayoutError {
    fn from(value: JsonError) -> Self {
        Self::Json(value)
    }
}

impl From<MapError> for LayoutError {
    fn from(value: MapError) -> Self {
        Self::Extent(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LayoutFile {
    pub path: String,
    pub mft_inode: u64,
    pub logical_size: u64,
    pub runs: Vec<RunPiece>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Layout {
    pub schema_version: u64,
    pub cluster_size: u64,
    pub volume_bytes: u64,
    pub files: Vec<LayoutFile>,
}

impl Layout {
    pub fn parse(bytes: &[u8]) -> Result<Self, LayoutError> {
        let json = parse(bytes)?;
        let schema_version = json
            .get("schema_version")
            .and_then(Json::as_u64)
            .ok_or(LayoutError::Missing("schema_version"))?;
        if schema_version != LAYOUT_SCHEMA_VERSION {
            return Err(LayoutError::Schema {
                expected: LAYOUT_SCHEMA_VERSION,
                found: schema_version,
            });
        }
        let cluster_size = json
            .get("cluster_size")
            .and_then(Json::as_u64)
            .ok_or(LayoutError::Missing("cluster_size"))?;
        if cluster_size != 4096 {
            return Err(LayoutError::UnsupportedClusterSize(cluster_size));
        }
        let volume_bytes = json
            .get("volume_bytes")
            .and_then(Json::as_u64)
            .ok_or(LayoutError::Missing("volume_bytes"))?;
        let files_json = json
            .get("files")
            .and_then(Json::as_array)
            .ok_or(LayoutError::Missing("files"))?;
        let mut files = Vec::with_capacity(files_json.len());
        let mut paths = HashMap::new();
        for file in files_json {
            let path = file
                .get("path")
                .and_then(Json::as_str)
                .ok_or(LayoutError::Missing("path"))?
                .to_string();
            let mft_inode = file
                .get("mft_inode")
                .and_then(Json::as_u64)
                .ok_or(LayoutError::Missing("mft_inode"))?;
            let logical_size = file
                .get("logical_size")
                .and_then(Json::as_u64)
                .ok_or(LayoutError::Missing("logical_size"))?;
            let runs_json = file
                .get("runs")
                .and_then(Json::as_array)
                .ok_or(LayoutError::Missing("runs"))?;
            let mut runs = Vec::with_capacity(runs_json.len());
            for run in runs_json {
                let vcn = run
                    .get("vcn")
                    .and_then(Json::as_u64)
                    .ok_or(LayoutError::Missing("vcn"))?;
                let lcn = run
                    .get("lcn")
                    .and_then(Json::as_u64)
                    .ok_or(LayoutError::Missing("lcn"))?;
                let clusters = run
                    .get("clusters")
                    .and_then(Json::as_u64)
                    .ok_or(LayoutError::Missing("clusters"))?;
                if clusters == 0 {
                    return Err(LayoutError::Invalid("clusters"));
                }
                runs.push(RunPiece {
                    vcn,
                    length_clusters: clusters,
                    lcn: Some(lcn),
                });
            }
            if runs.is_empty() || logical_size == 0 {
                return Err(LayoutError::Invalid("file"));
            }
            if paths.insert(path.clone(), ()).is_some() {
                return Err(LayoutError::DuplicatePath(path));
            }
            files.push(LayoutFile {
                path,
                mft_inode,
                logical_size,
                runs,
            });
        }
        if files.is_empty() {
            return Err(LayoutError::Empty);
        }
        Ok(Self {
            schema_version,
            cluster_size,
            volume_bytes,
            files,
        })
    }

    /// Builds the identity-preserving disk map. `file_ids` maps the exact
    /// layout path (e.g. `Favoritos/Abertos/SBT.trp`) to the manifest id.
    /// Gaps before/after the files are marked metadata so the dispatcher never
    /// falls back to a global active channel there.
    pub fn build_runtime_files(
        &self,
        file_ids: &HashMap<String, (u32, String, u64)>,
    ) -> Result<(ExtentMap, Vec<FavoriteFile>), LayoutError> {
        let mut map = ExtentMap::new(self.volume_bytes)?;
        let mut runtime_files = Vec::with_capacity(self.files.len());
        let mut first = u64::MAX;
        let mut last = 0u64;
        for file in &self.files {
            let key = file.path.trim_start_matches('/');
            let (file_id, channel_id, manifest_size) = file_ids
                .get(key)
                .or_else(|| file_ids.get(&file.path))
                .cloned()
                .ok_or_else(|| LayoutError::MissingFileId(file.path.clone()))?;
            if manifest_size != file.logical_size {
                return Err(LayoutError::SizeMismatch { file_id });
            }
            map.add_file_runs(file_id, &file.runs, file.logical_size)?;
            let extents = map.file_extents(file_id);
            first = first.min(extents.iter().map(|e| e.disk_start).min().unwrap_or(0));
            for run in &file.runs {
                if let Some(lcn) = run.lcn {
                    let allocation_end =
                        lcn.saturating_add(run.length_clusters).saturating_mul(4096);
                    last = last.max(allocation_end);
                }
            }
            runtime_files.push(FavoriteFile {
                file_id,
                channel_id,
                path: key.to_string(),
                logical_size: file.logical_size,
            });
        }
        if first > 0 {
            map.add_metadata(0, first)?;
        }
        if last < self.volume_bytes {
            map.add_metadata(last, self.volume_bytes)?;
        }
        Ok((map, runtime_files))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestMap {
    pub manifest_id: String,
    pub files: HashMap<String, (u32, String, u64)>,
}

/// Extracts the manifest identity map used by [`Layout::build_runtime_files`].
/// The canonical `manifest_id` and every entry are validated by
/// `Manifest::from_json`; a tampered manifest cannot be paired with the layout.
pub fn manifest_file_ids(manifest_bytes: &[u8]) -> Result<ManifestMap, LayoutError> {
    let manifest = crate::favorites::manifest::Manifest::from_json(manifest_bytes)
        .map_err(|error| LayoutError::Manifest(format!("{error:?}")))?;
    let mut files = HashMap::new();
    for entry in manifest.entries {
        files.insert(
            entry.path(),
            (entry.file_id, entry.channel_id, entry.logical_size),
        );
    }
    Ok(ManifestMap {
        manifest_id: manifest.manifest_id,
        files,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Package {
    pub template_sparse_sha256: String,
    pub template_sparse_bytes: u64,
    pub layout_sha256: String,
    pub layout_bytes: u64,
    pub volume_bytes: u64,
    pub files: Vec<String>,
    pub package_id: String,
}

impl Package {
    pub fn parse(bytes: &[u8]) -> Result<Self, LayoutError> {
        let json = parse(bytes)?;
        let schema = json
            .get("schema_version")
            .and_then(Json::as_u64)
            .ok_or(LayoutError::Missing("schema_version"))?;
        if schema != PACKAGE_SCHEMA_VERSION {
            return Err(LayoutError::Schema {
                expected: PACKAGE_SCHEMA_VERSION,
                found: schema,
            });
        }
        let text = |key: &'static str| -> Result<String, LayoutError> {
            json.get(key)
                .and_then(Json::as_str)
                .map(str::to_string)
                .ok_or(LayoutError::Missing(key))
        };
        let number = |key: &'static str| -> Result<u64, LayoutError> {
            json.get(key)
                .and_then(Json::as_u64)
                .ok_or(LayoutError::Missing(key))
        };
        let files = json
            .get("files")
            .and_then(Json::as_array)
            .ok_or(LayoutError::Missing("files"))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_string)
                    .ok_or(LayoutError::Invalid("files"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            template_sparse_sha256: text("template_sparse_sha256")?,
            template_sparse_bytes: number("template_sparse_bytes")?,
            layout_sha256: text("layout_sha256")?,
            layout_bytes: number("layout_bytes")?,
            volume_bytes: number("volume_bytes")?,
            files,
            package_id: text("package_id")?,
        })
    }

    fn canonical_without_id(&self) -> String {
        let files = self
            .files
            .iter()
            .map(|path| format!("\"{}\"", json_escape(path)))
            .collect::<Vec<_>>()
            .join(",");
        format!(
            "{{\"files\":[{}],\"layout_bytes\":{},\"layout_sha256\":\"{}\",\"schema_version\":{},\"template_sparse_bytes\":{},\"template_sparse_sha256\":\"{}\",\"volume_bytes\":{}}}",
            files,
            self.layout_bytes,
            json_escape(&self.layout_sha256),
            PACKAGE_SCHEMA_VERSION,
            self.template_sparse_bytes,
            json_escape(&self.template_sparse_sha256),
            self.volume_bytes
        )
    }

    /// Validates the package digests against the exact template and layout
    /// bytes. A hash mismatch is fatal; callers must not fall back to a
    /// partially matching package.
    pub fn verify(
        &self,
        template_sparse: &[u8],
        layout_bytes: &[u8],
        layout: &Layout,
    ) -> Result<(), LayoutError> {
        if template_sparse.len() as u64 != self.template_sparse_bytes {
            return Err(LayoutError::SizeMismatch { file_id: 0 });
        }
        if layout_bytes.len() as u64 != self.layout_bytes {
            return Err(LayoutError::SizeMismatch { file_id: 0 });
        }
        if sha256_hex(template_sparse) != self.template_sparse_sha256 {
            return Err(LayoutError::Invalid("template_sparse_sha256"));
        }
        if sha256_hex(layout_bytes) != self.layout_sha256 {
            return Err(LayoutError::Invalid("layout_sha256"));
        }
        if self.volume_bytes != layout.volume_bytes {
            return Err(LayoutError::Invalid("volume_bytes"));
        }
        if sha256_hex(self.canonical_without_id().as_bytes()) != self.package_id {
            return Err(LayoutError::Invalid("package_id"));
        }
        let package_files: Vec<&str> = self.files.iter().map(String::as_str).collect();
        for file in &layout.files {
            if !package_files.contains(&file.path.as_str())
                && !package_files.contains(&file.path.trim_start_matches('/'))
            {
                return Err(LayoutError::UnknownPath(file.path.clone()));
            }
        }
        Ok(())
    }
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn artifact(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/live-favorites-mvp")
            .join(name)
    }

    fn ids() -> HashMap<String, (u32, String, u64)> {
        HashMap::from([
            (
                "Favoritos/Abertos/SBT.trp".to_string(),
                (7u32, "sbt".to_string(), 4_000_000_000),
            ),
            (
                "Favoritos/Abertos/Record.trp".to_string(),
                (8u32, "record".to_string(), 4_000_000_000),
            ),
        ])
    }

    #[test]
    fn parses_and_maps_the_real_package_artifacts() {
        let layout_bytes = std::fs::read(artifact("favorites_layout.json")).unwrap();
        let package_bytes = std::fs::read(artifact("favorites_package.json")).unwrap();
        let template = std::fs::read(artifact("favorites_template.sparse.gz")).unwrap();
        let layout = Layout::parse(&layout_bytes).unwrap();
        assert_eq!(layout.volume_bytes, 128 * 1024 * 1024 * 1024);
        assert_eq!(layout.files.len(), 2);
        let package = Package::parse(&package_bytes).unwrap();
        package.verify(&template, &layout_bytes, &layout).unwrap();
        let (map, files) = layout.build_runtime_files(&ids()).unwrap();
        assert_eq!(files.len(), 2);
        let sbt = map.file_extents(7);
        assert_eq!(sbt.len(), 1);
        assert_eq!(sbt[0].disk_start, 4_195_432 * 4096);
        assert!(matches!(
            map.route(sbt[0].disk_start),
            crate::favorites::extents::MapRegion::File { file_id: 7, .. }
        ));
        let record = map.file_extents(8);
        assert_eq!(record[0].disk_start, 5_176_088 * 4096);
        assert!(matches!(
            map.route(0),
            crate::favorites::extents::MapRegion::Metadata { .. }
        ));
    }

    #[test]
    fn rejects_tampered_layout_and_package() {
        let layout_bytes = std::fs::read(artifact("favorites_layout.json")).unwrap();
        let package_bytes = std::fs::read(artifact("favorites_package.json")).unwrap();
        let template = std::fs::read(artifact("favorites_template.sparse.gz")).unwrap();
        let layout = Layout::parse(&layout_bytes).unwrap();
        let package = Package::parse(&package_bytes).unwrap();
        let mut tampered = layout_bytes.clone();
        tampered.push(b' ');
        assert!(package.verify(&template, &tampered, &layout).is_err());
        let mut bad_template = template.clone();
        bad_template[0] ^= 1;
        assert!(package
            .verify(&bad_template, &layout_bytes, &layout)
            .is_err());
        assert!(Layout::parse(b"{").is_err());
        assert!(Layout::parse(b"{\"files\":[],\"files\":[]}").is_err());
        assert!(Layout::parse(b"{\"x\":1} trailing").is_err());
    }

    #[test]
    fn json_parser_handles_escapes_and_rejects_deep_input() {
        let value = parse(br#"{"a":"\u00e9\n","b":[1,2,3],"c":true}"#).unwrap();
        assert_eq!(value.get("a").and_then(Json::as_str), Some("é\n"));
        assert_eq!(value.get("b").and_then(Json::as_array).unwrap().len(), 3);
        let deep = "[".repeat(80) + &"]".repeat(80);
        assert_eq!(parse(deep.as_bytes()), Err(JsonError::Depth));
    }

    #[test]
    fn manifest_map_validates_identity_and_paths() {
        use crate::favorites::manifest::{
            entry as manifest_entry, mvp_folder, CapacityPolicy, Manifest,
        };

        let manifest = Manifest::build(
            1,
            vec![manifest_entry(
                7,
                "live",
                "sbt",
                mvp_folder("Abertos"),
                "SBT.trp".to_string(),
                4_000_000_000,
            )
            .unwrap()],
            &CapacityPolicy::mvp_two_channels(),
        )
        .unwrap();
        let json = manifest.to_json();
        let map = manifest_file_ids(json.as_bytes()).unwrap();
        assert_eq!(map.manifest_id, manifest.manifest_id);
        assert_eq!(
            map.files.get("Favoritos/Abertos/SBT.trp"),
            Some(&(7u32, "sbt".to_string(), 4_000_000_000))
        );

        let missing_id = json.replace(&manifest.manifest_id, &"a".repeat(64));
        assert!(manifest_file_ids(missing_id.as_bytes()).is_err());
        let mut tampered = manifest.clone();
        tampered.entries[0].channel_id = "other".to_string();
        assert!(manifest_file_ids(tampered.to_json().as_bytes()).is_err());
        let vod = json.replace("\"live\"", "\"vod\"");
        assert!(manifest_file_ids(vod.as_bytes()).is_err());
    }
}
