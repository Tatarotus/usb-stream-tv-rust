//! Versioned, immutable favorites manifest.
//!
//! The manifest contains only public presentation data and opaque catalog
//! identities. Provider URLs, credentials, tokens and query strings never
//! belong here; the server resolves a channel id to a live source at runtime.
//!
//! `manifest_id` is the SHA-256 of a canonical, sorted representation. The
//! template, extent map and bootstraps in the future deployment package are
//! tied to that id, so a mixed package is rejected as a whole instead of
//! publishing a partially compatible mount.

use super::digest::sha256_hex;
use super::name::{sanitize_component, sanitize_folder, trp_filename, NameError, TRP_EXTENSION};

pub const SCHEMA_VERSION: u32 = 1;
pub const LAYOUT_VERSION: u32 = 1;
/// Logical size of one virtual Live file in the MVP. It is a projection, not
/// allocated bytes: `8 * size / bitrate` bounds the advertised duration.
pub const DEFAULT_LOGICAL_SIZE: u64 = 4_000_000_000;
/// Deployed Rust volume size (128 GiB).
pub const VOLUME_128_GIB: u64 = 128 * 1024 * 1024 * 1024;
/// Bytes kept below the logical data budget for NTFS metadata, MFT growth,
/// indexes, bitmap and safety margin.
pub const DEFAULT_METADATA_RESERVE: u64 = 1 << 30;
/// Legacy C catalog ceiling; retained as an explicit default rejection limit.
pub const DEFAULT_MAX_ENTRIES: usize = 32;
pub const MIN_MVP_ENTRIES: usize = 1;
pub const MAX_MVP_ENTRIES: usize = 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    Live,
}

impl EntryKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Live => "live",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestEntry {
    /// Stable, ordering-independent identity derived from namespace + channel.
    pub file_id: u32,
    /// Opaque id resolvable by the server; never a URL.
    pub channel_id: String,
    /// Catalog namespace, so equal ids from different catalogs are distinct.
    pub namespace: String,
    pub kind: EntryKind,
    /// Directory components below the `Favoritos` root.
    pub folder: Vec<String>,
    /// Visible file name, always ending in `.trp`.
    pub filename: String,
    pub logical_size: u64,
    /// Opaque reference to a finite, per-file synthetic prefix.
    pub bootstrap_id: String,
}

impl ManifestEntry {
    pub fn path(&self) -> String {
        let mut path = String::new();
        for component in &self.folder {
            path.push_str(component);
            path.push('/');
        }
        path.push_str(&self.filename);
        path
    }

    pub fn validate(&self) -> Result<(), ManifestError> {
        if self.channel_id.trim().is_empty() {
            return Err(ManifestError::EmptyChannelId {
                file_id: self.file_id,
            });
        }
        if self.channel_id.chars().any(char::is_control) {
            return Err(ManifestError::InvalidChannelId {
                file_id: self.file_id,
            });
        }
        if self.namespace.trim().is_empty() {
            return Err(ManifestError::EmptyNamespace {
                file_id: self.file_id,
            });
        }
        if self.folder.is_empty() {
            return Err(ManifestError::EmptyFolder {
                file_id: self.file_id,
            });
        }
        sanitize_folder(&self.folder).map_err(|source| ManifestError::InvalidFolder {
            file_id: self.file_id,
            source,
        })?;
        sanitize_component(&self.filename).map_err(|source| ManifestError::InvalidFilename {
            file_id: self.file_id,
            source,
        })?;
        if !self.filename.to_ascii_lowercase().ends_with(TRP_EXTENSION) {
            return Err(ManifestError::FilenameNotTrp {
                file_id: self.file_id,
                filename: self.filename.clone(),
            });
        }
        if self.logical_size == 0 {
            return Err(ManifestError::InvalidLogicalSize {
                file_id: self.file_id,
                size: self.logical_size,
            });
        }
        if self.bootstrap_id.trim().is_empty() {
            return Err(ManifestError::EmptyBootstrap {
                file_id: self.file_id,
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapacityPolicy {
    pub volume_bytes: u64,
    pub metadata_reserve: u64,
    pub max_entries: usize,
}

impl Default for CapacityPolicy {
    fn default() -> Self {
        Self {
            volume_bytes: VOLUME_128_GIB,
            metadata_reserve: DEFAULT_METADATA_RESERVE,
            max_entries: DEFAULT_MAX_ENTRIES,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CapacityUsage {
    pub entries: usize,
    pub logical_bytes: u64,
    pub available_bytes: u64,
}

impl CapacityPolicy {
    pub fn mvp_two_channels() -> Self {
        Self {
            max_entries: MAX_MVP_ENTRIES,
            ..Self::default()
        }
    }

    pub fn available_bytes(&self) -> u64 {
        self.volume_bytes.saturating_sub(self.metadata_reserve)
    }

    pub fn check(&self, entries: &[ManifestEntry]) -> Result<CapacityUsage, ManifestError> {
        if entries.len() > self.max_entries {
            return Err(ManifestError::TooManyEntries {
                count: entries.len(),
                max: self.max_entries,
            });
        }
        let available = self.available_bytes();
        let mut logical_bytes = 0u64;
        for entry in entries {
            logical_bytes = logical_bytes.checked_add(entry.logical_size).ok_or(
                ManifestError::CapacityOverflow {
                    file_id: entry.file_id,
                },
            )?;
        }
        if logical_bytes > available {
            return Err(ManifestError::CapacityExceeded {
                required: logical_bytes,
                available,
            });
        }
        Ok(CapacityUsage {
            entries: entries.len(),
            logical_bytes,
            available_bytes: available,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub schema_version: u32,
    pub layout_version: u32,
    pub generation: u64,
    /// SHA-256 of [`Manifest::canonical_bytes`].
    pub manifest_id: String,
    /// Sorted by stable file id, then path.
    pub entries: Vec<ManifestEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManifestError {
    UnsupportedSchema {
        found: u32,
        expected: u32,
    },
    UnsupportedLayout {
        found: u32,
        expected: u32,
    },
    EmptyCatalog,
    EmptyChannelId {
        file_id: u32,
    },
    InvalidChannelId {
        file_id: u32,
    },
    EmptyNamespace {
        file_id: u32,
    },
    EmptyFolder {
        file_id: u32,
    },
    InvalidFolder {
        file_id: u32,
        source: NameError,
    },
    InvalidFilename {
        file_id: u32,
        source: NameError,
    },
    FilenameNotTrp {
        file_id: u32,
        filename: String,
    },
    InvalidLogicalSize {
        file_id: u32,
        size: u64,
    },
    EmptyBootstrap {
        file_id: u32,
    },
    DuplicateFileId {
        file_id: u32,
    },
    DuplicateChannel {
        namespace: String,
        channel_id: String,
    },
    DuplicatePath {
        path: String,
    },
    TooManyEntries {
        count: usize,
        max: usize,
    },
    CapacityExceeded {
        required: u64,
        available: u64,
    },
    CapacityOverflow {
        file_id: u32,
    },
    ManifestIdMismatch {
        expected: String,
        found: String,
    },
    Json(String),
}

fn put_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn put_u64(out: &mut Vec<u8>, value: u64) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn put_str(out: &mut Vec<u8>, value: &str) {
    put_u32(out, value.len() as u32);
    out.extend_from_slice(value.as_bytes());
}

fn sort_entries(entries: &mut [ManifestEntry]) {
    entries.sort_by(|a, b| {
        a.file_id
            .cmp(&b.file_id)
            .then_with(|| a.namespace.cmp(&b.namespace))
            .then_with(|| a.channel_id.cmp(&b.channel_id))
            .then_with(|| a.folder.cmp(&b.folder))
            .then_with(|| a.filename.cmp(&b.filename))
    });
}

impl Manifest {
    /// Builds a validated manifest from an arbitrary entry order.
    pub fn build(
        generation: u64,
        mut entries: Vec<ManifestEntry>,
        capacity: &CapacityPolicy,
    ) -> Result<Self, ManifestError> {
        if entries.is_empty() {
            return Err(ManifestError::EmptyCatalog);
        }
        for entry in &entries {
            entry.validate()?;
        }
        sort_entries(&mut entries);

        let mut file_ids = std::collections::HashSet::new();
        let mut channels = std::collections::HashSet::new();
        let mut paths = std::collections::HashSet::new();
        for entry in &entries {
            if !file_ids.insert(entry.file_id) {
                return Err(ManifestError::DuplicateFileId {
                    file_id: entry.file_id,
                });
            }
            if !channels.insert((entry.namespace.clone(), entry.channel_id.clone())) {
                return Err(ManifestError::DuplicateChannel {
                    namespace: entry.namespace.clone(),
                    channel_id: entry.channel_id.clone(),
                });
            }
            let path = super::name::path_key(&entry.folder, &entry.filename);
            if !paths.insert(path.clone()) {
                return Err(ManifestError::DuplicatePath { path });
            }
        }
        capacity.check(&entries)?;

        let manifest_id = manifest_digest(SCHEMA_VERSION, LAYOUT_VERSION, generation, &entries);
        Ok(Self {
            schema_version: SCHEMA_VERSION,
            layout_version: LAYOUT_VERSION,
            generation,
            manifest_id,
            entries,
        })
    }

    pub fn canonical_bytes(&self) -> Vec<u8> {
        manifest_canonical_bytes(
            self.schema_version,
            self.layout_version,
            self.generation,
            &self.entries,
        )
    }

    pub fn recompute_id(&self) -> String {
        sha256_hex(&self.canonical_bytes())
    }

    pub fn validate(&self) -> Result<(), ManifestError> {
        self.validate_with(&CapacityPolicy::default())
    }

    pub fn validate_with(&self, capacity: &CapacityPolicy) -> Result<(), ManifestError> {
        if self.schema_version != SCHEMA_VERSION {
            return Err(ManifestError::UnsupportedSchema {
                found: self.schema_version,
                expected: SCHEMA_VERSION,
            });
        }
        if self.layout_version != LAYOUT_VERSION {
            return Err(ManifestError::UnsupportedLayout {
                found: self.layout_version,
                expected: LAYOUT_VERSION,
            });
        }
        if self.entries.is_empty() {
            return Err(ManifestError::EmptyCatalog);
        }

        let mut sorted = self.entries.clone();
        sort_entries(&mut sorted);
        let mut file_ids = std::collections::HashSet::new();
        let mut channels = std::collections::HashSet::new();
        let mut paths = std::collections::HashSet::new();
        for entry in &sorted {
            entry.validate()?;
            if !file_ids.insert(entry.file_id) {
                return Err(ManifestError::DuplicateFileId {
                    file_id: entry.file_id,
                });
            }
            if !channels.insert((entry.namespace.clone(), entry.channel_id.clone())) {
                return Err(ManifestError::DuplicateChannel {
                    namespace: entry.namespace.clone(),
                    channel_id: entry.channel_id.clone(),
                });
            }
            let path = super::name::path_key(&entry.folder, &entry.filename);
            if !paths.insert(path.clone()) {
                return Err(ManifestError::DuplicatePath { path });
            }
        }
        capacity.check(&sorted)?;
        let expected = manifest_digest(
            self.schema_version,
            self.layout_version,
            self.generation,
            &sorted,
        );
        if self.manifest_id != expected {
            return Err(ManifestError::ManifestIdMismatch {
                expected,
                found: self.manifest_id.clone(),
            });
        }
        Ok(())
    }

    /// Parses the deterministic JSON emitted by [`Manifest::to_json`] and
    /// rejects any tampering with the canonical `manifest_id` or entry set.
    pub fn from_json(bytes: &[u8]) -> Result<Self, ManifestError> {
        use super::layout::{parse, Json};

        let json = parse(bytes).map_err(|error| ManifestError::Json(format!("{error:?}")))?;
        let number = |key: &'static str| -> Result<u64, ManifestError> {
            json.get(key)
                .and_then(Json::as_u64)
                .ok_or_else(|| ManifestError::Json(format!("missing {key}")))
        };
        let text = |key: &'static str| -> Result<String, ManifestError> {
            json.get(key)
                .and_then(Json::as_str)
                .map(str::to_string)
                .ok_or_else(|| ManifestError::Json(format!("missing {key}")))
        };
        let schema_version = u32::try_from(number("schema_version")?)
            .map_err(|_| ManifestError::Json("schema_version".into()))?;
        let layout_version = u32::try_from(number("layout_version")?)
            .map_err(|_| ManifestError::Json("layout_version".into()))?;
        let generation = number("generation")?;
        let manifest_id = text("manifest_id")?;
        let entries = json
            .get("entries")
            .and_then(Json::as_array)
            .ok_or_else(|| ManifestError::Json("missing entries".into()))?;
        let mut parsed = Vec::with_capacity(entries.len());
        for entry in entries {
            let kind = entry
                .get("kind")
                .and_then(Json::as_str)
                .ok_or_else(|| ManifestError::Json("missing kind".into()))?;
            if kind != EntryKind::Live.as_str() {
                return Err(ManifestError::Json("non-live kind".into()));
            }
            let file_id = entry
                .get("file_id")
                .and_then(Json::as_u64)
                .and_then(|value| u32::try_from(value).ok())
                .ok_or_else(|| ManifestError::Json("file_id".into()))?;
            let channel_id = entry
                .get("channel_id")
                .and_then(Json::as_str)
                .ok_or_else(|| ManifestError::Json("channel_id".into()))?
                .to_string();
            let namespace = entry
                .get("namespace")
                .and_then(Json::as_str)
                .ok_or_else(|| ManifestError::Json("namespace".into()))?
                .to_string();
            let folder = entry
                .get("folder")
                .and_then(Json::as_array)
                .ok_or_else(|| ManifestError::Json("folder".into()))?
                .iter()
                .map(|component| {
                    component
                        .as_str()
                        .map(str::to_string)
                        .ok_or_else(|| ManifestError::Json("folder component".into()))
                })
                .collect::<Result<Vec<_>, _>>()?;
            let filename = entry
                .get("filename")
                .and_then(Json::as_str)
                .ok_or_else(|| ManifestError::Json("filename".into()))?
                .to_string();
            let logical_size = entry
                .get("logical_size")
                .and_then(Json::as_u64)
                .ok_or_else(|| ManifestError::Json("logical_size".into()))?;
            let bootstrap_id = entry
                .get("bootstrap_id")
                .and_then(Json::as_str)
                .ok_or_else(|| ManifestError::Json("bootstrap_id".into()))?
                .to_string();
            parsed.push(ManifestEntry {
                file_id,
                channel_id,
                namespace,
                kind: EntryKind::Live,
                folder,
                filename,
                logical_size,
                bootstrap_id,
            });
        }
        let manifest = Self {
            schema_version,
            layout_version,
            generation,
            manifest_id,
            entries: parsed,
        };
        manifest.validate()?;
        Ok(manifest)
    }

    /// Deterministic JSON for the exporter/API boundary. A future package
    /// writer can persist this next to the template and extent map.
    pub fn to_json(&self) -> String {
        let mut out = String::new();
        out.push_str("{\"schema_version\":");
        out.push_str(&self.schema_version.to_string());
        out.push_str(",\"manifest_id\":\"");
        out.push_str(&json_escape(&self.manifest_id));
        out.push_str("\",\"layout_version\":");
        out.push_str(&self.layout_version.to_string());
        out.push_str(",\"generation\":");
        out.push_str(&self.generation.to_string());
        out.push_str(",\"entries\":[");
        for (i, entry) in self.entries.iter().enumerate() {
            if i != 0 {
                out.push(',');
            }
            out.push_str("{\"file_id\":");
            out.push_str(&entry.file_id.to_string());
            out.push_str(",\"channel_id\":\"");
            out.push_str(&json_escape(&entry.channel_id));
            out.push_str("\",\"namespace\":\"");
            out.push_str(&json_escape(&entry.namespace));
            out.push_str("\",\"kind\":\"");
            out.push_str(entry.kind.as_str());
            out.push_str("\",\"folder\":[");
            for (j, component) in entry.folder.iter().enumerate() {
                if j != 0 {
                    out.push(',');
                }
                out.push('"');
                out.push_str(&json_escape(component));
                out.push('"');
            }
            out.push_str("],\"filename\":\"");
            out.push_str(&json_escape(&entry.filename));
            out.push_str("\",\"logical_size\":");
            out.push_str(&entry.logical_size.to_string());
            out.push_str(",\"bootstrap_id\":\"");
            out.push_str(&json_escape(&entry.bootstrap_id));
            out.push_str("\"}");
        }
        out.push_str("]}");
        out
    }
}

fn manifest_canonical_bytes(
    schema_version: u32,
    layout_version: u32,
    generation: u64,
    entries: &[ManifestEntry],
) -> Vec<u8> {
    let mut sorted = entries.to_vec();
    sort_entries(&mut sorted);
    let mut out = b"tv-live-favorites-manifest\0".to_vec();
    put_u32(&mut out, schema_version);
    put_u32(&mut out, layout_version);
    put_u64(&mut out, generation);
    put_u32(&mut out, sorted.len() as u32);
    for entry in &sorted {
        put_u32(&mut out, entry.file_id);
        put_str(&mut out, &entry.namespace);
        put_str(&mut out, &entry.channel_id);
        out.push(match entry.kind {
            EntryKind::Live => 1,
        });
        put_u32(&mut out, entry.folder.len() as u32);
        for component in &entry.folder {
            put_str(&mut out, component);
        }
        put_str(&mut out, &entry.filename);
        put_u64(&mut out, entry.logical_size);
        put_str(&mut out, &entry.bootstrap_id);
    }
    out
}

fn manifest_digest(
    schema_version: u32,
    layout_version: u32,
    generation: u64,
    entries: &[ManifestEntry],
) -> String {
    let bytes = manifest_canonical_bytes(schema_version, layout_version, generation, entries);
    sha256_hex(&bytes)
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

/// Convenience constructor used by the resolver and tests.
pub fn entry(
    file_id: u32,
    namespace: &str,
    channel_id: &str,
    folder: Vec<String>,
    filename: String,
    logical_size: u64,
) -> Result<ManifestEntry, ManifestError> {
    let entry = ManifestEntry {
        file_id,
        channel_id: channel_id.to_string(),
        namespace: namespace.to_string(),
        kind: EntryKind::Live,
        folder,
        filename,
        logical_size,
        bootstrap_id: format!("bootstrap-{file_id:08x}"),
    };
    entry.validate()?;
    Ok(entry)
}

/// Test/fixture helper: the MVP display shape for two channels.
pub fn mvp_folder(category: &str) -> Vec<String> {
    let mut folder = vec!["Favoritos".to_string()];
    folder.push(category.to_string());
    folder
}

/// Builds a `.trp` entry from a display name without extra rules.
pub fn entry_from_display(
    file_id: u32,
    namespace: &str,
    channel_id: &str,
    category: &[String],
    display_name: &str,
) -> Result<ManifestEntry, ManifestError> {
    let mut folder = vec!["Favoritos".to_string()];
    if category.is_empty() {
        folder.push("Outros".to_string());
    } else {
        folder.extend_from_slice(category);
    }
    let filename = trp_filename(display_name)
        .map_err(|source| ManifestError::InvalidFilename { file_id, source })?;
    entry(
        file_id,
        namespace,
        channel_id,
        folder,
        filename,
        DEFAULT_LOGICAL_SIZE,
    )
}

#[cfg(test)]
mod tests {
    use super::super::digest::sha256;
    use super::*;

    fn sbt() -> ManifestEntry {
        entry(
            0x1111_1111,
            "live",
            "207198",
            mvp_folder("Abertos"),
            "SBT.trp".to_string(),
            DEFAULT_LOGICAL_SIZE,
        )
        .unwrap()
    }

    fn record() -> ManifestEntry {
        entry(
            0x2222_2222,
            "live",
            "214640",
            mvp_folder("Abertos"),
            "Record.trp".to_string(),
            DEFAULT_LOGICAL_SIZE,
        )
        .unwrap()
    }

    #[test]
    fn build_is_order_independent_and_validates() {
        let a = Manifest::build(7, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        let b = Manifest::build(7, vec![record(), sbt()], &CapacityPolicy::default()).unwrap();
        assert_eq!(a.manifest_id, b.manifest_id);
        assert_eq!(a.entries, b.entries);
        assert_eq!(a.recompute_id(), a.manifest_id);
        a.validate().unwrap();
        assert_eq!(a.entries[0].filename, "SBT.trp");
    }

    #[test]
    fn generation_is_part_of_the_manifest_identity() {
        let a = Manifest::build(7, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        let b = Manifest::build(8, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        assert_ne!(a.manifest_id, b.manifest_id);
    }

    #[test]
    fn duplicate_identity_and_casefold_paths_are_rejected() {
        let dup = Manifest::build(1, vec![sbt(), sbt()], &CapacityPolicy::default()).unwrap_err();
        assert!(matches!(dup, ManifestError::DuplicateFileId { .. }));

        let mut lower = record();
        lower.filename = "sbt.trp".to_string();
        let err = Manifest::build(1, vec![sbt(), lower], &CapacityPolicy::default()).unwrap_err();
        assert!(matches!(err, ManifestError::DuplicatePath { .. }));
    }

    #[test]
    fn capacity_rejects_91_four_gigabyte_entries() {
        let mut entries = Vec::new();
        for i in 0..91u32 {
            entries.push(
                entry(
                    i,
                    "live",
                    &format!("channel-{i}"),
                    mvp_folder("Abertos"),
                    format!("Channel {i}.trp"),
                    DEFAULT_LOGICAL_SIZE,
                )
                .unwrap(),
            );
        }
        let policy = CapacityPolicy {
            max_entries: 128,
            ..CapacityPolicy::default()
        };
        let err = Manifest::build(1, entries, &policy).unwrap_err();
        match err {
            ManifestError::CapacityExceeded {
                required,
                available,
            } => {
                assert_eq!(required, 91 * DEFAULT_LOGICAL_SIZE);
                assert!(required > available);
            }
            other => panic!("expected capacity rejection, got {other:?}"),
        }
    }

    #[test]
    fn mvp_policy_rejects_more_than_two_entries() {
        let third = entry(
            3,
            "live",
            "third",
            mvp_folder("Abertos"),
            "Third.trp".to_string(),
            DEFAULT_LOGICAL_SIZE,
        )
        .unwrap();
        let err = Manifest::build(
            1,
            vec![sbt(), record(), third],
            &CapacityPolicy::mvp_two_channels(),
        )
        .unwrap_err();
        assert!(matches!(
            err,
            ManifestError::TooManyEntries { count: 3, max: 2 }
        ));
    }

    #[test]
    fn json_is_deterministic_and_contains_no_source_secrets() {
        let manifest =
            Manifest::build(7, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        let json = manifest.to_json();
        assert!(json.contains("\"filename\":\"SBT.trp\""));
        assert!(json.contains("\"kind\":\"live\""));
        assert!(!json.to_ascii_lowercase().contains("http"));
        assert!(!json.to_ascii_lowercase().contains("token"));
        assert!(!json.to_ascii_lowercase().contains("password"));
        assert_eq!(json, manifest.to_json());
    }

    #[test]
    fn tampered_id_and_unsupported_versions_are_rejected() {
        let mut manifest =
            Manifest::build(7, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        manifest.manifest_id = "00".repeat(32);
        assert!(matches!(
            manifest.validate(),
            Err(ManifestError::ManifestIdMismatch { .. })
        ));

        let mut manifest =
            Manifest::build(7, vec![sbt(), record()], &CapacityPolicy::default()).unwrap();
        manifest.schema_version = 99;
        assert!(matches!(
            manifest.validate(),
            Err(ManifestError::UnsupportedSchema { .. })
        ));
    }

    #[test]
    fn entry_validation_rejects_bad_paths_and_non_trp_names() {
        let mut bad = sbt();
        bad.folder = vec!["Favoritos".to_string(), "..".to_string()];
        assert!(matches!(
            bad.validate(),
            Err(ManifestError::InvalidFolder { .. })
        ));

        let mut bad = sbt();
        bad.filename = "SBT.ts".to_string();
        assert!(matches!(
            bad.validate(),
            Err(ManifestError::FilenameNotTrp { .. })
        ));
    }

    #[test]
    fn sha256_manifest_id_is_64_hex_characters() {
        let manifest = Manifest::build(1, vec![sbt()], &CapacityPolicy::default()).unwrap();
        assert_eq!(manifest.manifest_id.len(), 64);
        assert!(manifest.manifest_id.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(sha256(manifest.canonical_bytes().as_slice()).len(), 32);
    }
}
