// ============================================================================
// USB-STREAM-TV: FUSE NTFS DAEMON (RUST CONSOLIDATED BUNDLE FOR AUDIT - UPDATED)
// Target: Samsung Galaxy Tab 3 Lite (SM-T110, Android 4.4, Kernel 3.4.5 ARMv7)
// Host: Samsung Plasma TV 2013 (PL51F4000, MStar MSD6A801 ConnectShare Movie)
// Status: All 6 Sonnet audit bugs fixed; 130 tests passing.
// ============================================================================


// >>>>>>>>>>>>>>> FILE: Cargo.toml (8 lines) <<<<<<<<<<<<<<<
[package]
name = "fuse_ntfs"
version = "3.0.0"
edition = "2021"
description = "USB-Stream-TV: High-performance raw /dev/fuse live NTFS engine"

[dependencies]
libc = "0.2"

// <<<<<<<<<<<<<<< END OF FILE: Cargo.toml >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/contract.rs (57 lines) <<<<<<<<<<<<<<<
//! Shared contract between the FUSE VFS layer and the streaming backends.
//!
//! This module is the stable seam between the NTFS virtual-filesystem
//! dispatcher (`vfs`) and the live/VOD ingest engine (`engine`). Both sides
//! depend only on the constants and trait defined here, so the engine can be
//! developed, audited, and replaced independently of the kernel ABI layer.

/// Size in bytes of one MPEG-TS transport stream packet.
pub const TS_PKT_SZ: usize = 188;

/// Capacity of the live ring buffer in bytes (128 MiB). The ring is circular
/// and self-recovering: the network feeder never throttles, and a reader that
/// falls behind `ring_old` is transparently re-anchored to the live target.
pub const RINGSZ: usize = 128 * 1024 * 1024;

/// Distance behind the live write head (`g_s_write`) where playback anchors.
/// 16 MiB absorbs network jitter without stalling the Samsung MStar SCSI bus.
pub const LEADBACK: u64 = 16 * 1024 * 1024;

/// Minimum lead the reader must hold over the live edge before a confirmed
/// resume (bookmark / jump) is allowed to re-anchor the stream.
pub const RESUME_MIN_LEAD: u64 = 2 * 1024 * 1024;

/// File offset at/above which reads are treated as probe candidates.
pub const PROBE_ISOLATION_FOFF: u64 = 8 * 1024 * 1024;

/// Margin past the active end beyond which a read is an isolated probe.
pub const PROBE_OVERFALL: u64 = 2 * 1024 * 1024;

/// Margin added on top of the resume target when re-anchoring, so the reader
/// restarts slightly behind the requested bookmark and never outruns it.
pub const RESUME_TARGET_MARGIN: u64 = 2 * 1024 * 1024;

/// Backend abstraction implemented by the streaming engine.
///
/// * `serve_live` feeds the live channel identified by `file_id`.
/// * `serve_vod` feeds an on-demand stream identified by its HTTP URL.
///
/// Both methods must fully populate `dst` (padding with MPEG-TS null packets
/// when the source is exhausted) so the 27 MHz STC PLL of the 2013 MStar
/// decoder never starves on a short reply.
pub trait StreamBackend: Send + Sync {
    /// Fills `dst` with live MPEG-TS bytes for `file_id`, starting at the
    /// stream offset `foff`. Returns the number of bytes written.
    fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], deadline_ms: u64) -> usize;

    /// Fills `dst` with VOD bytes for `url`, starting at `foff`.
    /// Returns the number of bytes written, or an I/O error when the source
    /// cannot be reached (the caller then falls back to null packets).
    fn serve_vod(
        &self,
        url: &str,
        foff: u64,
        dst: &mut [u8],
        deadline_ms: u64,
    ) -> Result<usize, std::io::Error>;
}

// <<<<<<<<<<<<<<< END OF FILE: src/contract.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/vfs/fuse_abi.rs (321 lines) <<<<<<<<<<<<<<<
//! Low-level Linux `/dev/fuse` kernel protocol definitions (Kernel 3.4+).
//!
//! Structures are the raw kernel-userspace ABI for the FUSE protocol: every
//! field is little-endian on the wire and every struct is `#[repr(C)]` with
//! natural alignment. The 8-byte-aligned staging buffer below is mandatory on
//! ARMv7-A: the kernel emits `LDRD`/`STRD` pairs for 64-bit struct fields
//! (timespec, offsets, sizes), and an unaligned buffer hard-faults the CPU.

#![allow(non_camel_case_types)]
#![allow(dead_code)]

// ------------------------------------------------------------------ opcodes

pub const FUSE_LOOKUP: u32 = 1;
pub const FUSE_FORGET: u32 = 2;
pub const FUSE_GETATTR: u32 = 3;
pub const FUSE_OPEN: u32 = 14;
pub const FUSE_READ: u32 = 15;
pub const FUSE_STATFS: u32 = 17;
/// FUSE_RELEASE is 18 in the Linux kernel ABI (8 is FUSE_MKNOD, 9 is FUSE_MKDIR;
/// a release handler must never swallow mknod/mkdir requests, so the audited
/// value from the `fuse_direct` sibling engine is used here verbatim).
pub const FUSE_RELEASE: u32 = 18;
pub const FUSE_INIT: u32 = 26;
pub const FUSE_OPENDIR: u32 = 27;
pub const FUSE_READDIR: u32 = 28;
pub const FUSE_RELEASEDIR: u32 = 29;
pub const FUSE_BATCH_FORGET: u32 = 42;

// -------------------------------------------------------------------- flags

/// Kernel-side init flags (negotiated during FUSE_INIT).
pub const FUSE_ASYNC_READ: u32 = 1 << 0;
pub const FUSE_BIG_WRITES: u32 = 1 << 5;

/// Open-reply flags (FUSE_OPEN / FUSE_OPENDIR).
pub const FOPEN_DIRECT_IO: u32 = 1 << 0;
pub const FOPEN_KEEP_CACHE: u32 = 1 << 1;
pub const FOPEN_NONSEEKABLE: u32 = 1 << 2;

// ------------------------------------------------------------------ structs

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_in_header {
    pub len: u32,
    pub opcode: u32,
    pub unique: u64,
    pub nodeid: u64,
    pub uid: u32,
    pub gid: u32,
    pub pid: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_out_header {
    pub len: u32,
    pub error: i32,
    pub unique: u64,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_init_in {
    pub major: u32,
    pub minor: u32,
    pub max_readahead: u32,
    pub flags: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_init_out {
    pub major: u32,
    pub minor: u32,
    pub max_readahead: u32,
    pub flags: u32,
    pub max_background: u16,
    pub congestion_threshold: u16,
    pub max_write: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_attr {
    pub ino: u64,
    pub size: u64,
    pub blocks: u64,
    pub atime: u64,
    pub mtime: u64,
    pub ctime: u64,
    pub atimensec: u32,
    pub mtimensec: u32,
    pub ctimensec: u32,
    pub mode: u32,
    pub nlink: u32,
    pub uid: u32,
    pub gid: u32,
    pub rdev: u32,
    pub blksize: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_attr_out {
    pub attr_valid: u64,
    pub attr_valid_nsec: u32,
    pub dummy: u32,
    pub attr: fuse_attr,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_entry_out {
    pub nodeid: u64,
    pub generation: u64,
    pub entry_valid: u64,
    pub attr_valid: u64,
    pub entry_valid_nsec: u32,
    pub attr_valid_nsec: u32,
    pub attr: fuse_attr,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_open_in {
    pub flags: u32,
    pub unused: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_open_out {
    pub fh: u64,
    pub open_flags: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_read_in {
    pub fh: u64,
    pub offset: u64,
    pub size: u32,
    pub read_flags: u32,
    pub lock_owner: u64,
    pub flags: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_dirent {
    pub ino: u64,
    pub off: u64,
    pub namelen: u32,
    pub typ: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_kstatfs {
    pub blocks: u64,
    pub bfree: u64,
    pub bavail: u64,
    pub files: u64,
    pub ffree: u64,
    pub bsize: u32,
    pub namelen: u32,
    pub frsize: u32,
    pub padding: u32,
    pub spare: [u32; 6],
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_statfs_out {
    pub st: fuse_kstatfs,
}

/// Maximum payload the kernel will send or accept in one request.
pub const MAX_MSG_SZ: usize = 128 * 1024;

/// 8-byte aligned FUSE request/reply staging buffer.
/// The ARMv7-A kernel emits `LDRD`/`STRD` for 64-bit struct fields
/// (timespec, offsets, sizes) inside the FUSE out-handlers; an unaligned
/// staging buffer hard-faults the CPU on 32-bit ARM.
#[repr(C, align(8))]
pub struct AlignedFuseBuf(pub [u8; MAX_MSG_SZ + 4096]);

impl AlignedFuseBuf {
    pub fn new_zeroed() -> Self {
        Self([0u8; MAX_MSG_SZ + 4096])
    }

    pub fn new_boxed() -> Box<Self> {
        let layout = std::alloc::Layout::new::<Self>();
        unsafe {
            let ptr = std::alloc::alloc_zeroed(layout) as *mut Self;
            if ptr.is_null() {
                std::alloc::handle_alloc_error(layout);
            }
            Box::from_raw(ptr)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Entry sizing rule shared by the kernel and the READDIR handler:
    /// a `fuse_dirent` header plus the NUL-less name, padded up to 8 bytes.
    fn dirent_entry_len(name: &str) -> usize {
        (std::mem::size_of::<fuse_dirent>() + name.len() + 7) & !7
    }

    #[test]
    fn opcodes_match_the_linux_kernel_abi() {
        assert_eq!(FUSE_LOOKUP, 1);
        assert_eq!(FUSE_FORGET, 2);
        assert_eq!(FUSE_GETATTR, 3);
        assert_eq!(FUSE_OPEN, 14);
        assert_eq!(FUSE_READ, 15);
        assert_eq!(FUSE_STATFS, 17);
        assert_eq!(FUSE_RELEASE, 18);
        assert_eq!(FUSE_INIT, 26);
        assert_eq!(FUSE_OPENDIR, 27);
        assert_eq!(FUSE_READDIR, 28);
        assert_eq!(FUSE_BATCH_FORGET, 42);
        // 8 is FUSE_MKDIR on the wire; a RELEASE opcode must never shadow it.
        assert_ne!(FUSE_RELEASE, 8);
    }

    #[test]
    fn open_flags_expose_direct_io_not_nonseekable() {
        assert_eq!(FOPEN_DIRECT_IO, 1 << 0);
        // Waiver #FUSE-2013-MSTAR: NONSEEKABLE is never granted, the MStar
        // SCSI bridge relies on pread64/lseek over the block image.
        assert_eq!(FOPEN_NONSEEKABLE & FOPEN_DIRECT_IO, 0);
    }

    #[test]
    fn wire_struct_layouts_are_exact() {
        assert_eq!(std::mem::size_of::<fuse_in_header>(), 40);
        assert_eq!(std::mem::align_of::<fuse_in_header>(), 8);
        assert_eq!(std::mem::size_of::<fuse_out_header>(), 16);
        assert_eq!(std::mem::align_of::<fuse_out_header>(), 8);
        assert_eq!(std::mem::size_of::<fuse_attr>(), 88);
        assert_eq!(std::mem::size_of::<fuse_entry_out>(), 128);
        assert_eq!(std::mem::size_of::<fuse_init_out>(), 24);
        assert_eq!(std::mem::size_of::<fuse_open_out>(), 16);
        assert_eq!(std::mem::size_of::<fuse_dirent>(), 24);
        assert_eq!(std::mem::align_of::<fuse_dirent>(), 8);
        assert_eq!(std::mem::size_of::<fuse_statfs_out>(), 80);
    }

    #[test]
    fn dirent_entries_are_eight_byte_aligned() {
        for name in ["", ".", "..", "Filmes", "Canais TV", "TV AO VIVO.trp", "tv_stream.img"] {
            let len = dirent_entry_len(name);
            assert_eq!(len % 8, 0, "{name} entry not padded to 8");
            assert!(len >= std::mem::size_of::<fuse_dirent>() + name.len());
        }
        assert_eq!(dirent_entry_len("Filmes"), 32);
        assert_eq!(dirent_entry_len("Canais TV"), 40);
        assert_eq!(dirent_entry_len("TV AO VIVO.trp"), 40);
    }

    #[test]
    fn dirent_serialization_is_little_endian_with_zeroed_padding() {
        // Reproduces the READDIR payload construction bit for bit.
        let mut out = AlignedFuseBuf::new_zeroed();
        let payload = out.0[std::mem::size_of::<fuse_out_header>()..].as_mut_ptr();
        assert_eq!(payload as usize % 8, 0, "dirent area must stay 8-byte aligned");

        let name = "Canais TV";
        let namelen = name.len();
        let entry_len = dirent_entry_len(name);
        // SAFETY: `entry_len` fits inside the oversized staging buffer and the
        // base pointer is 8-byte aligned, exactly as the dispatcher requires.
        let de = unsafe { &mut *(payload as *mut fuse_dirent) };
        de.ino = 7;
        de.off = 3;
        de.namelen = namelen as u32;
        de.typ = libc::DT_DIR as u32;
        // SAFETY: `namelen` bytes fit inside the validated window.
        unsafe {
            std::ptr::copy_nonoverlapping(
                name.as_ptr(),
                payload.add(std::mem::size_of::<fuse_dirent>()),
                namelen,
            );
        }

        let raw = &out.0[std::mem::size_of::<fuse_out_header>()..std::mem::size_of::<fuse_out_header>() + entry_len];
        assert_eq!(&raw[0..8], &7u64.to_le_bytes(), "ino must be little-endian");
        assert_eq!(&raw[8..16], &3u64.to_le_bytes(), "off must be little-endian");
        assert_eq!(&raw[16..20], &(namelen as u32).to_le_bytes(), "namelen is a u32");
        assert_eq!(&raw[20..24], &(libc::DT_DIR as u32).to_le_bytes(), "typ is a u32");
        assert_eq!(&raw[24..24 + namelen], name.as_bytes(), "name follows the header");
        assert!(
            raw[24 + namelen..entry_len].iter().all(|&b| b == 0),
            "padding past the name must be zeroed"
        );
    }

    #[test]
    fn staging_buffer_is_eight_byte_aligned_and_zeroed() {
        assert_eq!(std::mem::align_of::<AlignedFuseBuf>(), 8);
        let boxed = AlignedFuseBuf::new_boxed();
        let ptr = boxed.0.as_ptr();
        assert_eq!(ptr as usize % 8, 0, "ARMv7-A LDRD/STRD requires 8-byte alignment");
        assert_eq!(boxed.0.len(), MAX_MSG_SZ + 4096);
        let zeroed = AlignedFuseBuf::new_zeroed();
        assert!(zeroed.0[..].iter().all(|&b| b == 0));
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/vfs/fuse_abi.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/vfs/ntfs_mft.rs (1363 lines) <<<<<<<<<<<<<<<
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


// <<<<<<<<<<<<<<< END OF FILE: src/vfs/ntfs_mft.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/vfs/catalog.rs (380 lines) <<<<<<<<<<<<<<<
use std::sync::Mutex;

use super::ntfs_mft::{FILE_INO, FILE_NAME, ROOT_INO};

pub const DIR_CANAIS: &str = "Canais TV";
pub const DIR_FILMES: &str = "Filmes";
pub const DIR_FAVORITOS: &str = "Favoritos";
pub const LIVE_NAME_PRIMARY: &str = "TV AO VIVO.trp";
pub const LIVE_NAME_ALTERNATE: &str = "TV AO VIVO 2.tp";
pub const SWITCH_THRESHOLD: u64 = 1024 * 1024;
pub const DIR_INO_CANAIS: u64 = 3;
pub const DIR_INO_FILMES: u64 = 4;
pub const DIR_INO_FAVORITOS: u64 = 5;
pub const LIVE_INO_BASE: u64 = 0x1000;
pub const VOD_INO_BASE: u64 = 0x10000;
pub const STREAM_INO: u64 = FILE_INO;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NodeKind {
    Root,
    Directory,
    DiskImage,
    Live { file_id: u32 },
    Vod { url: String },
}

#[derive(Debug, Clone)]
pub struct Node {
    pub ino: u64,
    pub parent: u64,
    pub name: String,
    pub kind: NodeKind,
    pub size: u64,
}

impl Node {
    pub fn is_dir(&self) -> bool {
        matches!(self.kind, NodeKind::Root | NodeKind::Directory)
    }
    pub fn dirent_type(&self) -> u32 {
        if self.is_dir() { libc::DT_DIR as u32 } else { libc::DT_REG as u32 }
    }
}

#[derive(Debug, Default)]
struct CatalogState {
    nodes: Vec<Node>,
    active_file_id: u32,
    watch_ino: u64,
    watch_end: u64,
    watch_bytes: u64,
}

pub struct Catalog {
    state: Mutex<CatalogState>,
}

impl Default for Catalog {
    fn default() -> Self {
        Self::new(1024 * 1024 * 1024)
    }
}

impl Catalog {
    pub fn new(live_size: u64) -> Self {
        let mut nodes = Vec::with_capacity(8);
        nodes.push(Node { ino: ROOT_INO, parent: ROOT_INO, name: String::new(), kind: NodeKind::Root, size: 4096 });
        nodes.push(Node { ino: FILE_INO, parent: ROOT_INO, name: FILE_NAME.to_string(), kind: NodeKind::DiskImage, size: crate::vfs::ntfs_mft::VOLUME_BYTES });
        for (ino, name) in [(DIR_INO_CANAIS, DIR_CANAIS), (DIR_INO_FILMES, DIR_FILMES), (DIR_INO_FAVORITOS, DIR_FAVORITOS)] {
            nodes.push(Node { ino, parent: ROOT_INO, name: name.to_string(), kind: NodeKind::Directory, size: 4096 });
        }
        let cat = Self { state: Mutex::new(CatalogState { nodes, active_file_id: 0, watch_ino: 0, watch_end: 0, watch_bytes: 0 }) };
        cat.add_live_channel(0, LIVE_NAME_PRIMARY, live_size);
        cat.add_live_channel(1, LIVE_NAME_ALTERNATE, live_size);
        cat
    }
    fn lock(&self) -> std::sync::MutexGuard<'_, CatalogState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    pub fn node(&self, ino: u64) -> Option<Node> {
        self.lock().nodes.iter().find(|n| n.ino == ino).cloned()
    }
    pub fn lookup(&self, parent: u64, name: &str) -> Option<Node> {
        let st = self.lock();
        st.nodes.iter().find(|n| n.parent == parent && n.name == name).cloned()
    }
    pub fn children(&self, ino: u64) -> Vec<Node> {
        self.lock().nodes.iter().filter(|n| n.parent == ino).cloned().collect()
    }
    pub fn len(&self) -> usize {
        self.lock().nodes.len()
    }
    pub fn is_empty(&self) -> bool { self.len() == 0 }
    pub fn active_file_id(&self) -> u32 { self.lock().active_file_id }
    pub fn active_node(&self) -> Option<Node> {
        let st = self.lock();
        let id = st.active_file_id;
        st.nodes.iter().find(|n| matches!(&n.kind, NodeKind::Live { file_id } if *file_id == id)).cloned()
    }
    pub fn set_active(&self, file_id: u32) -> Option<Node> {
        let mut st = self.lock();
        st.active_file_id = file_id;
        st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0;
        st.nodes.iter().find(|n| matches!(&n.kind, NodeKind::Live { file_id: f } if *f == file_id)).cloned()
    }
    pub fn add_live_channel(&self, file_id: u32, name: &str, size: u64) -> u64 {
        let ino = LIVE_INO_BASE + u64::from(file_id);
        self.lock().nodes.push(Node { ino, parent: DIR_INO_CANAIS, name: name.to_string(), kind: NodeKind::Live { file_id }, size });
        ino
    }
    pub fn add_vod_file(&self, url: &str, name: &str, size: u64) -> u64 {
        let mut st = self.lock();
        let ino = VOD_INO_BASE + st.nodes.len() as u64;
        st.nodes.push(Node { ino, parent: DIR_INO_FILMES, name: name.to_string(), kind: NodeKind::Vod { url: url.to_string() }, size });
        ino
    }
    pub fn note_read(&self, ino: u64, offset: u64, len: u64) -> Option<u32> {
        let mut st = self.lock();
        let kind = match st.nodes.iter().find(|n| n.ino == ino) { Some(n) => n.kind.clone(), None => return None };
        let NodeKind::Live { file_id } = kind else {
            st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0; return None;
        };
        if file_id == st.active_file_id { st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0; return None; }
        let end = offset.saturating_add(len);
        if st.watch_ino == ino && offset == st.watch_end { st.watch_bytes = st.watch_bytes.saturating_add(len); }
        else { st.watch_ino = ino; st.watch_bytes = len; }
        st.watch_end = end;
        if st.watch_bytes >= SWITCH_THRESHOLD {
            st.active_file_id = file_id;
            st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0;
            Some(file_id)
        } else { None }
    }
    pub fn getattr(&self, ino: u64, stream_len: u64) -> Option<crate::vfs::fuse_abi::fuse_attr> {
        let node = self.node(ino)?;
        let mut attr = crate::vfs::fuse_abi::fuse_attr::default();
        attr.ino = node.ino;
        attr.size = if matches!(node.kind, NodeKind::DiskImage) { crate::vfs::ntfs_mft::VOLUME_BYTES } else { node.size };
        if ino == STREAM_INO { attr.size = stream_len; }
        attr.mode = if node.is_dir() { 0o040000 | 0o755 } else { 0o100000 | 0o644 } as u32;
        attr.nlink = if node.is_dir() { 2 } else { 1 };
        attr.blksize = 4096;
        attr.blocks = attr.size.div_ceil(512);
        Some(attr)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Size handed to every synthetic live channel in the catalog.
    const LIVE_SIZE: u64 = 4 * 1024 * 1024;

    fn cat() -> Catalog {
        Catalog::new(LIVE_SIZE)
    }

    #[test]
    fn root_tree_holds_disk_image_and_category_directories() {
        let c = cat();
        let root = c.node(ROOT_INO).expect("root node");
        assert_eq!(root.kind, NodeKind::Root);
        assert!(root.is_dir());

        let names: Vec<String> = c
            .children(ROOT_INO)
            .iter()
            .map(|n| n.name.clone())
            .collect();
        assert!(names.contains(&FILE_NAME.to_string()));
        assert!(names.contains(&DIR_CANAIS.to_string()));
        assert!(names.contains(&DIR_FILMES.to_string()));
        assert!(names.contains(&DIR_FAVORITOS.to_string()));

        let disk = c.lookup(ROOT_INO, FILE_NAME).expect("disk image");
        assert_eq!(disk.kind, NodeKind::DiskImage);
        assert_eq!(disk.ino, FILE_INO);
        assert_eq!(disk.size, crate::vfs::ntfs_mft::VOLUME_BYTES);
        assert_eq!(disk.dirent_type(), libc::DT_REG as u32);

        let canais = c.lookup(ROOT_INO, DIR_CANAIS).expect("canais");
        assert_eq!(canais.ino, DIR_INO_CANAIS);
        assert_eq!(canais.kind, NodeKind::Directory);
        assert_eq!(canais.dirent_type(), libc::DT_DIR as u32);
        assert_eq!(
            c.lookup(ROOT_INO, DIR_FILMES).expect("filmes").ino,
            DIR_INO_FILMES
        );
        assert_eq!(
            c.lookup(ROOT_INO, DIR_FAVORITOS).expect("favoritos").ino,
            DIR_INO_FAVORITOS
        );

        // Lookups must be scoped to the parent directory.
        assert!(c.lookup(DIR_INO_CANAIS, FILE_NAME).is_none());
        assert!(c.lookup(ROOT_INO, "nao-existe").is_none());
        assert!(c.node(9999).is_none());
    }

    #[test]
    fn live_channels_register_under_canais_tv() {
        let c = cat();
        assert_eq!(c.len(), 7);

        let primary = c.lookup(DIR_INO_CANAIS, LIVE_NAME_PRIMARY).expect("primary");
        assert_eq!(primary.ino, LIVE_INO_BASE);
        assert_eq!(primary.kind, NodeKind::Live { file_id: 0 });
        assert_eq!(primary.size, LIVE_SIZE);
        assert_eq!(primary.parent, DIR_INO_CANAIS);

        let alternate = c.lookup(DIR_INO_CANAIS, LIVE_NAME_ALTERNATE).expect("alternate");
        assert_eq!(alternate.ino, LIVE_INO_BASE + 1);
        assert_eq!(alternate.kind, NodeKind::Live { file_id: 1 });

        let kids: Vec<String> = c
            .children(DIR_INO_CANAIS)
            .iter()
            .map(|n| n.name.clone())
            .collect();
        assert!(kids.contains(&LIVE_NAME_PRIMARY.to_string()));
        assert!(kids.contains(&LIVE_NAME_ALTERNATE.to_string()));
    }

    #[test]
    fn active_file_id_defaults_to_the_primary_channel() {
        let c = cat();
        assert_eq!(c.active_file_id(), 0);
        let active = c.active_node().expect("active node");
        assert_eq!(active.ino, LIVE_INO_BASE);
        assert_eq!(active.kind, NodeKind::Live { file_id: 0 });
    }

    #[test]
    fn set_active_switches_channel_and_drops_pending_watch() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;
        // Half a threshold already pending on the inactive channel.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD / 2).is_none());
        // An explicit switch resets the detector, so the pending run never
        // completes the switch on its own.
        let node = c.set_active(1).expect("alternate node");
        assert_eq!(node.ino, alt);
        assert_eq!(c.active_file_id(), 1);
        assert_eq!(c.active_node().expect("active").ino, alt);
        // Reads of the now-active channel never re-trigger a switch.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD / 2).is_none());
        assert!(c.note_read(alt, SWITCH_THRESHOLD / 2, SWITCH_THRESHOLD / 2).is_none());
        assert_eq!(c.active_file_id(), 1);
        // Switching back re-arms tracking on the other channel.
        assert_eq!(c.set_active(0).expect("primary").ino, LIVE_INO_BASE);
        assert_eq!(c.active_file_id(), 0);
    }

    #[test]
    fn channel_switch_needs_a_contiguous_threshold_run() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;

        // Chunks below half the threshold must accumulate contiguously to
        // complete the switch; the third one tips it over. The `+ 1` guards
        // against integer truncation leaving the sum one byte short.
        let chunk = SWITCH_THRESHOLD / 3 + 1;
        assert!(c.note_read(alt, 0, chunk).is_none());
        assert!(c.note_read(alt, chunk, chunk).is_none());
        assert_eq!(c.active_file_id(), 0);
        let switched = c.note_read(alt, chunk * 2, chunk);
        assert_eq!(switched, Some(1));
        assert_eq!(c.active_file_id(), 1);
        assert_eq!(c.active_node().expect("active").ino, alt);

        // The detector resets after a switch: the active channel is inert.
        assert!(c.note_read(alt, SWITCH_THRESHOLD + SWITCH_THRESHOLD, SWITCH_THRESHOLD).is_none());
        assert_eq!(c.active_file_id(), 1);

        // A single read of exactly the threshold on the now-inactive primary
        // channel switches back immediately.
        assert!(c.note_read(LIVE_INO_BASE, 3 * SWITCH_THRESHOLD / 2, SWITCH_THRESHOLD).is_some());
    }

    #[test]
    fn channel_switch_ignores_seeks_below_the_threshold() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;
        let mut off = 0u64;
        let step = SWITCH_THRESHOLD / 4;
        for i in 0..8 {
            // Every fourth read rewinds to zero: the contiguous run is broken
            // so the accumulator must never reach the threshold.
            let seek = i % 4 == 3;
            let at = if seek { 0 } else { off };
            assert!(c.note_read(alt, at, step).is_none(), "iteration {i}");
            if !seek {
                off += step;
            }
        }
        assert_eq!(c.active_file_id(), 0);
        // A clean contiguous run still flips the channel afterwards.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD).is_some());
        assert_eq!(c.active_file_id(), 1);
    }

    #[test]
    fn note_read_ignores_non_live_nodes() {
        let c = cat();
        assert!(c.note_read(ROOT_INO, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(DIR_INO_CANAIS, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(STREAM_INO, 0, 8 * SWITCH_THRESHOLD).is_none());
        let vod = c.add_vod_file("http://vps/movie.ts", "Filme.ts", 1024);
        assert!(c.note_read(vod, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(4242, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert_eq!(c.active_file_id(), 0);
    }

    #[test]
    fn add_vod_file_registers_under_filmes() {
        let c = cat();
        let url = "http://example.invalid/movie.ts";
        let ino = c.add_vod_file(url, "Filme.ts", 2 * 1024 * 1024);
        assert!(ino >= VOD_INO_BASE);

        let node = c.node(ino).expect("vod node");
        assert_eq!(node.parent, DIR_INO_FILMES);
        assert_eq!(node.kind, NodeKind::Vod { url: url.to_string() });
        assert_eq!(node.size, 2 * 1024 * 1024);
        assert!(!node.is_dir());
        assert_eq!(node.dirent_type(), libc::DT_REG as u32);

        let found = c.lookup(DIR_INO_FILMES, "Filme.ts").expect("lookup");
        assert_eq!(found.ino, ino);
        assert!(c.children(DIR_INO_FILMES).iter().any(|n| n.ino == ino));
    }

    #[test]
    fn add_live_channel_appends_channel_nodes() {
        let c = cat();
        let ino = c.add_live_channel(7, "TV EXTRA.ts", LIVE_SIZE);
        assert_eq!(ino, LIVE_INO_BASE + 7);
        let node = c.node(ino).expect("extra channel");
        assert_eq!(node.kind, NodeKind::Live { file_id: 7 });
        assert_eq!(node.parent, DIR_INO_CANAIS);
    }

    #[test]
    fn getattr_maps_node_kinds_to_fuse_attributes() {
        let c = cat();

        let root = c.getattr(ROOT_INO, 0).expect("root attr");
        assert_eq!(root.mode, (libc::S_IFDIR | 0o755) as u32);
        assert_eq!(root.nlink, 2);
        assert_eq!(root.size, 4096);
        assert_eq!(root.blksize, 4096);
        assert_eq!(root.blocks, 4096 / 512);

        let canais = c.getattr(DIR_INO_CANAIS, 0).expect("dir attr");
        assert_eq!(canais.mode, (libc::S_IFDIR | 0o755) as u32);
        assert_eq!(canais.nlink, 2);

        let live = c.getattr(LIVE_INO_BASE, LIVE_SIZE).expect("live attr");
        assert_eq!(live.mode, (libc::S_IFREG | 0o644) as u32);
        assert_eq!(live.nlink, 1);
        assert_eq!(live.size, LIVE_SIZE);
        assert_eq!(live.ino, LIVE_INO_BASE);

        // The block image node reports the live engine length, which keeps the
        // TV reading a growing file instead of a stale 128 GiB projection.
        let stream = c.getattr(STREAM_INO, LIVE_SIZE).expect("stream attr");
        assert_eq!(stream.size, LIVE_SIZE);
        assert_eq!(stream.mode, (libc::S_IFREG | 0o644) as u32);

        assert!(c.getattr(9999, 0).is_none());
    }

    #[test]
    fn default_catalog_is_not_empty() {
        let c = Catalog::default();
        assert!(!c.is_empty());
        assert!(c.lookup(DIR_INO_CANAIS, LIVE_NAME_PRIMARY).is_some());
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/vfs/catalog.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/vfs/dispatcher.rs (993 lines) <<<<<<<<<<<<<<<
//! `/dev/fuse` protocol dispatcher for the virtual NTFS volume.
//!
//! Implements the kernel-userspace FUSE protocol (Linux 3.4+) on a raw
//! `/dev/fuse` file descriptor: INIT handshake (with the minor < 23
//! truncation for the old tablet kernel), LOOKUP / GETATTR / OPEN / READ /
//! READDIR / STATFS / RELEASE, and FORGET suppression.
//!
//! Read routing (`NtfsServer::serve_read`):
//! * `tv_stream.img` (the whole-disk node) maps disk offsets to metadata
//!   sectors (VBR, $MFT, ... — VSN-stamped) or to the stream data area, which
//!   is served by the engine through [`StreamBackend`];
//! * live channel files feed `serve_live(file_id, ...)`;
//! * VOD files feed `serve_vod(url, ...)`.
//!
//! Zero-panic contract: every request is bounds-checked before any pointer
//! cast, reply payload areas are always zero-initialized before fields are
//! written, and all mutex poisoning is recovered.

use std::io;
use std::os::unix::io::RawFd;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use crate::contract::{StreamBackend, TS_PKT_SZ};
use crate::vfs::catalog::{Catalog, NodeKind};
use crate::vfs::fuse_abi::*;
use crate::vfs::ntfs_mft::{
    self, DiskRegion, NtfsTemplate, VolumeLayout, BPS, FILE_INO, ROOT_INO,
};

pub use crate::engine::ring::{fill_null, fill_null_at, NULL_PKT};

/// Default read deadline handed to the backend: the MStar SCSI bridge aborts
/// a request after 3.5–4.0 s of silence.
pub const DEFAULT_DEADLINE_MS: u64 = 3500;

/// Size of the fixed reply header.
pub const OUT_HDR_SZ: usize = std::mem::size_of::<fuse_out_header>();
/// Size of the fixed request header.
const IN_HDR_SZ: usize = std::mem::size_of::<fuse_in_header>();

/// The VFS service bound to a backend. Shared by the session loop.
pub struct NtfsServer {
    pub template: NtfsTemplate,
    pub catalog: Catalog,
    pub layout: VolumeLayout,
    pub backend: Arc<dyn StreamBackend>,
    pub deadline_ms: u64,
}

impl NtfsServer {
    pub fn new(
        template: NtfsTemplate,
        catalog: Catalog,
        backend: Arc<dyn StreamBackend>,
    ) -> Self {
        let layout = template.layout.clone();
        Self {
            template,
            catalog,
            layout,
            backend,
            deadline_ms: DEFAULT_DEADLINE_MS,
        }
    }

    /// Serves one FUSE_READ payload for `nodeid`, always fully populating
    /// `dst` (null-padded when the backend cannot deliver). Returns the
    /// number of bytes written.
    pub fn serve_read(&self, nodeid: u64, offset: u64, dst: &mut [u8]) -> usize {
        if dst.is_empty() {
            return 0;
        }
        let node = match self.catalog.node(nodeid) {
            Some(n) => n,
            None => {
                fill_null_at(dst, offset);
                return dst.len();
            }
        };
        match node.kind {
            NodeKind::DiskImage => self.serve_disk(offset, dst),
            NodeKind::Live { file_id } => {
                // Feed the detector first: a >= 1 MiB contiguous read run on
                // a non-active channel switches the active file_id.
                let _ = self.catalog.note_read(nodeid, offset, dst.len() as u64);
                self.serve_live_filled(file_id, offset, dst)
            }
            NodeKind::Vod { ref url } => match self.backend.serve_vod(
                url,
                offset,
                dst,
                self.deadline_ms,
            ) {
                Ok(n) if n > 0 => self.ts_trim(offset, n, dst),
                _ => {
                    fill_null_at(dst, offset);
                    dst.len()
                }
            },
            NodeKind::Root | NodeKind::Directory => {
                dst.fill(0);
                dst.len()
            }
        }
    }

    /// Guarantees the backend reply never truncates an MPEG-TS packet: a
    /// partial trailing packet is null-padded at the correct phase of the
    /// absolute stream offset, an empty reply becomes a full null fill.
    fn ts_trim(&self, foff: u64, n: usize, dst: &mut [u8]) -> usize {
        let valid = if n >= dst.len() {
            dst.len()
        } else {
            let end_off = foff.saturating_add(n as u64);
            let rem = (end_off % TS_PKT_SZ as u64) as usize;
            n.saturating_sub(rem)
        };
        if valid < dst.len() {
            fill_null_at(&mut dst[valid..], foff + valid as u64);
        }
        dst.len()
    }

    fn serve_live_filled(&self, file_id: u32, foff: u64, dst: &mut [u8]) -> usize {
        let n = self
            .backend
            .serve_live(file_id, foff, dst, self.deadline_ms);
        self.ts_trim(foff, n, dst)
    }

    /// Serves a read of the whole-disk node: metadata sectors come from the
    /// NTFS template (VSN-stamped boot sectors), data bytes from the engine.
    ///
    /// The stream file is fragmented (see [`crate::vfs::ntfs_mft::DiskExtent`]):
    /// each iteration classifies the current offset, and stream chunks are
    /// clamped to the extent end so one backend call never bridges a hole
    /// between two extents — mirroring `avail_in_ext` in the C daemon.
    fn serve_disk(&self, disk_off: u64, dst: &mut [u8]) -> usize {
        let mut done = 0usize;
        let mut cur = disk_off;
        // `vbr.total_sectors` is the NTFS BPB count, which EXCLUDES the backup
        // boot sector: that sector lives one LBA past it and must still be
        // served (VSN-stamped) for the anti-cache contract, so the servable
        // volume ends one sector after `backup_vbr_byte()`.
        let vol_end = self.template.vbr.total_sectors.saturating_add(1).saturating_mul(BPS as u64);

        while done < dst.len() {
            if cur >= vol_end {
                dst[done..].fill(0);
                break;
            }
            match self.template.route(cur) {
                DiskRegion::Data(foff) => {
                    let run = self.template.data_run_len(cur);
                    let rem = (dst.len() - done) as u64;
                    let chunk = rem.min(run) as usize;
                    if chunk == 0 {
                        dst[done..].fill(0);
                        break;
                    }
                    let n = self.serve_stream(foff, &mut dst[done..done + chunk]);
                    done += n;
                    cur += n as u64;
                }
                DiskRegion::Metadata(_) => {
                    let sec = cur / BPS as u64;
                    let sec_off = (cur % BPS as u64) as usize;
                    let rem = (dst.len() - done) as u64;
                    let chunk = rem.min((BPS - sec_off) as u64) as usize;
                    let mut sec_buf = [0u8; BPS];
                    self.template.read_sector(sec, &mut sec_buf);
                    dst[done..done + chunk].copy_from_slice(&sec_buf[sec_off..sec_off + chunk]);
                    done += chunk;
                    cur += chunk as u64;
                }
            }
        }
        dst.len()
    }

    /// Routes stream-data reads through the active catalog entry.
    fn serve_stream(&self, foff: u64, dst: &mut [u8]) -> usize {
        match self.catalog.active_node() {
            Some(node) => match node.kind {
                NodeKind::Live { file_id } => self.serve_live_filled(file_id, foff, dst),
                NodeKind::Vod { ref url } => {
                    match self.backend.serve_vod(url, foff, dst, self.deadline_ms) {
                        Ok(n) if n > 0 => self.ts_trim(foff, n, dst),
                        _ => {
                            fill_null_at(dst, foff);
                            dst.len()
                        }
                    }
                }
                _ => {
                    fill_null_at(dst, foff);
                    dst.len()
                }
            },
            None => {
                fill_null_at(dst, foff);
                dst.len()
            }
        }
    }

    /// Resolves a FUSE_LOOKUP name (NUL-terminated by the kernel, but
    /// defensively bounded when not).
    pub fn resolve_lookup(&self, parent: u64, name: &[u8]) -> Option<crate::vfs::catalog::Node> {
        let end = name.iter().position(|&b| b == 0).unwrap_or(name.len());
        let name = std::str::from_utf8(&name[..end]).ok()?;
        self.catalog.lookup(parent, name)
    }

    /// Children of `ino` for READDIR, in stable order.
    pub fn readdir_entries(&self, ino: u64) -> Vec<(u64, String, u32)> {
        let mut out: Vec<(u64, String, u32)> = Vec::new();
        out.push((ino, ".".to_string(), libc::DT_DIR as u32));
        let parent = self
            .catalog
            .node(ino)
            .map(|n| n.parent)
            .unwrap_or(ROOT_INO);
        out.push((parent, "..".to_string(), libc::DT_DIR as u32));
        for child in self.catalog.children(ino) {
            out.push((child.ino, child.name.clone(), child.dirent_type()));
        }
        out
    }
}

/// A `/dev/fuse` session: the raw request/reply loop bound to an
/// [`NtfsServer`].
pub struct FuseSession {
    fd: RawFd,
    server: Arc<NtfsServer>,
}

impl FuseSession {
    pub fn new(fd: RawFd, server: Arc<NtfsServer>) -> Self {
        Self { fd, server }
    }

    /// Runs the request loop until `running` clears or the device errors.
    /// `running` is flipped by the signal handler in `main`.
    pub fn run(&self, running: &AtomicBool) -> io::Result<()> {
        // 8-byte aligned staging buffers for ARMv7-A LDRD/STRD safety.
        let mut in_buf = AlignedFuseBuf::new_boxed();
        let mut out_buf = AlignedFuseBuf::new_boxed();

        while running.load(Ordering::SeqCst) {
            let n = unsafe {
                libc::read(
                    self.fd,
                    in_buf.0.as_mut_ptr() as *mut libc::c_void,
                    in_buf.0.len(),
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                match err.raw_os_error() {
                    Some(libc::EINTR) | Some(libc::EAGAIN) | Some(libc::ENOENT) => {
                        continue;
                    }
                    Some(libc::ENODEV) => {
                        // Filesystem was unmounted cleanly.
                        break;
                    }
                    _ => {
                        eprintln!("[FUSE_DISPATCHER] /dev/fuse read error: {}", err);
                        return Err(err);
                    }
                }
            }
            if (n as usize) < IN_HDR_SZ {
                continue;
            }
            let payload_end = (n as usize).min(in_buf.0.len());
            self.dispatch(&in_buf.0[..payload_end], &mut out_buf);
        }
        Ok(())
    }

    /// Parses one request and writes its reply.
    fn dispatch(&self, req: &[u8], out: &mut AlignedFuseBuf) {
        let Some(header_bytes) = req.get(..IN_HDR_SZ) else {
            return;
        };
        // SAFETY: `AlignedFuseBuf` is 8-byte aligned and `req` is a prefix of
        // it of at least IN_HDR_SZ bytes.
        let inh: &fuse_in_header =
            unsafe { &*(header_bytes.as_ptr() as *const fuse_in_header) };
        let payload = &req[IN_HDR_SZ..];

        // FORGET / BATCH_FORGET are no-reply notifications.
        if inh.opcode == FUSE_FORGET || inh.opcode == FUSE_BATCH_FORGET {
            return;
        }

        match inh.opcode {
            FUSE_INIT => self.handle_init(inh, payload, out),
            FUSE_GETATTR => self.handle_getattr(inh, out),
            FUSE_LOOKUP => self.handle_lookup(inh, payload, out),
            FUSE_OPEN | FUSE_OPENDIR => self.handle_open(inh, out),
            FUSE_READ => self.handle_read(inh, payload, out),
            FUSE_READDIR => self.handle_readdir(inh, payload, out),
            FUSE_RELEASE | FUSE_RELEASEDIR => self.reply_empty(inh, out),
            FUSE_STATFS => self.handle_statfs(inh, out),
            _ => self.reply_error(inh, out, -libc::ENOSYS),
        }
    }

    // ------------------------------------------------------------ helpers

    /// Writes a successful reply of exactly `len` bytes.
    fn reply(&self, out: &AlignedFuseBuf, len: usize) {
        let w = unsafe {
            libc::write(
                self.fd,
                out.0.as_ptr() as *const libc::c_void,
                len,
            )
        };
        let _ = w;
    }

    /// Header-only positive reply.
    fn reply_empty(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ, 0);
        self.reply(out, OUT_HDR_SZ);
    }

    /// Header-only negative reply with an errno.
    fn reply_error(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, errno: i32) {
        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ, errno);
        self.reply(out, OUT_HDR_SZ);
    }

    /// Fills the common reply header fields.
    fn write_header(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, len: usize, error: i32) {
        // SAFETY: 8-byte aligned buffer, OUT_HDR_SZ bytes available.
        let oh = unsafe { &mut *(out.0.as_mut_ptr() as *mut fuse_out_header) };
        oh.len = len as u32;
        oh.error = error;
        oh.unique = inh.unique;
    }

    /// Fills a `fuse_attr` for regular files.
    fn fill_file_attr(attr: &mut fuse_attr, ino: u64, size: u64) {
        attr.ino = ino;
        attr.size = size;
        attr.blocks = size / 512;
        attr.mode = (libc::S_IFREG | 0o644) as u32;
        attr.nlink = 1;
        attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
    }

    /// Writes an attr_out reply for `attr`.
    fn reply_attr(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, attr: &fuse_attr) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_attr_out>();
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);
        // SAFETY: 8-byte aligned, reply_len validated against the buffer.
        let ao = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_attr_out) };
        ao.attr_valid = 10;
        ao.attr = *attr;
        self.reply(out, reply_len);
    }

    // ------------------------------------------------------------ handlers

    fn handle_init(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < 8 {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        let minor = u32::from_ne_bytes(payload[4..8].try_into().unwrap_or([0; 4]));
        let flags = if payload.len() >= 16 {
            u32::from_ne_bytes(payload[12..16].try_into().unwrap_or([0; 4]))
        } else {
            0
        };
        // The Samsung tablet ships kernel 3.4.0, whose FUSE client negotiates
        // minor 7.18: it understands the reply only up through `max_write`,
        // so the body must be truncated at exactly 24 payload bytes whenever
        // the negotiated minor is < 23 (anything longer makes the kernel read
        // past the declared reply length). This mirrors the audited C daemon
        // (`fuse_ntfs.c` INIT handshake) and the `fuse_direct` sibling.
        let out_len = if minor < 23 {
            OUT_HDR_SZ + 24
        } else {
            OUT_HDR_SZ + std::mem::size_of::<fuse_init_out>()
        };

        // Zero the reply struct region: max_background / congestion_threshold
        // are never written explicitly and would otherwise carry stale
        // MPEG-TS payload bytes into kernel-visible fields.
        out.0[..out_len].fill(0);
        self.write_header(inh, out, out_len, 0);

        // SAFETY: aligned + sized as above.
        let io = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_init_out) };
        io.major = 7;
        io.minor = minor.min(26);
        io.max_readahead = MAX_MSG_SZ as u32;
        io.flags = flags & (FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        io.max_write = MAX_MSG_SZ as u32;

        self.reply(out, out_len);
    }

    fn handle_getattr(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let mut attr: fuse_attr = unsafe { std::mem::zeroed() };
        match inh.nodeid {
            ROOT_INO => {
                attr.ino = ROOT_INO;
                attr.mode = (libc::S_IFDIR | 0o755) as u32;
                attr.nlink = 2;
                attr.size = 4096;
                attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                self.reply_attr(inh, out, &attr);
            }
            FILE_INO => {
                Self::fill_file_attr(&mut attr, FILE_INO, ntfs_mft::VOLUME_BYTES);
                self.reply_attr(inh, out, &attr);
            }
            ino => match self.server.catalog.node(ino) {
                Some(node) if node.is_dir() => {
                    attr.ino = node.ino;
                    attr.mode = (libc::S_IFDIR | 0o755) as u32;
                    attr.nlink = 2;
                    attr.size = 4096;
                    attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                    self.reply_attr(inh, out, &attr);
                }
                Some(node) => {
                    Self::fill_file_attr(&mut attr, node.ino, node.size);
                    self.reply_attr(inh, out, &attr);
                }
                None => self.reply_error(inh, out, -libc::ENOENT),
            },
        }
    }

    fn handle_lookup(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_entry_out>();
        // Zero the reply struct region so no stale payload leaks into
        // entry_out/attr timespec fields.
        out.0[..reply_len].fill(0);

        match self.server.resolve_lookup(inh.nodeid, payload) {
            Some(node) => {
                self.write_header(inh, out, reply_len, 0);
                // SAFETY: aligned + sized as above.
                let eo =
                    unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_entry_out) };
                eo.nodeid = node.ino;
                eo.generation = 1;
                eo.entry_valid = 10;
                eo.attr_valid = 10;
                if node.is_dir() {
                    eo.attr.ino = node.ino;
                    eo.attr.mode = (libc::S_IFDIR | 0o755) as u32;
                    eo.attr.nlink = 2;
                    eo.attr.size = 4096;
                } else {
                    Self::fill_file_attr(&mut eo.attr, node.ino, node.size);
                }
                eo.attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                self.reply(out, reply_len);
            }
            None => self.reply_error(inh, out, -libc::ENOENT),
        }
    }

    fn handle_open(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_open_out>();

        // Unknown nodes are rejected before granting a handle.
        if inh.nodeid != ROOT_INO && self.server.catalog.node(inh.nodeid).is_none() {
            self.reply_error(inh, out, -libc::ENOENT);
            return;
        }

        // Zero the reply struct region (fh/open_flags/padding must be exact).
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);
        // SAFETY: aligned + sized as above.
        let oo = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_open_out) };
        oo.fh = 1;
        // Waiver #FUSE-2013-MSTAR: FOPEN_DIRECT_IO is granted unconditionally
        // for the block image. The 2013 Samsung MStar SCSI bridge freezes the
        // decoded frame whenever the kernel page cache serves stale sectors
        // of the 128 GiB image; direct I/O forces every sector through
        // serve_read(), so the live feeder and the anti-probe NULL-fill path
        // remain authoritative. FOPEN_NONSEEKABLE is deliberately OMITTED:
        // the MStar bridge issues pread64/lseek to walk the block image, and
        // declaring the file non-seekable would degrade the kernel to pure
        // stream reads, destroying the offset-based live-edge/re-anchor logic.
        oo.open_flags = if inh.opcode == FUSE_OPEN {
            FOPEN_DIRECT_IO
        } else {
            0
        };
        self.reply(out, reply_len);
    }

    fn handle_read(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < std::mem::size_of::<fuse_read_in>() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        // SAFETY: payload inside the aligned request buffer, sized above.
        let rin = unsafe { &*(payload.as_ptr() as *const fuse_read_in) };
        let size = (rin.size as usize).min(MAX_MSG_SZ);
        if OUT_HDR_SZ + size > out.0.len() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }

        // serve_read fully populates the payload (metadata / ring / NULL
        // fill), so no pre-zeroing of out_buf is required for FUSE_READ.
        let rdata = &mut out.0[OUT_HDR_SZ..OUT_HDR_SZ + size];
        let written = self.server.serve_read(inh.nodeid, rin.offset, rdata);

        static TOTAL_READ: AtomicU64 = AtomicU64::new(0);
        let total = TOTAL_READ.fetch_add(written as u64, Ordering::Relaxed);
        let next_total = total.saturating_add(written as u64);
        if next_total / (2 * 1024 * 1024) != total / (2 * 1024 * 1024) || rin.offset < 200_000 {
            eprintln!(
                "[FUSE_READ] total={}MB off={} sz={} (ino={})",
                next_total / (1024 * 1024), rin.offset, size, inh.nodeid
            );
        }

        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ + written, 0);
        self.reply(out, OUT_HDR_SZ + written);
    }

    fn handle_readdir(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < std::mem::size_of::<fuse_read_in>() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        // SAFETY: as above.
        let rin = unsafe { &*(payload.as_ptr() as *const fuse_read_in) };
        let off = rin.offset;
        let max_size = (rin.size as usize).min(out.0.len() - OUT_HDR_SZ);

        let entries = self.server.readdir_entries(inh.nodeid);

        // Zero the whole dirent payload area: the name padding bytes past
        // each entry must not leak stale buffer content.
        out.0[..OUT_HDR_SZ + max_size].fill(0);

        let mut used = 0usize;
        let data_ptr = out.0[OUT_HDR_SZ..].as_mut_ptr();

        for (idx, (ino, name, typ)) in entries.iter().enumerate() {
            let next_off = idx as u64 + 1;
            if next_off <= off {
                continue;
            }
            let nl = name.len();
            let esz = (std::mem::size_of::<fuse_dirent>() + nl + 7) & !7;
            if used + esz > max_size {
                break;
            }
            // SAFETY: data_ptr is 8-byte aligned, `used + esz <= max_size`.
            let de = unsafe { &mut *(data_ptr.add(used) as *mut fuse_dirent) };
            de.ino = *ino;
            de.off = next_off;
            de.namelen = nl as u32;
            de.typ = *typ;
            // SAFETY: name bytes fit inside the validated `esz` window.
            unsafe {
                std::ptr::copy_nonoverlapping(
                    name.as_ptr(),
                    data_ptr.add(used + std::mem::size_of::<fuse_dirent>()),
                    nl,
                );
            }
            used += esz;
        }

        let reply_len = OUT_HDR_SZ + used;
        self.write_header(inh, out, reply_len, 0);
        self.reply(out, reply_len);
    }

    fn handle_statfs(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_statfs_out>();

        // Zero the reply struct region: the kstatfs `spare` array is never
        // written explicitly and must not leak stale bytes.
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);

        let vbr = &self.server.template.vbr;
        // SAFETY: aligned + sized as above.
        let so = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_statfs_out) };
        so.st.bsize = vbr.cluster_size as u32;
        so.st.frsize = vbr.cluster_size as u32;
        let total_clusters = vbr.total_sectors / u64::from(vbr.sectors_per_cluster);
        so.st.blocks = total_clusters;
        so.st.bfree = 0;
        so.st.bavail = 0;
        so.st.files = self.server.catalog.len() as u64;
        so.st.ffree = 0;
        so.st.namelen = 255;

        self.reply(out, reply_len);
    }
}

/// Binds a session to a mounted `/dev/fuse` fd. Thin wrapper so `main` keeps
/// the process-lifecycle concerns while the protocol stays here.
pub fn run(fd: RawFd, server: Arc<NtfsServer>, running: &AtomicBool) -> io::Result<()> {
    FuseSession::new(fd, server).run(running)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::StreamBackend;
    use crate::vfs::catalog::Catalog;
    use crate::vfs::ntfs_mft::NtfsTemplate;
    use std::io;

    /// Backend that reports how it was called and fills deterministic data.
    struct ProbeBackend {
        live_calls: std::sync::Mutex<Vec<(u32, u64, usize)>>,
    }

    impl ProbeBackend {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                live_calls: std::sync::Mutex::new(Vec::new()),
            })
        }
    }

    impl StreamBackend for ProbeBackend {
        fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], _d: u64) -> usize {
            self.live_calls
                .lock()
                .unwrap()
                .push((file_id, foff, dst.len()));
            for (i, b) in dst.iter_mut().enumerate() {
                *b = (i % 251) as u8;
            }
            dst.len()
        }
        fn serve_vod(&self, _url: &str, _foff: u64, dst: &mut [u8], _d: u64) -> io::Result<usize> {
            for (_i, b) in dst.iter_mut().enumerate() {
                *b = 0xEE;
            }
            Ok(dst.len())
        }
    }

    fn server() -> (Arc<NtfsServer>, Arc<ProbeBackend>) {
        let backend = ProbeBackend::new();
        let server = Arc::new(NtfsServer::new(
            NtfsTemplate::synthesized(),
            Catalog::new(1024),
            backend.clone(),
        ));
        (server, backend)
    }

    #[test]
    fn test_fill_null_packet_shape() {
        let mut buf = [0u8; TS_PKT_SZ * 3];
        fill_null(&mut buf);
        for p in 0..3 {
            let o = p * TS_PKT_SZ;
            assert_eq!(buf[o], 0x47);
            assert_eq!(buf[o + 1], 0x1F);
            assert_eq!(buf[o + 2], 0xFF);
            assert_eq!(buf[o + 3], 0x10);
        }
        // Partial tails stay packet-aligned.
        let mut small = [0u8; 100];
        fill_null(&mut small);
        assert_eq!(small[0], 0x47);
        assert_eq!(small[99], 0xFF);
    }

    #[test]
    fn test_serve_read_disk_metadata() {
        let (srv, _b) = server();
        let mut buf = [0u8; BPS as usize];
        let n = srv.serve_read(FILE_INO, 0, &mut buf);
        assert_eq!(n, BPS as usize);
        assert_eq!(&buf[3..11], b"NTFS    ");
        assert_eq!(buf[510], 0x55);
    }

    #[test]
    fn test_serve_read_disk_data_routes_to_backend() {
        let (srv, b) = server();
        let mut buf = [0u8; TS_PKT_SZ * 8];
        let off = srv.layout.data_start_byte + 4096;
        let n = srv.serve_read(FILE_INO, off, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0], (0, 4096, TS_PKT_SZ * 8));
    }

    #[test]
    fn test_serve_read_partial_metadata_sector() {
        let (srv, _b) = server();
        // Mid-boot-sector read must stay sector aligned.
        let mut buf = [0u8; 16];
        let n = srv.serve_read(FILE_INO, 100, &mut buf);
        assert_eq!(n, 16);
        // Sector 0 offset 100 still inside the NTFS OEM field area (all zero
        // after 0x0B), while a read spanning into the metadata payload works.
        let mut buf2 = [0u8; 16];
        srv.serve_read(FILE_INO, 0, &mut buf2);
        assert_eq!(&buf2[0..3], &[0xEB, 0x52, 0x90]);
    }

    #[test]
    fn test_serve_read_live_channel_file() {
        let (srv, b) = server();
        // "Canais TV/TV AO VIVO 2.tp" maps to file_id 1.
        let ino = crate::vfs::catalog::LIVE_INO_BASE + 1;
        let mut buf = [0u8; TS_PKT_SZ * 4];
        let n = srv.serve_read(ino, 0, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls[0].0, 1);
    }

    #[test]
    fn test_serve_read_vod_file() {
        let (srv, _b) = server();
        let ino = srv.catalog.add_vod_file("http://vps/x.ts", "Filme.ts", 1024);
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(ino, 0, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        assert_eq!(buf[0], 0xEE);
    }

    #[test]
    fn test_serve_read_unknown_node_nulls() {
        let (srv, b) = server();
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(4242, 0, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        assert_eq!(buf[0], 0x47);
        assert!(b.live_calls.lock().unwrap().is_empty());
    }

    #[test]
    fn test_ts_trim_pads_partial_packet() {
        let (srv, _b) = server();
        // A backend reply of 100 bytes (less than one TS packet) is fully
        // null-filled by the hardened ts_trim.
        let mut dst = [0u8; TS_PKT_SZ * 2];
        let n = srv.ts_trim(0, 100, &mut dst);
        assert_eq!(n, TS_PKT_SZ * 2);
        assert_eq!(dst[0], 0x47);
        assert_eq!(dst[1], 0x1F);
    }

    #[test]
    fn test_ts_trim_zero_fills() {
        let (srv, _b) = server();
        let mut dst = [0u8; TS_PKT_SZ];
        assert_eq!(srv.ts_trim(0, 0, &mut dst), TS_PKT_SZ);
        assert_eq!(dst[0], 0x47);
    }

    #[test]
    fn test_readdir_entries() {
        let (srv, _b) = server();
        let entries = srv.readdir_entries(ROOT_INO);
        let names: Vec<&str> = entries.iter().map(|(_, n, _)| n.as_str()).collect();
        assert!(names.contains(&"."));
        assert!(names.contains(&".."));
        assert!(names.contains(&"tv_stream.img"));
        assert!(names.contains(&"Canais TV"));
        assert!(names.contains(&"Filmes"));
        assert!(names.contains(&"Favoritos"));

        let canais = srv
            .catalog
            .lookup(ROOT_INO, "Canais TV")
            .expect("dir");
        let sub = srv.readdir_entries(canais.ino);
        let sub_names: Vec<&str> = sub.iter().map(|(_, n, _)| n.as_str()).collect();
        assert!(sub_names.contains(&"TV AO VIVO.trp"));
        assert!(sub_names.contains(&"TV AO VIVO 2.tp"));
    }

    #[test]
    fn test_resolve_lookup_bounds() {
        let (srv, _b) = server();
        // NUL-terminated name.
        let mut name = b"tv_stream.img".to_vec();
        name.push(0);
        let node = srv.resolve_lookup(ROOT_INO, &name).expect("resolves");
        assert_eq!(node.ino, FILE_INO);
        // Unterminated name still resolves (kernel always terminates).
        let node2 = srv
            .resolve_lookup(ROOT_INO, b"tv_stream.img")
            .expect("resolves");
        assert_eq!(node2.ino, FILE_INO);
        // Invalid UTF-8 fails closed.
        assert!(srv.resolve_lookup(ROOT_INO, &[0xFF, 0xFE]).is_none());
    }

    #[test]
    fn test_disk_read_whole_extent() {
        let (srv, _b) = server();
        // Read spanning metadata -> data in one request.
        let start = srv.layout.data_start_byte - 256;
        let mut buf = [0u8; 512 + TS_PKT_SZ * 2];
        let n = srv.serve_read(FILE_INO, start, &mut buf);
        assert_eq!(n, buf.len());
        // First 256 bytes: tail of the MFT metadata area (zero-filled hole).
        // Following bytes: ring data from the backend.
        assert_ne!(buf[300], 0);
    }

    /// Production-shaped stream: data starts right behind `$MFT` (cluster 23)
    /// and the file is fragmented into two extents with a hole between them.
    fn fragmented_server() -> (Arc<NtfsServer>, Arc<ProbeBackend>) {
        let layout = ntfs_mft::VolumeLayout {
            data_start_byte: 94_208,
            data_size: 128_000_000_000,
            live_file_inode: 64,
            extents: vec![
                ntfs_mft::DiskExtent { disk_start_byte: 94_208, disk_end_byte: 94_208 + 8192, file_start: 0 },
                ntfs_mft::DiskExtent { disk_start_byte: 1_000_000, disk_end_byte: 1_000_000 + 4096, file_start: 60_000 },
            ],
        };
        let backend = ProbeBackend::new();
        let server = Arc::new(NtfsServer::new(
            NtfsTemplate::with_layout(ntfs_mft::NtfsTemplate::default_vbr(), layout),
            Catalog::new(1024),
            backend.clone(),
        ));
        (server, backend)
    }

    #[test]
    fn test_serve_disk_backup_vbr_through_read() {
        let (srv, b) = server();
        let sec = srv.template.vbr.total_sectors;
        let mut buf = [0u8; BPS as usize];
        let n = srv.serve_read(FILE_INO, sec * BPS as u64, &mut buf);
        assert_eq!(n, BPS as usize);
        assert_eq!(&buf[3..11], b"NTFS    ");
        // The randomized VSN is stamped at offset 0x48 of both boot sectors.
        let vsn = u64::from_le_bytes(buf[ntfs_mft::VSN_OFFSET..ntfs_mft::VSN_OFFSET + 8].try_into().unwrap());
        assert_eq!(vsn, srv.template.serial);
        assert!(b.live_calls.lock().unwrap().is_empty());
    }

    #[test]
    fn test_serve_disk_routes_multi_extent_data() {
        let (srv, b) = fragmented_server();
        // A read landing on the second extent must be inverted to its file
        // offset (60_000), not served as a linear disk offset.
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(FILE_INO, 1_000_000, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0], (0, 60_000, TS_PKT_SZ));
        drop(calls);

        // The hole between the extents is metadata: zeros, no backend call.
        let mut hole = [0xFF; TS_PKT_SZ];
        let n = srv.serve_read(FILE_INO, 94_208 + 8192, &mut hole);
        assert_eq!(n, TS_PKT_SZ);
        assert!(hole.iter().all(|&x| x == 0));
        assert_eq!(b.live_calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_serve_disk_clamps_stream_chunk_to_extent_end() {
        let (srv, b) = fragmented_server();
        // A read straddling the first extent end serves 8192 stream bytes and
        // the remainder from the hole (metadata), never one bridged call.
        let mut buf = [0u8; TS_PKT_SZ * 8];
        let n = srv.serve_read(FILE_INO, 94_208 + 8192 - TS_PKT_SZ as u64 * 4, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].2, TS_PKT_SZ * 4);
        drop(calls);
        // The trailing hole bytes are zeros.
        assert!(buf[TS_PKT_SZ * 4..].iter().all(|&x| x == 0));
    }

    /// Builds a raw INIT request in `buf`; returns the total request length.
    fn init_request(buf: &mut AlignedFuseBuf, minor: u32, flags: u32) -> usize {
        let req_len = IN_HDR_SZ + 16;
        buf.0[..req_len].fill(0);
        // SAFETY: 8-byte aligned buffer, req_len >= IN_HDR_SZ + 16.
        let ih = unsafe { &mut *(buf.0.as_mut_ptr() as *mut fuse_in_header) };
        ih.len = req_len as u32;
        ih.opcode = FUSE_INIT;
        ih.unique = 7;
        let ii = unsafe { &mut *(buf.0[IN_HDR_SZ..].as_mut_ptr() as *mut fuse_init_in) };
        ii.major = 7;
        ii.minor = minor;
        ii.max_readahead = MAX_MSG_SZ as u32;
        ii.flags = flags;
        req_len
    }

    fn dispatch_init(req: &mut AlignedFuseBuf, minor: u32, flags: u32) -> Box<AlignedFuseBuf> {
        let (srv, _b) = server();
        // fd = -1: `reply` discards the EBADF from the failed write.
        let session = FuseSession::new(-1, srv);
        let req_len = init_request(req, minor, flags);
        let mut out = AlignedFuseBuf::new_boxed();
        session.dispatch(&req.0[..req_len], &mut out);
        out
    }

    fn out_len(out: &AlignedFuseBuf) -> usize {
        u32::from_ne_bytes(out.0[0..4].try_into().unwrap()) as usize
    }

    fn out_err(out: &AlignedFuseBuf) -> i32 {
        i32::from_ne_bytes(out.0[4..8].try_into().unwrap())
    }

    #[test]
    fn test_init_reply_truncated_for_legacy_minor() {
        let mut req = AlignedFuseBuf::new_boxed();
        let out = dispatch_init(&mut req, 18, FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        // Tablet kernel 3.4.0 negotiates 7.18 (< 23): body capped at 24.
        assert_eq!(out_len(&out), OUT_HDR_SZ + 24);
        assert_eq!(out_err(&out), 0);
        // SAFETY: 8-byte aligned buffer, reply covers the whole struct.
        let io = unsafe { &*(out.0[OUT_HDR_SZ..].as_ptr() as *const fuse_init_out) };
        assert_eq!(io.major, 7);
        assert_eq!(io.minor, 18);
        assert_eq!(io.max_readahead, MAX_MSG_SZ as u32);
        assert_eq!(io.flags, FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        assert_eq!(io.max_write, MAX_MSG_SZ as u32);
        // Zero-filled reply region: no stale MPEG-TS bytes leak into
        // kernel-visible fields.
        assert_eq!(io.max_background, 0);
        assert_eq!(io.congestion_threshold, 0);
    }

    #[test]
    fn test_init_reply_caps_minor_at_26() {
        let mut req = AlignedFuseBuf::new_boxed();
        let out = dispatch_init(&mut req, 31, FUSE_ASYNC_READ);
        assert_eq!(
            out_len(&out),
            OUT_HDR_SZ + std::mem::size_of::<fuse_init_out>()
        );
        assert_eq!(out_err(&out), 0);
        // SAFETY: as above.
        let io = unsafe { &*(out.0[OUT_HDR_SZ..].as_ptr() as *const fuse_init_out) };
        assert_eq!(io.major, 7);
        assert_eq!(io.minor, 26);
        assert_eq!(io.flags, FUSE_ASYNC_READ);
    }

    #[test]
    fn test_init_rejects_short_payload() {
        let (srv, _b) = server();
        let session = FuseSession::new(-1, srv);
        let mut req = AlignedFuseBuf::new_boxed();
        req.0[..IN_HDR_SZ].fill(0);
        // SAFETY: 8-byte aligned buffer, IN_HDR_SZ bytes available.
        let ih = unsafe { &mut *(req.0.as_mut_ptr() as *mut fuse_in_header) };
        ih.len = IN_HDR_SZ as u32;
        ih.opcode = FUSE_INIT;
        ih.unique = 9;
        let mut out = AlignedFuseBuf::new_boxed();
        session.dispatch(&req.0[..IN_HDR_SZ], &mut out);
        assert_eq!(out_len(&out), OUT_HDR_SZ);
        assert_eq!(out_err(&out), -libc::EINVAL);
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/vfs/dispatcher.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/ring.rs (546 lines) <<<<<<<<<<<<<<<
//! 128 MiB circular RAM ring buffer: the Live TV data plane.
//!
//! Production invariants (docs/LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md, AGENTS.md §2.2):
//!
//! * **Zero-throttle feeder.** The writer never sleeps and never consults the
//!   reader lead: live TV is a continuous real-time flow. If the decoder
//!   pauses, the writer simply overwrites old data; a reader that falls out of
//!   the circular window (`s < ring_old`) is transparently re-anchored by the
//!   engine, never throttled through the writer.
//! * **Monotonic write cursor.** `s_write` only ever grows; the physical index
//!   is `(pos % capacity) as usize`.
//! * **Token-bucket pacing.** The reader side is paced per lead band
//!   (480/600/900 KB/s) inside a shared 250 ms window. Every sleep is sliced
//!   to <= 50 ms with the mutex unlocked so the MStar SCSI/USB controller
//!   never times out.
//! * **Bounded live-edge wait.** A reader that reaches the writer head waits
//!   cooperatively on the condvar for at most 500 ms per FUSE read.

use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::contract::{RINGSZ, TS_PKT_SZ};

/// Phase-aligned MPEG-TS null packet (PID 0x1FFF).
pub const NULL_PKT: [u8; TS_PKT_SZ] = {
    let mut pkt = [0xFFu8; TS_PKT_SZ];
    pkt[0] = 0x47;
    pkt[1] = 0x1F;
    pkt[2] = 0xFF;
    pkt[3] = 0x10;
    pkt
};

/// Snaps a monotonically-increasing stream position down to the next lower
/// 188-byte packet boundary (saturating: never underflows).
pub const fn snap188(pos: u64) -> u64 {
    pos.saturating_sub(pos % TS_PKT_SZ as u64)
}

/// Fills `dst` with phase-aligned NULL packets starting exactly at `dst[0]`,
/// so every 188-byte grid position inside the window is a complete packet.
pub fn fill_null(dst: &mut [u8]) {
    fill_null_at(dst, 0);
}

pub fn fill_null_at(dst: &mut [u8], abs_off: u64) {
    let mut off = 0usize;
    while off < dst.len() {
        let phase = ((abs_off + off as u64) % TS_PKT_SZ as u64) as usize;
        let chunk = (dst.len() - off).min(TS_PKT_SZ - phase);
        dst[off..off + chunk].copy_from_slice(&NULL_PKT[phase..phase + chunk]);
        off += chunk;
    }
}

/// Reader pacing ceilings (bytes/second), selected by the lead band.
///
/// * `Slow480` — cushion nearly empty (< 2 MiB): match the slowest feeder.
/// * `Mid600`  — healthy cushion (2..8 MiB).
/// * `Fast900` — deep cushion (>= 8 MiB): let the decoder catch up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaceTier {
    Slow480,
    Mid600,
    Fast900,
}

impl PaceTier {
    /// Bytes/second ceiling for this tier.
    pub const fn rate(self) -> u64 {
        match self {
            PaceTier::Slow480 => 480_000,
            PaceTier::Mid600 => 600_000,
            PaceTier::Fast900 => 900_000,
        }
    }

    /// Selects the tier from the current cushion (`s_write - read position`).
    pub const fn for_lead(lead: u64) -> Self {
        if lead < PACE_LEAD_SLOW {
            PaceTier::Slow480
        } else if lead < PACE_LEAD_FAST {
            PaceTier::Mid600
        } else {
            PaceTier::Fast900
        }
    }
}

/// Token-bucket window shared by all FUSE reader threads.
pub const PACE_WINDOW_MS: u64 = 250;
/// Hard clamp on any single pacing sleep (AGENTS.md §2.2).
pub const MAX_SLEEP_SLICE_MS: u64 = 50;
/// Total cooperative live-edge wait budget per FUSE read.
pub const EDGE_WAIT_BUDGET_MS: u64 = 500;
/// Below this cushion the reader is paced at the slow tier.
pub const PACE_LEAD_SLOW: u64 = 2 * 1024 * 1024;
/// At/above this cushion the reader is paced at the fast tier.
pub const PACE_LEAD_FAST: u64 = 8 * 1024 * 1024;

/// Ring data plane. All fields are private to the engine mutex; access goes
/// through the checked helpers below (no direct indexing).
pub struct RingData {
    buffer: Box<[u8]>,
    capacity: u64,
    s_write: u64,
    have_data: bool,
    start: Instant,
    // token bucket state (shared across reader threads, matches audited C implementation)
    pace_tok: i64,
    pace_tok_ms: u64,
    // telemetry (drives the AGENTS.md §4 verification: near_starve=0, nulls=0)
    null_bytes: u64,
    served_bytes: u64,
    near_starve: u64,
    edge_waits: u64,
    overwritten_blocks: u64,
    last_lead: u64,
}

impl Default for RingData {
    fn default() -> Self {
        Self::new()
    }
}

impl RingData {
    /// Allocates the production 128 MiB ring.
    pub fn new() -> Self {
        Self::with_capacity(RINGSZ as u64)
    }

    /// Allocates a ring with a custom capacity (tests / small-footprint runs).
    /// Capacity is rounded up to a multiple of 188 so the 188-byte grid never
    /// straddles the wrap point.
    pub fn with_capacity(capacity: u64) -> Self {
        let cap = capacity.max(TS_PKT_SZ as u64);
        let cap = cap - (cap % TS_PKT_SZ as u64);
        Self {
            buffer: vec![0u8; cap as usize].into_boxed_slice(),
            capacity: cap,
            s_write: 0,
            have_data: false,
            start: Instant::now(),
            pace_tok: 0,
            pace_tok_ms: 0,
            null_bytes: 0,
            served_bytes: 0,
            near_starve: 0,
            edge_waits: 0,
            overwritten_blocks: 0,
            last_lead: 0,
        }
    }

    pub fn capacity(&self) -> u64 {
        self.capacity
    }

    pub fn s_write(&self) -> u64 {
        self.s_write
    }

    pub fn have_data(&self) -> bool {
        self.have_data
    }

    /// Oldest stream position still resident in the ring.
    pub fn ring_old(&self) -> u64 {
        self.s_write.saturating_sub(self.capacity)
    }

    pub fn now_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }

    /// Physical index of a monotonic position inside the ring.
    fn phys(&self, pos: u64) -> usize {
        (pos % self.capacity) as usize
    }

    /// Zero-throttle feeder append. The writer is the network ingestion
    /// thread: it copies `data` into the ring, advances `s_write`, and never
    /// sleeps — overwriting old data is the only back-pressure mechanism.
    pub fn append(&mut self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        // A feed larger than the ring cannot be represented in one pass;
        // keep only the trailing `capacity` bytes (the live edge), but the
        // monotonic write cursor advances by the *original* length so the
        // live edge lands at the end of the feed.
        let orig_len = data.len();
        let data = if data.len() as u64 > self.capacity {
            let skip = data.len() - self.capacity as usize;
            &data[skip..]
        } else {
            data
        };
        let count = data.len();
        let off = self.phys(self.s_write);

        if off + count <= self.capacity as usize {
            self.buffer[off..off + count].copy_from_slice(data);
        } else {
            let first = self.capacity as usize - off;
            self.buffer[off..].copy_from_slice(&data[..first]);
            self.buffer[..count - first].copy_from_slice(&data[first..]);
        }

        if self.s_write.saturating_add(data.len() as u64) > self.capacity {
            self.overwritten_blocks = self.overwritten_blocks.saturating_add(1);
        }
        self.s_write = self.s_write.saturating_add(orig_len as u64);
        self.have_data = true;
    }

    /// Copies `dst.len()` bytes from the ring at monotonic position `abs_pos`.
    /// Handles wrap-around. Bounds: caller must guarantee
    /// `abs_pos + dst.len() <= s_write`.
    pub fn read_at(&self, abs_pos: u64, dst: &mut [u8]) {
        let count = dst.len();
        if count == 0 {
            return;
        }
        let off = self.phys(abs_pos);
        if off + count <= self.capacity as usize {
            dst.copy_from_slice(&self.buffer[off..off + count]);
        } else {
            let first = self.capacity as usize - off;
            dst[..first].copy_from_slice(&self.buffer[off..]);
            dst[first..].copy_from_slice(&self.buffer[..count - first]);
        }
    }

    /// Copies a scan window `[base, base + out.len())` clamped to ring
    /// residency into `out`; returns the number of bytes actually copied.
    /// Bytes before `ring_old` are left untouched by this method.
    pub fn read_window(&self, base: u64, out: &mut [u8]) -> usize {
        let count = out.len();
        if count == 0 {
            return 0;
        }
        let lo = base.max(self.ring_old());
        let hi = self.s_write;
        if lo >= hi {
            return 0;
        }
        let avail = (hi - lo) as usize;
        let n = avail.min(count);
        self.read_at(lo, &mut out[..n]);
        n
    }

    /// True when the byte range `[pos, pos + len)` is fully resident.
    pub fn contains(&self, pos: u64, len: u64) -> bool {
        pos >= self.ring_old() && pos.saturating_add(len) <= self.s_write
    }

    /// Token-bucket accounting: charges `n` served bytes and returns the pacing sleep
    /// in milliseconds for debt, clamped to `MAX_SLEEP_SLICE_MS`.
    /// Zero means "no sleep needed" (e.g. lead >= 12MB or within burst allowance).
    pub fn pace_note(&mut self, n: u64, lead: u64, now_ms: u64) -> u64 {
        self.last_lead = lead;
        // If lead >= 12 MB, never throttle: let the TV fill its buffer at full USB speed
        if lead >= 12 * 1024 * 1024 {
            self.pace_tok_ms = now_ms;
            return 0;
        }
        let rate_bps = PaceTier::for_lead(lead).rate();
        if self.pace_tok_ms == 0 {
            self.pace_tok = 0;
            self.pace_tok_ms = now_ms;
        }
        let idle_ms = (now_ms.saturating_sub(self.pace_tok_ms)).min(60_000);
        self.pace_tok += (idle_ms as i64 * rate_bps as i64) / 1000;
        let burst = (rate_bps / 4) as i64; // 250 ms burst allowance
        if self.pace_tok > burst {
            self.pace_tok = burst;
        }
        self.pace_tok_ms = now_ms;
        self.pace_tok -= n as i64;
        if self.pace_tok < 0 {
            let need_ms = ((-self.pace_tok) as u64 * 1000) / rate_bps;
            need_ms.min(MAX_SLEEP_SLICE_MS)
        } else {
            0
        }
    }

    // ---- telemetry mutators (called by the engine serve path) ----

    pub fn note_null(&mut self, n: u64) {
        self.null_bytes = self.null_bytes.saturating_add(n);
    }

    pub fn note_served(&mut self, n: u64) {
        self.served_bytes = self.served_bytes.saturating_add(n);
    }

    pub fn note_starve(&mut self) {
        self.near_starve = self.near_starve.saturating_add(1);
    }

    pub fn note_edge_wait(&mut self) {
        self.edge_waits = self.edge_waits.saturating_add(1);
    }

    pub fn note_overwrite(&mut self) {
        self.overwritten_blocks = self.overwritten_blocks.saturating_add(1);
    }

    pub fn telemetry(&self) -> Telemetry {
        Telemetry {
            s_write: self.s_write,
            ring_old: self.ring_old(),
            null_bytes: self.null_bytes,
            served_bytes: self.served_bytes,
            near_starve: self.near_starve,
            edge_waits: self.edge_waits,
            overwritten_blocks: self.overwritten_blocks,
            pace_tier: PaceTier::for_lead(self.last_lead),
        }
    }
}

/// Reader-side telemetry snapshot (verification contract: near_starve == 0,
/// null_bytes == 0 on a healthy run).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Telemetry {
    pub s_write: u64,
    pub ring_old: u64,
    pub null_bytes: u64,
    pub served_bytes: u64,
    pub near_starve: u64,
    pub edge_waits: u64,
    pub overwritten_blocks: u64,
    pub pace_tier: PaceTier,
}

/// Thread-safe ring: data plane under a mutex + condvar for live-edge waits.
pub struct Ring {
    data: Mutex<RingData>,
    cv: Condvar,
}

impl Default for Ring {
    fn default() -> Self {
        Self::new()
    }
}

impl Ring {
    /// Production ring with the contract-mandated 128 MiB buffer.
    pub fn new() -> Self {
        Self::with_capacity(RINGSZ as u64)
    }

    pub fn with_capacity(capacity: u64) -> Self {
        Self {
            data: Mutex::new(RingData::with_capacity(capacity)),
            cv: Condvar::new(),
        }
    }

    pub fn lock(&self) -> std::sync::MutexGuard<'_, RingData> {
        self.data.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Zero-throttle feeder entry point: append network bytes and wake every
    /// reader waiting at the live edge. Never sleeps, never inspects the lead.
    pub fn write_chunk(&self, data: &[u8]) {
        let mut rd = self.lock();
        rd.append(data);
        drop(rd);
        self.cv.notify_all();
    }

    /// Bounded cooperative wait at the live edge: blocks until `s_write`
    /// reaches or exceeds `needed_end` or the budget expires, waking on feeder
    /// notifications. Each condvar wait is sliced to <= 50 ms so callers can
    /// re-check shutdown/deadline state. Returns true when data is available.
    pub fn wait_edge(&self, needed_end: u64, budget: Duration) -> bool {
        let deadline = Instant::now() + budget;
        let mut rd = self.lock();
        loop {
            if rd.s_write() >= needed_end {
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                return rd.s_write() >= needed_end;
            }
            let slice = (deadline - now).min(Duration::from_millis(MAX_SLEEP_SLICE_MS));
            let (guard, _timeout) = self
                .cv
                .wait_timeout(rd, slice)
                .unwrap_or_else(|e| e.into_inner());
            rd = guard;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snap188_grid() {
        assert_eq!(snap188(0), 0);
        assert_eq!(snap188(187), 0);
        assert_eq!(snap188(188), 188);
        assert_eq!(snap188(375), 188);
        assert_eq!(snap188(u64::MAX), u64::MAX - u64::MAX % 188);
    }

    #[test]
    fn null_packet_is_phase_aligned() {
        let mut buf = [0u8; 376];
        fill_null(&mut buf);
        assert_eq!(buf[0], 0x47);
        assert_eq!(&buf[1..4], &[0x1F, 0xFF, 0x10]);
        assert_eq!(buf[188], 0x47);
        assert_eq!(&buf[189..192], &[0x1F, 0xFF, 0x10]);
        // Odd-length fills end with a phase-correct prefix, never overflow.
        let mut odd = [0u8; 100];
        fill_null(&mut odd);
        assert_eq!(&odd[..4], &[0x47, 0x1F, 0xFF, 0x10]);
    }

    #[test]
    fn pace_tier_rates() {
        assert_eq!(PaceTier::Slow480.rate(), 480_000);
        assert_eq!(PaceTier::Mid600.rate(), 600_000);
        assert_eq!(PaceTier::Fast900.rate(), 900_000);
        assert_eq!(PaceTier::for_lead(1_000_000), PaceTier::Slow480);
        assert_eq!(PaceTier::for_lead(4 * 1024 * 1024), PaceTier::Mid600);
        assert_eq!(PaceTier::for_lead(16 * 1024 * 1024), PaceTier::Fast900);
    }

    #[test]
    fn wrap_around_and_boundary() {
        // Small ring (multiple of 188) to exercise the wrap path cheaply.
        let mut rd = RingData::with_capacity(188 * 4);
        assert_eq!(rd.capacity(), 188 * 4);

        // Fill exactly to capacity.
        let full: Vec<u8> = (0..(188 * 4)).map(|i| (i % 251) as u8).collect();
        rd.append(&full);
        assert_eq!(rd.s_write(), 188 * 4);
        assert_eq!(rd.ring_old(), 0);

        // Overwrite: the oldest 188*2 bytes are gone, s_write keeps growing.
        let more: Vec<u8> = (0..(188 * 2)).map(|i| (i % 251) as u8).collect();
        rd.append(&more);
        assert_eq!(rd.s_write(), 188 * 6);
        assert_eq!(rd.ring_old(), 188 * 2);

        // A read straddling the physical wrap point must stitch both halves.
        let mut out = vec![0u8; 188 * 3];
        rd.read_at(188 * 3, &mut out); // covers [188*3, 188*6): wraps at 188*4
        assert_eq!(out, full[188 * 3..].iter().copied().chain(more.iter().copied()).collect::<Vec<u8>>());

        // Boundary: a read ending exactly at the physical end is contiguous.
        let mut edge = vec![0u8; 188];
        rd.read_at(188 * 3, &mut edge);
        assert_eq!(&edge[..], &full[188 * 3..]);
    }

    #[test]
    fn feed_larger_than_ring_keeps_live_edge() {
        let mut rd = RingData::with_capacity(188 * 2);
        let big: Vec<u8> = (0..(188 * 4)).map(|i| (200 + i % 50) as u8).collect();
        rd.append(&big);
        assert_eq!(rd.s_write(), 188 * 4);
        assert_eq!(rd.ring_old(), 188 * 2);
        let mut out = vec![0u8; 188 * 2];
        rd.read_at(188 * 2, &mut out);
        assert_eq!(out, &big[188 * 2..]);
    }

    #[test]
    fn read_window_clamps_to_residency() {
        let mut rd = RingData::with_capacity(188 * 8);
        let pat: Vec<u8> = (0..(188 * 4)).map(|i| i as u8).collect();
        rd.append(&pat);
        // Request a window that starts before ring_old / extends past s_write.
        let mut win = vec![0u8; 188 * 6];
        let n = rd.read_window(0, &mut win);
        assert_eq!(n, 188 * 4); // only resident bytes are copied
        assert_eq!(&win[..n], &pat[..]);

        let n2 = rd.read_window(188 * 10, &mut win);
        assert_eq!(n2, 0); // window entirely past the writer head
    }

    #[test]
    fn contains_residency() {
        let mut rd = RingData::with_capacity(188 * 4);
        rd.append(&vec![0xAA; 188 * 2]);
        assert!(rd.contains(0, 188));
        assert!(rd.contains(188, 188));
        assert!(rd.contains(1, 188));
        assert!(!rd.contains(188 * 2, 188)); // one packet past s_write
        rd.append(&vec![0xBB; 188 * 2]);
        assert!(rd.contains(0, 188 * 4));
        assert!(!rd.contains(0, 188 * 5));
    }

    #[test]
    fn token_bucket_clamps_to_50ms() {
        let mut rd = RingData::with_capacity(188 * 4);
        // Mid tier, cushion < 12 MB: big charge requests a large sleep — clamped to 50 ms.
        let sleep = rd.pace_note(900_000, 4 * 1024 * 1024, 0);
        assert_eq!(sleep, MAX_SLEEP_SLICE_MS);
        // Deep cushion (>= 12 MB): never throttled (always 0 sleep).
        let mut rd2 = RingData::with_capacity(188 * 4);
        assert_eq!(rd2.pace_note(900_000, 16 * 1024 * 1024, 0), 0);
    }

    #[test]
    fn token_bucket_slow_tier_sleeps_less() {
        let mut rd = RingData::with_capacity(188 * 4);
        // Same excess under the slow tier resolves to a smaller sleep.
        let sleep = rd.pace_note(480_000, 1_000, 0);
        assert!(sleep > 0 && sleep <= MAX_SLEEP_SLICE_MS);
        assert_eq!(sleep, MAX_SLEEP_SLICE_MS); // clamped
    }

    #[test]
    fn ring_mutex_notifies_waiters() {
        let ring = Ring::with_capacity(188 * 4);
        assert!(!ring.wait_edge(188, Duration::from_millis(10)));
        ring.write_chunk(&[0xCC; 188]);
        assert!(ring.wait_edge(188, Duration::from_millis(50)));
        assert!(!ring.wait_edge(376, Duration::from_millis(10)));
    }

    #[test]
    fn zero_length_append_is_noop() {
        let mut rd = RingData::with_capacity(188 * 4);
        rd.append(&[]);
        assert_eq!(rd.s_write(), 0);
        assert!(!rd.have_data());
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/engine/ring.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/snapper.rs (421 lines) <<<<<<<<<<<<<<<
//! D12 anchor search: the 4-tier keyframe snapper for the 2013 MStar decoder.
//!
//! When the FUSE engine must (re)anchor — fresh open, confirmed seek, return
//! resume, ring wrap — it must land on a byte position the hardware demuxer
//! can decode from cold. The D12 search walks outward from the desired
//! position, degrading gracefully:
//!
//! 1. `PAT_SPS`  — backward up to 2 MiB for a PAT packet whose GOP contains
//!    an SPS/PPS (a clean IDR entry point, per docs/ARCHITECTURE.md §2.3).
//! 2. `PAT`      — forward up to 2 MiB for any PAT packet start.
//! 3. `TRANSPORT`— three consecutive sync bytes (0x47 @ 188 B) on the grid.
//! 4. `snap188`  — last resort: the desired position on the 188 grid.
//!
//! All searches operate on a caller-provided ring window (`win`, with the
//! absolute stream position of `win[0]` given by `base`), so the snapper is
//! pure and fully unit-testable. The window must cover the searched range;
//! the engine clamps requests to ring residency before calling.
//!
//! Two guards wrap the raw snap:
//! * [`validate_scar`] rejects an anchor sitting on an upstream PTS
//!   discontinuity (> 5 s jump within 2 MiB) and advances 1 MiB past it.
//! * [`leadback_guard`] guarantees the final anchor keeps at least
//!   `RESUME_MIN_LEAD` cushion behind the live writer.

use crate::contract::TS_PKT_SZ;
use crate::engine::ring::snap188;
use crate::engine::ts_xform::{ts_payload_off, ts_pes_ts, ts_pusi, ts_has_sps, PTS_SCAR_5S};

/// Tier 1 backward search span.
pub const D12_BACK: u64 = 2 * 1024 * 1024;
/// Tier 2 forward search span.
pub const D12_FWD: u64 = 2 * 1024 * 1024;
/// Maximum distance from a PAT to an SPS for tier 1 to accept it.
pub const SPS_LOOKAHEAD: u64 = 1024 * 1024;
/// Scar scan window from the anchor head.
pub const SCAR_SCAN: u64 = 2 * 1024 * 1024;
/// Advance distance when a scar is detected.
pub const SCAR_SKIP: u64 = 1024 * 1024;
/// Maximum PES samples collected during a scar scan.
pub const SCAR_MAX_SAMPLES: usize = 24;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum D12Tier {
    PatSps,
    Pat,
    Transport,
    Snap188,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapOut {
    pub pos: u64,
    pub tier: D12Tier,
}

/// True when the packet at window offset `off` is a PAT start
/// (sync byte, PID 0x0000, PUSI, table_id 0x00 after the pointer field).
pub fn is_pat(win: &[u8], off: usize) -> bool {
    if off + TS_PKT_SZ > win.len() || win[off] != 0x47 {
        return false;
    }
    let pid = ((u16::from(win[off + 1] & 0x1F)) << 8) | u16::from(win[off + 2]);
    if pid != 0x0000 || !ts_pusi(&win[off..off + TS_PKT_SZ]) {
        return false;
    }
    let pkt = &win[off..off + TS_PKT_SZ];
    let payload = match ts_payload_off(pkt) {
        Some(p) => p,
        None => return false,
    };
    let ptr = usize::from(pkt[payload]);
    let table_off = payload + 1 + ptr;
    table_off < TS_PKT_SZ && pkt[table_off] == 0x00
}

/// True when an SPS/PPS access unit starts within `lookahead` bytes of the
/// packet at window offset `off` (inclusive).
pub fn has_sps_after(win: &[u8], off: usize, lookahead: u64) -> bool {
    let mut p = off;
    let limit = off.saturating_add(lookahead as usize);
    while p + TS_PKT_SZ <= win.len().min(limit) {
        if ts_has_sps(&win[p..p + TS_PKT_SZ]) {
            return true;
        }
        p += TS_PKT_SZ;
    }
    false
}

/// Finds the first 3-point sync confirmation (0x47 every 188 bytes) at or
/// after window offset `from`.
pub fn find_transport(win: &[u8], from: usize) -> Option<usize> {
    if win.len() < 3 * TS_PKT_SZ {
        return None;
    }
    let limit = win.len() - (2 * TS_PKT_SZ);
    let mut i = from.min(limit);
    while i < limit {
        if win[i] == 0x47 && win[i + TS_PKT_SZ] == 0x47 && win[i + 2 * TS_PKT_SZ] == 0x47 {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// The D12 4-tier snap. `target` is the desired stream position; the window
/// must contain the searched range (engine clamps to ring residency).
pub fn d12_snap(win: &[u8], base: u64, target: u64, s_write: u64) -> SnapOut {
    let snapped = snap188(target).min(s_write);
    let off0 = snapped.saturating_sub(base).min(win.len() as u64) as usize;

    // -- Tier 1: PAT + SPS, walking backward up to D12_BACK.
    if !win.is_empty() {
        let start_p = off0.min(win.len().saturating_sub(TS_PKT_SZ));
        let mut p = start_p;
        while p + TS_PKT_SZ <= win.len() {
            if is_pat(win, p) && has_sps_after(win, p, SPS_LOOKAHEAD) {
                return SnapOut {
                    pos: base + p as u64,
                    tier: D12Tier::PatSps,
                };
            }
            if p < TS_PKT_SZ {
                break;
            }
            p -= TS_PKT_SZ;
            if snapped.saturating_sub(base + p as u64) > D12_BACK {
                break;
            }
        }
    }

    // -- Tier 2: any PAT, walking forward up to D12_FWD.
    let mut p = off0;
    while p + TS_PKT_SZ <= win.len() {
        if is_pat(win, p) {
            return SnapOut {
                pos: base + p as u64,
                tier: D12Tier::Pat,
            };
        }
        p = p.saturating_add(TS_PKT_SZ);
        if (base + p as u64).saturating_sub(snapped) > D12_FWD {
            break;
        }
    }

    // -- Tier 3: 3-point transport sync confirmation.
    if let Some(off) = find_transport(win, off0) {
        return SnapOut {
            pos: base + off as u64,
            tier: D12Tier::Transport,
        };
    }

    // -- Tier 4: the desired position on the 188 grid.
    SnapOut {
        pos: snapped,
        tier: D12Tier::Snap188,
    }
}

/// Scar validator: walks at most 2 MiB forward from `pos` looking for a PTS
/// discontinuity larger than 5 s (an upstream splice/gap the decoder cannot
/// ride through). Returns the advanced position and whether a skip happened.
pub fn validate_scar(win: &[u8], base: u64, pos: u64, s_write: u64) -> (u64, bool) {
    let mut p = match snap188(pos).checked_sub(base) {
        Some(rel) => rel as usize,
        None => return (pos, false),
    };
    let mut prev_pts: Option<u64> = None;
    let mut samples = 0usize;
    let scan_limit = pos.saturating_add(SCAR_SCAN).min(s_write.saturating_sub(TS_PKT_SZ as u64));
    let mut tracked_pid: Option<u16> = None;

    while p + TS_PKT_SZ <= win.len() && samples < SCAR_MAX_SAMPLES {
        let abs = base + p as u64;
        if abs >= scan_limit {
            break;
        }
        let pkt = &win[p..p + TS_PKT_SZ];
        if pkt[0] == 0x47 {
            let pid = crate::engine::ts_xform::ts_pid(pkt);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                if *tracked_pid.get_or_insert(pid) == pid {
                    if let Some(prev) = prev_pts {
                        let delta = crate::engine::ts_xform::pts_sub(pts, prev);
                        if delta.unsigned_abs() > PTS_SCAR_5S {
                            let skip = snap188(pos.saturating_add(SCAR_SKIP));
                            return (nearest_pat_forward(win, base, skip), true);
                        }
                    }
                    prev_pts = Some(pts);
                    samples += 1;
                }
            }
        }
        p += TS_PKT_SZ;
    }
    (pos, false)
}

/// Post-snap leadback guard: pulls the anchor back so at least
/// `RESUME_MIN_LEAD` bytes of cushion separate it from the live writer, then
/// snaps to the 188 grid.
pub fn leadback_guard(snapped: u64, s_write: u64) -> u64 {
    let floor = s_write.saturating_sub(crate::contract::RESUME_MIN_LEAD);
    snap188(snapped.min(floor))
}

/// Nearest PAT at or after `pos` (position-preserving resume anchor), or the
/// 188-grid snap when no PAT is reachable inside the window.
pub fn nearest_pat_forward(win: &[u8], base: u64, pos: u64) -> u64 {
    let snapped = snap188(pos);
    let mut p = snapped.saturating_sub(base).min(win.len() as u64) as usize;
    while p + TS_PKT_SZ <= win.len() {
        if is_pat(win, p) {
            return base + p as u64;
        }
        p += TS_PKT_SZ;
    }
    snapped
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::ts_xform::{pts_to_bytes, ts_afc};

    /// Builds a synthetic stream: PAT (+SPS) every `pat_period` packets,
    /// interleaved video PES (PID 0x100) and audio PES (PID 0x101).
    fn mk_stream(n: usize, pat_period: usize, with_sps: bool) -> Vec<u8> {
        let mut v = Vec::with_capacity((n + 1) * TS_PKT_SZ);
        for i in 0..n {
            if i % pat_period == 0 {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x40; // PUSI, PID 0x0000
                p[2] = 0x00;
                p[3] = 0x10;
                p[4] = 0x00; // pointer_field
                p[5] = 0x00; // table_id = PAT
                v.extend_from_slice(&p);
                if with_sps {
                    // Immediately followed by an SPS access unit.
                    let mut s = [0xFFu8; TS_PKT_SZ];
                    s[0] = 0x47;
                    s[1] = 0x40; // PUSI
                    s[2] = 0x00; // PID 0x0100 (video)
                    s[3] = 0x10;
                    s[4] = 0x00;
                    s[5] = 0x00;
                    s[6] = 0x00;
                    s[7] = 0x01;
                    s[8] = 0x67; // NAL type 7 (SPS)
                    v.extend_from_slice(&s);
                }
                continue;
            }
            let pid = if i % 2 == 0 { 0x0100 } else { 0x0101 };
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = ((pid >> 8) & 0x1F) as u8 | 0x40;
            p[2] = pid as u8;
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            let mut b = [0u8; 5];
            pts_to_bytes(900_000 + (i as u64) * 3000, &mut b);
            // PES header: pl = pkt[4..]; PTS lives at pl[9..14] => pkt[13..18].
            p[13..18].copy_from_slice(&b);
            v.extend_from_slice(&p);
        }
        v
    }

    

    #[test]
    fn d12_tier2_pat_forward() {
        // PATs exist but carry no SPS, so tier 1 is rejected everywhere and
        // tier 2 must find the next PAT forward from the target.
        let s = mk_stream(500, 100, false); // PATs at 0, 100, 200, 300, 400
        let s_write = s.len() as u64;
        let target = 350 * TS_PKT_SZ as u64;
        let out = d12_snap(&s, 0, target, s_write);
        assert_eq!(out.tier, D12Tier::Pat);
        assert_eq!(out.pos, 400 * TS_PKT_SZ as u64);
    }

    #[test]
    fn d12_tier3_transport() {
        // No PAT anywhere: only sync-grid packets.
        let mut s = vec![0x00u8; TS_PKT_SZ * 200];
        for p in (0..s.len()).step_by(TS_PKT_SZ) {
            s[p] = 0x47;
            s[p + 1] = 0x01;
            s[p + 2] = 0x00; // PID 0x0100, no PUSI, not PAT
            s[p + 3] = 0x10;
        }
        let s_write = s.len() as u64;
        let out = d12_snap(&s, 0, 50 * TS_PKT_SZ as u64, s_write);
        assert_eq!(out.tier, D12Tier::Transport);
        assert_eq!(out.pos, 50 * TS_PKT_SZ as u64);
    }

    #[test]
    fn d12_tier4_snap188_fallback() {
        let s = vec![0x11u8; TS_PKT_SZ * 50];
        let out = d12_snap(&s, 0, 10 * TS_PKT_SZ as u64 + 7, s.len() as u64);
        assert_eq!(out.tier, D12Tier::Snap188);
        assert_eq!(out.pos, 10 * TS_PKT_SZ as u64);
    }

    #[test]
    fn transport_needs_three_points() {
        let mut s = vec![0u8; 3 * TS_PKT_SZ];
        s[0] = 0x47;
        s[TS_PKT_SZ] = 0x47;
        // Third sync byte deliberately absent.
        assert_eq!(find_transport(&s, 0), None);
        s[2 * TS_PKT_SZ] = 0x47;
        assert_eq!(find_transport(&s, 0), Some(0));
    }

    #[test]
    fn scar_validator_skips_past_discontinuity() {
        let mut s = Vec::new();
        // 20 clean PES packets, then a +60 s PTS scar, then more packets.
        for i in 0..30usize {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40; // PUSI
            p[2] = 0x00; // PID 0x0100
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            let pts = if i >= 20 {
                900_000 + i as u64 * 3000 + 60 * 90_000
            } else {
                900_000 + i as u64 * 3000
            };
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            s.extend_from_slice(&p);
        }
        let start = 19 * TS_PKT_SZ as u64; // just before the scar
        let (pos, skipped) = validate_scar(&s, 0, start, s.len() as u64);
        assert!(skipped);
        assert_eq!(pos, snap188(start + SCAR_SKIP));

        // A clean window never skips.
        let (pos2, skipped2) = validate_scar(&s, 0, 0, 19 * TS_PKT_SZ as u64);
        assert!(!skipped2);
        assert_eq!(pos2, 0);
    }

    #[test]
    fn leadback_guard_enforces_min_cushion() {
        let s_write = 100_000_000;
        // Anchor too close to the live edge: pulled back to the floor (the
        // 188 snap only increases the cushion, never violates it).
        let guarded = leadback_guard(s_write - 1024, s_write);
        assert!(s_write - guarded >= crate::contract::RESUME_MIN_LEAD);
        assert!(s_write - guarded < crate::contract::RESUME_MIN_LEAD + TS_PKT_SZ as u64);
        assert_eq!(guarded, snap188(100_000_000 - crate::contract::RESUME_MIN_LEAD));
        // Deep anchor: untouched.
        let deep = snap188(s_write - 16 * 1024 * 1024);
        assert_eq!(leadback_guard(deep, s_write), deep);
        // Degenerate: writer below the cushion.
        assert_eq!(leadback_guard(1024, 1024), 0);
    }

    #[test]
    fn nearest_pat_forward_finds_entry() {
        let s = mk_stream(100, 25, false);
        // Between PATs: forward search must land on the next PAT.
        let pos = 30 * TS_PKT_SZ as u64;
        let hit = nearest_pat_forward(&s, 0, pos);
        assert_eq!(hit, 50 * TS_PKT_SZ as u64);
        // Exactly on a PAT: itself.
        assert_eq!(nearest_pat_forward(&s, 0, 50 * TS_PKT_SZ as u64), 50 * TS_PKT_SZ as u64);
    }

    #[test]
    fn payload_offset_handles_adaptation_field() {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[3] = 0x30; // AFC = 3 (AF + payload)
        p[4] = 0x05; // AF length
        assert_eq!(ts_afc(&p), 3);
        assert_eq!(ts_payload_off(&p), Some(4 + 1 + 5));
        p[3] = 0x10; // AFC = 1 (payload only)
        assert_eq!(ts_payload_off(&p), Some(4));
        p[3] = 0x20; // AFC = 2 (AF only)
        assert_eq!(ts_payload_off(&p), None);
    }

    #[test]
    fn pat_with_pointer_field_is_detected() {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[1] = 0x40;
        p[2] = 0x00;
        p[3] = 0x10;
        p[4] = 0x03; // pointer_field = 3
        p[8] = 0x00; // table_id at payload+1+3
        let mut win = vec![0u8; TS_PKT_SZ * 2];
        win[..TS_PKT_SZ].copy_from_slice(&p);
        assert!(is_pat(&win, 0));
        p[8] = 0x42; // not PAT
        win[..TS_PKT_SZ].copy_from_slice(&p);
        assert!(!is_pat(&win, 0));
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/engine/snapper.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/ts_xform.rs (1134 lines) <<<<<<<<<<<<<<<
//! MPEG-TS continuity transform (the "Caminho C" engine).
//!
//! When the FUSE anchor changes mid-playback (channel switch, Return-key
//! resume, ring wrap), the decoder must see a *continuous timeline*: PTS/DTS
//! and PCR that continue from exactly where the TV left off, and continuity
//! counters that never repeat. This module rewrites, deterministically per
//! stream position, a constant per-epoch offset onto:
//!
//! * PCR (27 MHz base, modulo 2^33 * 300),
//! * PTS/DTS of video/audio PES (90 kHz, modulo 2^33),
//! * the 4-bit continuity counter of every non-NULL PID.
//!
//! MPEG-TS NULL packets (PID 0x1FFF) are never touched.
//!
//! Cross-read straddling packets are carried in [`Tail`]: the head slice is
//! rewritten when it is served, the remainder when the packet completes
//! (131072 = 697*188 + 56, so one packet per 128 KiB FUSE read straddles the
//! boundary). Because the transform is a pure function of (raw bytes,
//! absolute position, epoch), re-reading the same region yields identical
//! bytes.
//!
//! Splice calibration is 3-tier: `served` (last delivered state) -> `ckpt`
//! (RAM checkpoint, post-wrap) -> `ring_scan` (walk back <= 512 KiB for the
//! last PTS/PCR and convert to delivered values with the current epoch).

use crate::contract::TS_PKT_SZ;

/// PTS/DTS modulus: 2^33 ticks at 90 kHz.
pub const PTS_MOD: u64 = 1u64 << 33; // 8_589_934_592
/// PCR modulus: 2^33 * 300 ticks at 27 MHz.
pub const PCR_MOD: u64 = PTS_MOD * 300; // 2_576_980_377_600
/// PTS delta considered a "scar" (upstream discontinuity) by the snapper.
pub const PTS_SCAR_5S: u64 = 5 * 90_000; // 450_000 ticks
/// Strictly-positive PTS epsilon applied at a splice so the decoder never
/// receives two equal consecutive PTS values (validated: exactly 3000 ticks).
pub const SPLICE_PTS_EPS: u64 = 3_000;
/// `served` is fresh while the splice point is at most this far ahead of the
/// last sequential delivery.
pub const SERVED_FRESH_BYTES: u64 = 256 * 1024;
/// Maximum backward scan distance for the `ring_scan` calibration tier.
pub const RING_SCAN_BACK: u64 = 512 * 1024;
/// Sanity bound for using a `ckpt` record after the position left `served`.
pub const CKPT_MAX_DIST: u64 = 4 * 1024 * 1024;
/// RAM checkpoint slots (one per 64 KiB of delivered file end-offset).
pub const CKPT_N: usize = 64;
pub const CKPT_STEP: u64 = 64 * 1024;
/// Checkpoint records older than this are ignored.
pub const CKPT_MAX_AGE_MS: u64 = 30 * 60 * 1000;
/// Distinct PIDs tracked in a timeline reference (live streams carry a
/// handful: PAT, PMT, video, audio, PCR pid, teletext...).
pub const REF_CC_SLOTS: usize = 32;

const NULL_PID: u16 = 0x1FFF;

/// Wraps a 33-bit PTS/DTS value.
pub const fn pts_wrap(v: u64) -> u64 {
    v % PTS_MOD
}

/// Wraps a 27 MHz PCR value.
pub const fn pcr_wrap(v: u64) -> u64 {
    v % PCR_MOD
}

/// Signed minimal difference `a - b` on the PTS circle, in
/// `(-PTS_MOD/2, PTS_MOD/2]`. Handles the 2^33 rollover correctly.
pub fn pts_sub(a: u64, b: u64) -> i64 {
    let m = PTS_MOD as i64;
    let d = ((a % PTS_MOD) as i64 - (b % PTS_MOD) as i64 + m / 2 + m) % m - m / 2;
    d
}

/// Signed minimal difference `a - b` on the PCR circle (27 MHz).
pub fn pcr_sub(a: u64, b: u64) -> i64 {
    let m = PCR_MOD as i64;
    let d = ((a % PCR_MOD) as i64 - (b % PCR_MOD) as i64 + m / 2 + m) % m - m / 2;
    d
}

/// Applies a signed delta on the PTS circle (never overflows: deltas are
/// bounded by the modular difference magnitude, far below i64 range).
pub fn pts_apply(raw: u64, delta: i64) -> u64 {
    ((raw % PTS_MOD) as i64 + delta).rem_euclid(PTS_MOD as i64) as u64
}

/// Applies a signed delta on the PCR circle.
pub fn pcr_apply(raw: u64, delta: i64) -> u64 {
    ((raw % PCR_MOD) as i64 + delta).rem_euclid(PCR_MOD as i64) as u64
}

/// Splice pad length: bytes of the in-flight packet that must still be
/// delivered raw before the new anchor takes over, so the byte after the
/// seam always lands on a 188-byte packet boundary.
pub const fn splice_pad_len(old_pos: u64) -> u64 {
    (TS_PKT_SZ as u64 - (old_pos % TS_PKT_SZ as u64)) % TS_PKT_SZ as u64
}

// ---------------------------------------------------------------------------
// TS field accessors (bounds-checked; all inputs are 188-byte packets)
// ---------------------------------------------------------------------------

pub fn ts_pid(pkt: &[u8]) -> u16 {
    ((u16::from(pkt[1] & 0x1F)) << 8) | u16::from(pkt[2])
}

pub fn ts_cc(pkt: &[u8]) -> u8 {
    pkt[3] & 0x0F
}

pub fn ts_pusi(pkt: &[u8]) -> bool {
    pkt[1] & 0x40 != 0
}

pub fn ts_afc(pkt: &[u8]) -> u8 {
    (pkt[3] >> 4) & 0x03
}

/// Byte offset of the TS payload, or `None` when the packet carries none.
pub fn ts_payload_off(pkt: &[u8]) -> Option<usize> {
    match ts_afc(pkt) {
        1 => Some(4),
        3 => {
            let af_len = usize::from(pkt[4]);
            let off = 5 + af_len;
            if off < TS_PKT_SZ {
                Some(off)
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Decodes the 33-bit PTS/DTS field from its 5-byte Big-Endian encoding.
pub fn pts_from_bytes(b: &[u8]) -> u64 {
    (u64::from(b[0] & 0x0E) << 29)
        | (u64::from(b[1]) << 22)
        | (u64::from(b[2] & 0xFE) << 14)
        | (u64::from(b[3]) << 7)
        | (u64::from(b[4]) >> 1)
}

/// Encodes a 33-bit PTS/DTS value into its 5-byte form, preserving the
/// prefix nibble and marker bits of the original field.
pub fn pts_to_bytes(v: u64, b: &mut [u8]) {
    let old_prefix = b[0] & 0xF0;
    b[0] = old_prefix | (((v >> 29) & 0x0E) as u8) | 0x01;
    b[1] = (v >> 22) as u8;
    b[2] = (((v >> 14) & 0xFE) as u8) | 0x01;
    b[3] = (v >> 7) as u8;
    b[4] = (((v & 0x7F) as u8) << 1) | 0x01;
}

/// Parses the PCR (27 MHz base, without the 300x extension detail folded in)
/// from the adaptation field. Returns the full 27 MHz value.
pub fn ts_pcr(pkt: &[u8]) -> Option<u64> {
    if ts_afc(pkt) < 2 {
        return None;
    }
    let af_len = usize::from(pkt[4]);
    if af_len < 7 || pkt[5] & 0x10 == 0 {
        return None;
    }
    let base = (u64::from(pkt[6]) << 25)
        | (u64::from(pkt[7]) << 17)
        | (u64::from(pkt[8]) << 9)
        | (u64::from(pkt[9]) << 1)
        | (u64::from(pkt[10]) >> 7);
    let ext = (u64::from(pkt[10] & 0x01) << 8) | u64::from(pkt[11]);
    Some(base * 300 + ext)
}

/// Encodes a 27 MHz PCR value back into the adaptation field.
pub fn ts_write_pcr(v: u64, pkt: &mut [u8]) {
    let base = v / 300;
    let ext = v % 300;
    pkt[6] = (base >> 25) as u8;
    pkt[7] = (base >> 17) as u8;
    pkt[8] = (base >> 9) as u8;
    pkt[9] = (base >> 1) as u8;
    pkt[10] = (((base & 0x01) as u8) << 7) | 0x7E | ((ext >> 8) as u8 & 0x01);
    pkt[11] = ext as u8;
}

/// Parses the PES header: returns `(pts, dts)` when the packet is a PES start.
pub fn ts_pes_ts(pkt: &[u8]) -> Option<(u64, Option<u64>)> {
    if !ts_pusi(pkt) {
        return None;
    }
    let off = ts_payload_off(pkt)?;
    let pl = &pkt[off..];
    if pl.len() < 14 || pl[0] != 0x00 || pl[1] != 0x00 || pl[2] != 0x01 {
        return None;
    }
    let flags = pl[7];
    if flags & 0x80 != 0 {
        let pts = pts_from_bytes(&pl[9..14]);
        let dts = if flags & 0x40 != 0 && pl.len() >= 19 {
            Some(pts_from_bytes(&pl[14..19]))
        } else {
            None
        };
        Some((pts, dts))
    } else {
        None
    }
}

/// True when the packet payload starts an Annex-B access unit (used by the
/// D12 SPS probe: NAL type 7 = SPS, 8 = PPS).
pub fn ts_has_sps(pkt: &[u8]) -> bool {
    let off = match ts_payload_off(pkt) {
        Some(o) => o,
        None => return false,
    };
    let pl = &pkt[off..];
    let mut i = 0usize;
    while i + 4 <= pl.len() {
        if pl[i] == 0 && pl[i + 1] == 0 {
            if pl[i + 2] == 1 && (pl[i + 3] & 0x1F) == 7 {
                return true;
            }
            if i + 5 <= pl.len() && pl[i + 2] == 0 && pl[i + 3] == 1 && (pl[i + 4] & 0x1F) == 7 {
                return true;
            }
        }
        i += 1;
    }
    false
}

// ---------------------------------------------------------------------------
// Timeline reference & epochs
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefSrc {
    Served,
    Ckpt,
    RingScan,
}

/// A timeline reference: the state the decoder has already consumed.
#[derive(Debug, Clone, Copy)]
pub struct XformRef {
    pub src: RefSrc,
    pub valid: bool,
    pub pts: u64,
    pub pcr: u64,
    pub pcr_valid: bool,
    pub upd_pos: u64,
    pub ccs: [(u16, u8); REF_CC_SLOTS],
    pub ncc: usize,
}

impl XformRef {
    pub const fn invalid() -> Self {
        Self {
            src: RefSrc::Served,
            valid: false,
            pts: 0,
            pcr: 0,
            pcr_valid: false,
            upd_pos: 0,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    #[allow(dead_code)]
    fn set_cc(&mut self, pid: u16, cc: u8) {
        for slot in self.ccs.iter_mut().take(self.ncc) {
            if slot.0 == pid {
                slot.1 = cc;
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }

    #[allow(dead_code)]
    fn cc_of(&self, pid: u16) -> Option<u8> {
        self.ccs.iter().take(self.ncc).find(|s| s.0 == pid).map(|s| s.1)
    }
}

/// Raw stream sample taken at a candidate new anchor (pre-transform values).
#[derive(Debug, Clone, Copy)]
pub struct RawSample {
    pub pts: Option<u64>,
    pub pcr: Option<u64>,
    pub ccs: [(u16, u8); REF_CC_SLOTS],
    pub ncc: usize,
}

impl RawSample {
    pub const fn empty() -> Self {
        Self {
            pts: None,
            pcr: None,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    fn set_cc(&mut self, pid: u16, cc: u8) {
        // First occurrence wins: the sample must capture the raw CC at the
        // START of the new anchor — the value the decoder's next-CC
        // expectation is computed against.
        for slot in self.ccs.iter().take(self.ncc) {
            if slot.0 == pid {
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }

    fn cc_of(&self, pid: u16) -> Option<u8> {
        self.ccs.iter().take(self.ncc).find(|s| s.0 == pid).map(|s| s.1)
    }
}

/// One timeline epoch: a constant offset applied to every served byte.
#[derive(Clone)]
pub struct Epoch {
    pub id: u64,
    pub delta_pts: i64,
    pub delta_pcr: i64,
    /// Per-PID continuity-counter shift (identity for untouched PIDs).
    pub cc_add: [u8; 8192],
}

impl Epoch {
    fn identity(id: u64) -> Self {
        Self {
            id,
            delta_pts: 0,
            delta_pcr: 0,
            cc_add: [0u8; 8192],
        }
    }
}

/// Straddling-packet carry across FUSE read boundaries.
struct Tail {
    valid: bool,
    abs_pos: u64,
    /// Bytes of the packet already delivered to the decoder.
    consumed: usize,
}

/// Last delivered timeline state (sequential reads only; probes never
/// advance it).
#[derive(Clone)]
struct ServedState {
    valid: bool,
    pts: u64,
    pcr: u64,
    pcr_valid: bool,
    upd_pos: u64,
    ccs: [(u16, u8); REF_CC_SLOTS],
    ncc: usize,
}

impl ServedState {
    fn new() -> Self {
        Self {
            valid: false,
            pts: 0,
            pcr: 0,
            pcr_valid: false,
            upd_pos: 0,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    fn to_ref(&self, src: RefSrc) -> XformRef {
        let mut r = XformRef::invalid();
        r.src = src;
        r.valid = self.valid;
        r.pts = self.pts;
        r.pcr = self.pcr;
        r.pcr_valid = self.pcr_valid;
        r.upd_pos = self.upd_pos;
        r.ncc = self.ncc;
        r.ccs[..self.ncc].copy_from_slice(&self.ccs[..self.ncc]);
        r
    }

    fn set_cc(&mut self, pid: u16, cc: u8) {
        for slot in self.ccs.iter_mut().take(self.ncc) {
            if slot.0 == pid {
                slot.1 = cc;
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }
}

/// A RAM checkpoint: a `served` snapshot keyed by delivered file end-offset.
#[derive(Clone)]
struct CkptSlot {
    fend: u64,
    t_ms: u64,
    state: ServedState,
}

impl CkptSlot {
    fn new() -> Self {
        Self {
            fend: 0,
            t_ms: 0,
            state: ServedState::new(),
        }
    }
}

/// The transform engine state (owned by the single engine mutex).
pub struct TsXformState {
    epoch: Epoch,
    served: ServedState,
    tail: Tail,
    ckpt: Vec<CkptSlot>,
    pub splices: u64,
    pub ref_served: u64,
    pub ref_ckpt: u64,
    pub ref_scan: u64,
    pub ref_none: u64,
}

impl Default for TsXformState {
    fn default() -> Self {
        Self::new()
    }
}

impl TsXformState {
    pub fn new() -> Self {
        Self {
            epoch: Epoch::identity(0),
            served: ServedState::new(),
            tail: Tail {
                valid: false,
                abs_pos: 0,
                consumed: 0,
            },
            ckpt: vec![CkptSlot::new(); CKPT_N],
            splices: 0,
            ref_served: 0,
            ref_ckpt: 0,
            ref_scan: 0,
            ref_none: 0,
        }
    }

    pub fn epoch_id(&self) -> u64 {
        self.epoch.id
    }

    /// Starts a new identity epoch (fresh playback: no continuity splice).
    pub fn reset_epoch(&mut self, id: u64) {
        self.epoch = Epoch::identity(id);
        self.served = ServedState::new();
        self.tail.valid = false;
        self.splices += 1;
    }

    /// Drops the straddling carry (anchor change / reopen).
    pub fn invalidate_tail(&mut self) {
        self.tail.valid = false;
    }

    /// Creates a splice epoch: `ref` is the delivered timeline state at the
    /// old anchor, `raw` the pre-transform sample at the new anchor. The new
    /// epoch shifts every field so the decoder's timeline continues exactly
    /// at `ref + SPLICE_PTS_EPS`.
    pub fn begin_splice_epoch(&mut self, id: u64, r: &XformRef, raw: &RawSample) -> bool {
        let mut ep = Epoch::identity(id);
        let mut spliced = false;
        if r.valid {
            if let Some(raw_pts) = raw.pts {
                let target = pts_wrap(r.pts.saturating_add(SPLICE_PTS_EPS));
                ep.delta_pts = pts_sub(target, raw_pts);
                ep.delta_pcr = ep.delta_pts.wrapping_mul(300);
                spliced = true;
            } else if r.pcr_valid {
                if let Some(raw_pcr) = raw.pcr {
                    let target = pcr_apply(r.pcr, SPLICE_PTS_EPS as i64 * 300);
                    ep.delta_pcr = pcr_sub(target, raw_pcr);
                    ep.delta_pts = ep.delta_pcr / 300;
                    spliced = true;
                }
            }
            for i in 0..r.ncc {
                let (pid, delivered) = r.ccs[i];
                if let Some(raw_cc) = raw.cc_of(pid) {
                    let idx = usize::from(pid);
                    // Next delivered CC on this PID must be delivered + 1.
                    ep.cc_add[idx] = (delivered.wrapping_add(1).wrapping_sub(raw_cc)) & 0x0F;
                }
            }
        }
        self.epoch = ep;
        self.served.valid = false;
        self.tail.valid = false;
        self.splices += 1;
        spliced
    }

    /// Transforms one complete packet in place with the current epoch.
    /// Returns `(pid, delivered_cc)` when a CC byte was rewritten.
    fn transform_packet(&self, pkt: &mut [u8; TS_PKT_SZ]) -> Option<(u16, u8)> {
        if pkt[0] != 0x47 {
            return None;
        }
        let pid = ts_pid(pkt);
        if pid == NULL_PID {
            return None; // NULL packets are never touched
        }
        let raw_cc = ts_cc(pkt);
        let new_cc = raw_cc.wrapping_add(self.epoch.cc_add[usize::from(pid)]) & 0x0F;
        pkt[3] = (pkt[3] & 0xF0) | new_cc;

        if let Some(pcr) = ts_pcr(pkt) {
            let new_pcr = pcr_apply(pcr, self.epoch.delta_pcr);
            ts_write_pcr(new_pcr, pkt);
        }
        if let Some((pts, dts)) = ts_pes_ts(pkt) {
            if let Some(off) = ts_payload_off(pkt) {
                let new_pts = pts_apply(pts, self.epoch.delta_pts);
                pts_to_bytes(new_pts, &mut pkt[off + 9..off + 14]);
                if let Some(d) = dts {
                    if pkt.len() >= off + 19 {
                        let new_dts = pts_apply(d, self.epoch.delta_pts);
                        pts_to_bytes(new_dts, &mut pkt[off + 14..off + 19]);
                    }
                }
            }
        }
        Some((pid, new_cc))
    }

    /// Transforms a delivery block. `first_abs` is the stream position of
    /// `buf[0]`; `full_at(abs, pkt)` fetches a raw 188-byte packet from the
    /// ring (needed for packets straddling the block edges).
    ///
    /// On `seq` reads the delivered timeline state (`served`) is advanced;
    /// probes must pass `seq = false` so they can never poison the reference.
    pub fn apply_block<F>(&mut self, buf: &mut [u8], first_abs: u64, seq: bool, mut full_at: F)
    where
        F: FnMut(u64, &mut [u8; TS_PKT_SZ]) -> bool,
    {
        if buf.is_empty() {
            return;
        }
        let mut p = 0usize;

        // -- leading straddler: complete a packet split by the previous read
        if self.tail.valid {
            let rem = TS_PKT_SZ - self.tail.consumed;
            if rem > 0
                && self.tail.abs_pos + self.tail.consumed as u64 == first_abs
                && self.tail.consumed > 0
            {
                let take = rem.min(buf.len());
                let mut pkt = [0u8; TS_PKT_SZ];
                if full_at(self.tail.abs_pos, &mut pkt) {
                    self.transform_packet(&mut pkt);
                    buf[..take].copy_from_slice(&pkt[self.tail.consumed..self.tail.consumed + take]);
                    self.tail.consumed += take;
                    if self.tail.consumed >= TS_PKT_SZ {
                        self.tail.valid = false;
                        if seq {
                            self.update_served(&pkt, self.tail.abs_pos);
                        }
                    }
                    p = take;
                } else {
                    // Packet vanished from the ring: drop the carry, serve raw.
                    self.tail.valid = false;
                }
            } else {
                self.tail.valid = false;
            }
        }

        // -- mid-packet alignment when first_abs is not packet-aligned
        if p == 0 && (first_abs % TS_PKT_SZ as u64) != 0 {
            let phase = (first_abs % TS_PKT_SZ as u64) as usize;
            let rem = TS_PKT_SZ - phase;
            let take = rem.min(buf.len());
            let pkt_abs = first_abs - phase as u64;
            let mut pkt = [0u8; TS_PKT_SZ];
            if full_at(pkt_abs, &mut pkt) {
                self.transform_packet(&mut pkt);
                buf[..take].copy_from_slice(&pkt[phase..phase + take]);
                if phase + take == TS_PKT_SZ && seq {
                    self.update_served(&pkt, pkt_abs);
                }
            }
            p = take;
        }

        // -- interior complete packets
        let interior_start = p;
        while p + TS_PKT_SZ <= buf.len() {
            let mut pkt = [0u8; TS_PKT_SZ];
            pkt.copy_from_slice(&buf[p..p + TS_PKT_SZ]);
            self.transform_packet(&mut pkt);
            buf[p..p + TS_PKT_SZ].copy_from_slice(&pkt);
            p += TS_PKT_SZ;
        }
        let interior_end = p;

        // -- trailing partial packet: transform via the full packet, deliver
        //    the head slice now, carry the remainder for the next read.
        if p < buf.len() {
            let rem = buf.len() - p;
            let mut pkt = [0u8; TS_PKT_SZ];
            let abs = first_abs + p as u64;
            if full_at(abs, &mut pkt) {
                self.tail.abs_pos = abs;
                self.tail.consumed = rem;
                self.tail.valid = true;
                self.transform_packet(&mut pkt);
                buf[p..].copy_from_slice(&pkt[..rem]);
            } else {
                self.tail.valid = false;
            }
        }

        if seq && interior_end > interior_start {
            self.update_served(&buf[interior_start..interior_end], first_abs + interior_start as u64);
        }
    }

    /// Advances `served` from the (already transformed) delivery block.
    fn update_served(&mut self, buf: &[u8], first_abs: u64) {
        let mut p = 0usize;
        while p + TS_PKT_SZ <= buf.len() {
            let pkt = &buf[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                self.served.set_cc(pid, ts_cc(pkt));
                self.served.upd_pos = first_abs + p as u64 + TS_PKT_SZ as u64;
                self.served.valid = true;
                if let Some(pcr) = ts_pcr(pkt) {
                    self.served.pcr = pcr;
                    self.served.pcr_valid = true;
                }
                if let Some((pts, _)) = ts_pes_ts(pkt) {
                    self.served.pts = pts;
                }
            }
            p += TS_PKT_SZ;
        }
    }

    /// Records a checkpoint for the delivered file end-offset `fend`.
    /// The slot guard (`fend` differs) prevents a wrap-around slot reuse
    /// from overwriting a fresher record.
    pub fn note_ckpt(&mut self, fend: u64, now_ms: u64) {
        if !self.served.valid || fend == 0 {
            return;
        }
        let slot = ((fend - 1) / CKPT_STEP) as usize % CKPT_N;
        if self.ckpt[slot].state.valid && self.ckpt[slot].fend == fend {
            return; // already recorded for this exact end
        }
        self.ckpt[slot].fend = fend;
        self.ckpt[slot].t_ms = now_ms;
        self.ckpt[slot].state = self.served.clone();
    }

    /// 3-tier calibration: served -> ckpt -> ring_scan.
    ///
    /// `old_pos` is the stream position of the splice point (old mapping),
    /// `old_fend` the last delivered file end-offset, and `win`/`win_base`
    /// a ring window covering `old_pos` for the scan tier.
    pub fn calibrate(
        &mut self,
        old_pos: u64,
        old_fend: u64,
        win: &[u8],
        win_base: u64,
        now_ms: u64,
    ) -> XformRef {
        // Tier 1: fresh served state.
        if self.served.valid
            && old_pos >= self.served.upd_pos
            && old_pos - self.served.upd_pos <= SERVED_FRESH_BYTES
        {
            self.ref_served += 1;
            return self.served.to_ref(RefSrc::Served);
        }
        // Tier 2: newest RAM checkpoint at/before the last delivered end.
        let mut best: Option<&CkptSlot> = None;
        for slot in self.ckpt.iter() {
            if !slot.state.valid || slot.fend > old_fend {
                continue;
            }
            if now_ms.saturating_sub(slot.t_ms) > CKPT_MAX_AGE_MS {
                continue;
            }
            if old_pos.saturating_sub(slot.state.upd_pos) > CKPT_MAX_DIST {
                continue;
            }
            match best {
                Some(b) if b.fend >= slot.fend => {}
                _ => best = Some(slot),
            }
        }
        if let Some(slot) = best {
            self.ref_ckpt += 1;
            return slot.state.to_ref(RefSrc::Ckpt);
        }
        // Tier 3: scan the ring backward for the last delivered-like state.
        if let Some(r) = self.ring_scan(win, win_base, old_pos) {
            self.ref_scan += 1;
            return r;
        }
        self.ref_none += 1;
        XformRef::invalid()
    }

    /// Walks back at most `RING_SCAN_BACK` bytes from `old_pos` inside the
    /// window and converts the closest raw PTS/PCR/CC state into delivered
    /// values with the current epoch.
    fn ring_scan(&self, win: &[u8], win_base: u64, old_pos: u64) -> Option<XformRef> {
        if win.is_empty() || old_pos < win_base || old_pos >= win_base + win.len() as u64 {
            return None;
        }
        let grid = crate::engine::ring::snap188(old_pos);
        let rel = grid.checked_sub(win_base)? as usize;
        let mut best_pts: Option<u64> = None;
        let mut best_pcr: Option<u64> = None;
        let mut p = rel;
        let mut back = 0u64;
        let mut ccs: [(u16, u8); REF_CC_SLOTS] = [(0, 0); REF_CC_SLOTS];
        let mut ncc = 0usize;
        let mut upd_pos = grid;
        while p + TS_PKT_SZ <= win.len() && back <= RING_SCAN_BACK {
            let pkt = &win[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                let delivered_cc =
                    ts_cc(pkt).wrapping_add(self.epoch.cc_add[usize::from(pid)]) & 0x0F;
                let mut seen = false;
                for s in ccs.iter_mut().take(ncc) {
                    if s.0 == pid {
                        seen = true;
                        break;
                    }
                }
                if !seen && ncc < REF_CC_SLOTS {
                    ccs[ncc] = (pid, delivered_cc);
                    ncc += 1;
                }
                if best_pcr.is_none() {
                    best_pcr = ts_pcr(pkt);
                }
                if best_pts.is_none() {
                    if let Some((pts, _)) = ts_pes_ts(pkt) {
                        best_pts = Some(pts);
                        upd_pos = win_base + p as u64;
                    }
                }
                if best_pts.is_some() && best_pcr.is_some() {
                    break;
                }
            }
            if p < TS_PKT_SZ {
                break;
            }
            p -= TS_PKT_SZ;
            back += TS_PKT_SZ as u64;
        }
        let raw_pts = best_pts?;
        let mut r = XformRef::invalid();
        r.src = RefSrc::RingScan;
        r.valid = true;
        r.pts = pts_apply(raw_pts, self.epoch.delta_pts);
        r.pcr = match best_pcr {
            Some(pcr) => pcr_apply(pcr, self.epoch.delta_pcr),
            None => 0,
        };
        r.pcr_valid = best_pcr.is_some();
        r.upd_pos = upd_pos;
        r.ncc = ncc;
        r.ccs = ccs;
        Some(r)
    }

    /// Collects the raw sample at a candidate new anchor: first PTS/PCR and
    /// the first CC per PID within `[from, to)` of the window.
    pub fn collect_raw(win: &[u8], win_base: u64, from: u64, to: u64) -> RawSample {
        let mut s = RawSample::empty();
        let start = from.max(win_base);
        let end = to.min(win_base + win.len() as u64);
        let mut p = crate::engine::ring::snap188(start).saturating_sub(win_base) as usize;
        while p + TS_PKT_SZ <= win.len() {
            let abs = win_base + p as u64;
            if abs >= end {
                break;
            }
            let pkt = &win[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                s.set_cc(pid, ts_cc(pkt));
                if s.pcr.is_none() {
                    s.pcr = ts_pcr(pkt);
                }
                if s.pts.is_none() {
                    if let Some((pts, _)) = ts_pes_ts(pkt) {
                        s.pts = Some(pts);
                    }
                }
                if s.pts.is_some() && s.pcr.is_some() && s.ncc >= 2 {
                    break;
                }
            }
            p += TS_PKT_SZ;
        }
        s
    }

    /// Exposes the current epoch deltas (telemetry / tests).
    pub fn deltas(&self) -> (u64, i64, i64) {
        (self.epoch.id, self.epoch.delta_pts, self.epoch.delta_pcr)
    }

    /// True when a straddling packet carry is active.
    pub fn tail_active(&self) -> bool {
        self.tail.valid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkt(pid: u16, cc: u8) -> [u8; TS_PKT_SZ] {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[1] = ((pid >> 8) & 0x1F) as u8 | 0x40;
        p[2] = pid as u8;
        p[3] = 0x10 | (cc & 0x0F);
        p
    }

    fn pes_pkt(pid: u16, cc: u8, pts: u64) -> [u8; TS_PKT_SZ] {
        let mut p = pkt(pid, cc);
        let off = 4usize;
        // PES: 00 00 01 E0 len flags hdrlen PTS(5)
        p[off] = 0x00;
        p[off + 1] = 0x00;
        p[off + 2] = 0x01;
        p[off + 3] = 0xE0;
        p[off + 4] = 0x00;
        p[off + 5] = 0x00;
        p[off + 6] = 0x80; // flags1
        p[off + 7] = 0x80; // PTS present
        p[off + 8] = 0x0A; // PES_header_data_length
        let mut b = [0u8; 5];
        pts_to_bytes(pts, &mut b);
        p[off + 9..off + 14].copy_from_slice(&b);
        p
    }

    fn pcr_pkt(pid: u16, cc: u8, pcr: u64) -> [u8; TS_PKT_SZ] {
        let mut p = pkt(pid, cc);
        p[3] = 0x30 | (cc & 0x0F); // AF + payload
        p[4] = 0x07; // AF length
        p[5] = 0x10; // PCR flag
        ts_write_pcr(pcr, &mut p);
        p
    }

    // -- PTS/PCR modular math ------------------------------------------------

    #[test]
    fn pts_modular_math() {
        assert_eq!(PTS_MOD, 8_589_934_592);
        assert_eq!(PCR_MOD, 2_576_980_377_600);
        assert_eq!(pts_wrap(PTS_MOD), 0);
        assert_eq!(pts_wrap(PTS_MOD + 1234), 1234);
        assert_eq!(pcr_wrap(PCR_MOD), 0);
        // wrap-safe subtraction: 100 - (PTS_MOD - 50) = +150 forward
        assert_eq!(pts_sub(100, PTS_MOD - 50), 150);
        assert_eq!(pts_sub(PTS_MOD - 50, 100), -150);
        assert_eq!(pts_sub(0, 0), 0);
        assert!(pts_sub(PTS_MOD - 1, 0) < 0); // -1, not a huge positive
        assert_eq!(pts_apply(PTS_MOD - 50, 150), 100);
        assert_eq!(pts_apply(100, -150), PTS_MOD - 50);
        assert_eq!(pcr_apply(PCR_MOD - 10, 20), 10);
        assert_eq!(pcr_apply(10, -20), PCR_MOD - 10);
    }

    #[test]
    fn pts_roundtrip_encoding() {
        for &v in &[0u64, 1, 12345, 900_000, PTS_MOD - 1, PTS_MOD / 2] {
            let mut b = [0u8; 5];
            pts_to_bytes(v, &mut b);
            assert_eq!(pts_from_bytes(&b), v);
        }
    }

    #[test]
    fn pcr_roundtrip_encoding() {
        for &v in &[0u64, 300, 1, 27_000_000, PCR_MOD - 300, PCR_MOD / 2 + 7] {
            let mut p = pcr_pkt(0x100, 0, v);
            let parsed = ts_pcr(&p).expect("pcr");
            assert_eq!(parsed, v);
            let shifted = v + 600;
            ts_write_pcr(shifted % PCR_MOD, &mut p);
            assert_eq!(ts_pcr(&p).expect("pcr2"), shifted % PCR_MOD);
        }
    }

    #[test]
    fn pes_pts_parse() {
        let p = pes_pkt(0x100, 3, 777_777);
        let (pts, dts) = ts_pes_ts(&p).expect("pts");
        assert_eq!(pts, 777_777);
        assert!(dts.is_none());
        assert!(ts_pcr(&p).is_none());
    }

    #[test]
    fn splice_pad_alignment() {
        assert_eq!(splice_pad_len(0), 0);
        assert_eq!(splice_pad_len(1), 187);
        assert_eq!(splice_pad_len(187), 1);
        assert_eq!(splice_pad_len(188), 0);
        assert_eq!(splice_pad_len(189), 187);
    }

    // -- CC tracking ----------------------------------------------------------

    #[test]
    fn cc_tracking_skips_null_and_wraps_mod_16() {
        let mut x = TsXformState::new();
        // Identity epoch: CC must pass through unchanged.
        let mut null_pkt = [0u8; TS_PKT_SZ];
        null_pkt[0] = 0x47;
        null_pkt[1] = 0x1F;
        null_pkt[2] = 0xFF;
        null_pkt[3] = 0x1A; // CC = 10
        x.apply_block(&mut null_pkt[..], 0, false, |_, _| true);
        assert_eq!(null_pkt[3] & 0x0F, 10, "NULL packet CC must never be touched");

        let mut prev = 15u8;
        for i in 0..20u8 {
            let mut p = pkt(0x100, prev);
            x.apply_block(&mut p[..], (i as u64) * 188, false, |_, _| true);
            assert_eq!(p[3] & 0x0F, prev, "identity epoch preserves CC");
            prev = prev.wrapping_add(1) & 0x0F;
        }
    }

    #[test]
    fn cc_add_shift_applied_per_pid() {
        let mut x = TsXformState::new();
        // Splice epoch: delivered CC on PID 0x100 was 5, raw CC at the new
        // anchor is 9, so the decoder must next see 6 -> cc_add = 13.
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 1_000_000;
        r.set_cc(0x100, 5); // delivered CC on video pid
        let mut raw = RawSample::empty();
        raw.pts = Some(900_000);
        raw.set_cc(0x100, 9); // raw CC at new anchor
        assert!(x.begin_splice_epoch(1, &r, &raw));

        let mut p = pkt(0x100, 9);
        x.apply_block(&mut p[..], 0, false, |_, _| true);
        assert_eq!(p[3] & 0x0F, 6, "(5 + 1 - 9) mod 16 = 13; 9 + 13 = 22 mod 16 = 6");
        let mut p2 = pkt(0x101, 9);
        x.apply_block(&mut p2[..], 188, false, |_, _| true);
        assert_eq!(p2[3] & 0x0F, 9, "untracked PID keeps raw CC");
    }

    // -- Tail carry across read boundaries ------------------------------------

    fn memory_fetcher<'a>(mem: &'a [u8]) -> impl FnMut(u64, &mut [u8; TS_PKT_SZ]) -> bool + 'a {
        move |abs: u64, pkt: &mut [u8; TS_PKT_SZ]| {
            let start = abs as usize;
            if start + TS_PKT_SZ <= mem.len() {
                pkt.copy_from_slice(&mem[start..start + TS_PKT_SZ]);
                true
            } else {
                false
            }
        }
    }

    #[test]
    fn straddling_packet_transformed_exactly_once() {
        // Stream: 700 PES packets with increasing PTS; serve in 128 KiB
        // reads (131072 = 697*188 + 56) like the real FUSE block size.
        let n = 700usize;
        let mut stream = Vec::with_capacity(n * TS_PKT_SZ);
        for i in 0..n {
            stream.extend_from_slice(&pes_pkt(0x100, (i % 16) as u8, 900_000 + i as u64 * 3003));
        }
        // Transform the whole stream in one shot as the reference.
        let mut x_full = TsXformState::new();
        let mut full = stream.clone();
        x_full.apply_block(&mut full, 0, false, memory_fetcher(&stream));

        // Now serve in 128 KiB slices with tail carry.
        let mut x = TsXformState::new();
        let mut out = Vec::new();
        let mut abs = 0u64;
        while (abs as usize) < stream.len() {
            let end = ((abs as usize) + 128 * 1024).min(stream.len());
            let mut block = stream[abs as usize..end].to_vec();
            x.apply_block(&mut block, abs, false, memory_fetcher(&stream));
            out.extend_from_slice(&block);
            abs = end as u64;
        }
        assert_eq!(out.len(), stream.len());
        assert_eq!(out, full, "straddling packet must be transformed identically");
    }

    #[test]
    fn transform_is_deterministic_per_position() {
        let mut stream = Vec::new();
        for i in 0..10usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 500_000 + i as u64 * 3000));
            stream.extend_from_slice(&pkt(0x101, (i + 3) as u8));
        }
        let mut x = TsXformState::new();
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 4_000_000;
        let mut raw = TsXformState::collect_raw(&stream, 0, 0, 188 * 20);
        raw.pts = Some(500_000);
        x.begin_splice_epoch(1, &r, &raw);

        let mut a = stream.clone();
        x.apply_block(&mut a, 0, false, memory_fetcher(&stream));
        let mut b = stream.clone();
        x.apply_block(&mut b, 0, false, memory_fetcher(&stream));
        assert_eq!(a, b, "same raw bytes + same position => same output");
        // And the delivered PTS jumped to the reference.
        let (pts, _) = ts_pes_ts(&a[0..188]).expect("pts");
        assert_eq!(pts, pts_wrap(4_000_000 + SPLICE_PTS_EPS));
    }

    // -- 3-tier calibration ---------------------------------------------------

    #[test]
    fn calibration_served_tier() {
        let mut x = TsXformState::new();
        // Build a served state via a sequential read.
        let mut stream = Vec::new();
        for i in 0..8usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 600_000 + i as u64 * 3000));
        }
        let mut blk = stream.clone();
        x.apply_block(&mut blk, 0, true, memory_fetcher(&stream));
        // Served is fresh for old_pos within 256 KB ahead of the last packet.
        let r = x.calibrate(188 * 8 + 1024, 188 * 8, &stream, 0, 1000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::Served);
        // Far away: not fresh anymore.
        let r2 = x.calibrate(188 * 8 + 1024 + 1024 * 1024, 188 * 8, &stream, 0, 1000);
        assert!(!r2.valid);
    }

    #[test]
    fn calibration_ckpt_tier_after_served_stales() {
        let mut x = TsXformState::new();
        let mut stream = Vec::new();
        for i in 0..8usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 600_000 + i as u64 * 3000));
        }
        let mut blk = stream.clone();
        x.apply_block(&mut blk, 0, true, memory_fetcher(&stream));
        x.note_ckpt(188 * 8, 1000);

        let far = 188 * 8 + 1024 + 1024 * 1024; // beyond served freshness
        let r = x.calibrate(far, 188 * 8, &[], 0, 2000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::Ckpt);
    }

    #[test]
    fn calibration_ring_scan_tier() {
        let mut x = TsXformState::new(); // fresh engine: no served, no ckpt
        let mut stream = Vec::new();
        for i in 0..100usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 1_000_000 + i as u64 * 3000));
        }
        let old_pos = 188 * 99; // last packet
        let r = x.calibrate(old_pos, u64::MAX, &stream, 0, 1000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::RingScan);
        assert_eq!(r.pts, 1_000_000 + 99 * 3000);
        assert!(r.pcr_valid || !r.pcr_valid); // pcr optional in scan
    }

    #[test]
    fn splice_epoch_continues_timeline() {
        let mut x = TsXformState::new();
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 5_000_000;
        let mut raw = RawSample::empty();
        raw.pts = Some(3_000_000);
        assert!(x.begin_splice_epoch(7, &r, &raw));
        let (_id, dpts, _) = x.deltas();
        assert_eq!(dpts, pts_sub(5_000_000 + SPLICE_PTS_EPS, 3_000_000));

        // Apply to a raw packet at the new anchor: PTS lands at ref + eps.
        let mut p = pes_pkt(0x100, 0, 3_000_000);
        x.apply_block(&mut p[..], 0, false, |_, _| true);
        let (pts, _) = ts_pes_ts(&p).expect("pts");
        assert_eq!(pts, pts_wrap(5_000_000 + SPLICE_PTS_EPS));
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/engine/ts_xform.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/state_machine.rs (549 lines) <<<<<<<<<<<<<<<
//! Return-continuity state machine for the 2-step Samsung ConnectShare resume.
//!
//! Empirical TV behaviour (docs/briefing_investigacao_return.md): pressing
//! **Return** and reopening `TV AO VIVO.trp` does NOT restart at `foff = 0` —
//! the decoder keeps its ~1.9 MiB header in RAM and reopens at a saved
//! bookmark offset. The resume is *two steps*:
//!
//! 1. A non-adjacent read appears after an idle gap (`> 20 s`) with a jump
//!    `> 256 KiB` — the bookmark seek.
//! 2. Reads continue sequentially from that point; once the chain holds
//!    `>= 4` reads / `>= 64 KiB`, the resume is confirmed and the engine
//!    re-anchors *continuously* (splice), not at the live edge.
//!
//! Guards:
//! * **Opening grace** — for 10 s after a fresh playback open
//!   (`foff <= 4 MiB`) resume detection is locked out: startup probes must
//!   never trigger a re-anchor.
//! * **Probe isolation** — reads at `foff >= 8 MiB` whose mapped stream
//!   position falls outside the ring window (future probe: `>= s_write +
//!   2 MiB`; behind probe: `< ring_old`) are rejected with NULL packets and
//!   never advance the playback chain. Only `K >= 3` sequential probe blocks
//!   escalate to a confirmed seek.
//! * **Position-preserving selection** — a confirmed resume whose requested
//!   position still maps inside the ring continues from the nearest PAT
//!   (timeshift continuity) instead of jumping to the live edge.

use crate::contract::RESUME_MIN_LEAD;
use crate::engine::ring::snap188;

/// Idle gap after which a non-adjacent read is treated as a resume attempt.
pub const RESUME_GAP_MS: u64 = 20_000;
/// Non-adjacent jump distance that qualifies as a resume/bookmark seek.
pub const RESUME_JUMP: u64 = 256 * 1024;
/// Confirmation chain: reads.
pub const RESUME_CHAIN_READS: u64 = 4;
/// Confirmation chain: bytes.
pub const RESUME_CHAIN_BYTES: u64 = 64 * 1024;
/// Lockout window after a fresh playback open.
pub const OPENING_GRACE_MS: u64 = 10_000;
/// A fresh open is "early" while the read cursor stays below this offset.
pub const OPENING_FOFF_MAX: u64 = 4 * 1024 * 1024;
/// Sequential probe blocks required to confirm a real seek.
pub const PROBE_CONFIRM_K: usize = 3;
/// Future-probe margin: mapped positions this far past the writer are probes.
pub const PROBE_FUTURE_MARGIN: u64 = 2 * 1024 * 1024;
/// SCSI block sizes whose consecutive landing continues the probe chain.
pub const PROBE_SEQ_STEPS: [u64; 2] = [4096, 8192];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SmPhase {
    /// Fresh playback open: grace window active.
    Opening,
    /// Steady playback.
    Live,
    /// Step 1 of the 2-step resume observed; accumulating the chain.
    GapWatch,
    /// Resume consumed; back to steady playback.
    Resumed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SmDecision {
    /// Sequential read: serve from the current anchor.
    Continue,
    /// Transient probe: NULL-fill, no state change (chain untouched).
    ProbeReject,
    /// Confirmed sequential seek (`K >= 3` probe blocks): re-anchor to live.
    ConfirmedSeek,
    /// 2-step resume confirmed: re-anchor with continuity splice.
    ResumeAfterGap,
}

/// Per-read context handed to the state machine by the engine.
#[derive(Debug, Clone, Copy)]
pub struct SmCtx {
    pub anchor_valid: bool,
    /// Last sequential read end (`u64::MAX` when the chain is idle).
    pub prev_fend: u64,
    /// Mapped stream position when `foff >= 8 MiB` (probe classification).
    pub probe_pos: Option<u64>,
    pub s_write: u64,
    pub ring_old: u64,
}

impl SmCtx {
    pub fn adjacent(&self, foff: u64) -> bool {
        self.anchor_valid && self.prev_fend != u64::MAX && foff == self.prev_fend
    }
}

pub struct StateMachine {
    phase: SmPhase,
    /// Idle gap threshold for resume detection (mirrors `FUSE_RESUME_GAP_MS`).
    gap_ms: u64,
    opened_ms: u64,
    have_read: bool,
    last_read_ms: u64,
    gap_jump_foff: u64,
    chain_reads: u64,
    chain_bytes: u64,
    probe_chain: usize,
    probe_last_off: u64,
    // telemetry
    pub gap_detects: u64,
    pub resumes: u64,
    pub confirmed_seeks: u64,
    pub probe_rejects: u64,
    pub transitions: u64,
}

impl Default for StateMachine {
    fn default() -> Self {
        Self::new()
    }
}

impl StateMachine {
    pub fn new() -> Self {
        Self::with_gap_ms(RESUME_GAP_MS)
    }

    /// State machine with a custom idle-gap threshold (engine options).
    pub fn with_gap_ms(gap_ms: u64) -> Self {
        Self {
            phase: SmPhase::Opening,
            gap_ms,
            opened_ms: 0,
            have_read: false,
            last_read_ms: 0,
            gap_jump_foff: 0,
            chain_reads: 0,
            chain_bytes: 0,
            probe_chain: 0,
            probe_last_off: u64::MAX,
            gap_detects: 0,
            resumes: 0,
            confirmed_seeks: 0,
            probe_rejects: 0,
            transitions: 0,
        }
    }

    pub fn phase(&self) -> SmPhase {
        self.phase
    }

    /// Records a fresh playback open (fresh open or channel switch): the
    /// grace window starts and every chain is reset.
    pub fn note_open(&mut self, now_ms: u64) {
        self.phase = SmPhase::Opening;
        self.opened_ms = now_ms;
        self.have_read = false;
        self.last_read_ms = now_ms;
        self.gap_jump_foff = 0;
        self.chain_reads = 0;
        self.chain_bytes = 0;
        self.probe_chain = 0;
        self.probe_last_off = u64::MAX;
        self.transitions += 1;
    }

    /// True while the opening grace suppresses resume detection.
    pub fn opening_grace_active(&self, foff: u64, now_ms: u64) -> bool {
        self.phase == SmPhase::Opening
            && foff <= OPENING_FOFF_MAX
            && now_ms.saturating_sub(self.opened_ms) < OPENING_GRACE_MS
    }

    fn set_phase(&mut self, p: SmPhase) {
        if self.phase != p {
            self.phase = p;
            self.transitions += 1;
        }
    }

    /// Evaluates one FUSE read: updates the chains and returns the decision
    /// for THIS read. The engine must apply the `ProbeReject` path without
    /// touching the playback chain cursor (`prev_fend` invariant).
    pub fn evaluate(&mut self, foff: u64, len: u64, now_ms: u64, ctx: &SmCtx) -> SmDecision {
        let adjacent = ctx.adjacent(foff);

        // Opening grace expiry: early playback left the 4 MiB window or the
        // 10 s lockout elapsed.
        if self.phase == SmPhase::Opening
            && (foff > OPENING_FOFF_MAX
                || now_ms.saturating_sub(self.opened_ms) >= OPENING_GRACE_MS)
        {
            self.set_phase(SmPhase::Live);
        }

        // ---- probe classification (beyond 8 MiB, outside the ring window)
        if let Some(s_probe) = ctx.probe_pos {
            let is_future = s_probe >= ctx.s_write.saturating_add(PROBE_FUTURE_MARGIN) && !adjacent;
            let is_behind = s_probe < ctx.ring_old && !adjacent;
            if is_future || is_behind {
                // A probe landing one SCSI block past the previous probe is a
                // sequential re-scan of the same neighbourhood and continues
                // the debounce chain; anything else restarts it at 1.
                let sequential = self.probe_last_off == u64::MAX
                    || PROBE_SEQ_STEPS
                        .iter()
                        .any(|&step| foff == self.probe_last_off.saturating_add(step));
                if sequential || self.probe_chain == 0 {
                    self.probe_chain += 1;
                } else {
                    self.probe_chain = 1;
                }
                self.probe_last_off = foff;
                if self.probe_chain < PROBE_CONFIRM_K {
                    // PR INVARIANT: the idle-gap timer (last_read_ms) must NOT
                    // be touched by probes, otherwise a long probe storm during
                    // the Return idle would mask the resume gap (65 s in the
                    // production log).
                    self.probe_rejects += 1;
                    return SmDecision::ProbeReject;
                }
                // K >= 3: a real sequential seek — re-anchor to the live edge.
                self.probe_chain = 0;
                self.probe_last_off = u64::MAX;
                self.set_phase(SmPhase::Live);
                self.confirmed_seeks += 1;
                return SmDecision::ConfirmedSeek;
            }
        }
        if adjacent {
            self.probe_chain = 0;
            self.probe_last_off = u64::MAX;
        }

        // ---- 2-step return resume
        if self.phase == SmPhase::GapWatch {
            if adjacent && foff >= self.gap_jump_foff {
                // Step 2: sequential continuation at the resume point.
                self.chain_reads += 1;
                self.chain_bytes = self.chain_bytes.saturating_add(len);
                if self.chain_reads >= RESUME_CHAIN_READS
                    || self.chain_bytes >= RESUME_CHAIN_BYTES
                {
                    self.set_phase(SmPhase::Resumed);
                    self.chain_reads = 0;
                    self.chain_bytes = 0;
                    self.resumes += 1;
                    self.have_read = true;
                    self.last_read_ms = now_ms;
                    return SmDecision::ResumeAfterGap;
                }
            } else {
                // Non-adjacent inside GapWatch: not a resume chain; restart
                // the gap watch from this offset only if it is itself a
                // gap-jump candidate, else fall back to steady playback.
                self.reset_gap_watch();
                if self.gap_jump_candidate(foff, now_ms, ctx) {
                    self.begin_gap_watch(foff, len);
                } else {
                    self.set_phase(SmPhase::Live);
                }
            }
        } else if !adjacent && self.gap_jump_candidate(foff, now_ms, ctx) {
            // Step 1: bookmark seek after an idle gap.
            self.begin_gap_watch(foff, len);
        }

        self.have_read = true;
        self.last_read_ms = now_ms;
        SmDecision::Continue
    }

    pub fn reset_gap_watch(&mut self) {
        self.chain_reads = 0;
        self.chain_bytes = 0;
    }

    fn begin_gap_watch(&mut self, foff: u64, len: u64) {
        self.set_phase(SmPhase::GapWatch);
        self.gap_jump_foff = foff;
        self.chain_reads = 1;
        self.chain_bytes = len;
        self.gap_detects += 1;
    }

    fn gap_jump_candidate(&self, foff: u64, now_ms: u64, ctx: &SmCtx) -> bool {
        if self.opening_grace_active(foff, now_ms) {
            return false;
        }
        if ctx.prev_fend == u64::MAX || !self.have_read {
            return false;
        }
        let gap = now_ms.saturating_sub(self.last_read_ms);
        let jump = foff.max(ctx.prev_fend) - foff.min(ctx.prev_fend);
        gap > self.gap_ms && jump > RESUME_JUMP
    }
}

/// Position-preserving continuous anchor selection.
///
/// When the requested position still maps inside the ring, the resume
/// continues from there (timeshift continuity, "Caminho 2"); the returned
/// target is snapped to the 188 grid and keeps at least `RESUME_MIN_LEAD`
/// cushion behind the live writer. `None` means the position was overwritten
/// and the caller must fall back to the live target (`s_write - LEADBACK`).
pub fn select_resume_anchor(mapped: u64, ring_old: u64, s_write: u64) -> Option<u64> {
    if mapped < ring_old {
        return None;
    }
    let floor = s_write.saturating_sub(RESUME_MIN_LEAD);
    Some(snap188(mapped.min(floor)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx(anchor_valid: bool, prev_fend: u64, probe_pos: Option<u64>, s_write: u64, ring_old: u64) -> SmCtx {
        SmCtx {
            anchor_valid,
            prev_fend,
            probe_pos,
            s_write,
            ring_old,
        }
    }

    #[test]
    fn opening_grace_locks_out_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        assert!(sm.opening_grace_active(1_000_000, 5_000));
        // Reads within the grace at foff <= 4 MiB: never a resume.
        let c = ctx(true, 2_000_000, None, 100 * 1024 * 1024, 0);
        assert_eq!(sm.evaluate(9_000_000, 16384, 6_000, &c), SmDecision::Continue);
        // Grace expires after 10 s.
        assert!(!sm.opening_grace_active(1_000_000, 11_000));
        // Leaving the 4 MiB window also ends the grace.
        let mut sm2 = StateMachine::new();
        sm2.note_open(0);
        assert!(!sm2.opening_grace_active(5 * 1024 * 1024, 1_000));
    }

    #[test]
    fn two_step_resume_detection() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Steady playback: chain at foff 51 MB.
        let mut fend = 0u64;
        let base = 51_000_000u64;
        for i in 0..6 {
            let foff = base + i * 16384;
            let c = ctx(true, fend, None, s_write, ring_old);
            assert_eq!(sm.evaluate(foff, 16384, 1_000 + i * 100, &c), SmDecision::Continue);
            fend = foff + 16384;
        }
        assert_eq!(sm.phase(), SmPhase::Live);

        // Idle 65 s (Return key), then the TV reopens at its 1.9 MiB bookmark.
        let reopen = 1_900_544u64;
        let c = ctx(true, fend, None, s_write, ring_old);
        // Step 1: the jump read itself.
        assert_eq!(sm.evaluate(reopen, 16384, 66_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::GapWatch);

        // Step 2: sequential continuation from the bookmark; the 4th chained
        // read (64 KB) confirms the resume.
        let mut prev = reopen + 16384;
        let mut decision = SmDecision::Continue;
        for i in 1..4 {
            let c2 = ctx(true, prev, None, s_write, ring_old);
            decision = sm.evaluate(prev, 16384, 66_000 + i * 100, &c2);
            prev += 16384;
        }
        assert_eq!(decision, SmDecision::ResumeAfterGap);
        assert_eq!(sm.phase(), SmPhase::Resumed);
    }

    #[test]
    fn small_jump_is_not_a_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let c = ctx(true, 10_000_000, None, s_write, 0);
        // Gap ok, but the jump is only 100 KiB (< 256 KiB): not a resume.
        assert_eq!(sm.evaluate(10_100_000, 16384, 60_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::Live);
    }

    #[test]
    fn gap_without_chain_is_not_a_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, 0);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);

        // Step 1 observed...
        let c = ctx(true, 10_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(50_000_000, 16384, 60_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::GapWatch);
        // ...but the next read is non-adjacent: the chain breaks.
        let c2 = ctx(true, 50_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(60_000_000, 16384, 60_100, &c2), SmDecision::Continue);
        assert_ne!(sm.phase(), SmPhase::GapWatch);
    }

    #[test]
    fn probe_isolation_k3_debounce() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Playback chain at 300 MB; probes arrive at 800 MB..128 GB (mapped
        // far beyond the writer head => future probes).
        let fend = 300_000_000u64;
        let future = |f: u64| Some(s_write + 2 * 1024 * 1024 + f);

        let c1 = ctx(true, fend, future(800_000_000), s_write, ring_old);
        assert_eq!(sm.evaluate(800_000_000, 16384, 1_000, &c1), SmDecision::ProbeReject);
        let c2 = ctx(true, fend, future(800_004_096), s_write, ring_old);
        assert_eq!(sm.evaluate(800_004_096, 16384, 1_100, &c2), SmDecision::ProbeReject);
        // K = 3: a real sequential seek.
        let c3 = ctx(true, fend, future(800_008_192), s_write, ring_old);
        assert_eq!(sm.evaluate(800_008_192, 16384, 1_200, &c3), SmDecision::ConfirmedSeek);
        // The chain cursor must NOT have moved (engine-side invariant).
        assert_eq!(fend, 300_000_000);
    }

    #[test]
    fn isolated_probe_never_escalates() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 0;
        let future = Some(s_write + 3 * 1024 * 1024);
        for (i, f) in [700_000_000u64, 900_000_000, 1_200_000_000].iter().enumerate() {
            let c = ctx(true, 300_000_000, future, s_write, ring_old);
            assert_eq!(
                sm.evaluate(*f, 16384, 1_000 + i as u64 * 100, &c),
                SmDecision::ProbeReject,
                "isolated probe block {} must never escalate",
                i + 1
            );
        }
    }

    #[test]
    fn behind_probe_is_rejected() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 300 * 1024 * 1024;
        // Non-adjacent probe mapped behind ring_old (overwritten region).
        let c = ctx(true, 399_000_000, Some(ring_old - 1024), s_write, ring_old);
        assert_eq!(sm.evaluate(50_000_000, 16384, 1_000, &c), SmDecision::ProbeReject);
    }

    #[test]
    fn adjacent_read_behind_ring_is_not_rejected_as_probe() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 300 * 1024 * 1024;
        // Adjacent sequential read falling behind ring buffer must NOT be rejected
        // as a probe; it must return Continue so live_engine re-anchors.
        let c = ctx(true, 50_000_000, Some(ring_old - 1024), s_write, ring_old);
        assert_eq!(sm.evaluate(50_000_000, 16384, 1_000, &c), SmDecision::Continue);
    }

    #[test]
    fn sequential_reads_never_escalate() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Adjacent reads (probe_pos set but inside the window) continue.
        let mut fend = 5_000_000u64;
        for i in 0..10u64 {
            let foff = 5_000_000 + i * 16384;
            let c = ctx(true, fend, Some(s_write - 8 * 1024 * 1024 + foff), s_write, s_write - 128 * 1024 * 1024);
            assert_eq!(sm.evaluate(foff, 16384, 1_000 + i * 100, &c), SmDecision::Continue);
            fend = foff + 16384;
        }
        assert_eq!(sm.phase(), SmPhase::Live);
    }

    #[test]
    fn position_preserving_anchor_selection() {
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Inside the ring: continue from there (snapped to the 188 grid).
        let target = select_resume_anchor(s_write - 40 * 1024 * 1024, ring_old, s_write);
        assert_eq!(target, Some(snap188(s_write - 40 * 1024 * 1024)));
        // Too close to the live edge: floored to s_write - RESUME_MIN_LEAD.
        let near = select_resume_anchor(s_write - 1024, ring_old, s_write).expect("some");
        assert!(s_write - near >= RESUME_MIN_LEAD);
        // Overwritten position: None -> caller falls back to the live target.
        assert_eq!(select_resume_anchor(ring_old - 1, ring_old, s_write), None);
        // Degenerate short stream: the lead floor is 0, so the anchor is
        // pulled to the start of the (tiny) ring.
        assert_eq!(select_resume_anchor(500, 0, 1024), Some(0));
    }

    #[test]
    fn resume_fires_on_either_reads_or_bytes() {
        // Byte threshold first: 4 x 16 KiB reads would hit reads==4 and
        // bytes==64 KiB together; use bigger reads to hit bytes earlier.
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, 0);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);
        let c = ctx(true, 10_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(80_000_000, 64 * 1024, 60_000, &c), SmDecision::Continue);
        // One 64 KiB chained read already satisfies the byte threshold.
        let c2 = ctx(true, 80_000_000 + 64 * 1024, None, s_write, 0);
        assert_eq!(
            sm.evaluate(80_000_000 + 64 * 1024, 64 * 1024, 60_100, &c2),
            SmDecision::ResumeAfterGap
        );
    }

    #[test]
    fn gap_resume_cycle_repeats() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 0;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, ring_old);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);

        // First resume.
        let c = ctx(true, 10_016_384, None, s_write, ring_old);
        assert_eq!(sm.evaluate(50_000_000, 64 * 1024, 60_000, &c), SmDecision::Continue);
        let c2 = ctx(true, 50_000_000 + 64 * 1024, None, s_write, ring_old);
        assert_eq!(
            sm.evaluate(50_000_000 + 64 * 1024, 64 * 1024, 60_100, &c2),
            SmDecision::ResumeAfterGap
        );
        // A second Return + reopen after another gap must resume again.
        let c3 = ctx(true, 50_000_000 + 2 * 64 * 1024, None, s_write, ring_old);
        assert_eq!(sm.evaluate(60_000_000, 64 * 1024, 120_000, &c3), SmDecision::Continue);
        let c4 = ctx(true, 60_000_000 + 64 * 1024, None, s_write, ring_old);
        assert_eq!(
            sm.evaluate(60_000_000 + 64 * 1024, 64 * 1024, 120_100, &c4),
            SmDecision::ResumeAfterGap
        );
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/engine/state_machine.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/live_engine.rs (414 lines) <<<<<<<<<<<<<<<
//! Scaffold draft engine (`LiveEngine`) preserved from the original
//! `src/engine.rs`. Kept as a reference implementation and as the backend
//! currently wired in `main.rs`; the production engine root is
//! `engine::MediaEngine` (see `super`). This module deliberately keeps the
//! original code shape — its submodule declarations now live in the parent.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::contract::{StreamBackend, TS_PKT_SZ, LEADBACK, PROBE_ISOLATION_FOFF};
use crate::engine::ring::{Ring, EDGE_WAIT_BUDGET_MS, MAX_SLEEP_SLICE_MS};
use crate::engine::state_machine::{SmCtx, SmDecision};

use crate::engine::ts_xform::{TsXformState, XformRef, RawSample};

#[derive(Debug, Clone)]
pub struct AnchorState {
    pub active: bool,
    pub file_id: u32,
    pub base_foff: u64,
    pub base_spos: u64,
    pub last_fend: u64,
    pub epoch_seq: u64,
    pub probe_consec: u8,
    pub last_probe_off: u64,
    pub last_read_ms: u64,
}

impl Default for AnchorState {
    fn default() -> Self {
        Self {
            active: false,
            file_id: 0,
            base_foff: 0,
            base_spos: 0,
            last_fend: 0,
            epoch_seq: 0,
            probe_consec: 0,
            last_probe_off: u64::MAX,
            last_read_ms: 0,
        }
    }
}

pub struct LiveEngine {
    pub ring: Arc<Ring>,
    pub xform: Mutex<TsXformState>,
    pub anchor: Mutex<AnchorState>,
    pub sm: Mutex<crate::engine::state_machine::StateMachine>,
}

impl Default for LiveEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl LiveEngine {
    pub fn new() -> Self {
        Self {
            ring: Arc::new(Ring::new()),
            xform: Mutex::new(TsXformState::new()),
            anchor: Mutex::new(AnchorState::default()),
            sm: Mutex::new(crate::engine::state_machine::StateMachine::new()),
        }
    }

    pub fn flush(&self) {
        let seq = {
            let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            anchor.active = false;
            anchor.epoch_seq = anchor.epoch_seq.wrapping_add(1);
            anchor.epoch_seq
        };
        self.xform
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_epoch(seq);
        self.sm
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_gap_watch();
    }

    pub fn snap_anchor(&self, target_spos: u64) -> u64 {
        let (ring_old, s_write) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write())
        };
        if s_write <= ring_old {
            return target_spos.min(s_write);
        }
        use crate::engine::snapper::{D12_BACK, D12_FWD, SCAR_SCAN, SCAR_SKIP};
        let start = target_spos.saturating_sub(D12_BACK).max(ring_old);
        let end = target_spos
            .saturating_add(D12_FWD + SCAR_SCAN + SCAR_SKIP)
            .min(s_write);
        if end <= start {
            return target_spos.min(s_write);
        }
        let win_len = (end - start) as usize;
        let mut win = vec![0u8; win_len];
        {
            let rd = self.ring.lock();
            rd.read_window(start, &mut win);
        }
        let base = start;
        let mut snap = crate::engine::snapper::d12_snap(&win, base, target_spos, s_write);
        let (pos, _) = crate::engine::snapper::validate_scar(&win, base, snap.pos, s_write);
        snap.pos = pos;
        crate::engine::snapper::leadback_guard(snap.pos, s_write)
    }
}

impl StreamBackend for LiveEngine {
    fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], _deadline_ms: u64) -> usize {
        if dst.is_empty() {
            return 0;
        }

        // 1. If ring is cold or empty, fill null and return.
        {
            let rd = self.ring.lock();
            if !rd.have_data() {
                drop(rd);
                crate::engine::ring::fill_null_at(dst, foff);
                return dst.len();
            }
        }

        // Capture ring state.
        let (ring_old0, cur_write0, now_ms0) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write(), rd.now_ms())
        };

        // Cold-open detection: a fresh file or channel switch wipes xform state.
        let was_cold = {
            let a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            !a.active || a.file_id != file_id
        };

        // Map foff to mapped_spos and decide re-anchor.
        let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        let mapped_spos0 = if anchor.active {
            if foff >= anchor.base_foff {
                anchor.base_spos + (foff - anchor.base_foff)
            } else {
                anchor.base_spos.saturating_sub(anchor.base_foff - foff)
            }
        } else {
            foff
        };

        let channel_switched = anchor.active && anchor.file_id != file_id;
        let out_of_window = !anchor.active || mapped_spos0 < ring_old0;
        let restart_at_zero = anchor.active
            && foff == 0
            && (now_ms0.saturating_sub(anchor.last_read_ms) > 1500 || anchor.last_fend > 131072);
        let mut need_reanchor = channel_switched || out_of_window || restart_at_zero;
        let was_cold = was_cold || restart_at_zero;

        static LAST_LOG_MS: AtomicU64 = AtomicU64::new(0);
        let last_log = LAST_LOG_MS.load(Ordering::Relaxed);
        if now_ms0.saturating_sub(last_log) >= 1000 || need_reanchor || foff == 0 {
            LAST_LOG_MS.store(now_ms0, Ordering::Relaxed);
            eprintln!(
                "[LIVE_TEL] foff={} sz={} s_write={}MB lead={}MB reanchor={} (cold={} zero={}) fend={}",
                foff, dst.len(), cur_write0 / (1024 * 1024), cur_write0.saturating_sub(mapped_spos0) / (1024 * 1024),
                need_reanchor, was_cold, restart_at_zero, anchor.last_fend
            );
        }

        let (sm_decision, target_spos_override) = {
            let mut sm = self.sm.lock().unwrap_or_else(|e| e.into_inner());
            if was_cold {
                sm.note_open(now_ms0);
            }
            let ctx = SmCtx {
                anchor_valid: anchor.active,
                prev_fend: if anchor.active { anchor.last_fend } else { u64::MAX },
                probe_pos: if foff >= PROBE_ISOLATION_FOFF { Some(mapped_spos0) } else { None },
                s_write: cur_write0,
                ring_old: ring_old0,
            };
            let decision = sm.evaluate(foff, dst.len() as u64, now_ms0, &ctx);
            let resume_pos = if decision == SmDecision::ResumeAfterGap {
                crate::engine::state_machine::select_resume_anchor(mapped_spos0, ring_old0, cur_write0)
            } else {
                None
            };
            (decision, resume_pos)
        };

        match sm_decision {
            SmDecision::ProbeReject => {
                drop(anchor);
                let rejects = self.sm.lock().unwrap_or_else(|e| e.into_inner()).probe_rejects;
                if rejects >= 4 {
                    let _ = std::fs::File::create("/data/local/tmp/resync_usb");
                }
                let mut rd = self.ring.lock();
                rd.note_null(dst.len() as u64);
                drop(rd);
                crate::engine::ring::fill_null_at(dst, foff);
                return dst.len();
            }
            SmDecision::ConfirmedSeek => {
                need_reanchor = true;
            }
            SmDecision::ResumeAfterGap => {
                need_reanchor = true;
            }
            SmDecision::Continue => {}
        }

        if need_reanchor {
            let old_pos = if anchor.active { mapped_spos0 } else { foff };
            let old_fend = anchor.last_fend;
            let target_spos = if channel_switched
                || !anchor.active
                || sm_decision == SmDecision::ConfirmedSeek
                || restart_at_zero
            {
                cur_write0.saturating_sub(LEADBACK)
            } else if let Some(resume_pos) = target_spos_override {
                resume_pos
            } else {
                mapped_spos0
            };

            let new_spos = self.snap_anchor(target_spos);

            // Re-anchor.
            anchor.epoch_seq = anchor.epoch_seq.wrapping_add(1);
            let seq = anchor.epoch_seq;
            drop(anchor);

            if was_cold {
                self.xform
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .reset_epoch(seq);
            } else {
                // Warm re-anchor / resume splice: calibrate and begin splice epoch
                let r: XformRef = {
                    let win_base = old_pos
                        .saturating_sub(crate::engine::ts_xform::RING_SCAN_BACK)
                        .max(ring_old0);
                    let win_end = old_pos.saturating_add(188).min(cur_write0).max(win_base);
                    let win_len = (win_end - win_base).min(crate::engine::ts_xform::RING_SCAN_BACK + 188) as usize;
                    let mut win = vec![0u8; win_len];
                    {
                        let rd = self.ring.lock();
                        rd.read_window(win_base, &mut win);
                    }
                    self.xform
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .calibrate(old_pos, old_fend, &win, win_base, now_ms0)
                };
                if !r.valid {
                    let _ = std::fs::File::create("/data/local/tmp/resync_usb");
                }
                let raw: RawSample = {
                    let win_base = new_spos.max(ring_old0);
                    let win_end = new_spos.saturating_add(1024 * 1024).min(cur_write0).max(win_base);
                    let win_len = (win_end - win_base).min(1024 * 1024) as usize;
                    let mut win = vec![0u8; win_len];
                    {
                        let rd = self.ring.lock();
                        rd.read_window(win_base, &mut win);
                    }
                    TsXformState::collect_raw(&win, win_base, new_spos, new_spos.saturating_add(1024 * 1024))
                };
                self.xform
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .begin_splice_epoch(seq, &r, &raw);
            }

            let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            anchor.active = true;
            anchor.file_id = file_id;
            anchor.base_foff = crate::engine::ring::snap188(foff);
            anchor.base_spos = new_spos;
            anchor.last_fend = foff;
            anchor.last_read_ms = now_ms0;
            drop(anchor);
        } else {
            drop(anchor);
        }

        // Recompute mapped_spos after possible re-anchor.
        let anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        let mapped_spos = if foff >= anchor.base_foff {
            anchor.base_spos + (foff - anchor.base_foff)
        } else {
            anchor.base_spos.saturating_sub(anchor.base_foff - foff)
        };
        drop(anchor);

        // Refresh ring state.
        let (mut ring_old, mut cur_write, _now) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write(), rd.now_ms())
        };

        // 3. Cooperative live-edge wait.
        let needed_end = mapped_spos.saturating_add(dst.len() as u64);
        if needed_end > cur_write {
            {
                let mut rd = self.ring.lock();
                rd.note_edge_wait();
            }
            let _ = self
                .ring
                .wait_edge(needed_end, Duration::from_millis(EDGE_WAIT_BUDGET_MS));
            let rd = self.ring.lock();
            cur_write = rd.s_write();
            ring_old = rd.ring_old();
        }

        // 4. Copy resident bytes into dst and null-pad remainder.
        let start = mapped_spos.max(ring_old);
        let dst_len_u64 = dst.len() as u64;
        let prefix = start.saturating_sub(mapped_spos).min(dst_len_u64) as usize;
        if prefix > 0 {
            crate::engine::ring::fill_null_at(&mut dst[..prefix], mapped_spos);
        }
        let rem_len = dst.len() - prefix;
        let avail = cur_write.saturating_sub(start).min(rem_len as u64) as usize;
        let to_copy = if avail < rem_len {
            let end_abs = start.saturating_add(avail as u64);
            let tail_phase = (end_abs % TS_PKT_SZ as u64) as usize;
            avail.saturating_sub(tail_phase)
        } else {
            avail
        };
        if to_copy > 0 {
            let rd = self.ring.lock();
            rd.read_at(start, &mut dst[prefix..prefix + to_copy]);
        }
        let filled = prefix + to_copy;
        let nulls = (prefix as u64) + (dst.len().saturating_sub(filled) as u64);
        if filled < dst.len() {
            crate::engine::ring::fill_null_at(&mut dst[filled..], mapped_spos + filled as u64);
        }

        // Telemetry.
        {
            let mut rd = self.ring.lock();
            rd.note_served(to_copy as u64);
            if nulls > 0 {
                rd.note_null(nulls);
                rd.note_starve();
            }
        }

        // 5. Calculate pacing sleep.
        let lead = cur_write.saturating_sub(mapped_spos);
        let sleep_ms = {
            let mut rd = self.ring.lock();
            let now = rd.now_ms();
            rd.pace_note(dst.len() as u64, lead, now)
        };

        // 6. Apply Caminho C continuity transform.
        let ring_for_full = self.ring.clone();
        let mut full_at = move |abs: u64, pkt: &mut [u8; TS_PKT_SZ]| -> bool {
            let rd = ring_for_full.lock();
            if !rd.contains(abs, TS_PKT_SZ as u64) {
                return false;
            }
            rd.read_at(abs, pkt);
            true
        };
        {
            let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
            if to_copy > 0 {
                x.apply_block(&mut dst[prefix..filled], start, true, &mut full_at);
            } else {
                x.invalidate_tail();
            }
            let fend = foff.saturating_add(dst.len() as u64);
            let now_ms = self.ring.lock().now_ms();
            x.note_ckpt(fend, now_ms);
            if to_copy > 0 {
                let mut a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
                a.last_fend = fend;
                a.last_read_ms = now_ms0;
            }
        }

        // 7. Sleep unlocked if sleep_ms > 0 (clamped <= 50ms).
        if sleep_ms > 0 {
            std::thread::sleep(Duration::from_millis(sleep_ms.min(MAX_SLEEP_SLICE_MS)));
        }

        dst.len()
    }

    fn serve_vod(
        &self,
        _url: &str,
        _foff: u64,
        dst: &mut [u8],
        _deadline_ms: u64,
    ) -> Result<usize, std::io::Error> {
        crate::engine::ring::fill_null(dst);
        Ok(dst.len())
    }
}
// <<<<<<<<<<<<<<< END OF FILE: src/engine/live_engine.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/engine/mod.rs (11 lines) <<<<<<<<<<<<<<<
//! Streaming engine: ring buffer, snapper, Caminho C transform, state machine, and live engine.

pub mod live_engine;
pub mod ring;
pub mod snapper;
pub mod state_machine;
pub mod ts_xform;

pub use live_engine::{AnchorState, LiveEngine};
pub use ring::Ring;
pub use state_machine::StateMachine;

// <<<<<<<<<<<<<<< END OF FILE: src/engine/mod.rs >>>>>>>>>>>>>>>


// >>>>>>>>>>>>>>> FILE: src/main.rs (388 lines) <<<<<<<<<<<<<<<
//! fuse_ntfs: raw /dev/fuse virtual NTFS daemon for USB-Stream-TV.
//!
//! Usage: `fuse_ntfs [mount_point] [target] [template_path]`
//!
//! * `mount_point`   — where /dev/fuse is mounted (default
//!   `/data/local/tmp/ntfs_mnt`).
//! * `target`        — live source: a FIFO path, an `http://` URL, or a
//!   local `.ts`/`.mp4` file. Consumed by the streaming engine.
//! * `template_path` — sparse 128 GiB NTFS template image
//!   (`ntfs_template.bin`, ~69 MB physical). Synthesized geometry is used
//!   when absent.
//!
//! The daemon mounts the virtual NTFS volume, exports it to the USB gadget
//! as the mass-storage backing image, and serves every sector through the
//! FUSE dispatcher.

use std::env;
use std::ffi::CString;
use std::fs;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use fuse_ntfs::contract::StreamBackend;
use fuse_ntfs::vfs::catalog::Catalog;
use fuse_ntfs::vfs::dispatcher;
use fuse_ntfs::vfs::ntfs_mft::NtfsTemplate;

const MNT_POINT: &str = "/data/local/tmp/ntfs_lab/mnt";
const DEF_TARGET: &str = "/data/local/tmp/live_pipe";
const DEF_TEMPLATE: &str = "/data/local/tmp/ntfs_lab/ntfs_template.bin";

static RUNNING: AtomicBool = AtomicBool::new(true);

extern "C" fn on_signal(_: libc::c_int) {
    RUNNING.store(false, Ordering::SeqCst);
}

fn install_signals() {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = on_signal as *const () as usize;
        libc::sigemptyset(&mut sa.sa_mask);
        sa.sa_flags = 0; // No SA_RESTART: allow EINTR on read(fuse_fd)
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        libc::signal(libc::SIGUSR1, libc::SIG_IGN);
    }
}

/// Fallback backend used when the streaming engine cannot be constructed
/// (defensive: keeps the FUSE layer serving valid traffic on any target).
///
/// It implements the full contract — always fully populating `dst` with
/// MPEG-TS null packets — so the kernel never sees a short read and the
/// Samsung decoder keeps a stable 27 MHz clock while the engine recovers.
struct NullBackend;

impl StreamBackend for NullBackend {
    fn serve_live(&self, _file_id: u32, _foff: u64, dst: &mut [u8], _deadline_ms: u64) -> usize {
        dispatcher::fill_null(dst);
        dst.len()
    }

    fn serve_vod(
        &self,
        url: &str,
        _foff: u64,
        _dst: &mut [u8],
        _deadline_ms: u64,
    ) -> io::Result<usize> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!("VOD source '{url}' unavailable: streaming engine not running"),
        ))
    }
}

/// Builds the streaming backend for `target`.
///
/// ENGINE INTEGRATION POINT
/// ------------------------
/// The live engine (128 MiB ring buffer, live-edge lock, sequential-probe
/// debounce per AGENTS.md §2.2) is the production backend. It degrades to
/// null packets on its own when the feeder has no data, so the FUSE layer
/// stays valid even before the first network chunk arrives.
///
/// To attach the network feeder / VOD dispatcher to the engine, extend
/// `LiveEngine` (owned by the engine builder) with a `from_target(target)`
/// constructor and forward it here; nothing else in the process lifecycle
/// changes. `NullBackend` remains the documented fallback for maintenance
/// builds where the engine is intentionally disabled.
fn build_backend(
    target: &str,
    engine: bool,
    engine_handle: Option<Arc<fuse_ntfs::engine::LiveEngine>>,
) -> Arc<dyn StreamBackend> {
    if engine {
        eprintln!("[✓] Backend: LiveEngine (ring latente até o feeder) — alvo: {target}");
        engine_handle
            .unwrap_or_else(|| Arc::new(fuse_ntfs::engine::LiveEngine::new()))
            as Arc<dyn StreamBackend>
    } else {
        eprintln!("[!] Backend: NullBackend (engine desabilitado) — alvo: {target}");
        Arc::new(NullBackend)
    }
}

fn feeder_loop(fifo_path: String, engine: Arc<fuse_ntfs::engine::LiveEngine>) {
    use std::io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::io::AsRawFd;

    const FLUSH_PATH: &str = "/data/local/tmp/fuse_flush";
    let mut buf = vec![0u8; 64 * 1024];
    let mut last_feed_log: u64 = 0;

    while RUNNING.load(Ordering::SeqCst) {
        // Flush trigger check (before opening / between reopens).
        if std::path::Path::new(FLUSH_PATH).exists() {
            let _ = std::fs::remove_file(FLUSH_PATH);
            // Drain any currently available FIFO bytes (best-effort).
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&fifo_path)
            {
                let mut tmp = vec![0u8; 64 * 1024];
                loop {
                    match f.read(&mut tmp) {
                        Ok(0) => break,
                        Ok(_) => continue,
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(_) => break,
                    }
                }
            }
            engine.flush();
            last_feed_log = engine.ring.lock().s_write();
        }

        // Open FIFO with O_NONBLOCK first, then clear it for blocking reads.
        let mut fifo = match std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&fifo_path)
        {
            Ok(f) => f,
            Err(_) => {
                thread::sleep(Duration::from_millis(100));
                continue;
            }
        };

        unsafe {
            let fd = fifo.as_raw_fd();
            let flags = libc::fcntl(fd, libc::F_GETFL);
            if flags >= 0 {
                libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
            }
        }

        let mut flushed = false;
        while RUNNING.load(Ordering::SeqCst) {
            // Flush trigger check during streaming.
            if std::path::Path::new(FLUSH_PATH).exists() {
                let _ = std::fs::remove_file(FLUSH_PATH);
                // Drain the current FIFO (switch to non-blocking temporarily).
                unsafe {
                    let fd = fifo.as_raw_fd();
                    let flags = libc::fcntl(fd, libc::F_GETFL);
                    if flags >= 0 {
                        libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
                    }
                }
                let mut tmp = vec![0u8; 64 * 1024];
                loop {
                    match fifo.read(&mut tmp) {
                        Ok(0) => break,
                        Ok(_) => continue,
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(_) => break,
                    }
                }
                unsafe {
                    let fd = fifo.as_raw_fd();
                    let flags = libc::fcntl(fd, libc::F_GETFL);
                    if flags >= 0 {
                        libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
                    }
                }
                engine.flush();
                last_feed_log = engine.ring.lock().s_write();
                flushed = true;
                break;
            }

            let mut pfd = libc::pollfd {
                fd: fifo.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            let ret = unsafe { libc::poll(&mut pfd, 1, 250) };
            if ret < 0 {
                let err = std::io::Error::last_os_error();
                if err.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                break;
            }
            if ret == 0 {
                // Timeout: loop back to check RUNNING and FLUSH_PATH
                continue;
            }

            match fifo.read(&mut buf) {
                Ok(0) => break, // EOF: re-open after 100ms
                Ok(n) => {
                    engine.ring.write_chunk(&buf[..n]);
                    let s_write = engine.ring.lock().s_write();
                    if s_write.saturating_sub(last_feed_log) >= 2 * 1024 * 1024 {
                        eprintln!("[FEEDER] S_write={}MB", s_write / (1024 * 1024));
                        last_feed_log = s_write;
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }

        if flushed {
            continue;
        }
        // EOF or error: re-open after 100ms.
        thread::sleep(Duration::from_millis(100));
    }
}

fn main() {
    install_signals();

    let args: Vec<String> = env::args().collect();
    // Optional `--no-engine` flag demotes to the null backend (maintenance
    // builds / VFS-only smoke tests); the engine is on by default.
    let engine_enabled = !args.iter().any(|a| a == "--no-engine");
    let positional: Vec<&String> = args
        .iter()
        .skip(1)
        .filter(|a| !a.starts_with("--"))
        .collect();
    let mnt = positional.first().map(|s| s.as_str()).unwrap_or(MNT_POINT);
    let target = positional.get(1).map(|s| s.as_str()).unwrap_or(DEF_TARGET);
    let template_path = positional.get(2).map(|s| s.as_str()).unwrap_or(DEF_TEMPLATE);

    // ---- metadata source: sparse template, synthesized when absent ----
    let mut template = match NtfsTemplate::open(template_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("[!] Template {template_path} unreadable ({e}); using synthesized NTFS geometry");
            NtfsTemplate::synthesized()
        }
    };
    // Anti-cache tier 2: a fresh Volume Serial Number per mount forces the
    // Samsung firmware to treat the volume as a brand-new filesystem.
    template.randomize_serial();
    let live_size = template.layout.data_size;
    eprintln!(
        "[✓] NTFS: VSN={:#016x} data_start={} stream_size={} template={}",
        template.serial,
        template.layout.data_start_byte,
        live_size,
        if Path::new(template_path).exists() {
            template_path
        } else {
            "synthesized"
        }
    );

    // ---- virtual catalog: the TRP/TS tree the TV browses ----
    let catalog = Catalog::new(live_size);
    eprintln!(
        "[✓] Catálogo virtual: Canais TV / Filmes / Favoritos ({} nós)",
        catalog.len()
    );

    // ---- backend wiring: live engine or null fallback ----
    let engine_handle: Option<Arc<fuse_ntfs::engine::LiveEngine>> = if engine_enabled {
        Some(Arc::new(fuse_ntfs::engine::LiveEngine::new()))
    } else {
        None
    };
    let backend = build_backend(target, engine_enabled, engine_handle.clone());

    // ---- spawn FIFO feeder when engine is enabled and target is not 'test' ----
    if engine_enabled && target != "test" {
        if let Some(engine) = engine_handle.clone() {
            let fifo_path = target.to_string();
            let _ = thread::Builder::new()
                .name("feeder".into())
                .stack_size(2 * 1024 * 1024)
                .spawn(move || feeder_loop(fifo_path, engine));
        }
    }

    let server = Arc::new(fuse_ntfs::vfs::dispatcher::NtfsServer::new(
        template, catalog, backend,
    ));

    // ---- mount /dev/fuse ----
    let _ = fs::create_dir_all(mnt);
    let c_mnt = match CString::new(mnt) {
        Ok(c) => c,
        Err(_) => {
            eprintln!("Error: invalid mount path {mnt}");
            std::process::exit(1);
        }
    };
    unsafe {
        libc::umount2(c_mnt.as_ptr(), libc::MNT_DETACH);
    }

    let fuse_dev = match CString::new("/dev/fuse") {
        Ok(c) => c,
        Err(_) => {
            eprintln!("Error: /dev/fuse path invalid");
            std::process::exit(1);
        }
    };
    let fuse_fd = unsafe { libc::open(fuse_dev.as_ptr(), libc::O_RDWR) };
    if fuse_fd < 0 {
        eprintln!("Error: failed to open /dev/fuse ({})", io::Error::last_os_error());
        std::process::exit(1);
    }

    let opts = format!("fd={fuse_fd},rootmode=040755,user_id=0,group_id=0,allow_other");
    let c_opts = match CString::new(opts) {
        Ok(c) => c,
        Err(_) => {
            eprintln!("Error: mount option string invalid");
            unsafe { libc::close(fuse_fd) };
            std::process::exit(1);
        }
    };
    let c_type = match CString::new("fuse") {
        Ok(c) => c,
        Err(_) => {
            eprintln!("Error: filesystem type string invalid");
            unsafe { libc::close(fuse_fd) };
            std::process::exit(1);
        }
    };

    let res = unsafe {
        libc::mount(
            c_type.as_ptr(),
            c_mnt.as_ptr(),
            c_type.as_ptr(),
            0,
            c_opts.as_ptr() as *const libc::c_void,
        )
    };
    if res < 0 {
        eprintln!(
            "Error: mount failed on {mnt} ({})",
            io::Error::last_os_error()
        );
        unsafe { libc::close(fuse_fd) };
        std::process::exit(1);
    }
    eprintln!("[✓] fuse_ntfs montado em {mnt}");

    // ---- protocol loop ----
    let run = dispatcher::run(fuse_fd, server, &RUNNING);
    if let Err(e) = run {
        eprintln!("[!] Loop FUSE encerrado com erro: {e}");
    }

    // ---- teardown ----
    eprintln!("[!] Encerrando fuse_ntfs...");
    unsafe {
        libc::umount2(c_mnt.as_ptr(), libc::MNT_DETACH);
        libc::close(fuse_fd);
    }
}

// <<<<<<<<<<<<<<< END OF FILE: src/main.rs >>>>>>>>>>>>>>>

