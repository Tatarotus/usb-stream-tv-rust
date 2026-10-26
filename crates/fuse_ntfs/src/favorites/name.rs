//! NTFS-safe names, folder paths, and deterministic collision handling.
//!
//! The TV enumerates the on-disk NTFS directory entries directly, so a name
//! that is valid in the web catalog is not automatically valid on the volume.
//! This module rejects traversal, separators, control characters, Windows
//! reserved names, trailing dots/spaces and over-long UTF-16 components.
//!
//! Unicode policy: names are compared with Unicode lower-casing (the closest
//! available approximation of NTFS case-insensitive matching) and combining
//! marks are rejected. Rejecting decomposed sequences is deliberate: without
//! an NFC database in this dependency-free crate, silently normalising would
//! claim a guarantee the code cannot verify. Composed characters (`á`) are
//! accepted; a decomposed `a` + combining acute is an explicit error.

use std::collections::HashMap;

use super::digest::fnv1a32;

/// Maximum NTFS file-name length in UTF-16 code units.
pub const MAX_COMPONENT_UTF16: usize = 255;
/// Maximum number of components below the volume root.
pub const MAX_PATH_COMPONENTS: usize = 32;
/// Logical stream extension required by the MVP.
pub const TRP_EXTENSION: &str = ".trp";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NameError {
    EmptyComponent,
    AbsolutePath,
    ParentTraversal,
    Separator,
    ForbiddenCharacter(char),
    ControlCharacter(char),
    CombiningMark(char),
    ReservedName(String),
    TrailingDotOrSpace,
    TooLong { utf16_units: usize, max: usize },
    TooManyComponents { components: usize, max: usize },
    EmptyPath,
}

fn is_combining_mark(c: char) -> bool {
    matches!(
        c as u32,
        0x0300..=0x036F
            | 0x1AB0..=0x1AFF
            | 0x1DC0..=0x1DFF
            | 0x20D0..=0x20F0
            | 0xFE20..=0xFE2F
    )
}

fn is_reserved_base(base: &str) -> bool {
    let upper = base.to_ascii_uppercase();
    matches!(upper.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "CLOCK$")
        || (upper.len() == 4
            && (upper.starts_with("COM") || upper.starts_with("LPT"))
            && upper.as_bytes()[3].is_ascii_digit()
            && upper.as_bytes()[3] != b'0')
}

fn utf16_len(value: &str) -> usize {
    value.encode_utf16().count()
}

fn truncate_utf16(value: &str, max_units: usize) -> String {
    let mut out = String::new();
    let mut used = 0usize;
    for ch in value.chars() {
        let units = ch.len_utf16();
        if used + units > max_units {
            break;
        }
        out.push(ch);
        used += units;
    }
    out
}

/// Validates and returns one NTFS directory-entry component.
pub fn sanitize_component(raw: &str) -> Result<String, NameError> {
    if raw.is_empty() {
        return Err(NameError::EmptyComponent);
    }
    if raw == "." || raw == ".." {
        return Err(NameError::ParentTraversal);
    }
    if raw.starts_with('/') || raw.starts_with('\\') {
        return Err(NameError::AbsolutePath);
    }
    for ch in raw.chars() {
        match ch {
            '/' | '\\' => return Err(NameError::Separator),
            '<' | '>' | ':' | '"' | '|' | '?' | '*' => {
                return Err(NameError::ForbiddenCharacter(ch))
            }
            c if (c as u32) < 0x20 || c == '\u{7f}' => return Err(NameError::ControlCharacter(c)),
            c if is_combining_mark(c) => return Err(NameError::CombiningMark(c)),
            _ => {}
        }
    }
    if raw.ends_with('.') || raw.ends_with(' ') {
        return Err(NameError::TrailingDotOrSpace);
    }
    let base = raw.split('.').next().unwrap_or(raw);
    if is_reserved_base(base) {
        return Err(NameError::ReservedName(base.to_string()));
    }
    let utf16_units = utf16_len(raw);
    if utf16_units > MAX_COMPONENT_UTF16 {
        return Err(NameError::TooLong {
            utf16_units,
            max: MAX_COMPONENT_UTF16,
        });
    }
    Ok(raw.to_string())
}

/// Validates every component of a folder below the volume root.
pub fn sanitize_folder(raw: &[String]) -> Result<Vec<String>, NameError> {
    if raw.len() > MAX_PATH_COMPONENTS {
        return Err(NameError::TooManyComponents {
            components: raw.len(),
            max: MAX_PATH_COMPONENTS,
        });
    }
    let mut out = Vec::with_capacity(raw.len());
    for component in raw {
        out.push(sanitize_component(component)?);
    }
    Ok(out)
}

/// Builds the visible `.trp` file name for a channel display name.
///
/// Known video extensions are replaced by `.trp`; any other suffix is kept and
/// `.trp` appended, preserving the extension required by the TV.
pub fn trp_filename(raw: &str) -> Result<String, NameError> {
    let lower = raw.to_ascii_lowercase();
    let stem = match lower.rfind('.') {
        Some(pos) => match &lower[pos..] {
            ".trp" | ".ts" | ".tp" | ".m4v" | ".mp4" | ".mkv" | ".avi" | ".mov" => &raw[..pos],
            _ => raw,
        },
        None => raw,
    };
    let stem = sanitize_component(stem)?;
    let name = format!("{stem}{TRP_EXTENSION}");
    sanitize_component(&name)
}

/// Case-insensitive NTFS key for one component. Trailing dot/space bytes are
/// already rejected, so this is a pure lower-casing.
pub fn ntfs_key(component: &str) -> String {
    component.to_lowercase()
}

/// Case-insensitive key for a complete relative path.
pub fn path_key(folder: &[String], filename: &str) -> String {
    let mut key = String::new();
    for component in folder {
        key.push_str(&ntfs_key(component));
        key.push('/');
    }
    key.push_str(&ntfs_key(filename));
    key
}

/// Deterministic, identity-derived suffix used when two channels request the
/// same case-insensitive path.
fn stable_suffix(channel_id: &str) -> String {
    format!("{:08x}", fnv1a32(channel_id.as_bytes()))
}

/// Allocates unique visible names within immutable folder snapshots.
///
/// The resolver sorts channels by stable identity before calling this
/// allocator, so the resulting name assignment does not depend on favorite
/// ordering. A collision appends a suffix derived from the channel id, never
/// from the ordinal position.
#[derive(Debug, Default)]
pub struct NameAllocator {
    used: HashMap<String, String>,
}

impl NameAllocator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Allocates `requested` (already a `.trp` file name) under `folder`.
    /// Returns the allocated name.
    pub fn allocate(
        &mut self,
        folder: &[String],
        requested: &str,
        channel_id: &str,
    ) -> Result<String, NameError> {
        let folder = sanitize_folder(folder)?;
        let requested = sanitize_component(requested)?;
        let key = path_key(&folder, &requested);
        match self.used.get(&key) {
            None => {
                self.used.insert(key, channel_id.to_string());
                return Ok(requested);
            }
            Some(owner) if owner == channel_id => return Ok(requested),
            Some(_) => {}
        }

        let suffix = stable_suffix(channel_id);
        let stem = requested.strip_suffix(TRP_EXTENSION).unwrap_or(&requested);
        let mut attempt = 0u32;
        loop {
            let extra = if attempt == 0 {
                format!("~{suffix}")
            } else {
                format!("~{suffix}-{attempt}")
            };
            let max_stem =
                MAX_COMPONENT_UTF16.saturating_sub(utf16_len(&extra) + TRP_EXTENSION.len());
            let candidate = format!(
                "{}{}{}",
                truncate_utf16(stem, max_stem),
                extra,
                TRP_EXTENSION
            );
            let candidate_key = path_key(&folder, &candidate);
            if let std::collections::hash_map::Entry::Vacant(entry) = self.used.entry(candidate_key)
            {
                entry.insert(channel_id.to_string());
                return Ok(candidate);
            }
            attempt = attempt.saturating_add(1);
            if attempt > 4096 {
                return Err(NameError::TooLong {
                    utf16_units: utf16_len(&candidate),
                    max: MAX_COMPONENT_UTF16,
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_expected_mvp_names() {
        assert_eq!(sanitize_component("SBT.trp").unwrap(), "SBT.trp");
        assert_eq!(
            sanitize_component("Record TV Rio.trp").unwrap(),
            "Record TV Rio.trp"
        );
        assert_eq!(trp_filename("SBT").unwrap(), "SBT.trp");
        assert_eq!(trp_filename("Record.ts").unwrap(), "Record.trp");
        assert_eq!(trp_filename("SBT.trp").unwrap(), "SBT.trp");
    }

    #[test]
    fn rejects_traversal_separators_and_absolute_paths() {
        assert_eq!(sanitize_component(".."), Err(NameError::ParentTraversal));
        assert_eq!(sanitize_component("../SBT"), Err(NameError::Separator));
        assert_eq!(sanitize_component("/SBT"), Err(NameError::AbsolutePath));
        assert_eq!(sanitize_component("A/B"), Err(NameError::Separator));
        assert_eq!(sanitize_component("A\\B"), Err(NameError::Separator));
        assert_eq!(sanitize_component(""), Err(NameError::EmptyComponent));
    }

    #[test]
    fn rejects_forbidden_controls_reserved_and_trailing_bytes() {
        assert_eq!(
            sanitize_component("SBT:1"),
            Err(NameError::ForbiddenCharacter(':'))
        );
        assert_eq!(
            sanitize_component("SBT\u{7f}"),
            Err(NameError::ControlCharacter('\u{7f}'))
        );
        assert_eq!(
            sanitize_component("CON.trp"),
            Err(NameError::ReservedName("CON".to_string()))
        );
        assert_eq!(
            sanitize_component("LPT9"),
            Err(NameError::ReservedName("LPT9".to_string()))
        );
        assert_eq!(
            sanitize_component("SBT.trp "),
            Err(NameError::TrailingDotOrSpace)
        );
        assert_eq!(
            sanitize_component("SBT."),
            Err(NameError::TrailingDotOrSpace)
        );
    }

    #[test]
    fn rejects_decomposed_unicode_instead_of_claiming_nfc() {
        assert!(matches!(
            sanitize_component("Sa\u{0301}o"),
            Err(NameError::CombiningMark(_))
        ));
        assert_eq!(sanitize_component("São").unwrap(), "São");
    }

    #[test]
    fn enforces_utf16_component_length() {
        let max = "a".repeat(MAX_COMPONENT_UTF16);
        assert!(sanitize_component(&max).is_ok());
        let too_long = "a".repeat(MAX_COMPONENT_UTF16 + 1);
        assert!(matches!(
            sanitize_component(&too_long),
            Err(NameError::TooLong { .. })
        ));
        // Astral characters consume two UTF-16 units each.
        let astral = "\u{1F600}".repeat(MAX_COMPONENT_UTF16 / 2 + 1);
        assert!(matches!(
            sanitize_component(&astral),
            Err(NameError::TooLong { .. })
        ));
    }

    #[test]
    fn folder_helpers_validate_depth_and_components() {
        let folder = vec!["Favoritos".to_string(), "Abertos".to_string()];
        assert_eq!(sanitize_folder(&folder).unwrap(), folder);
        assert_eq!(
            sanitize_folder(&["..".to_string()]),
            Err(NameError::ParentTraversal)
        );
        let deep = vec!["a".to_string(); MAX_PATH_COMPONENTS + 1];
        assert!(matches!(
            sanitize_folder(&deep),
            Err(NameError::TooManyComponents { .. })
        ));
    }

    #[test]
    fn collision_suffix_is_identity_derived_not_ordinal() {
        let folder = vec!["Favoritos".to_string(), "Abertos".to_string()];
        let mut alloc = NameAllocator::new();
        assert_eq!(
            alloc.allocate(&folder, "SBT.trp", "channel-a").unwrap(),
            "SBT.trp"
        );
        let collided = alloc.allocate(&folder, "sbt.trp", "channel-b").unwrap();
        assert_ne!(collided, "sbt.trp");
        assert!(collided.ends_with(".trp"));
        assert!(collided.contains('~'));
        // Same owner requesting the same path is idempotent.
        assert_eq!(
            alloc.allocate(&folder, "SBT.trp", "channel-a").unwrap(),
            "SBT.trp"
        );
    }

    #[test]
    fn collision_key_is_case_insensitive() {
        let folder = vec!["Favoritos".to_string()];
        let mut alloc = NameAllocator::new();
        alloc.allocate(&folder, "SBT.trp", "a").unwrap();
        let b = alloc.allocate(&folder, "sbt.trp", "b").unwrap();
        assert_ne!(ntfs_key(&b), ntfs_key("sbt.trp"));
    }
}
