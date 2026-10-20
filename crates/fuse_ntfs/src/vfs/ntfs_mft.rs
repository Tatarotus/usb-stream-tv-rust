//! NTFS on-disk geometry, Master File Table record parsing, and the dynamic
//! Volume Serial Number (VSN) anti-cache patcher.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::io::{self, Read};
use std::path::Path;

pub const BPS: usize = 512;
pub const SPC: usize = 8;
pub const CLUSTER_SIZE: usize = BPS * SPC;
pub const VOLUME_BYTES: u64 = 128 * 1024 * 1024 * 1024;
pub const TOTAL_SECTORS: u64 = VOLUME_BYTES / BPS as u64;
pub const TOTAL_BYTES: u64 = VOLUME_BYTES;
pub const TOTAL_CLUSTERS: u64 = VOLUME_BYTES / CLUSTER_SIZE as u64;
pub const MFT_LCN: u64 = 4;
pub const MFT_BYTE_OFFSET: u64 = MFT_LCN * CLUSTER_SIZE as u64;
pub const MFT_RECORD_SIZE: usize = 1024;
/// Upper bound of MFT records scanned for the live stream file. The deployed
/// 128 GiB template carries the `TV AO VIVO.trp` record at index 64 (the ~27
/// system records plus index/bitmap growth), so the scan window must exceed
/// it. Records past the `$MFT` allocation read as sparse zeros and fail fixup
/// validation, so the extra iterations are wasted preads at most — never a
/// false positive.
pub const MFT_RECORDS: usize = 256;
pub const MFT_MIRROR_LCN: u64 = TOTAL_CLUSTERS / 2;
pub const DATA_START_CLUSTER: u64 = 64;
pub const DATA_START_BYTE: u64 = DATA_START_CLUSTER * CLUSTER_SIZE as u64;
pub const VSN_OFFSET: usize = 0x48;
pub const ROOT_INO: u64 = 1;
pub const FILE_INO: u64 = 2;
pub const FILE_NAME: &str = "tv_stream.img";
pub const NTFS_ROOT_INODE: u64 = 5;
pub const LIVE_FILE_INODE: u64 = 27;
pub const LIVE_FILE_NAME: &str = "TV AO VIVO.trp";
/// Anti-cache alternate name: `patch_trp` rewrites the same MFT record to this
/// name on every live restart (AGENTS.md §2.3 tier 1), so the layout probe
/// must accept either spelling or the re-anchored volume would silently fall
/// back to synthesized geometry.
pub const LIVE_FILE_NAME_ALT: &str = "TV AO VIVO 2.tp";
pub const MFT_REC_IN_USE: u16 = 0x0001;
pub const MFT_REC_IS_DIRECTORY: u16 = 0x0002;
pub const ATTR_STANDARD_INFORMATION: u32 = 0x10;
pub const ATTR_ATTRIBUTE_LIST: u32 = 0x20;
pub const ATTR_FILE_NAME: u32 = 0x30;
pub const ATTR_OBJECT_ID: u32 = 0x40;
pub const ATTR_VOLUME_NAME: u32 = 0x60;
pub const ATTR_DATA: u32 = 0x80;
pub const ATTR_INDEX_ROOT: u32 = 0x90;
pub const ATTR_INDEX_ALLOCATION: u32 = 0xA0;
pub const ATTR_BITMAP: u32 = 0xB0;
pub const RESIDENT_HDR_SZ: usize = 0x18;
pub const NONRESIDENT_HDR_SZ: usize = 0x40;

#[derive(Debug)]
pub enum MftError {
    CorruptRecord(&'static str),
    TornRecord { sector: usize, expected: u16, found: u16 },
    InvalidVbr(&'static str),
}

fn rd_u16(buf: &[u8], off: usize) -> Result<u16, MftError> {
    let b = buf.get(off..off+2).ok_or(MftError::CorruptRecord("rd_u16 oob"))?;
    Ok(u16::from_le_bytes([b[0], b[1]]))
}
fn rd_u32(buf: &[u8], off: usize) -> Result<u32, MftError> {
    let b = buf.get(off..off+4).ok_or(MftError::CorruptRecord("rd_u32 oob"))?;
    Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
}
fn rd_u64(buf: &[u8], off: usize) -> Result<u64, MftError> {
    let b = buf.get(off..off+8).ok_or(MftError::CorruptRecord("rd_u64 oob"))?;
    Ok(u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
}
fn wr_u16(buf: &mut [u8], off: usize, val: u16) -> Result<(), MftError> {
    let dst = buf.get_mut(off..off+2).ok_or(MftError::CorruptRecord("wr_u16 oob"))?;
    dst.copy_from_slice(&val.to_le_bytes());
    Ok(())
}
#[allow(dead_code)]
fn wr_u32(buf: &mut [u8], off: usize, val: u32) -> Result<(), MftError> {
    let dst = buf.get_mut(off..off+4).ok_or(MftError::CorruptRecord("wr_u32 oob"))?;
    dst.copy_from_slice(&val.to_le_bytes());
    Ok(())
}
fn wr_u64(buf: &mut [u8], off: usize, val: u64) -> Result<(), MftError> {
    let dst = buf.get_mut(off..off+8).ok_or(MftError::CorruptRecord("wr_u64 oob"))?;
    dst.copy_from_slice(&val.to_le_bytes());
    Ok(())
}

fn usa_bounds(rec: &[u8], min_payload_off: usize) -> Result<(usize, usize, usize), MftError> {
    if rec.len() < BPS || rec.len() % BPS != 0 { return Err(MftError::CorruptRecord("size")); }
    let off = usize::from(rd_u16(rec, 4)?);
    let cnt = usize::from(rd_u16(rec, 6)?);
    let expected_cnt = (rec.len() / BPS) + 1;
    let usa_end = off + cnt * 2;
    if off < 0x28 || (off & 1) != 0 || cnt != expected_cnt || usa_end > min_payload_off || usa_end > rec.len() {
        return Err(MftError::CorruptRecord("bounds"));
    }
    Ok((off, cnt, expected_cnt))
}
pub fn check_fixup(rec: &[u8], min_payload_off: usize) -> Result<(), MftError> {
    let (off, cnt, _) = usa_bounds(rec, min_payload_off)?;
    let usn = rd_u16(rec, off)?;
    if usn == 0 || usn == 0xFFFF { return Err(MftError::CorruptRecord("usn")); }
    for i in 1..cnt {
        let tail_off = i * BPS - 2;
        let tail_usn = rd_u16(rec, tail_off)?;
        if tail_usn != usn { return Err(MftError::TornRecord { sector: i, expected: usn, found: tail_usn }); }
    }
    Ok(())
}
pub fn apply_fixup(rec: &mut [u8], min_payload_off: usize) -> Result<(), MftError> {
    check_fixup(rec, min_payload_off)?;
    let (off, cnt, _) = usa_bounds(rec, min_payload_off)?;
    for i in 1..cnt {
        let tail_off = i * BPS - 2;
        let saved = rd_u16(rec, off + i * 2)?;
        wr_u16(rec, tail_off, saved)?;
    }
    Ok(())
}
/// Bumps the update sequence number and re-arms every sector tail.
///
/// Accepts the record in either representation:
/// * **on-disk form** (tails already carry the USN, saved originals live in the
///   array): only the USN advances, so the originals survive;
/// * **expanded form** (a previous [`apply_fixup`] restored the real tail
///   bytes): the current tails are saved back into the array first.
///
/// Blindly saving the tails in on-disk form would overwrite the saved
/// originals with the old USN and silently corrupt the record on the next
/// [`apply_fixup`], hence the representation probe below.
pub fn stamp_fixup(rec: &mut [u8], min_payload_off: usize) -> Result<(), MftError> {
    let (off, cnt, _) = usa_bounds(rec, min_payload_off)?;
    let cur = rd_u16(rec, off)?;
    let mut usn = cur.wrapping_add(1);
    if usn == 0 || usn == 0xFFFF { usn = 1; }
    let on_disk_form = (1..cnt).all(|i| rd_u16(rec, i * BPS - 2).ok() == Some(cur));
    wr_u16(rec, off, usn)?;
    for i in 1..cnt {
        let tail_off = i * BPS - 2;
        if !on_disk_form {
            let saved = rd_u16(rec, tail_off)?;
            wr_u16(rec, off + i * 2, saved)?;
        }
        wr_u16(rec, tail_off, usn)?;
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VbrInfo {
    pub bytes_per_sector: u32,
    pub sectors_per_cluster: u8,
    pub cluster_size: u32,
    pub total_sectors: u64,
    pub mft_lcn: u64,
    pub mft_mirror_lcn: u64,
    pub mft_record_size: u32,
    pub index_record_size: u32,
    pub serial: u64,
}
fn decode_size_code(code: i8, cluster_size: u32, what: &'static str) -> Result<u32, MftError> {
    let size = if code < 0 {
        let shift = i32::from(-code);
        if shift > 31 { return Err(MftError::InvalidVbr(what)); }
        1u32.checked_shl(shift as u32).ok_or(MftError::InvalidVbr(what))?
    } else {
        (code as u32).checked_mul(cluster_size).ok_or(MftError::InvalidVbr(what))?
    };
    if size == 0 || size > 64 * 1024 { return Err(MftError::InvalidVbr(what)); }
    Ok(size)
}
pub fn parse_vbr(vbr: &[u8]) -> Result<VbrInfo, MftError> {
    if vbr.len() < BPS { return Err(MftError::InvalidVbr("short")); }
    if vbr[510] != 0x55 || vbr[511] != 0xAA { return Err(MftError::InvalidVbr("sig")); }
    if &vbr[3..11] != b"NTFS    " { return Err(MftError::InvalidVbr("oem")); }
    let bytes_per_sector = u32::from(rd_u16(vbr, 11)?);
    if bytes_per_sector as usize != BPS { return Err(MftError::InvalidVbr("bps")); }
    let sectors_per_cluster = vbr[13];
    if sectors_per_cluster == 0 || (sectors_per_cluster & (sectors_per_cluster - 1)) != 0 { return Err(MftError::InvalidVbr("spc")); }
    let cluster_size = bytes_per_sector * u32::from(sectors_per_cluster);
    if cluster_size as usize != CLUSTER_SIZE { return Err(MftError::InvalidVbr("cls")); }
    let total_sectors = rd_u64(vbr, 0x28)?;
    if total_sectors == 0 { return Err(MftError::InvalidVbr("tot")); }
    let mft_lcn = rd_u64(vbr, 0x30)?;
    let mft_mirror_lcn = rd_u64(vbr, 0x38)?;
    let mft_record_size = decode_size_code(vbr[64] as i8, cluster_size, "mft")?;
    let index_record_size = decode_size_code(vbr[68] as i8, cluster_size, "idx")?;
    let serial = rd_u64(vbr, 0x48)?;
    Ok(VbrInfo { bytes_per_sector, sectors_per_cluster, cluster_size, total_sectors, mft_lcn, mft_mirror_lcn, mft_record_size, index_record_size, serial })
}
pub fn synthesize_vbr(out: &mut [u8; BPS], geometry: &VbrInfo, serial: u64) {
    out.fill(0);
    out[0..3].copy_from_slice(&[0xEB, 0x52, 0x90]);
    out[3..11].copy_from_slice(b"NTFS    ");
    wr_u16(out, 11, geometry.bytes_per_sector as u16).ok();
    out[13] = geometry.sectors_per_cluster;
    out[21] = 0xF8;
    wr_u16(out, 24, 63).ok();
    wr_u16(out, 26, 255).ok();
    wr_u64(out, 0x28, geometry.total_sectors).ok();
    wr_u64(out, 0x30, geometry.mft_lcn).ok();
    wr_u64(out, 0x38, geometry.mft_mirror_lcn).ok();
    let rec_code: i8 = match geometry.mft_record_size { 1024 => -10, 2048 => -11, 4096 => -12, n => i8::try_from(n / u32::from(geometry.cluster_size)).unwrap_or(1) };
    out[64] = rec_code as u8;
    let idx_code: i8 = i8::try_from(geometry.index_record_size / geometry.cluster_size).unwrap_or(1).max(1);
    out[68] = idx_code as u8;
    wr_u64(out, VSN_OFFSET, serial).ok();
    out[510] = 0x55;
    out[511] = 0xAA;
}
pub fn random_serial() -> u64 {
    let mut rnd = [0u8; 8];
    if let Ok(mut f) = File::open("/dev/urandom") { let _ = f.read_exact(&mut rnd); }
    if rnd == [0u8; 8] {
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0x9E37_79B9_7F4A_7C15);
        rnd = nanos.to_le_bytes();
    }
    let serial = u64::from_le_bytes(rnd);
    if serial == 0 { 0x2545_F491_4F6C_DD1D } else { serial }
}

/// One contiguous piece of the live stream file on disk.
///
/// The file is fragmented: the deployed template stores it in three
/// physically separated runs (`$LogFile` / `$Bitmap` metadata live in the
/// gaps). The Samsung NTFS driver computes cluster LBAs from the runlist, so
/// every read arrives at the disk offset of its cluster and the daemon must
/// invert the mapping back to the file offset the ring buffer understands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskExtent {
    /// First disk byte of the extent (cluster aligned).
    pub disk_start_byte: u64,
    /// One past the last disk byte of the extent.
    pub disk_end_byte: u64,
    /// File offset that maps to `disk_start_byte`.
    pub file_start: u64,
}

impl DiskExtent {
    /// Byte length of the extent.
    pub fn len(&self) -> u64 {
        self.disk_end_byte.saturating_sub(self.disk_start_byte)
    }
    /// File offset one past the extent end.
    pub fn file_end(&self) -> u64 {
        self.file_start + self.len()
    }
}

/// Disk geometry of the live stream, probed from the template `$MFT`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeLayout {
    /// First disk byte of stream data (start of the first extent).
    pub data_start_byte: u64,
    /// Live file size reported by its `$DATA` attribute.
    pub data_size: u64,
    /// MFT record index of the live file.
    pub live_file_inode: u64,
    /// Stream extents sorted by disk offset. The fallback layout (no live
    /// record parsed) is a single extent covering the volume tail.
    pub extents: Vec<DiskExtent>,
}

/// Classification of one disk offset for the whole-disk read path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiskRegion {
    /// Filesystem metadata or a sparse hole: served from the template.
    /// Payload is the contiguous run length available from the queried offset.
    Metadata(u64),
    /// Live stream data. Payload is the file offset (foff) for the backend.
    Data(u64),
}

pub struct NtfsTemplate {
    #[allow(dead_code)]
    file: Option<File>,
    pub vbr: VbrInfo,
    pub layout: VolumeLayout,
    pub serial: u64,
}
impl NtfsTemplate {
    pub fn open<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let path = path.as_ref();
        if !path.exists() { return Ok(Self::synthesized()); }
        let file = File::open(path)?;
        let mut boot = [0u8; BPS];
        let vbr = if file.read_exact_at(&mut boot, 0).is_ok() { parse_vbr(&boot).unwrap_or_else(|_| Self::default_vbr()) } else { Self::default_vbr() };
        let layout = probe_layout(&file, &vbr);
        Ok(Self { file: Some(file), vbr, layout, serial: random_serial() })
    }
    pub fn default_vbr() -> VbrInfo { VbrInfo { bytes_per_sector: BPS as u32, sectors_per_cluster: SPC as u8, cluster_size: CLUSTER_SIZE as u32, total_sectors: TOTAL_SECTORS - 1, mft_lcn: MFT_LCN, mft_mirror_lcn: MFT_MIRROR_LCN, mft_record_size: MFT_RECORD_SIZE as u32, index_record_size: CLUSTER_SIZE as u32, serial: 0 } }
    pub fn synthesized() -> Self { Self { file: None, vbr: Self::default_vbr(), layout: fallback_layout(), serial: random_serial() } }
    pub fn randomize_serial(&mut self) { self.serial = random_serial(); }
    /// Builds a template-free (synthesized-metadata) view over an explicit
    /// geometry + stream layout. Useful for tests and for maintenance builds
    /// that serve the stream without a backing template image.
    pub fn with_layout(vbr: VbrInfo, layout: VolumeLayout) -> Self {
        Self { file: None, vbr, layout, serial: random_serial() }
    }
    /// Byte offset of the backup boot sector. The NTFS BPB counts volume
    /// sectors EXCLUDING the backup boot sector, so the backup lives at LBA
    /// `total_sectors` — verified against both the 64 MiB lab template
    /// (BPB 131071 on a 131072-sector disk) and the deployed 128 GiB template
    /// (BPB 268435455 on a 268435456-sector disk), both carrying the NTFS
    /// signature at that exact LBA.
    pub fn backup_vbr_byte(&self) -> u64 {
        self.vbr.total_sectors.saturating_mul(BPS as u64)
    }
    /// Extent containing `offset`, if the offset falls inside stream data.
    pub fn extent_at(&self, offset: u64) -> Option<DiskExtent> {
        self.layout.extents.iter().copied().find(|e| offset >= e.disk_start_byte && offset < e.disk_end_byte)
    }
    /// Classifies a disk offset for the read path: metadata (served from the
    /// template, holes included) or stream data (routed to the backend with
    /// the extent-inverted file offset).
    pub fn route(&self, offset: u64) -> DiskRegion {
        if offset >= self.backup_vbr_byte() {
            return DiskRegion::Metadata(TOTAL_BYTES.saturating_sub(offset));
        }
        if let Some(e) = self.extent_at(offset) {
            return DiskRegion::Data(e.file_start + (offset - e.disk_start_byte));
        }
        // Metadata below the data start, or a sparse hole between extents.
        let next = self.layout.extents.iter().map(|e| e.disk_start_byte).find(|&s| s > offset);
        let end = next.unwrap_or(self.backup_vbr_byte().max(self.layout.data_start_byte)).min(TOTAL_BYTES);
        DiskRegion::Metadata(end.saturating_sub(offset).max(1))
    }
    /// Bytes from `offset` to the end of the extent containing it (0 when the
    /// offset is not stream data). Callers use it to keep a single backend
    /// call from bridging a hole between two extents.
    pub fn data_run_len(&self, offset: u64) -> u64 {
        match self.extent_at(offset) {
            Some(e) => e.disk_end_byte.saturating_sub(offset),
            None => 0,
        }
    }
    /// True when `offset` is not stream data (metadata or hole).
    pub fn is_metadata_offset(&self, offset: u64) -> bool {
        !matches!(self.route(offset), DiskRegion::Data(_))
    }
    pub fn read_sector(&self, sec: u64, out: &mut [u8; BPS]) {
        out.fill(0);
        if sec == 0 || sec == self.vbr.total_sectors {
            if let Some(ref f) = self.file {
                let off = if sec == 0 { 0 } else { sec.saturating_mul(BPS as u64) };
                let _ = f.read_exact_at(out, off);
                let _ = wr_u64(out, VSN_OFFSET, self.serial);
                return;
            } else {
                synthesize_vbr(out, &self.vbr, self.serial);
                return;
            }
        }
        if let Some(ref f) = self.file {
            let _ = f.read_at(out, sec.saturating_mul(BPS as u64));
        } else {
            // fill zeros already done
        }
    }
}

/// Layout used when no live stream record can be probed from the template:
/// one extent covering the whole volume tail, so the whole-disk read path
/// still routes data to the backend instead of serving zeros forever.
fn fallback_layout() -> VolumeLayout {
    VolumeLayout {
        data_start_byte: DATA_START_BYTE,
        data_size: (TOTAL_CLUSTERS - DATA_START_CLUSTER) * CLUSTER_SIZE as u64,
        live_file_inode: LIVE_FILE_INODE,
        extents: vec![DiskExtent {
            disk_start_byte: DATA_START_BYTE,
            disk_end_byte: TOTAL_BYTES,
            file_start: 0,
        }],
    }
}
pub fn probe_layout(file: &File, vbr: &VbrInfo) -> VolumeLayout {
    let mut layout = fallback_layout();
    let mft_base = vbr.mft_lcn.saturating_mul(u64::from(vbr.cluster_size));
    let rec_sz = (vbr.mft_record_size as usize).max(BPS);
    for i in 0..MFT_RECORDS {
        let off = mft_base + (i as u64) * (rec_sz as u64);
        let mut rec = vec![0u8; rec_sz];
        if file.read_exact_at(&mut rec, off).is_err() {
            continue;
        }
        let meta = match parse_record_meta(&rec) {
            Ok(m) => m,
            Err(_) => continue,
        };
        let attrs_offset = meta.attrs_offset;
        if apply_fixup(&mut rec, attrs_offset).is_err() {
            continue;
        }
        let mut cur = attrs_offset;
        let mut is_live_record = false;
        while let Ok(Some(attr)) = parse_attr_header(&rec, cur) {
            if attr.attr_type == ATTR_FILE_NAME {
                let val_off = attr.offset + attr.runlist_offset as usize;
                let val_end = val_off + attr.real_size as usize;
                if val_end <= rec.len() {
                    let value = &rec[val_off..val_end];
                    if let Some(fn_name) = parse_file_name(value) {
                        if fn_name.name == LIVE_FILE_NAME || fn_name.name == LIVE_FILE_NAME_ALT {
                            is_live_record = true;
                            break;
                        }
                    }
                }
            }
            cur += attr.length as usize;
        }

        if is_live_record {
            layout.live_file_inode = i as u64;
            if let Ok(Some(data_attr)) = find_attr(&rec, attrs_offset, ATTR_DATA) {
                if data_attr.non_resident && data_attr.real_size > 0 {
                    layout.data_size = data_attr.real_size;
                    // Invert the runlist into disk extents: the TV
                    // addresses the file by cluster LBA, and the
                    // daemon needs LBA -> file offset to feed the
                    // ring buffer. Falls back to the volume-tail
                    // extent when the runlist is empty/unusable.
                    let rl_start = data_attr.offset + data_attr.runlist_offset as usize;
                    let rl_end = (data_attr.offset + data_attr.length as usize).min(rec.len());
                    if rl_start < rl_end {
                        let extents = extents_from_runs(&parse_runlist(&rec[rl_start..rl_end]));
                        if !extents.is_empty() {
                            layout.data_start_byte = extents[0].disk_start_byte;
                            layout.extents = extents;
                        }
                    }
                }
            }
            return layout;
        }
    }
    layout
}

/// One decoded runlist piece: `length_clusters` clusters starting at `lcn`
/// (`None` marks a sparse hole).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunPiece {
    pub vcn: u64,
    pub length_clusters: u64,
    pub lcn: Option<u64>,
}

/// Decodes an NTFS runlist into VCN/LCN pieces.
///
/// Each run is a header byte (low nibble = length field size, high nibble =
/// offset field size), the unsigned length, then the signed LCN delta. A zero
/// offset field marks a sparse run.
pub fn parse_runlist(buf: &[u8]) -> Vec<RunPiece> {
    let mut runs = Vec::new();
    let mut i = 0usize;
    let mut vcn: u64 = 0;
    let mut lcn: i64 = 0;
    while i < buf.len() {
        let header = buf[i];
        if header == 0 {
            break;
        }
        let len_sz = usize::from(header & 0x0F);
        let off_sz = usize::from(header >> 4);
        i += 1;
        if len_sz == 0 || len_sz > 8 || off_sz > 8 || i + len_sz + off_sz > buf.len() {
            break;
        }
        let length = u64::from_le_bytes(pad8(&buf[i..i + len_sz]));
        i += len_sz;
        if off_sz == 0 {
            if length == 0 {
                continue;
            }
            runs.push(RunPiece { vcn, length_clusters: length, lcn: None });
            vcn = vcn.saturating_add(length);
            continue;
        }
        let delta = i64::from_le_bytes(sign_extend(&buf[i..i + off_sz]));
        i += off_sz;
        lcn = lcn.wrapping_add(delta);
        if length == 0 {
            continue;
        }
        runs.push(RunPiece { vcn, length_clusters: length, lcn: Some(lcn.max(0) as u64) });
        vcn = vcn.saturating_add(length);
    }
    runs
}

/// Little-endian fixed-width read padded to 8 bytes.
fn pad8(b: &[u8]) -> [u8; 8] {
    let mut out = [0u8; 8];
    out[..b.len()].copy_from_slice(b);
    out
}

/// Little-endian two's-complement read for the signed LCN delta.
fn sign_extend(b: &[u8]) -> [u8; 8] {
    let mut out = [0u8; 8];
    out[..b.len()].copy_from_slice(b);
    if b.last().is_some_and(|&m| m & 0x80 != 0) {
        for byte in out.iter_mut().skip(b.len()) {
            *byte = 0xFF;
        }
    }
    out
}

/// Folds runlist pieces into disk extents, coalescing physically adjacent
/// pieces so each extent is one contiguous disk range with a single file
/// offset base. File offsets accumulate over every piece (holes included),
/// matching how the NTFS driver maps VCNs to file positions.
pub fn extents_from_runs(runs: &[RunPiece]) -> Vec<DiskExtent> {
    let mut extents: Vec<DiskExtent> = Vec::new();
    let mut file_start: u64 = 0;
    for run in runs {
        let len_bytes = run.length_clusters.saturating_mul(CLUSTER_SIZE as u64);
        if let Some(lcn) = run.lcn {
            let disk_start = lcn.saturating_mul(CLUSTER_SIZE as u64);
            let disk_end = disk_start.saturating_add(len_bytes);
            match extents.last_mut() {
                Some(last) if last.disk_end_byte == disk_start => {
                    last.disk_end_byte = disk_end;
                }
                _ => extents.push(DiskExtent { disk_start_byte: disk_start, disk_end_byte: disk_end, file_start }),
            }
        }
        file_start = file_start.saturating_add(len_bytes);
    }
    // Runlists are VCN-ordered; LCNs can jump backwards. Sorting by disk
    // offset keeps `route`'s "next extent" scan and the hole runs correct.
    extents.sort_by_key(|e| e.disk_start_byte);
    extents
}

#[derive(Debug, Clone)]
pub struct FileNameAttr { pub parent_inode: u64, pub name: String }
pub fn parse_file_name(value: &[u8]) -> Option<FileNameAttr> {
    if value.len() < 0x42 { return None; }
    let parent_ref = rd_u64(value, 0).ok()?;
    let parent_inode = parent_ref & 0x0000_FFFF_FFFF_FFFF;
    let name_length = value[0x40] as usize;
    let name_bytes_len = name_length * 2;
    if 0x42 + name_bytes_len > value.len() { return None; }
    let utf16: Vec<u16> = value[0x42..0x42+name_bytes_len].chunks_exact(2).map(|c| u16::from_le_bytes([c[0],c[1]])).collect();
    let name = String::from_utf16_lossy(&utf16);
    Some(FileNameAttr { parent_inode, name })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MftRecordMeta { pub sequence: u16, pub link_count: u16, pub flags: u16, pub bytes_in_use: u32, pub attrs_offset: usize }
pub fn parse_record_meta(rec: &[u8]) -> Result<MftRecordMeta, MftError> {
    if rec.len() < 0x30 { return Err(MftError::CorruptRecord("short")); }
    Ok(MftRecordMeta { sequence: rd_u16(rec,0x10)?, link_count: rd_u16(rec,0x12)?, flags: rd_u16(rec,0x16)?, bytes_in_use: rd_u32(rec,0x18)?, attrs_offset: rd_u16(rec,0x14)? as usize })
}
#[derive(Debug, Clone, Copy)]
pub struct AttrHeader { pub offset: usize, pub attr_type: u32, pub length: u32, pub non_resident: bool, pub runlist_offset: u16, pub real_size: u64 }

pub fn parse_attr_header(rec: &[u8], cur: usize) -> Result<Option<AttrHeader>, MftError> {
    if cur + 8 > rec.len() {
        return Ok(None);
    }
    let a_type = rd_u32(rec, cur)?;
    if a_type == 0xFFFF_FFFF {
        return Ok(None);
    }
    let length = rd_u32(rec, cur + 4)?;
    if length == 0 || cur + length as usize > rec.len() {
        return Ok(None);
    }
    let non_resident = rec.get(cur + 8).copied().unwrap_or(0) != 0;
    let (runlist_offset, real_size) = if non_resident {
        let ro = rd_u16(rec, cur + 0x20).unwrap_or(0);
        let rs = rd_u64(rec, cur + 0x30).unwrap_or(0);
        (ro, rs)
    } else {
        let ro = rd_u16(rec, cur + 0x14).unwrap_or(0);
        let vs = rd_u32(rec, cur + 0x10).unwrap_or(0) as u64;
        (ro, vs)
    };
    Ok(Some(AttrHeader {
        offset: cur,
        attr_type: a_type,
        length,
        non_resident,
        runlist_offset,
        real_size,
    }))
}

pub fn find_attr(rec: &[u8], attrs_offset: usize, attr_type: u32) -> Result<Option<AttrHeader>, MftError> {
    let mut cur = attrs_offset;
    while let Some(attr) = parse_attr_header(rec, cur)? {
        if attr.attr_type == attr_type {
            return Ok(Some(attr));
        }
        cur += attr.length as usize;
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Update sequence number used across the synthetic records below.
    const USN: u16 = 0x0001;
    /// Offset of the first attribute (must be past the USA array).
    const ATTRS_OFF: usize = 0x38;
    /// Real size reported by the synthetic non-resident `$DATA`.
    const DATA_REAL_SIZE: u64 = 0x1_0000_0000;
    /// Resident value offset inside an attribute (common 0x10 + resident 0x18).
    const RES_VALUE_OFF: u16 = 0x28;
    /// Live-stream file name length in UTF-16 code units.
    const LIVE_UTF16_LEN: usize = 14;

    /// Synthetic 2-sector (1024 B) `FILE` record with a valid update sequence
    /// array: `usa_offset = 0x30`, `usa_count = 3`, both 512-byte sector tails
    /// stamped with the USN and the saved originals parked at 0x32 / 0x34.
    fn synth_record() -> Vec<u8> {
        let mut rec = vec![0u8; MFT_RECORD_SIZE];
        rec[0..4].copy_from_slice(b"FILE");
        wr_u16(&mut rec, 4, 0x30).unwrap();
        wr_u16(&mut rec, 6, 3).unwrap();
        wr_u16(&mut rec, 0x10, 7).unwrap();
        wr_u16(&mut rec, 0x12, 1).unwrap();
        wr_u16(&mut rec, 0x16, MFT_REC_IN_USE | MFT_REC_IS_DIRECTORY).unwrap();
        wr_u32(&mut rec, 0x18, MFT_RECORD_SIZE as u32).unwrap();
        wr_u16(&mut rec, 0x14, ATTRS_OFF as u16).unwrap();
        wr_u16(&mut rec, 0x30, USN).unwrap();
        wr_u16(&mut rec, 0x32, 0x1234).unwrap();
        wr_u16(&mut rec, 0x34, 0x5678).unwrap();
        wr_u16(&mut rec, 510, USN).unwrap();
        wr_u16(&mut rec, 1022, USN).unwrap();
        rec
    }

    fn put_resident_file_name_attr(rec: &mut [u8], at: usize, parent_inode: u64) -> usize {
        put_named_file_name_attr(rec, at, parent_inode, LIVE_FILE_NAME)
    }

    /// Resident `$FILE_NAME` attribute with an arbitrary name (UTF-16).
    fn put_named_file_name_attr(rec: &mut [u8], at: usize, parent_inode: u64, name: &str) -> usize {
        let utf16: Vec<u16> = name.encode_utf16().collect();
        let value_off = at + RES_VALUE_OFF as usize;
        let value_len = 0x42 + utf16.len() * 2;
        wr_u64(rec, value_off, parent_inode << 48).unwrap();
        rec[value_off + 0x40] = utf16.len() as u8;
        rec[value_off + 0x41] = 0x03;
        for (i, u) in utf16.iter().enumerate() {
            wr_u16(rec, value_off + 0x42 + i * 2, *u).unwrap();
        }
        let length = (RES_VALUE_OFF as usize + value_len) as u32;
        wr_u32(rec, at, ATTR_FILE_NAME).unwrap();
        wr_u32(rec, at + 4, length).unwrap();
        rec[at + 8] = 0;
        wr_u32(rec, at + 0x10, value_len as u32).unwrap();
        wr_u16(rec, at + 0x14, RES_VALUE_OFF).unwrap();
        at + length as usize
    }

    fn put_nonresident_data_attr(rec: &mut [u8], at: usize, real_size: u64) -> usize {
        wr_u32(rec, at, ATTR_DATA).unwrap();
        wr_u32(rec, at + 4, 0x48).unwrap();
        rec[at + 8] = 1;
        wr_u16(rec, at + 0x20, 0x40).unwrap();
        wr_u64(rec, at + 0x30, real_size).unwrap();
        at + 0x48
    }

    /// Minimal little-endian encoding of `v` preserving its signedness.
    fn minimal_le(v: i64, signed: bool) -> Vec<u8> {
        let bytes = v.to_le_bytes();
        for n in 1..=8 {
            let cand = &bytes[..n];
            let mut wide = [0u8; 8];
            wide[..n].copy_from_slice(cand);
            if signed && cand[n - 1] & 0x80 != 0 {
                for b in wide.iter_mut().skip(n) {
                    *b = 0xFF;
                }
            }
            if i64::from_le_bytes(wide) == v {
                return cand.to_vec();
            }
        }
        bytes.to_vec()
    }

    /// Encodes `(lcn_delta, length_clusters)` pairs as an NTFS runlist with a
    /// terminating zero header.
    fn encode_runlist(runs: &[(i64, u64)]) -> Vec<u8> {
        let mut out = Vec::new();
        for &(delta, len) in runs {
            let lb = minimal_le(len as i64, false);
            // A zero offset field means SPARSE — a zero *delta* must keep its
            // own 1-byte 0x00 encoding.
            let db = minimal_le(delta, true);
            out.push((lb.len() as u8) | ((db.len() as u8) << 4));
            out.extend_from_slice(&lb);
            out.extend_from_slice(&db);
        }
        out.push(0);
        out
    }

    /// Non-resident `$DATA` attribute carrying a real runlist.
    fn put_runlist_data_attr(rec: &mut [u8], at: usize, real_size: u64, runs: &[(i64, u64)]) -> usize {
        let rl = encode_runlist(runs);
        let runlist_off = NONRESIDENT_HDR_SZ;
        let total = runlist_off + rl.len();
        wr_u32(rec, at, ATTR_DATA).unwrap();
        wr_u32(rec, at + 4, total as u32).unwrap();
        rec[at + 8] = 1;
        wr_u16(rec, at + 0x20, runlist_off as u16).unwrap();
        wr_u64(rec, at + 0x30, real_size).unwrap();
        rec[at + runlist_off..at + total].copy_from_slice(&rl);
        at + total
    }

    /// Live record with a multi-extent runlist (production shape).
    fn synth_live_record_with_runs(runs: &[(i64, u64)], real_size: u64) -> Vec<u8> {
        let mut rec = synth_record();
        let next = put_resident_file_name_attr(&mut rec, ATTRS_OFF, NTFS_ROOT_INODE);
        let next = put_runlist_data_attr(&mut rec, next, real_size, runs);
        end_attrs(&mut rec, next);
        rec
    }

    fn end_attrs(rec: &mut [u8], at: usize) {
        wr_u32(rec, at, 0xFFFF_FFFF).unwrap();
    }

    /// Valid-on-disk record carrying the live-file name plus a `$DATA` run.
    fn synth_live_record() -> Vec<u8> {
        let mut rec = synth_record();
        let next = put_resident_file_name_attr(&mut rec, ATTRS_OFF, NTFS_ROOT_INODE);
        let next = put_nonresident_data_attr(&mut rec, next, DATA_REAL_SIZE);
        end_attrs(&mut rec, next);
        rec
    }

    // ------------------------------------------------------------ MFT records

    #[test]
    fn mft_record_meta_parses_header_fields() {
        let rec = synth_record();
        let meta = parse_record_meta(&rec).expect("meta");
        assert_eq!(meta.sequence, 7);
        assert_eq!(meta.link_count, 1);
        assert_eq!(meta.flags, MFT_REC_IN_USE | MFT_REC_IS_DIRECTORY);
        assert_eq!(meta.bytes_in_use, MFT_RECORD_SIZE as u32);
        assert_eq!(meta.attrs_offset, ATTRS_OFF);
    }

    #[test]
    fn mft_record_meta_rejects_short_record() {
        let short = vec![0u8; 0x20];
        assert!(matches!(parse_record_meta(&short), Err(MftError::CorruptRecord("short"))));
    }

    #[test]
    fn fixup_check_accepts_consistent_record() {
        let rec = synth_record();
        check_fixup(&rec, ATTRS_OFF).expect("valid fixup array");
    }

    #[test]
    fn fixup_check_detects_torn_record() {
        let mut rec = synth_record();
        wr_u16(&mut rec, 510, USN + 1).unwrap();
        match check_fixup(&rec, ATTRS_OFF) {
            Err(MftError::TornRecord { sector, expected, found }) => {
                assert_eq!(sector, 1);
                assert_eq!(expected, USN);
                assert_eq!(found, USN + 1);
            }
            other => panic!("expected torn record, got {other:?}"),
        }
        let mut rec = synth_record();
        wr_u16(&mut rec, 1022, 0xBEEF).unwrap();
        assert!(matches!(
            check_fixup(&rec, ATTRS_OFF),
            Err(MftError::TornRecord { sector: 2, .. })
        ));
    }

    #[test]
    fn fixup_check_rejects_broken_usa_geometry() {
        // Not a whole number of sectors.
        let rec = synth_record();
        assert!(matches!(check_fixup(&rec[..1023], ATTRS_OFF), Err(MftError::CorruptRecord("size"))));
        // Wrong array length.
        let mut rec = synth_record();
        wr_u16(&mut rec, 6, 2).unwrap();
        assert!(matches!(check_fixup(&rec, ATTRS_OFF), Err(MftError::CorruptRecord("bounds"))));
        // Array parked inside the fixed header.
        let mut rec = synth_record();
        wr_u16(&mut rec, 4, 0x10).unwrap();
        assert!(matches!(check_fixup(&rec, ATTRS_OFF), Err(MftError::CorruptRecord("bounds"))));
        // Array straddling the payload start.
        let rec = synth_record();
        assert!(matches!(check_fixup(&rec, 0x30), Err(MftError::CorruptRecord("bounds"))));
        // Reserved USN values.
        for reserved in [0u16, 0xFFFF] {
            let mut rec = synth_record();
            wr_u16(&mut rec, 0x30, reserved).unwrap();
            assert!(matches!(check_fixup(&rec, ATTRS_OFF), Err(MftError::CorruptRecord("usn"))));
        }
    }

    #[test]
    fn apply_fixup_restores_sector_tails() {
        let mut rec = synth_record();
        apply_fixup(&mut rec, ATTRS_OFF).expect("apply");
        assert_eq!(rd_u16(&rec, 510).unwrap(), 0x1234);
        assert_eq!(rd_u16(&rec, 1022).unwrap(), 0x5678);
        // The record is now expanded: its tails no longer carry the USN, so a
        // re-check must fail until it is re-stamped (write-back contract).
        assert!(check_fixup(&rec, ATTRS_OFF).is_err());
        apply_fixup(&mut synth_record(), ATTRS_OFF).expect("apply on fresh record");
    }

    #[test]
    fn stamp_fixup_rearms_on_disk_form_without_losing_originals() {
        let mut rec = synth_record();
        let usn0 = rd_u16(&rec, 0x30).unwrap();
        stamp_fixup(&mut rec, ATTRS_OFF).expect("stamp");
        let usn1 = rd_u16(&rec, 0x30).unwrap();
        assert_eq!(usn1, usn0.wrapping_add(1));
        // Saved originals survived the re-stamp.
        assert_eq!(rd_u16(&rec, 0x32).unwrap(), 0x1234);
        assert_eq!(rd_u16(&rec, 0x34).unwrap(), 0x5678);
        // Both tails re-armed so the record validates on disk.
        assert_eq!(rd_u16(&rec, 510).unwrap(), usn1);
        assert_eq!(rd_u16(&rec, 1022).unwrap(), usn1);
        check_fixup(&rec, ATTRS_OFF).expect("stamped record validates");
        // Applying after a stamp restores the same originals.
        apply_fixup(&mut rec, ATTRS_OFF).expect("apply after stamp");
        assert_eq!(rd_u16(&rec, 510).unwrap(), 0x1234);
        assert_eq!(rd_u16(&rec, 1022).unwrap(), 0x5678);
    }

    #[test]
    fn stamp_fixup_rearms_expanded_form_by_saving_current_tails() {
        let mut rec = synth_record();
        apply_fixup(&mut rec, ATTRS_OFF).expect("apply");
        // Real payload bytes now live in the tails.
        wr_u16(&mut rec, 510, 0x0BAD).unwrap();
        wr_u16(&mut rec, 1022, 0xF00D).unwrap();
        let usn0 = rd_u16(&rec, 0x30).unwrap();
        stamp_fixup(&mut rec, ATTRS_OFF).expect("stamp expanded");
        let usn1 = rd_u16(&rec, 0x30).unwrap();
        assert_eq!(usn1, usn0.wrapping_add(1));
        // The payload bytes were saved back into the array.
        assert_eq!(rd_u16(&rec, 0x32).unwrap(), 0x0BAD);
        assert_eq!(rd_u16(&rec, 0x34).unwrap(), 0xF00D);
        assert_eq!(rd_u16(&rec, 510).unwrap(), usn1);
        check_fixup(&rec, ATTRS_OFF).expect("stamped record validates");
        apply_fixup(&mut rec, ATTRS_OFF).expect("apply");
        assert_eq!(rd_u16(&rec, 510).unwrap(), 0x0BAD);
        assert_eq!(rd_u16(&rec, 1022).unwrap(), 0xF00D);
    }

    #[test]
    fn stamp_fixup_skips_reserved_usn_values() {
        let mut rec = synth_record();
        wr_u16(&mut rec, 0x30, 0xFFFE).unwrap();
        wr_u16(&mut rec, 510, 0xFFFE).unwrap();
        wr_u16(&mut rec, 1022, 0xFFFE).unwrap();
        stamp_fixup(&mut rec, ATTRS_OFF).expect("stamp");
        let usn = rd_u16(&rec, 0x30).unwrap();
        assert_eq!(usn, 1);
        assert_eq!(rd_u16(&rec, 510).unwrap(), usn);
    }

    // ------------------------------------------------------- attribute walk

    #[test]
    fn find_attr_resolves_resident_and_nonresident_attributes() {
        let rec = synth_live_record();
        let name_attr = find_attr(&rec, ATTRS_OFF, ATTR_FILE_NAME)
            .unwrap()
            .expect("file name attribute");
        assert!(!name_attr.non_resident);
        assert_eq!(name_attr.offset, ATTRS_OFF);
        assert_eq!(name_attr.attr_type, ATTR_FILE_NAME);
        assert_eq!(name_attr.runlist_offset, RES_VALUE_OFF);
        assert_eq!(name_attr.real_size as usize, 0x42 + LIVE_UTF16_LEN * 2);

        let data_attr = find_attr(&rec, ATTRS_OFF, ATTR_DATA)
            .unwrap()
            .expect("data attribute");
        assert!(data_attr.non_resident);
        assert_eq!(data_attr.real_size, DATA_REAL_SIZE);
        assert_eq!(data_attr.runlist_offset, 0x40);

        assert!(find_attr(&rec, ATTRS_OFF, ATTR_INDEX_ROOT).unwrap().is_none());
        assert!(find_attr(&rec, ATTRS_OFF, 0xFFFF_FFFF).unwrap().is_none());
    }

    #[test]
    fn parse_file_name_decodes_utf16_name() {
        let mut value = vec![0u8; 0x42 + LIVE_UTF16_LEN * 2];
        // MFT reference carries the sequence in the high 16 bits.
        wr_u64(&mut value, 0, (NTFS_ROOT_INODE << 48) | 5).unwrap();
        value[0x40] = LIVE_UTF16_LEN as u8;
        value[0x41] = 0x03;
        for (i, u) in LIVE_FILE_NAME.encode_utf16().enumerate() {
            wr_u16(&mut value, 0x42 + i * 2, u).unwrap();
        }
        let parsed = parse_file_name(&value).expect("name");
        assert_eq!(parsed.parent_inode, NTFS_ROOT_INODE);
        assert_eq!(parsed.name, LIVE_FILE_NAME);
        assert!(parse_file_name(&value[..0x41]).is_none());
    }

    #[test]
    fn probe_layout_locates_the_live_file_record() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_probe_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .expect("scratch mft image");
        let rec = synth_live_record();
        let rec_off = MFT_BYTE_OFFSET + 2 * MFT_RECORD_SIZE as u64;
        file.write_all_at(&rec, rec_off).expect("write mft record");

        let layout = probe_layout(&file, &NtfsTemplate::default_vbr());
        assert_eq!(layout.live_file_inode, 2);
        assert_eq!(layout.data_size, DATA_REAL_SIZE);
        assert_eq!(layout.data_start_byte, DATA_START_BYTE);

        drop(file);
        let _ = std::fs::remove_file(&path);
    }

    // ------------------------------------------------------- runlist extents

    /// Production runlist deltas (LCNs 23 -> 4195433 -> 16793601 over the
    /// deployed 128 GiB template, where the live record sits at index 64).
    /// Deltas are relative to the previous run's START LCN, as NTFS encodes
    /// them — verified byte-for-byte against the deployed template.
    const PROD_RUNS: [(i64, u64); 3] =
        [(23, 1_907_388), (4_195_410, 12_581_782), (12_598_168, 16_760_830)];

    #[test]
    fn parse_runlist_decodes_lengths_deltas_and_sparse_holes() {
        let mut buf = Vec::new();
        // Run 1: 1 cluster at LCN 23. Run 2: 2 clusters at LCN 24. Sparse: 3.
        buf.extend_from_slice(&[0x11, 0x01, 0x17]);
        buf.extend_from_slice(&[0x11, 0x02, 0x01]);
        buf.extend_from_slice(&[0x01, 0x03]);
        buf.push(0x00);
        let runs = parse_runlist(&buf);
        assert_eq!(runs.len(), 3);
        assert_eq!(runs[0], RunPiece { vcn: 0, length_clusters: 1, lcn: Some(23) });
        assert_eq!(runs[1], RunPiece { vcn: 1, length_clusters: 2, lcn: Some(24) });
        assert_eq!(runs[2], RunPiece { vcn: 3, length_clusters: 3, lcn: None });
        // A trailing terminator alone is an empty runlist.
        assert!(parse_runlist(&[0x00]).is_empty());
        // Truncated header stops the decode without panicking.
        assert!(parse_runlist(&[0x44, 0x01]).is_empty());
        // The encoder helper round-trips through the decoder.
        let encoded = encode_runlist(&[(23, 1), (2_288_022, 100)]);
        let runs = parse_runlist(&encoded);
        assert_eq!(runs[0].lcn, Some(23));
        assert_eq!(runs[1].lcn, Some(2_288_045));
        assert_eq!(runs[1].length_clusters, 100);
    }

    #[test]
    fn extents_from_runs_coalesces_and_accumulates_file_offsets() {
        let runs = parse_runlist(&{
            let mut b = encode_runlist(&[(23, 1), (1, 2)]);
            b.extend_from_slice(&[0x01, 0x03]); // sparse hole: 3 clusters
            b.push(0x00);
            b
        });
        let extents = extents_from_runs(&runs);
        // The two physical runs are adjacent, so they coalesce; the sparse
        // hole contributes file bytes but no disk extent.
        assert_eq!(extents.len(), 1);
        assert_eq!(extents[0].disk_start_byte, 23 * CLUSTER_SIZE as u64);
        assert_eq!(extents[0].disk_end_byte, 26 * CLUSTER_SIZE as u64);
        assert_eq!(extents[0].file_start, 0);
        assert_eq!(extents[0].len(), 3 * CLUSTER_SIZE as u64);
        assert_eq!(extents[0].file_end(), 3 * CLUSTER_SIZE as u64);

        // A physically reversed second run still sorts by disk offset.
        let runs = parse_runlist(&encode_runlist(&[(500, 2), (-400, 2)]));
        let extents = extents_from_runs(&runs);
        assert_eq!(extents.len(), 2);
        // Sorted by disk offset: LCN 100 (file offset 2 clusters) first.
        assert_eq!(extents[0].disk_start_byte, 100 * CLUSTER_SIZE as u64);
        assert_eq!(extents[0].file_start, 2 * CLUSTER_SIZE as u64);
        assert_eq!(extents[1].disk_start_byte, 500 * CLUSTER_SIZE as u64);
        assert_eq!(extents[1].disk_end_byte, 502 * CLUSTER_SIZE as u64);
        assert_eq!(extents[1].file_start, 0);
    }

    #[test]
    fn probe_layout_parses_production_three_extent_runlist() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_prod_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .expect("scratch mft image");
        // Live record at the production index (64), past the ~27 system
        // records — this is what makes the MFT scan window matter.
        let rec = synth_live_record_with_runs(&PROD_RUNS, 128_000_000_000);
        let rec_off = MFT_BYTE_OFFSET + 64 * MFT_RECORD_SIZE as u64;
        file.write_all_at(&rec, rec_off).expect("write mft record");

        let layout = probe_layout(&file, &NtfsTemplate::default_vbr());
        assert_eq!(layout.live_file_inode, 64);
        assert_eq!(layout.data_size, 128_000_000_000);
        // First stream cluster sits right behind $MFT (LCN 23).
        assert_eq!(layout.data_start_byte, 23 * CLUSTER_SIZE as u64);
        assert_eq!(layout.extents.len(), 3);
        assert_eq!(layout.extents[0], DiskExtent { disk_start_byte: 94_208, disk_end_byte: 7_812_755_456, file_start: 0 });
        assert_eq!(
            layout.extents[1],
            DiskExtent { disk_start_byte: 17_184_493_568, disk_end_byte: 68_719_472_640, file_start: 7_812_661_248 }
        );
        assert_eq!(
            layout.extents[2],
            DiskExtent { disk_start_byte: 68_786_589_696, disk_end_byte: 137_438_949_376, file_start: 59_347_640_320 }
        );

        drop(file);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn probe_layout_matches_alternate_live_name() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_altname_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .expect("scratch mft image");
        // patch_trp renames the record to the alternate spelling after an
        // anti-cache cycle; the probe must still find the live file.
        let mut rec = synth_record();
        let next = put_named_file_name_attr(&mut rec, ATTRS_OFF, NTFS_ROOT_INODE, LIVE_FILE_NAME_ALT);
        let next = put_runlist_data_attr(&mut rec, next, 1 << 30, &[(23, 1_907_388)]);
        end_attrs(&mut rec, next);
        file.write_all_at(&rec, MFT_BYTE_OFFSET).expect("write mft record");

        let layout = probe_layout(&file, &NtfsTemplate::default_vbr());
        assert_eq!(layout.live_file_inode, 0);
        assert_eq!(layout.data_start_byte, 23 * CLUSTER_SIZE as u64);
        assert_eq!(layout.data_size, 1 << 30);

        drop(file);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn probe_layout_matches_long_name_when_dos_name_precedes() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_dosname_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .expect("scratch mft image");
        let mut rec = synth_record();
        // First put a DOS 8.3 name attribute (e.g. "TVAOVI~1.TRP")
        let next = put_named_file_name_attr(&mut rec, ATTRS_OFF, NTFS_ROOT_INODE, "TVAOVI~1.TRP");
        // Then put the long Win32 name attribute ("TV AO VIVO.trp")
        let next = put_named_file_name_attr(&mut rec, next, NTFS_ROOT_INODE, LIVE_FILE_NAME);
        let next = put_runlist_data_attr(&mut rec, next, 1 << 30, &[(23, 1_907_388)]);
        end_attrs(&mut rec, next);
        file.write_all_at(&rec, MFT_BYTE_OFFSET).expect("write mft record");

        let layout = probe_layout(&file, &NtfsTemplate::default_vbr());
        assert_eq!(layout.live_file_inode, 0);
        assert_eq!(layout.data_start_byte, 23 * CLUSTER_SIZE as u64);
        assert_eq!(layout.data_size, 1 << 30);

        drop(file);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn route_maps_data_holes_metadata_and_backup_vbr() {
        let runs = parse_runlist(&encode_runlist(&PROD_RUNS));
        let layout = VolumeLayout {
            data_start_byte: 23 * CLUSTER_SIZE as u64,
            data_size: 128_000_000_000,
            live_file_inode: 64,
            extents: extents_from_runs(&runs),
        };
        let tpl = NtfsTemplate { file: None, vbr: NtfsTemplate::default_vbr(), layout: layout.clone(), serial: 42 };

        // Everything below the first stream cluster is metadata.
        assert!(tpl.is_metadata_offset(0));
        assert!(tpl.is_metadata_offset(layout.data_start_byte - 1));

        // Extent 0: file offset == disk offset - 94208.
        assert_eq!(tpl.route(layout.data_start_byte), DiskRegion::Data(0));
        assert_eq!(tpl.route(layout.data_start_byte + 4096), DiskRegion::Data(4096));
        assert_eq!(tpl.data_run_len(layout.data_start_byte), 7_812_661_248);
        // Extent 1 starts mid-volume: the inverted offset jumps by the gap.
        let e1 = layout.extents[1];
        assert_eq!(tpl.route(e1.disk_start_byte), DiskRegion::Data(e1.file_start));
        assert_eq!(tpl.route(e1.disk_start_byte + CLUSTER_SIZE as u64), DiskRegion::Data(e1.file_start + CLUSTER_SIZE as u64));
        // Extent 2 (last): file offset maps near the 128 GB file end.
        let e2 = layout.extents[2];
        assert_eq!(tpl.route(e2.disk_start_byte), DiskRegion::Data(e2.file_start));

        // Holes between extents and the region before the first extent are
        // metadata (served from the sparse template).
        assert!(tpl.is_metadata_offset(7_812_755_456));
        assert!(tpl.is_metadata_offset(7_900_000_000));
        // The backup boot sector is metadata too, and the sector after the
        // last stream cluster still maps through the final extent.
        let backup = tpl.backup_vbr_byte();
        assert_eq!(backup, (TOTAL_SECTORS - 1) * BPS as u64);
        assert!(tpl.is_metadata_offset(backup));
        assert!(matches!(tpl.route(backup), DiskRegion::Metadata(_)));
        let last_stream = layout.extents[2].disk_end_byte - 1;
        assert!(matches!(tpl.route(last_stream), DiskRegion::Data(_)));
        assert!(tpl.is_metadata_offset(layout.extents[2].disk_end_byte));
    }

    #[test]
    fn backup_vbr_serial_is_stamped_at_bpb_total_sectors() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_backup_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let mut boot = [0u8; BPS];
        synthesize_vbr(&mut boot, &NtfsTemplate::default_vbr(), 0x1111_2222_3333_4444);
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .expect("scratch template");
        file.set_len(TOTAL_BYTES).expect("sparse 128 GiB image");
        // Primary VBR at sector 0, backup VBR at LBA total_sectors (the BPB
        // count excludes the backup boot sector).
        file.write_all_at(&boot, 0).expect("primary vbr");
        file.write_all_at(&boot, (TOTAL_SECTORS - 1) * BPS as u64).expect("backup vbr");

        let tpl = NtfsTemplate::open(&path).expect("open template");
        assert_eq!(tpl.vbr.total_sectors, TOTAL_SECTORS - 1);

        let mut sector = [0u8; BPS];
        tpl.read_sector(0, &mut sector);
        assert_eq!(rd_u64(&sector, VSN_OFFSET).unwrap(), tpl.serial);
        assert_eq!(parse_vbr(&sector).expect("primary").serial, tpl.serial);

        // The backup boot sector carries the freshly randomized VSN.
        tpl.read_sector(tpl.vbr.total_sectors, &mut sector);
        assert_eq!(&sector[3..11], b"NTFS    ");
        assert_eq!(rd_u64(&sector, VSN_OFFSET).unwrap(), tpl.serial);

        // The sector before the backup is NOT a boot sector (off-by-one guard).
        tpl.read_sector(tpl.vbr.total_sectors - 1, &mut sector);
        assert!(sector.iter().all(|&b| b == 0));

        drop(file);
        let _ = std::fs::remove_file(&path);
    }

    // ------------------------------------------------------------ boot sector

    #[test]
    fn parse_vbr_round_trips_synthesized_boot_sector() {
        let geometry = NtfsTemplate::default_vbr();
        let serial = 0x0102_0304_0506_0708;
        let mut boot = [0u8; BPS];
        synthesize_vbr(&mut boot, &geometry, serial);

        let parsed = parse_vbr(&boot).expect("vbr");
        assert_eq!(parsed.bytes_per_sector, BPS as u32);
        assert_eq!(parsed.sectors_per_cluster, SPC as u8);
        assert_eq!(parsed.cluster_size, CLUSTER_SIZE as u32);
        assert_eq!(parsed.total_sectors, TOTAL_SECTORS - 1);
        assert_eq!(parsed.mft_lcn, MFT_LCN);
        assert_eq!(parsed.mft_mirror_lcn, MFT_MIRROR_LCN);
        assert_eq!(parsed.mft_record_size, MFT_RECORD_SIZE as u32);
        assert_eq!(parsed.index_record_size, CLUSTER_SIZE as u32);
        assert_eq!(parsed.serial, serial);

        assert_eq!(&boot[0..3], &[0xEB, 0x52, 0x90]);
        assert_eq!(&boot[3..11], b"NTFS    ");
        assert_eq!(boot[510], 0x55);
        assert_eq!(boot[511], 0xAA);
        // The dynamic VSN is re-stamped on every serve at 0x48.
        assert_eq!(rd_u64(&boot, VSN_OFFSET).unwrap(), serial);
    }

    #[test]
    fn parse_vbr_rejects_invalid_boot_sectors() {
        let mut good = [0u8; BPS];
        synthesize_vbr(&mut good, &NtfsTemplate::default_vbr(), 7);

        assert!(matches!(parse_vbr(&good[..400]), Err(MftError::InvalidVbr("short"))));
        let mut bad = good;
        bad[510] = 0x00;
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("sig"))));
        let mut bad = good;
        bad[9] = b'X';
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("oem"))));
        let mut bad = good;
        wr_u16(&mut bad, 11, 1024).unwrap();
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("bps"))));
        let mut bad = good;
        bad[13] = 3;
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("spc"))));
        let mut bad = good;
        bad[13] = 16;
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("cls"))));
        let mut bad = good;
        wr_u64(&mut bad, 0x28, 0).unwrap();
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("tot"))));
        let mut bad = good;
        bad[64] = 0;
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("mft"))));
        let mut bad = good;
        bad[68] = 0;
        assert!(matches!(parse_vbr(&bad), Err(MftError::InvalidVbr("idx"))));
    }

    #[test]
    fn random_serial_is_never_zero() {
        for _ in 0..16 {
            assert_ne!(random_serial(), 0);
        }
    }

    #[test]
    fn synthesized_template_serves_live_vsn_boot_sectors() {
        let tpl = NtfsTemplate::synthesized();
        assert_ne!(tpl.serial, 0);
        assert!(tpl.file.is_none());

        // Primary boot sector.
        let mut sector = [0u8; BPS];
        tpl.read_sector(0, &mut sector);
        let parsed = parse_vbr(&sector).expect("boot sector");
        assert_eq!(parsed.serial, tpl.serial);
        assert_eq!(rd_u64(&sector, VSN_OFFSET).unwrap(), tpl.serial);

        // Backup boot sector at the very last sector of the volume.
        let mut backup = [0u8; BPS];
        tpl.read_sector(TOTAL_SECTORS - 1, &mut backup);
        assert_eq!(&backup[3..11], b"NTFS    ");
        assert_eq!(rd_u64(&backup, VSN_OFFSET).unwrap(), tpl.serial);

        // Non-boot metadata sectors are zero-filled holes.
        let mut hole = [0xFF; BPS];
        tpl.read_sector(1, &mut hole);
        assert!(hole.iter().all(|&b| b == 0));
        let mut mid = [0xFF; BPS];
        tpl.read_sector(MFT_BYTE_OFFSET / BPS as u64, &mut mid);
        assert!(mid.iter().all(|&b| b == 0));

        // Geometry helpers classify offsets against the data start.
        assert!(tpl.is_metadata_offset(DATA_START_BYTE - 1));
        assert!(!tpl.is_metadata_offset(DATA_START_BYTE));
        assert_eq!(tpl.layout.data_size, (TOTAL_CLUSTERS - DATA_START_CLUSTER) * CLUSTER_SIZE as u64);
        assert_eq!(tpl.layout.live_file_inode, LIVE_FILE_INODE);
    }

    #[test]
    fn template_randomize_serial_bumps_served_vsn() {
        let mut tpl = NtfsTemplate::synthesized();
        let before = tpl.serial;
        for _ in 0..4 {
            tpl.randomize_serial();
            assert_ne!(tpl.serial, 0);
        }
        let mut sector = [0u8; BPS];
        tpl.read_sector(0, &mut sector);
        assert_eq!(rd_u64(&sector, VSN_OFFSET).unwrap(), tpl.serial);
        assert_ne!(tpl.serial, before);
    }

    #[test]
    fn synthesized_template_reads_existing_boot_sector_from_file() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!(
            "fuse_ntfs_tpl_{}_{}.bin",
            std::process::id(),
            nanos
        ));
        let serial = 0xdead_beef_cafe_1234;
        let mut boot = [0u8; BPS];
        synthesize_vbr(&mut boot, &NtfsTemplate::default_vbr(), serial);
        std::fs::write(&path, boot).expect("write template");
        let tpl = NtfsTemplate::open(&path).expect("open template");
        assert_eq!(tpl.serial != 0, true);
        let mut sector = [0u8; BPS];
        tpl.read_sector(0, &mut sector);
        // The template VBR geometry is honoured and the VSN re-stamped.
        assert_eq!(parse_vbr(&sector).expect("vbr").total_sectors, TOTAL_SECTORS - 1);
        assert_eq!(rd_u64(&sector, VSN_OFFSET).unwrap(), tpl.serial);
        let _ = std::fs::remove_file(&path);
    }
}

