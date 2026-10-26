//! Resolves persisted favorites ids into a validated Live manifest.
//!
//! The server owns the real catalog and credentials. This module only accepts
//! an abstract catalog snapshot, so host tests cannot accidentally reach a
//! provider. Every favorite is either resolved into a Live entry or reported
//! with a per-entry rejection; by default a partial manifest is never
//! published silently.

use std::collections::{HashMap, HashSet};

use super::digest::fnv1a32;
use super::manifest::{
    CapacityPolicy, Manifest, ManifestEntry, ManifestError, DEFAULT_LOGICAL_SIZE,
};
use super::name::{trp_filename, NameAllocator, NameError};

pub const DEFAULT_NAMESPACE: &str = "live";
pub const FAVORITES_ROOT: &str = "Favoritos";
pub const FALLBACK_CATEGORY: &str = "Outros";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelKind {
    Live,
    Vod,
    Series,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogChannel {
    pub namespace: String,
    pub channel_id: String,
    pub name: String,
    /// Category path below `Favoritos`, e.g. `["Abertos"]`.
    pub category: Vec<String>,
    pub kind: ChannelKind,
}

/// Read-only view of the catalog owned by the server.
pub trait LiveCatalog {
    fn channel(&self, namespace: &str, channel_id: &str) -> Option<CatalogChannel>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolverPolicy {
    pub namespace: String,
    pub capacity: CapacityPolicy,
    pub default_logical_size: u64,
    /// Explicit opt-in for diagnostics. The default is strict: any rejection
    /// fails the whole resolution so the active mount never shrinks silently.
    pub allow_partial: bool,
}

impl Default for ResolverPolicy {
    fn default() -> Self {
        Self {
            namespace: DEFAULT_NAMESPACE.to_string(),
            capacity: CapacityPolicy::default(),
            default_logical_size: DEFAULT_LOGICAL_SIZE,
            allow_partial: false,
        }
    }
}

impl ResolverPolicy {
    /// MVP policy: at most the two authorized channels.
    pub fn mvp_two_channels() -> Self {
        Self {
            capacity: CapacityPolicy::mvp_two_channels(),
            ..Self::default()
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RejectionReason {
    EmptyFavoriteId,
    MissingFromCatalog,
    NotLive {
        kind: ChannelKind,
    },
    EmptyDisplayName,
    InvalidDisplayName(NameError),
    DuplicateChannel {
        kept_favorite_id: String,
    },
    FileIdCollision {
        file_id: u32,
        other_channel_id: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejection {
    pub favorite_id: String,
    pub reason: RejectionReason,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolution {
    pub manifest: Manifest,
    pub accepted_favorites: usize,
    pub deduplicated: Vec<String>,
    /// Empty on success; populated only with `allow_partial`.
    pub rejected: Vec<Rejection>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolutionError {
    Rejected(Vec<Rejection>),
    Manifest(ManifestError),
}

impl From<ManifestError> for ResolutionError {
    fn from(value: ManifestError) -> Self {
        Self::Manifest(value)
    }
}

/// Stable file id derived from the catalog namespace + channel id, never from
/// the favorite's position in the list.
pub fn stable_file_id(namespace: &str, channel_id: &str) -> u32 {
    let mut material = Vec::with_capacity(namespace.len() + channel_id.len() + 1);
    material.extend_from_slice(namespace.as_bytes());
    material.push(0);
    material.extend_from_slice(channel_id.as_bytes());
    fnv1a32(&material)
}

struct Candidate {
    namespace: String,
    channel_id: String,
    file_id: u32,
    folder: Vec<String>,
    requested_filename: String,
    logical_size: u64,
    bootstrap_id: String,
}

fn category_folder(category: &[String]) -> Vec<String> {
    let mut folder = vec![FAVORITES_ROOT.to_string()];
    if category.is_empty() {
        folder.push(FALLBACK_CATEGORY.to_string());
        return folder;
    }
    if category
        .first()
        .map(|c| c.eq_ignore_ascii_case(FAVORITES_ROOT))
        .unwrap_or(false)
    {
        folder.clear();
    }
    folder.extend_from_slice(category);
    folder
}

/// Resolves the persisted favorite ids into a manifest for `generation`.
pub fn resolve_favorites<C: LiveCatalog>(
    favorite_ids: &[String],
    catalog: &C,
    generation: u64,
    policy: &ResolverPolicy,
) -> Result<Resolution, ResolutionError> {
    let mut seen_favorites = HashSet::new();
    let mut seen_channels: HashMap<(String, String), String> = HashMap::new();
    let mut file_ids: HashMap<u32, String> = HashMap::new();
    let mut candidates: Vec<Candidate> = Vec::new();
    let mut rejected: Vec<Rejection> = Vec::new();
    let mut deduplicated: Vec<String> = Vec::new();

    for raw_id in favorite_ids {
        let favorite_id = raw_id.trim();
        if favorite_id.is_empty() {
            rejected.push(Rejection {
                favorite_id: raw_id.clone(),
                reason: RejectionReason::EmptyFavoriteId,
            });
            continue;
        }
        if !seen_favorites.insert(favorite_id.to_string()) {
            deduplicated.push(favorite_id.to_string());
            continue;
        }
        let Some(channel) = catalog.channel(&policy.namespace, favorite_id) else {
            rejected.push(Rejection {
                favorite_id: favorite_id.to_string(),
                reason: RejectionReason::MissingFromCatalog,
            });
            continue;
        };
        if channel.kind != ChannelKind::Live {
            rejected.push(Rejection {
                favorite_id: favorite_id.to_string(),
                reason: RejectionReason::NotLive { kind: channel.kind },
            });
            continue;
        }
        if channel.channel_id.trim().is_empty() || channel.name.trim().is_empty() {
            rejected.push(Rejection {
                favorite_id: favorite_id.to_string(),
                reason: RejectionReason::EmptyDisplayName,
            });
            continue;
        }

        let namespace = if channel.namespace.is_empty() {
            policy.namespace.clone()
        } else {
            channel.namespace.clone()
        };
        let channel_key = (namespace.clone(), channel.channel_id.clone());
        if let Some(kept) = seen_channels.get(&channel_key) {
            deduplicated.push(favorite_id.to_string());
            let _ = kept;
            continue;
        }

        let file_id = stable_file_id(&namespace, &channel.channel_id);
        if let Some(other) = file_ids.get(&file_id) {
            rejected.push(Rejection {
                favorite_id: favorite_id.to_string(),
                reason: RejectionReason::FileIdCollision {
                    file_id,
                    other_channel_id: other.clone(),
                },
            });
            continue;
        }

        let filename = match trp_filename(&channel.name) {
            Ok(name) => name,
            Err(source) => {
                rejected.push(Rejection {
                    favorite_id: favorite_id.to_string(),
                    reason: RejectionReason::InvalidDisplayName(source),
                });
                continue;
            }
        };
        let folder = category_folder(&channel.category);
        seen_channels.insert(channel_key, favorite_id.to_string());
        file_ids.insert(file_id, channel.channel_id.clone());
        candidates.push(Candidate {
            namespace,
            channel_id: channel.channel_id,
            file_id,
            folder,
            requested_filename: filename,
            logical_size: policy.default_logical_size,
            bootstrap_id: format!("bootstrap-{file_id:08x}"),
        });
    }

    if !rejected.is_empty() && !policy.allow_partial {
        return Err(ResolutionError::Rejected(rejected));
    }
    if candidates.is_empty() {
        return Err(ResolutionError::Manifest(ManifestError::EmptyCatalog));
    }

    // Deterministic name allocation: identity order, never favorite order.
    candidates.sort_by(|a, b| {
        a.namespace
            .cmp(&b.namespace)
            .then_with(|| a.channel_id.cmp(&b.channel_id))
    });
    let mut allocator = NameAllocator::new();
    let mut entries = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let filename = match allocator.allocate(
            &candidate.folder,
            &candidate.requested_filename,
            &candidate.channel_id,
        ) {
            Ok(name) => name,
            Err(source) => {
                rejected.push(Rejection {
                    favorite_id: candidate.channel_id.clone(),
                    reason: RejectionReason::InvalidDisplayName(source),
                });
                continue;
            }
        };
        entries.push(ManifestEntry {
            file_id: candidate.file_id,
            channel_id: candidate.channel_id,
            namespace: candidate.namespace,
            kind: super::manifest::EntryKind::Live,
            folder: candidate.folder,
            filename,
            logical_size: candidate.logical_size,
            bootstrap_id: candidate.bootstrap_id,
        });
    }

    if !rejected.is_empty() {
        return Err(ResolutionError::Rejected(rejected));
    }

    let manifest = Manifest::build(generation, entries, &policy.capacity)?;
    manifest.validate_with(&policy.capacity)?;
    Ok(Resolution {
        accepted_favorites: manifest.entries.len(),
        manifest,
        deduplicated,
        rejected,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeCatalog(Vec<CatalogChannel>);

    impl LiveCatalog for FakeCatalog {
        fn channel(&self, namespace: &str, channel_id: &str) -> Option<CatalogChannel> {
            self.0
                .iter()
                .find(|c| c.namespace == namespace && c.channel_id == channel_id)
                .cloned()
        }
    }

    fn live(namespace: &str, id: &str, name: &str, category: &[&str]) -> CatalogChannel {
        CatalogChannel {
            namespace: namespace.to_string(),
            channel_id: id.to_string(),
            name: name.to_string(),
            category: category.iter().map(|s| s.to_string()).collect(),
            kind: ChannelKind::Live,
        }
    }

    fn catalog() -> FakeCatalog {
        FakeCatalog(vec![
            live("live", "207198", "SBT", &["Abertos"]),
            live("live", "214640", "Record", &["Abertos"]),
        ])
    }

    #[test]
    fn resolves_sbt_and_record_with_identity_independent_ids() {
        let policy = ResolverPolicy::mvp_two_channels();
        let a =
            resolve_favorites(&["207198".into(), "214640".into()], &catalog(), 7, &policy).unwrap();
        let b =
            resolve_favorites(&["214640".into(), "207198".into()], &catalog(), 7, &policy).unwrap();
        assert_eq!(a.manifest.manifest_id, b.manifest.manifest_id);
        assert_eq!(a.manifest.entries, b.manifest.entries);
        assert_eq!(a.accepted_favorites, 2);
        let sbt = a
            .manifest
            .entries
            .iter()
            .find(|e| e.channel_id == "207198")
            .expect("SBT entry");
        let record = a
            .manifest
            .entries
            .iter()
            .find(|e| e.channel_id == "214640")
            .expect("Record entry");
        assert_eq!(sbt.filename, "SBT.trp");
        assert_eq!(record.filename, "Record.trp");
        assert_eq!(
            sbt.folder,
            vec!["Favoritos".to_string(), "Abertos".to_string()]
        );
        assert_eq!(sbt.file_id, stable_file_id("live", "207198"));
    }

    #[test]
    fn rejects_missing_vod_and_traversal_names_explicitly() {
        let catalog = FakeCatalog(vec![
            live("live", "1", "SBT", &["Abertos"]),
            CatalogChannel {
                namespace: "live".into(),
                channel_id: "2".into(),
                name: "Movie".into(),
                category: vec!["Filmes".into()],
                kind: ChannelKind::Vod,
            },
            live("live", "3", "../Record", &["Abertos"]),
        ]);
        let policy = ResolverPolicy::mvp_two_channels();
        let err = resolve_favorites(
            &["1".into(), "2".into(), "3".into(), "missing".into()],
            &catalog,
            1,
            &policy,
        )
        .unwrap_err();
        match err {
            ResolutionError::Rejected(rejections) => {
                assert_eq!(rejections.len(), 3);
                assert!(matches!(
                    rejections[0].reason,
                    RejectionReason::NotLive {
                        kind: ChannelKind::Vod
                    }
                ));
                assert!(matches!(
                    rejections[1].reason,
                    RejectionReason::InvalidDisplayName(NameError::Separator)
                ));
                assert_eq!(rejections[2].reason, RejectionReason::MissingFromCatalog);
            }
            other => panic!("expected per-entry rejections, got {other:?}"),
        }
    }

    #[test]
    fn deduplicates_the_same_channel_and_favorite() {
        let policy = ResolverPolicy::mvp_two_channels();
        let resolution = resolve_favorites(
            &["207198".into(), "207198".into(), "207198".into()],
            &catalog(),
            1,
            &policy,
        )
        .unwrap();
        assert_eq!(resolution.accepted_favorites, 1);
        assert_eq!(resolution.deduplicated.len(), 2);
    }

    #[test]
    fn fallback_category_is_used_for_uncategorized_channels() {
        let catalog = FakeCatalog(vec![live("live", "9", "SBT", &[])]);
        let resolution = resolve_favorites(
            &["9".into()],
            &catalog,
            1,
            &ResolverPolicy::mvp_two_channels(),
        )
        .unwrap();
        assert_eq!(
            resolution.manifest.entries[0].folder,
            vec!["Favoritos".to_string(), "Outros".to_string()]
        );
    }

    #[test]
    fn casefold_collisions_get_identity_suffix_deterministically() {
        let catalog = FakeCatalog(vec![
            live("live", "a", "SBT", &["Abertos"]),
            live("live", "b", "sbt", &["Abertos"]),
        ]);
        let policy = ResolverPolicy::mvp_two_channels();
        let a = resolve_favorites(&["a".into(), "b".into()], &catalog, 1, &policy).unwrap();
        let b = resolve_favorites(&["b".into(), "a".into()], &catalog, 1, &policy).unwrap();
        assert_eq!(a.manifest.entries, b.manifest.entries);
        let names: Vec<&str> = a
            .manifest
            .entries
            .iter()
            .map(|e| e.filename.as_str())
            .collect();
        assert_eq!(names.iter().filter(|n| n.ends_with(".trp")).count(), 2);
        assert_ne!(names[0].to_ascii_lowercase(), names[1].to_ascii_lowercase());
    }

    #[test]
    fn mvp_policy_rejects_third_channel() {
        let catalog = FakeCatalog(vec![
            live("live", "1", "SBT", &["Abertos"]),
            live("live", "2", "Record", &["Abertos"]),
            live("live", "3", "Band", &["Abertos"]),
        ]);
        let err = resolve_favorites(
            &["1".into(), "2".into(), "3".into()],
            &catalog,
            1,
            &ResolverPolicy::mvp_two_channels(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            ResolutionError::Manifest(ManifestError::TooManyEntries { count: 3, max: 2 })
        );
    }

    #[test]
    fn capacity_rejects_91_entries_even_without_mvp_limit() {
        let mut channels = Vec::new();
        let mut ids = Vec::new();
        for i in 0..91u32 {
            let id = i.to_string();
            channels.push(live("live", &id, &format!("Channel {i}"), &["Abertos"]));
            ids.push(id);
        }
        let policy = ResolverPolicy {
            capacity: CapacityPolicy {
                max_entries: 128,
                ..CapacityPolicy::default()
            },
            ..ResolverPolicy::default()
        };
        let err = resolve_favorites(&ids, &FakeCatalog(channels), 1, &policy).unwrap_err();
        assert!(matches!(
            err,
            ResolutionError::Manifest(ManifestError::CapacityExceeded { .. })
        ));
    }

    #[test]
    fn stable_file_id_changes_when_namespace_changes() {
        assert_ne!(
            stable_file_id("live", "207198"),
            stable_file_id("other", "207198")
        );
        assert_eq!(
            stable_file_id("live", "207198"),
            stable_file_id("live", "207198")
        );
    }
}
