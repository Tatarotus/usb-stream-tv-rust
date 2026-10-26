//! Identity-preserving disk range map for the virtual favorites volume.
//!
//! The TV addresses the gadget by sector/LBA, not by file name. A read can
//! therefore cross NTFS metadata, file A and file B in one SCSI command. The
//! map below derives disk extents from the same runlist pieces the NTFS layer
//! already parses ([`extents_from_runs`]) and clips every routed segment at
//! the next boundary. The result carries `file_id` + in-file offset, so the
//! stream path never has to consult a global `active_node()` to decide which
//! channel a physical byte belongs to.
//!
//! Contract: `route(disk_byte) -> Metadata | File { file_id, file_offset } |
//! Unmapped`. Extents are half-open `[start, end)` byte ranges.

use crate::vfs::ntfs_mft::{extents_from_runs, RunPiece, CLUSTER_SIZE};

pub const CLUSTER_BYTES: u64 = CLUSTER_SIZE as u64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Span {
    pub start: u64,
    pub end: u64,
}

impl Span {
    pub fn len(&self) -> u64 {
        self.end.saturating_sub(self.start)
    }

    pub fn is_empty(&self) -> bool {
        self.end <= self.start
    }

    fn overlaps(&self, other: &Span) -> bool {
        self.start < other.end && other.start < self.end
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileExtent {
    pub file_id: u32,
    pub disk_start: u64,
    pub disk_end: u64,
    pub file_start: u64,
}

impl FileExtent {
    pub fn len(&self) -> u64 {
        self.disk_end.saturating_sub(self.disk_start)
    }

    pub fn is_empty(&self) -> bool {
        self.disk_end <= self.disk_start
    }

    pub fn file_end(&self) -> u64 {
        self.file_start.saturating_add(self.len())
    }

    fn span(&self) -> Span {
        Span {
            start: self.disk_start,
            end: self.disk_end,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MapRegion {
    Metadata {
        len: u64,
    },
    File {
        file_id: u32,
        file_offset: u64,
        len: u64,
    },
    Unmapped {
        len: u64,
    },
}

impl MapRegion {
    pub fn len(&self) -> u64 {
        match self {
            Self::Metadata { len } | Self::Unmapped { len } => *len,
            Self::File { len, .. } => *len,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Segment {
    pub disk_offset: u64,
    pub region: MapRegion,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MapError {
    InvalidVolume,
    InvalidRange {
        start: u64,
        end: u64,
    },
    EmptyRuns,
    OutOfVolume {
        disk_end: u64,
        volume_bytes: u64,
    },
    Overflow {
        file_id: u32,
    },
    Overlap {
        start: u64,
        end: u64,
    },
    DuplicateFileId {
        file_id: u32,
    },
    InvalidRun {
        file_id: u32,
        vcn: u64,
    },
    SparseRun {
        file_id: u32,
        vcn: u64,
    },
    NonContiguousVcn {
        file_id: u32,
        expected: u64,
        found: u64,
    },
    IncompleteFile {
        file_id: u32,
        mapped_bytes: u64,
        logical_size: u64,
    },
}

#[derive(Debug, Clone, Default)]
pub struct ExtentMap {
    volume_bytes: u64,
    files: Vec<FileExtent>,
    metadata: Vec<Span>,
    // Keep allocation rounding as reserved space even though it is not routed
    // as file data. Another file or metadata cannot reuse its final cluster.
    allocations: Vec<Span>,
}

fn validate_range(start: u64, end: u64, volume_bytes: u64) -> Result<(), MapError> {
    if end <= start || end > volume_bytes {
        return Err(MapError::InvalidRange { start, end });
    }
    Ok(())
}

fn check_file_overlaps(files: &[FileExtent]) -> Result<(), MapError> {
    for (i, a) in files.iter().enumerate() {
        for b in files.iter().skip(i + 1) {
            if a.span().overlaps(&b.span()) {
                let start = a.disk_start.max(b.disk_start);
                let end = a.disk_end.min(b.disk_end);
                return Err(MapError::Overlap { start, end });
            }
        }
    }
    Ok(())
}

fn check_metadata_overlaps(files: &[FileExtent], metadata: &[Span]) -> Result<(), MapError> {
    for span in metadata {
        for file in files {
            if span.overlaps(&file.span()) {
                let start = span.start.max(file.disk_start);
                let end = span.end.min(file.disk_end);
                return Err(MapError::Overlap { start, end });
            }
        }
    }
    for (i, a) in metadata.iter().enumerate() {
        for b in metadata.iter().skip(i + 1) {
            if a.overlaps(b) {
                let start = a.start.max(b.start);
                let end = a.end.min(b.end);
                return Err(MapError::Overlap { start, end });
            }
        }
    }
    Ok(())
}

fn check_span_overlaps(spans: &[Span]) -> Result<(), MapError> {
    for (index, left) in spans.iter().enumerate() {
        for right in &spans[index + 1..] {
            if left.overlaps(right) {
                return Err(MapError::Overlap {
                    start: left.start.max(right.start),
                    end: left.end.min(right.end),
                });
            }
        }
    }
    Ok(())
}

impl ExtentMap {
    pub fn new(volume_bytes: u64) -> Result<Self, MapError> {
        if volume_bytes == 0 {
            return Err(MapError::InvalidVolume);
        }
        Ok(Self {
            volume_bytes,
            files: Vec::new(),
            metadata: Vec::new(),
            allocations: Vec::new(),
        })
    }

    pub fn volume_bytes(&self) -> u64 {
        self.volume_bytes
    }

    pub fn files(&self) -> &[FileExtent] {
        &self.files
    }

    pub fn metadata(&self) -> &[Span] {
        &self.metadata
    }

    /// Marks a reserved metadata range (boot sectors, MFT, indexes, bitmap).
    pub fn add_metadata(&mut self, start: u64, end: u64) -> Result<(), MapError> {
        validate_range(start, end, self.volume_bytes)?;
        let span = Span { start, end };
        let mut metadata = self.metadata.clone();
        metadata.push(span);
        check_metadata_overlaps(&self.files, &metadata)?;
        let mut reserved = self.allocations.clone();
        reserved.extend(metadata.iter().copied());
        check_span_overlaps(&reserved)?;
        metadata.sort_by_key(|s| s.start);
        self.metadata = metadata;
        Ok(())
    }

    /// Adds one favorite file from real NTFS runlist pieces.
    ///
    /// `logical_size` is the `$DATA` size; allocation rounding past EOF is
    /// clipped so a sector beyond the visible file never becomes a channel
    /// byte. The complete VCN-ordered runlist must start at VCN zero, cover all
    /// logical bytes without sparse runs and remain inside the volume. Every
    /// allocation is checked with non-saturating arithmetic before passing it
    /// to the shared runlist converter. Returns the number of disk extents added.
    pub fn add_file_runs(
        &mut self,
        file_id: u32,
        runs: &[RunPiece],
        logical_size: u64,
    ) -> Result<usize, MapError> {
        if runs.is_empty() {
            return Err(MapError::EmptyRuns);
        }
        if logical_size == 0 {
            return Err(MapError::InvalidRange { start: 0, end: 0 });
        }
        if self.files.iter().any(|extent| extent.file_id == file_id) {
            return Err(MapError::DuplicateFileId { file_id });
        }

        let mut expected_vcn = 0u64;
        let mut added_allocations = Vec::with_capacity(runs.len());
        for run in runs {
            if run.length_clusters == 0 {
                return Err(MapError::InvalidRun {
                    file_id,
                    vcn: run.vcn,
                });
            }
            if run.vcn != expected_vcn {
                return Err(MapError::NonContiguousVcn {
                    file_id,
                    expected: expected_vcn,
                    found: run.vcn,
                });
            }
            let lcn = run.lcn.ok_or(MapError::SparseRun {
                file_id,
                vcn: run.vcn,
            })?;
            expected_vcn = expected_vcn
                .checked_add(run.length_clusters)
                .ok_or(MapError::Overflow { file_id })?;
            let length = run
                .length_clusters
                .checked_mul(CLUSTER_BYTES)
                .ok_or(MapError::Overflow { file_id })?;
            let disk_start = lcn
                .checked_mul(CLUSTER_BYTES)
                .ok_or(MapError::Overflow { file_id })?;
            let disk_end = disk_start
                .checked_add(length)
                .ok_or(MapError::Overflow { file_id })?;
            if disk_end > self.volume_bytes {
                return Err(MapError::OutOfVolume {
                    disk_end,
                    volume_bytes: self.volume_bytes,
                });
            }
            added_allocations.push(Span {
                start: disk_start,
                end: disk_end,
            });
        }
        let mapped_bytes = expected_vcn
            .checked_mul(CLUSTER_BYTES)
            .ok_or(MapError::Overflow { file_id })?;
        if mapped_bytes < logical_size {
            return Err(MapError::IncompleteFile {
                file_id,
                mapped_bytes,
                logical_size,
            });
        }
        let mut allocations = self.allocations.clone();
        allocations.extend(added_allocations);
        let mut reserved = allocations.clone();
        reserved.extend(self.metadata.iter().copied());
        check_span_overlaps(&reserved)?;

        let raw_extents = extents_from_runs(runs);
        if raw_extents.is_empty() {
            return Err(MapError::EmptyRuns);
        }

        let mut added = Vec::new();
        for extent in raw_extents {
            let remaining = logical_size.saturating_sub(extent.file_start);
            if remaining == 0 {
                // The converter sorts by physical address, not file offset.
                // A run beyond EOF may precede a valid earlier VCN on disk.
                continue;
            }
            let len = extent.len().min(remaining);
            if len == 0 {
                continue;
            }
            let disk_end = extent
                .disk_start_byte
                .checked_add(len)
                .ok_or(MapError::Overflow { file_id })?;
            if disk_end > self.volume_bytes {
                return Err(MapError::OutOfVolume {
                    disk_end,
                    volume_bytes: self.volume_bytes,
                });
            }
            added.push(FileExtent {
                file_id,
                disk_start: extent.disk_start_byte,
                disk_end,
                file_start: extent.file_start,
            });
        }
        if added.is_empty() {
            return Err(MapError::EmptyRuns);
        }

        let mut files = self.files.clone();
        files.extend(added.iter().copied());
        check_file_overlaps(&files)?;
        check_metadata_overlaps(&files, &self.metadata)?;
        files.sort_by_key(|e| e.disk_start);
        self.files = files;
        self.allocations = allocations;
        Ok(added.len())
    }

    pub fn file_extents(&self, file_id: u32) -> Vec<FileExtent> {
        self.files
            .iter()
            .filter(|e| e.file_id == file_id)
            .copied()
            .collect()
    }

    fn file_at(&self, disk_offset: u64) -> Option<FileExtent> {
        self.files
            .iter()
            .find(|e| disk_offset >= e.disk_start && disk_offset < e.disk_end)
            .copied()
    }

    fn metadata_at(&self, disk_offset: u64) -> Option<Span> {
        self.metadata
            .iter()
            .find(|m| disk_offset >= m.start && disk_offset < m.end)
            .copied()
    }

    fn next_boundary(&self, disk_offset: u64) -> u64 {
        let file = self
            .files
            .iter()
            .map(|e| e.disk_start)
            .filter(|&s| s > disk_offset)
            .min();
        let meta = self
            .metadata
            .iter()
            .map(|m| m.start)
            .filter(|&s| s > disk_offset)
            .min();
        file.into_iter()
            .chain(meta)
            .chain(std::iter::once(self.volume_bytes))
            .min()
            .unwrap_or(self.volume_bytes)
            .max(disk_offset.saturating_add(1))
    }

    /// Classifies one disk byte.
    pub fn route(&self, disk_offset: u64) -> MapRegion {
        if disk_offset >= self.volume_bytes {
            return MapRegion::Unmapped { len: 0 };
        }
        if let Some(extent) = self.file_at(disk_offset) {
            return MapRegion::File {
                file_id: extent.file_id,
                file_offset: extent.file_start + (disk_offset - extent.disk_start),
                len: extent.disk_end - disk_offset,
            };
        }
        if let Some(span) = self.metadata_at(disk_offset) {
            return MapRegion::Metadata {
                len: span.end - disk_offset,
            };
        }
        let boundary = self.next_boundary(disk_offset);
        MapRegion::Unmapped {
            len: boundary - disk_offset,
        }
    }

    /// Splits a request into identity-preserving segments. Every segment ends
    /// at the first boundary among the request, the extent, the file and the
    /// volume; no segment ever reports a mixed file id.
    pub fn route_read(&self, disk_offset: u64, len: u64) -> Result<Vec<Segment>, MapError> {
        let end = disk_offset.checked_add(len).ok_or(MapError::InvalidRange {
            start: disk_offset,
            end: u64::MAX,
        })?;
        let mut out = Vec::new();
        let mut cur = disk_offset;
        while cur < end {
            if cur >= self.volume_bytes {
                out.push(Segment {
                    disk_offset: cur,
                    region: MapRegion::Unmapped { len: end - cur },
                });
                break;
            }
            let region = self.route(cur);
            let available = region.len();
            if available == 0 {
                out.push(Segment {
                    disk_offset: cur,
                    region: MapRegion::Unmapped { len: end - cur },
                });
                break;
            }
            let take = available.min(end - cur);
            let clipped = match region {
                MapRegion::Metadata { .. } => MapRegion::Metadata { len: take },
                MapRegion::Unmapped { .. } => MapRegion::Unmapped { len: take },
                MapRegion::File {
                    file_id,
                    file_offset,
                    ..
                } => MapRegion::File {
                    file_id,
                    file_offset,
                    len: take,
                },
            };
            out.push(Segment {
                disk_offset: cur,
                region: clipped,
            });
            cur += take;
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MI: u64 = 1024 * 1024;

    fn runs(lcn: u64, clusters: u64) -> Vec<RunPiece> {
        vec![RunPiece {
            vcn: 0,
            length_clusters: clusters,
            lcn: Some(lcn),
        }]
    }

    fn sbt_record_map() -> ExtentMap {
        let mut map = ExtentMap::new(16 * MI).unwrap();
        map.add_metadata(0, MI).unwrap();
        // LCN 256 = 1 MiB, 256 clusters = 1 MiB.
        map.add_file_runs(0x1111, &runs(256, 256), MI).unwrap();
        // LCN 1024 = 4 MiB, 4 clusters = 16 KiB.
        map.add_file_runs(0x2222, &runs(1024, 4), 16 * 1024)
            .unwrap();
        map
    }

    #[test]
    fn routes_metadata_then_sbt_then_hole_then_record() {
        let map = sbt_record_map();
        // Start 8 bytes before file A; end 16 KiB into file B.
        let read_len = 8 + MI + 2 * MI + 16 * 1024;
        let segments = map.route_read(MI - 8, read_len).unwrap();
        assert_eq!(segments.len(), 4);
        assert_eq!(segments[0].region, MapRegion::Metadata { len: 8 });
        assert_eq!(
            segments[1].region,
            MapRegion::File {
                file_id: 0x1111,
                file_offset: 0,
                len: MI
            }
        );
        assert_eq!(segments[2].region, MapRegion::Unmapped { len: 2 * MI });
        assert_eq!(
            segments[3].region,
            MapRegion::File {
                file_id: 0x2222,
                file_offset: 0,
                len: 16 * 1024
            }
        );
        assert_eq!(segments[3].disk_offset, 4 * MI);
    }

    #[test]
    fn route_carries_file_offsets_for_unaligned_reads() {
        let map = sbt_record_map();
        let region = map.route(MI + 1234);
        assert_eq!(
            region,
            MapRegion::File {
                file_id: 0x1111,
                file_offset: 1234,
                len: MI - 1234
            }
        );
        let segments = map.route_read(MI + MI - 4, 8).unwrap();
        assert_eq!(
            segments[0].region,
            MapRegion::File {
                file_id: 0x1111,
                file_offset: MI - 4,
                len: 4
            }
        );
        assert_eq!(segments[1].region, MapRegion::Unmapped { len: 4 });
    }

    #[test]
    fn overlaps_with_files_and_metadata_are_rejected() {
        let mut map = ExtentMap::new(16 * MI).unwrap();
        map.add_metadata(0, MI).unwrap();
        map.add_file_runs(1, &runs(256, 16), 64 * 1024).unwrap();
        assert!(matches!(
            map.add_file_runs(2, &runs(256, 8), 32 * 1024),
            Err(MapError::Overlap { .. })
        ));
        assert!(matches!(
            map.add_file_runs(3, &runs(128, 256), MI),
            Err(MapError::Overlap { .. })
        ));
        assert!(matches!(
            map.add_metadata(MI, MI + 4096),
            Err(MapError::Overlap { .. })
        ));
    }

    #[test]
    fn logical_size_clips_allocation_rounding() {
        let mut map = ExtentMap::new(16 * MI).unwrap();
        // Allocated 16 clusters (64 KiB) but only 100 bytes are visible.
        map.add_file_runs(7, &runs(256, 16), 100).unwrap();
        let extents = map.file_extents(7);
        assert_eq!(extents.len(), 1);
        assert_eq!(extents[0].disk_start, MI);
        assert_eq!(extents[0].disk_end, MI + 100);
        assert_eq!(
            map.route(MI + 99),
            MapRegion::File {
                file_id: 7,
                file_offset: 99,
                len: 1
            }
        );
        assert_eq!(
            map.route(MI + 100),
            MapRegion::Unmapped { len: 15 * MI - 100 }
        );
    }

    #[test]
    fn clipped_eof_does_not_drop_fragmented_runs_in_physical_order() {
        let mut map = ExtentMap::new(MI).unwrap();
        let fragmented = [
            RunPiece {
                vcn: 0,
                length_clusters: 2,
                lcn: Some(100),
            },
            RunPiece {
                vcn: 2,
                length_clusters: 2,
                lcn: Some(10),
            },
        ];
        // The physical first run belongs after EOF. It must not stop the scan
        // before the logical head at the higher LCN.
        assert_eq!(map.add_file_runs(7, &fragmented, CLUSTER_BYTES), Ok(1));
        assert_eq!(
            map.route(100 * CLUSTER_BYTES),
            MapRegion::File {
                file_id: 7,
                file_offset: 0,
                len: CLUSTER_BYTES,
            }
        );
        assert!(matches!(
            map.route(10 * CLUSTER_BYTES),
            MapRegion::Unmapped { .. }
        ));
    }

    #[test]
    fn fragmented_complete_file_keeps_its_logical_offsets() {
        let mut map = ExtentMap::new(MI).unwrap();
        let fragmented = [
            RunPiece {
                vcn: 0,
                length_clusters: 2,
                lcn: Some(100),
            },
            RunPiece {
                vcn: 2,
                length_clusters: 2,
                lcn: Some(10),
            },
        ];
        assert_eq!(map.add_file_runs(7, &fragmented, 4 * CLUSTER_BYTES), Ok(2));
        assert_eq!(
            map.route(10 * CLUSTER_BYTES + 5),
            MapRegion::File {
                file_id: 7,
                file_offset: 2 * CLUSTER_BYTES + 5,
                len: 2 * CLUSTER_BYTES - 5,
            }
        );
        assert_eq!(
            map.route(100 * CLUSTER_BYTES + 5),
            MapRegion::File {
                file_id: 7,
                file_offset: 5,
                len: 2 * CLUSTER_BYTES - 5,
            }
        );
    }

    #[test]
    fn incomplete_and_sparse_live_files_are_rejected_without_mutation() {
        let mut map = ExtentMap::new(16 * MI).unwrap();
        assert!(matches!(
            map.add_file_runs(1, &runs(256, 256), 4_000_000_000),
            Err(MapError::IncompleteFile { .. })
        ));
        let sparse = [
            RunPiece {
                vcn: 0,
                length_clusters: 1,
                lcn: Some(100),
            },
            RunPiece {
                vcn: 1,
                length_clusters: 1,
                lcn: None,
            },
        ];
        assert_eq!(
            map.add_file_runs(1, &sparse, 2 * CLUSTER_BYTES),
            Err(MapError::SparseRun { file_id: 1, vcn: 1 })
        );
        assert!(map.files().is_empty());
        assert!(map.allocations.is_empty());
        // Rejected inputs did not reserve their physical clusters.
        map.add_file_runs(1, &runs(100, 2), 2 * CLUSTER_BYTES)
            .unwrap();
    }

    #[test]
    fn duplicate_holey_or_out_of_order_vcns_are_rejected() {
        for found in [0, 2] {
            let mut map = ExtentMap::new(MI).unwrap();
            let invalid = [
                RunPiece {
                    vcn: 0,
                    length_clusters: 1,
                    lcn: Some(100),
                },
                RunPiece {
                    vcn: found,
                    length_clusters: 1,
                    lcn: Some(10),
                },
            ];
            assert_eq!(
                map.add_file_runs(3, &invalid, 2 * CLUSTER_BYTES),
                Err(MapError::NonContiguousVcn {
                    file_id: 3,
                    expected: 1,
                    found
                })
            );
            assert!(map.files().is_empty());
        }
        let mut map = ExtentMap::new(MI).unwrap();
        assert_eq!(
            map.add_file_runs(
                3,
                &[RunPiece {
                    vcn: 1,
                    length_clusters: 1,
                    lcn: Some(100),
                }],
                CLUSTER_BYTES
            ),
            Err(MapError::NonContiguousVcn {
                file_id: 3,
                expected: 0,
                found: 1
            })
        );
    }

    #[test]
    fn a_file_cannot_be_registered_twice_at_a_different_lcn() {
        let mut map = ExtentMap::new(MI).unwrap();
        map.add_file_runs(7, &runs(100, 1), CLUSTER_BYTES).unwrap();
        let before = map.files().to_vec();
        assert_eq!(
            map.add_file_runs(7, &runs(10, 1), CLUSTER_BYTES),
            Err(MapError::DuplicateFileId { file_id: 7 })
        );
        assert_eq!(map.files(), before);
    }

    #[test]
    fn overflowing_and_zero_length_runs_are_rejected() {
        let mut map = ExtentMap::new(u64::MAX).unwrap();
        for invalid in [
            runs(u64::MAX, 1),
            runs(1, u64::MAX),
            runs(u64::MAX / CLUSTER_BYTES, 1),
        ] {
            assert_eq!(
                map.add_file_runs(9, &invalid, 1),
                Err(MapError::Overflow { file_id: 9 })
            );
            assert!(map.files().is_empty());
        }
        assert_eq!(
            map.add_file_runs(9, &runs(1, 0), 1),
            Err(MapError::InvalidRun { file_id: 9, vcn: 0 })
        );
    }

    #[test]
    fn allocation_tail_cannot_extend_past_volume_or_overlap_reserved_space() {
        let mut map = ExtentMap::new(2 * MI).unwrap();
        // The first visible byte fits, but the actual run goes past the volume.
        assert!(matches!(
            map.add_file_runs(1, &runs(511, 2), 1),
            Err(MapError::OutOfVolume { .. })
        ));
        map.add_file_runs(1, &runs(100, 1), 1).unwrap();
        let tail_start = 100 * CLUSTER_BYTES + 1;
        assert!(matches!(
            map.add_metadata(tail_start, tail_start + 10),
            Err(MapError::Overlap { .. })
        ));
        assert!(map.metadata().is_empty());
    }

    #[test]
    fn metadata_overlap_is_rejected_transactionally() {
        let mut map = ExtentMap::new(MI).unwrap();
        map.add_metadata(0, CLUSTER_BYTES).unwrap();
        let before = map.metadata().to_vec();
        assert!(matches!(
            map.add_metadata(CLUSTER_BYTES / 2, 2 * CLUSTER_BYTES),
            Err(MapError::Overlap { .. })
        ));
        assert_eq!(map.metadata(), before);
        map.add_metadata(CLUSTER_BYTES, 2 * CLUSTER_BYTES).unwrap();
        assert_eq!(map.metadata().len(), 2);
    }

    #[test]
    fn empty_and_out_of_volume_inputs_are_rejected() {
        let mut map = ExtentMap::new(2 * MI).unwrap();
        assert!(matches!(
            map.add_file_runs(1, &[], 1024),
            Err(MapError::EmptyRuns)
        ));
        assert!(matches!(
            map.add_file_runs(1, &runs(512, 8), 32 * 1024),
            Err(MapError::OutOfVolume { .. })
        ));
        assert!(matches!(
            map.add_metadata(0, 2 * MI + 1),
            Err(MapError::InvalidRange { .. })
        ));
    }

    #[test]
    fn read_past_volume_is_clipped_without_panicking() {
        let map = sbt_record_map();
        let segments = map.route_read(16 * MI - 4, 16).unwrap();
        assert_eq!(segments.len(), 2);
        assert_eq!(segments[0].region, MapRegion::Unmapped { len: 4 });
        assert_eq!(segments[1].region, MapRegion::Unmapped { len: 12 });
        assert!(map.route(16 * MI).is_empty());
    }
}
