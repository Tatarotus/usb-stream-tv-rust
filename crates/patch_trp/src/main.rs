//! patch_trp v2 (Rust port of patch_trp.c)
//!
//! In-place NTFS MFT & Directory Index patcher for Samsung ConnectShare.
//! Dynamically alternates live stream filenames ("TV AO VIVO.trp" <-> "TV AO VIVO 2.tp")
//! inside ntfs_template.bin without unmounting the USB mass storage gadget.
//!
//! Synthesized from Gemini 4 Argon & Claude Sonnet 5 Adversarial Audits:
//! - Zero external dependencies; zero-panic guarantee on arbitrary/corrupted disk images.
//! - Validates 112-byte in-place alignment invariant for both 14-char and 15-char names.
//! - Strict Update Sequence Array (USA/Fixup) decode & encode with boundary checks.
//! - Safe non-resident data run parser with checked 64-bit arithmetic and no signed UB.
//! - Two-phase validation with dual sync barriers (fdatasync) to ensure crash consistency on eMMC.

#![forbid(unsafe_op_in_unsafe_fn)]

use std::env;
use std::fmt;
use std::fs::OpenOptions;
use std::io::{self, Seek, SeekFrom};
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::process::ExitCode;

const SEC_SIZE: usize = 512;
const ROOT_INODE: u64 = 5;
const MAX_MFT_SCAN: u64 = 256;

const NAME_A: &str = "TV AO VIVO.trp";
const NAME_B: &str = "TV AO VIVO 2.tp";

const LOCK_EX: i32 = 2;
const LOCK_NB: i32 = 4;

extern "C" {
    fn flock(fd: i32, operation: i32) -> i32;
}

#[derive(Debug, PartialEq, Eq)]
pub enum PatchError {
    Io(String),
    LockBusy,
    InvalidVbr(&'static str),
    TornRecord { magic: [u8; 4], sector: usize, expected: u16, found: u16 },
    CorruptRecord(&'static str),
    TargetNotFound,
    DirectoryEntryNotFound(u64),
    CollationViolation(&'static str),
    LayoutMismatch(&'static str),
}

impl fmt::Display for PatchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(e) => write!(f, "I/O error: {e}"),
            Self::LockBusy => write!(f, "Could not acquire exclusive flock (concurrent process active)"),
            Self::InvalidVbr(msg) => write!(f, "Invalid NTFS VBR: {msg}"),
            Self::TornRecord { magic, sector, expected, found } => write!(
                f,
                "Torn record in {}{}{}{} at sector {sector} (USN 0x{found:04x} != 0x{expected:04x})",
                magic[0] as char, magic[1] as char, magic[2] as char, magic[3] as char
            ),
            Self::CorruptRecord(msg) => write!(f, "Corrupted NTFS structure: {msg}"),
            Self::TargetNotFound => write!(f, "Stream file '{NAME_A}' or '{NAME_B}' not found in MFT"),
            Self::DirectoryEntryNotFound(ino) => {
                write!(f, "Directory index entry for Inode {ino} not found in root index")
            }
            Self::CollationViolation(loc) => write!(
                f,
                "Collation error: B-Tree alphabetical order would be violated in {loc} (use --force-order)"
            ),
            Self::LayoutMismatch(loc) => {
                write!(f, "Layout error: In-place entry size incompatible in {loc}")
            }
        }
    }
}

impl From<io::Error> for PatchError {
    fn from(e: io::Error) -> Self {
        Self::Io(e.to_string())
    }
}

// ------------------------------------------------------------- Safe Endian Readers

#[inline]
fn get_u16(buf: &[u8], off: usize) -> Result<u16, PatchError> {
    let bytes: [u8; 2] = buf
        .get(off..off.checked_add(2).ok_or(PatchError::CorruptRecord("u16 offset overflow"))?)
        .ok_or(PatchError::CorruptRecord("u16 out of bounds"))?
        .try_into()
        .map_err(|_| PatchError::CorruptRecord("u16 slice conversion"))?;
    Ok(u16::from_le_bytes(bytes))
}

#[inline]
fn get_u32(buf: &[u8], off: usize) -> Result<u32, PatchError> {
    let bytes: [u8; 4] = buf
        .get(off..off.checked_add(4).ok_or(PatchError::CorruptRecord("u32 offset overflow"))?)
        .ok_or(PatchError::CorruptRecord("u32 out of bounds"))?
        .try_into()
        .map_err(|_| PatchError::CorruptRecord("u32 slice conversion"))?;
    Ok(u32::from_le_bytes(bytes))
}

#[inline]
fn get_u64(buf: &[u8], off: usize) -> Result<u64, PatchError> {
    let bytes: [u8; 8] = buf
        .get(off..off.checked_add(8).ok_or(PatchError::CorruptRecord("u64 offset overflow"))?)
        .ok_or(PatchError::CorruptRecord("u64 out of bounds"))?
        .try_into()
        .map_err(|_| PatchError::CorruptRecord("u64 slice conversion"))?;
    Ok(u64::from_le_bytes(bytes))
}

#[inline]
fn put_u16(buf: &mut [u8], off: usize, val: u16) -> Result<(), PatchError> {
    let end = off.checked_add(2).ok_or(PatchError::CorruptRecord("put_u16 overflow"))?;
    let dst = buf.get_mut(off..end).ok_or(PatchError::CorruptRecord("put_u16 out of bounds"))?;
    dst.copy_from_slice(&val.to_le_bytes());
    Ok(())
}

#[inline]
fn put_u32(buf: &mut [u8], off: usize, val: u32) -> Result<(), PatchError> {
    let end = off.checked_add(4).ok_or(PatchError::CorruptRecord("put_u32 overflow"))?;
    let dst = buf.get_mut(off..end).ok_or(PatchError::CorruptRecord("put_u32 out of bounds"))?;
    dst.copy_from_slice(&val.to_le_bytes());
    Ok(())
}

// ---------------------------------------------------- Update Sequence Array (USA)

/// Decodes and validates the NTFS Update Sequence Array (USA) in-place.
/// Replaces sector tail words with the original bytes saved in the USA.
pub fn ntfs_fixup_decode(rec: &mut [u8], magic: &[u8; 4], min_payload_off: usize) -> Result<(), PatchError> {
    if rec.len() < SEC_SIZE || rec.len() % SEC_SIZE != 0 {
        return Err(PatchError::CorruptRecord("Record size not a multiple of 512"));
    }
    if rec.get(0..4) != Some(magic.as_slice()) {
        return Err(PatchError::CorruptRecord("Record magic mismatch"));
    }
    let off = usize::from(get_u16(rec, 4)?);
    let cnt = usize::from(get_u16(rec, 6)?);
    let expected_cnt = (rec.len() / SEC_SIZE) + 1;
    let usa_end = off
        .checked_add(cnt.checked_mul(2).ok_or(PatchError::CorruptRecord("USA size overflow"))?)
        .ok_or(PatchError::CorruptRecord("USA end overflow"))?;

    if off < 0x28 || (off & 1) != 0 || cnt != expected_cnt || usa_end > min_payload_off || usa_end > rec.len() {
        return Err(PatchError::CorruptRecord("Invalid USA header bounds"));
    }

    let usn = get_u16(rec, off)?;
    if usn == 0 || usn == 0xFFFF {
        return Err(PatchError::CorruptRecord("Invalid USN value (0 or 0xFFFF)"));
    }

    for i in 1..cnt {
        let tail_off = i * SEC_SIZE - 2;
        let tail_usn = get_u16(rec, tail_off)?;
        if tail_usn != usn {
            return Err(PatchError::TornRecord {
                magic: *magic,
                sector: i,
                expected: usn,
                found: tail_usn,
            });
        }
        let saved = get_u16(rec, off + i * 2)?;
        put_u16(rec, tail_off, saved)?;
    }
    Ok(())
}

/// Increments the USN and encodes sector tails back into the Update Sequence Array.
/// Strictly re-validates off and cnt bounds before mutation (addressing Sonnet F-03).
pub fn ntfs_fixup_encode(rec: &mut [u8], min_payload_off: usize) -> Result<(), PatchError> {
    if rec.len() < SEC_SIZE || rec.len() % SEC_SIZE != 0 {
        return Err(PatchError::CorruptRecord("Record size not a multiple of 512"));
    }
    let off = usize::from(get_u16(rec, 4)?);
    let cnt = usize::from(get_u16(rec, 6)?);
    let expected_cnt = (rec.len() / SEC_SIZE) + 1;
    let usa_end = off
        .checked_add(cnt.checked_mul(2).ok_or(PatchError::CorruptRecord("USA size overflow"))?)
        .ok_or(PatchError::CorruptRecord("USA end overflow"))?;

    if off < 0x28 || (off & 1) != 0 || cnt != expected_cnt || usa_end > min_payload_off || usa_end > rec.len() {
        return Err(PatchError::CorruptRecord("Invalid USA header bounds in encode"));
    }

    let mut usn = get_u16(rec, off)?.wrapping_add(1);
    if usn == 0 || usn == 0xFFFF {
        usn = 1;
    }
    put_u16(rec, off, usn)?;

    for i in 1..cnt {
        let tail_off = i * SEC_SIZE - 2;
        let orig_bytes = get_u16(rec, tail_off)?;
        put_u16(rec, off + i * 2, orig_bytes)?;
        put_u16(rec, tail_off, usn)?;
    }
    Ok(())
}

// ------------------------------------------------------------- Collation & Encoding

#[inline]
pub fn up16(c: u16) -> u16 {
    if (b'a' as u16..=b'z' as u16).contains(&c) {
        c - 32
    } else {
        c
    }
}

pub fn coll_utf16_le(a: &[u8], b: &[u8]) -> Result<std::cmp::Ordering, PatchError> {
    let a_chars = a.len() / 2;
    let b_chars = b.len() / 2;
    let min_chars = a_chars.min(b_chars);
    for i in 0..min_chars {
        let x = up16(get_u16(a, i * 2)?);
        let y = up16(get_u16(b, i * 2)?);
        if x != y {
            return Ok(x.cmp(&y));
        }
    }
    Ok(a_chars.cmp(&b_chars))
}

pub fn encode_ascii_utf16le(s: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(s.len() * 2);
    for b in s.bytes() {
        out.push(b);
        out.push(0);
    }
    out
}

// -------------------------------------------------------- Data Runs Parser (Safe)

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DataRun {
    pub lcn: Option<u64>, // None = sparse run
    pub cluster_len: u64,
}

pub struct DataRunIter<'a> {
    buf: &'a [u8],
    current_lcn: i64,
}

impl<'a> DataRunIter<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Self { buf, current_lcn: 0 }
    }

    pub fn next_run(&mut self) -> Result<Option<DataRun>, PatchError> {
        let Some(&hdr) = self.buf.first() else {
            return Ok(None);
        };
        if hdr == 0 {
            return Ok(None);
        }
        self.buf = &self.buf[1..];

        let len_sz = usize::from(hdr & 0x0F);
        let off_sz = usize::from(hdr >> 4);
        if len_sz == 0 || len_sz > 8 || off_sz > 8 || self.buf.len() < len_sz + off_sz {
            return Err(PatchError::CorruptRecord("Invalid data run header sizes"));
        }

        let mut cluster_len: u64 = 0;
        for b in 0..len_sz {
            cluster_len |= u64::from(self.buf[b]) << (8 * b);
        }
        self.buf = &self.buf[len_sz..];

        if cluster_len == 0 {
            return Err(PatchError::CorruptRecord("Invalid zero data run cluster length"));
        }

        if off_sz == 0 {
            return Ok(Some(DataRun { lcn: None, cluster_len }));
        }

        let mut raw_delta: u64 = 0;
        for b in 0..off_sz {
            raw_delta |= u64::from(self.buf[b]) << (8 * b);
        }
        self.buf = &self.buf[off_sz..];

        // Sign-extend variable-length two's complement integer (1..=8 bytes) without UB
        if off_sz < 8 && ((raw_delta >> (off_sz * 8 - 1)) & 1) != 0 {
            raw_delta |= !0u64 << (off_sz * 8);
        }
        let delta = i64::from_ne_bytes(raw_delta.to_ne_bytes());

        // Checked signed addition preventing UB on INT64_MIN (Argon F-03)
        let new_lcn = self
            .current_lcn
            .checked_add(delta)
            .ok_or(PatchError::CorruptRecord("Data run LCN signed overflow"))?;
        if new_lcn < 0 {
            return Err(PatchError::CorruptRecord("Data run LCN negative"));
        }
        self.current_lcn = new_lcn;

        Ok(Some(DataRun {
            lcn: Some(new_lcn as u64),
            cluster_len,
        }))
    }
}

// ------------------------------------------------------------- Directory Index Walk

/// Walks an `INDEX_NODE_HEADER` slice, validates layout & collation, and optionally mutates in-place.
pub fn index_rename(
    node_slice: &mut [u8],
    target_ino: u64,
    target_seq: u16,
    new_u16: &[u8],
    commit: bool,
    force_order: bool,
    loc_name: &'static str,
) -> Result<bool, PatchError> {
    if node_slice.len() < 16 {
        return Err(PatchError::CorruptRecord("INDEX_NODE_HEADER < 16 bytes"));
    }
    let eoff = usize::try_from(get_u32(node_slice, 0)?).map_err(|_| PatchError::CorruptRecord("eoff"))?;
    let ilen = usize::try_from(get_u32(node_slice, 4)?).map_err(|_| PatchError::CorruptRecord("ilen"))?;
    let alen = usize::try_from(get_u32(node_slice, 8)?).map_err(|_| PatchError::CorruptRecord("alen"))?;

    if eoff < 16 || eoff > ilen || ilen > alen || ilen > node_slice.len() {
        return Err(PatchError::CorruptRecord("Invalid INDEX_NODE_HEADER bounds"));
    }

    let new_chars = u8::try_from(new_u16.len() / 2).map_err(|_| PatchError::CorruptRecord("name too long"))?;
    let mut cur = eoff;
    let mut prev_range: Option<(usize, usize)> = None;

    while cur.checked_add(16).map_or(false, |end_hdr| end_hdr <= ilen) {
        let elen = usize::from(get_u16(node_slice, cur + 8)?);
        let klen = usize::from(get_u16(node_slice, cur + 10)?);
        let flags = get_u32(node_slice, cur + 12)?;

        if elen < 16 || (elen & 7) != 0 || cur.checked_add(elen).map_or(true, |e| e > ilen) {
            return Err(PatchError::CorruptRecord("Invalid INDEX_ENTRY length"));
        }
        if (flags & 2) != 0 {
            break; // End marker entry
        }

        let entry_ino = get_u64(node_slice, cur)? & 0x0000_FFFF_FFFF_FFFF;
        let entry_seq = get_u16(node_slice, cur + 6)?;

        if klen >= 66 && 16 + klen <= elen && entry_ino == target_ino && entry_seq == target_seq {
            let parent_ino = get_u64(node_slice, cur + 16)? & 0x0000_FFFF_FFFF_FFFF;
            if parent_ino == ROOT_INODE {
                // Validate collation against previous entry in same block
                if let Some((p_off, p_elen)) = prev_range {
                    let p_chars = usize::from(*node_slice.get(p_off + 80).ok_or(PatchError::CorruptRecord("prev chars"))?);
                    let p_name_end = p_off + 82 + p_chars * 2;
                    if p_name_end <= p_off + p_elen {
                        let p_name = &node_slice[p_off + 82..p_name_end];
                        if coll_utf16_le(p_name, new_u16)?.is_ge() && !force_order {
                            return Err(PatchError::CollationViolation(loc_name));
                        }
                    }
                }

                // Validate collation against next entry in same block (if not end marker)
                let next_off = cur + elen;
                if next_off + 16 <= ilen {
                    let n_elen = usize::from(get_u16(node_slice, next_off + 8)?);
                    let n_flags = get_u32(node_slice, next_off + 12)?;
                    if (n_flags & 2) == 0 && next_off + n_elen <= ilen {
                        let n_chars = usize::from(*node_slice.get(next_off + 80).ok_or(PatchError::CorruptRecord("next chars"))?);
                        let n_name_end = next_off + 82 + n_chars * 2;
                        if n_name_end <= next_off + n_elen {
                            let n_name = &node_slice[next_off + 82..n_name_end];
                            if coll_utf16_le(new_u16, n_name)?.is_ge() && !force_order {
                                return Err(PatchError::CollationViolation(loc_name));
                            }
                        }
                    }
                }

                // Verify 8-byte aligned entry size invariant (14-char vs 15-char proof: both require 112 bytes)
                let nklen = 66usize + new_u16.len();
                let subnode_extra = if (flags & 1) != 0 { 8usize } else { 0usize };
                let need = ((16 + nklen + 7) & !7) + subnode_extra;
                if need != elen {
                    return Err(PatchError::LayoutMismatch(loc_name));
                }

                if commit {
                    put_u16(node_slice, cur + 10, nklen as u16)?;
                    node_slice[cur + 80] = new_chars;
                    let name_dst = &mut node_slice[cur + 82..cur + 82 + new_u16.len()];
                    name_dst.copy_from_slice(new_u16);

                    let used = 82 + new_u16.len();
                    let tail = elen - subnode_extra; // Preserve trailing 8-byte sub-node VCN if present
                    if tail > used {
                        node_slice[cur + used..cur + tail].fill(0);
                    }
                }
                return Ok(true);
            }
        }

        prev_range = Some((cur, elen));
        cur += elen;
    }

    Ok(false)
}

// ----------------------------------------------------------------- VBR Parsing

#[derive(Debug, PartialEq, Eq)]
pub struct VbrInfo {
    pub bps: u16,
    pub spc: u8,
    pub cluster_size: u64,
    pub mft_lcn: u64,
    pub rec_size: usize,
    pub idx_sz: usize,
    pub mft_base: u64,
}

pub fn parse_vbr(vbr: &[u8; 512]) -> Result<VbrInfo, PatchError> {
    if vbr[510] != 0x55 || vbr[511] != 0xAA || &vbr[3..11] != b"NTFS    " {
        return Err(PatchError::InvalidVbr("Missing 0x55AA signature or 'NTFS    ' OEM ID"));
    }

    let bps = get_u16(vbr, 11)?;
    if !matches!(bps, 512 | 1024 | 2048 | 4096) {
        return Err(PatchError::InvalidVbr("Invalid Bytes Per Sector (BPS)"));
    }

    let spc = vbr[13];
    if spc == 0 || (spc & (spc - 1)) != 0 {
        return Err(PatchError::InvalidVbr("SPC must be a non-zero power of two"));
    }

    let cluster_size = u64::from(bps) * u64::from(spc);
    let mft_lcn = get_u64(vbr, 48)?;

    let mft_rec_code = vbr[64] as i8;
    let rec_size: usize = if mft_rec_code < 0 {
        let shift = (-i32::from(mft_rec_code)) as u32;
        if !(9..=12).contains(&shift) {
            return Err(PatchError::InvalidVbr("MFT record shift out of valid range (512..4096)"));
        }
        1usize << shift
    } else if mft_rec_code > 0 {
        usize::try_from(u64::from(mft_rec_code as u8).saturating_mul(cluster_size))
            .map_err(|_| PatchError::InvalidVbr("MFT rec_size overflow"))?
    } else {
        return Err(PatchError::InvalidVbr("MFT record size code is 0"));
    };

    if !(512..=4096).contains(&rec_size) || (rec_size & (rec_size - 1)) != 0 {
        return Err(PatchError::InvalidVbr("MFT record size must be power of two in 512..=4096"));
    }

    let ic = vbr[68] as i8;
    let idx_sz: usize = if ic < 0 {
        let shift = (-i32::from(ic)) as u32;
        if (9..=16).contains(&shift) {
            1usize << shift
        } else {
            4096
        }
    } else if ic > 0 {
        usize::try_from(u64::from(ic as u8).saturating_mul(cluster_size)).unwrap_or(4096)
    } else {
        4096
    };
    let idx_sz = if (512..=65536).contains(&idx_sz) && (idx_sz & (idx_sz - 1)) == 0 {
        idx_sz
    } else {
        4096
    };

    let mft_base = mft_lcn
        .checked_mul(cluster_size)
        .ok_or(PatchError::InvalidVbr("mft_base byte offset overflow"))?;

    Ok(VbrInfo {
        bps,
        spc,
        cluster_size,
        mft_lcn,
        rec_size,
        idx_sz,
        mft_base,
    })
}

// ------------------------------------------------------------- Main Pipeline

fn run() -> Result<(), PatchError> {
    let mut path = String::from("/data/local/tmp/ntfs_lab/ntfs_template.bin");
    let mut force_mode: Option<String> = None;
    let mut force_order = false;

    for arg in env::args().skip(1) {
        if arg == "--force-order" {
            force_order = true;
        } else if arg.starts_with('-') {
            force_mode = Some(arg);
        } else {
            path = arg;
        }
    }

    let mut file = OpenOptions::new().read(true).write(true).open(&path)?;

    // Exclusive non-blocking flock to eliminate multi-process races
    let lock_rc = unsafe { flock(file.as_raw_fd(), LOCK_EX | LOCK_NB) };
    if lock_rc < 0 {
        return Err(PatchError::LockBusy);
    }

    // SeekFrom::End(0) works accurately on regular files and raw block devices
    let img_size = file.seek(SeekFrom::End(0))?;

    let name_a_u16 = encode_ascii_utf16le(NAME_A);
    let name_b_u16 = encode_ascii_utf16le(NAME_B);

    // 1. Parse and validate VBR
    let mut vbr = [0u8; 512];
    file.read_exact_at(&mut vbr, 0)?;
    let vbr_info = parse_vbr(&vbr)?;

    println!(
        "[*] NTFS VBR: BPS={} SPC={} cluster_sz={} mft_lcn={} rec_sz={} idx_sz={} mft_base={}",
        vbr_info.bps,
        vbr_info.spc,
        vbr_info.cluster_size,
        vbr_info.mft_lcn,
        vbr_info.rec_size,
        vbr_info.idx_sz,
        vbr_info.mft_base
    );

    // 2. Scan MFT records for target file and validate resident $FILE_NAME layout capacity
    let mut mft_buf = vec![0u8; vbr_info.rec_size];
    let mut target_info: Option<(u64, u16, u64, usize, bool)> = None;

    for ino in 0..MAX_MFT_SCAN {
        let Some(off) = vbr_info
            .mft_base
            .checked_add(ino.checked_mul(vbr_info.rec_size as u64).ok_or(PatchError::CorruptRecord("MFT off"))?)
        else {
            break;
        };
        if off.checked_add(vbr_info.rec_size as u64).map_or(true, |end| end > img_size) {
            break;
        }
        if file.read_exact_at(&mut mft_buf, off).is_err() {
            break;
        }
        if mft_buf.get(0..4) != Some(b"FILE") {
            continue;
        }

        let Ok(attr_off_u16) = get_u16(&mft_buf, 20) else { continue };
        let attr_off = usize::from(attr_off_u16);
        if attr_off < 42 || attr_off + 8 > vbr_info.rec_size {
            continue;
        }

        if ntfs_fixup_decode(&mut mft_buf, b"FILE", attr_off).is_err() {
            continue;
        }

        // Must be in-use (flag & 1) and a base MFT record (base_ref at +32 == 0)
        let Ok(flags) = get_u16(&mft_buf, 22) else { continue };
        let Ok(base_ref) = get_u64(&mft_buf, 32) else { continue };
        if (flags & 1) == 0 || base_ref != 0 {
            continue;
        }

        let mut cur = attr_off;
        while cur + 8 <= vbr_info.rec_size {
            let Ok(attr_type) = get_u32(&mft_buf, cur) else { break };
            let Ok(attr_len_u32) = get_u32(&mft_buf, cur + 4) else { break };
            let attr_len = attr_len_u32 as usize;
            if attr_type == 0xFFFF_FFFF || attr_len == 0 || cur.checked_add(attr_len).map_or(true, |e| e > vbr_info.rec_size) {
                break;
            }

            if attr_type == 0x30 && mft_buf.get(cur + 8) == Some(&0) && attr_len >= 24 {
                let val_off = usize::from(get_u16(&mft_buf, cur + 20)?);
                let fn_data = cur.checked_add(val_off).ok_or(PatchError::CorruptRecord("fn_data"))?;
                if fn_data + 66 <= cur + attr_len {
                    let fn_len = usize::from(mft_buf[fn_data + 64]);
                    let fn_bytes = fn_len * 2;
                    if fn_data + 66 + fn_bytes <= cur + attr_len {
                        let fn_slice = &mft_buf[fn_data + 66..fn_data + 66 + fn_bytes];
                        if fn_slice == name_b_u16.as_slice() {
                            let seq = get_u16(&mft_buf, 16)?;
                            target_info = Some((ino, seq, off, cur, true));
                            break;
                        } else if fn_slice == name_a_u16.as_slice() {
                            let seq = get_u16(&mft_buf, 16)?;
                            target_info = Some((ino, seq, off, cur, false));
                            break;
                        }
                    }
                }
            }
            cur += attr_len;
        }
        if target_info.is_some() {
            break;
        }
    }

    let (target_inode, target_seq, target_mft_off, target_fn_attr_off, current_is_b) =
        target_info.ok_or(PatchError::TargetNotFound)?;

    let target_name = match force_mode.as_deref() {
        Some("--tp") => NAME_B,
        Some("--trp") => NAME_A,
        _ => if current_is_b { NAME_A } else { NAME_B },
    };
    let target_u16 = encode_ascii_utf16le(target_name);
    let val_len = u32::try_from(66 + target_u16.len()).map_err(|_| PatchError::CorruptRecord("val_len"))?;

    // Strict Phase 1 check on MFT $FILE_NAME resident attribute capacity (Argon F-01 & Sonnet F-02)
    {
        let fn_attr_len = get_u32(&mft_buf, target_fn_attr_off + 4)? as usize;
        let fn_val_off = usize::from(get_u16(&mft_buf, target_fn_attr_off + 20)?);
        let required_attr_len = (fn_val_off + 66 + target_u16.len() + 7) & !7;
        if required_attr_len != fn_attr_len || fn_val_off + 66 + target_u16.len() > fn_attr_len {
            return Err(PatchError::LayoutMismatch("$MFT $FILE_NAME (0x30)"));
        }
    }

    println!(
        "[*] Located file at Inode {} (seq={}, offset={}). Switching to '{}'",
        target_inode, target_seq, target_mft_off, target_name
    );

    // 3. Phase 1 Dry-Run Validation on Root Directory Index (Inode 5)
    let root_mft_off = vbr_info
        .mft_base
        .checked_add(ROOT_INODE * (vbr_info.rec_size as u64))
        .ok_or(PatchError::CorruptRecord("root_mft_off overflow"))?;
    let mut root_mft = vec![0u8; vbr_info.rec_size];
    file.read_exact_at(&mut root_mft, root_mft_off)?;
    let rattr_off = usize::from(get_u16(&root_mft, 20)?);
    if rattr_off < 42 || rattr_off + 8 > vbr_info.rec_size {
        return Err(PatchError::CorruptRecord("Invalid Inode 5 attr_off"));
    }
    ntfs_fixup_decode(&mut root_mft, b"FILE", rattr_off)?;

    let mut found_in_root_attr: Option<usize> = None;
    let mut found_in_alloc_off: Option<u64> = None;
    let mut blk_buf = vec![0u8; vbr_info.idx_sz];

    let mut rcur = rattr_off;
    while rcur + 8 <= vbr_info.rec_size && found_in_root_attr.is_none() && found_in_alloc_off.is_none() {
        let atype = get_u32(&root_mft, rcur)?;
        let alen = get_u32(&root_mft, rcur + 4)? as usize;
        if atype == 0xFFFF_FFFF || alen == 0 || rcur.checked_add(alen).map_or(true, |e| e > vbr_info.rec_size) {
            break;
        }

        if atype == 0x90 && root_mft.get(rcur + 8) == Some(&0) && alen >= 24 {
            // Resident $INDEX_ROOT
            let vlen = get_u32(&root_mft, rcur + 16)? as usize;
            let voff = usize::from(get_u16(&root_mft, rcur + 20)?);
            if voff + 16 <= vlen && rcur + voff + vlen <= rcur + alen {
                let node_slice = &mut root_mft[rcur + voff + 16..rcur + voff + vlen];
                if index_rename(
                    node_slice,
                    target_inode,
                    target_seq,
                    &target_u16,
                    false,
                    force_order,
                    "$INDEX_ROOT",
                )? {
                    found_in_root_attr = Some(rcur);
                    break;
                }
            }
        } else if atype == 0xA0 && root_mft.get(rcur + 8) != Some(&0) && alen >= 40 {
            // Non-resident $INDEX_ALLOCATION
            let run_off = usize::from(get_u16(&root_mft, rcur + 32)?);
            if run_off < alen {
                let runs_slice = &root_mft[rcur + run_off..rcur + alen];
                let mut run_iter = DataRunIter::new(runs_slice);

                while let Some(run) = run_iter.next_run()? {
                    let Some(lcn) = run.lcn else { continue }; // Skip sparse runs
                    let Some(byte_off) = lcn.checked_mul(vbr_info.cluster_size) else {
                        return Err(PatchError::CorruptRecord("Run byte_off overflow"));
                    };
                    if byte_off >= img_size {
                        break;
                    }
                    let Some(raw_byte_sz) = run.cluster_len.checked_mul(vbr_info.cluster_size) else {
                        return Err(PatchError::CorruptRecord("Run byte_sz overflow"));
                    };
                    let byte_sz = raw_byte_sz.min(img_size - byte_off);

                    let mut b: u64 = 0;
                    while b.checked_add(vbr_info.idx_sz as u64).map_or(false, |end| end <= byte_sz) {
                        let blk_off = byte_off + b;
                        b += vbr_info.idx_sz as u64;

                        if file.read_exact_at(&mut blk_buf, blk_off).is_err() {
                            continue;
                        }
                        if blk_buf.get(0..4) != Some(b"INDX") {
                            continue;
                        }
                        let Ok(eoff_u32) = get_u32(&blk_buf, 0x18) else { continue };
                        let min_payload = 0x18usize.saturating_add(eoff_u32 as usize);
                        if ntfs_fixup_decode(&mut blk_buf, b"INDX", min_payload).is_err() {
                            continue;
                        }

                        if index_rename(
                            &mut blk_buf[0x18..],
                            target_inode,
                            target_seq,
                            &target_u16,
                            false,
                            force_order,
                            "INDX",
                        )? {
                            found_in_alloc_off = Some(blk_off);
                            break;
                        }
                    }
                    if found_in_alloc_off.is_some() {
                        break;
                    }
                }
            }
        }
        rcur += alen;
    }

    if found_in_root_attr.is_none() && found_in_alloc_off.is_none() {
        return Err(PatchError::DirectoryEntryNotFound(target_inode));
    }

    // 4. Phase 2 (Commit): Apply validated mutations with dual sync barriers
    // 4a. Re-read, mutate, encode, write, and sync Target MFT Record
    file.read_exact_at(&mut mft_buf, target_mft_off)?;
    let target_attr_off = usize::from(get_u16(&mft_buf, 20)?);
    ntfs_fixup_decode(&mut mft_buf, b"FILE", target_attr_off)?;

    let fn_cur = target_fn_attr_off;
    let fn_attr_len = get_u32(&mft_buf, fn_cur + 4)? as usize;
    let fn_val_off = usize::from(get_u16(&mft_buf, fn_cur + 20)?);
    let fn_data = fn_cur + fn_val_off;

    put_u32(&mut mft_buf, fn_cur + 16, val_len)?;
    mft_buf[fn_data + 64] = (target_u16.len() / 2) as u8;
    mft_buf[fn_data + 66..fn_data + 66 + target_u16.len()].copy_from_slice(&target_u16);

    let pad_start = fn_data + 66 + target_u16.len();
    let pad_end = fn_cur + fn_attr_len;
    if pad_end > pad_start {
        mft_buf[pad_start..pad_end].fill(0);
    }

    ntfs_fixup_encode(&mut mft_buf, target_attr_off)?;
    file.write_all_at(&mft_buf, target_mft_off)?;
    file.sync_data()?; // Barrier 1: Ensure MFT record is durable before index update
    println!("[✓] Inode {} MFT record updated to '{}'", target_inode, target_name);

    // 4b. Re-read, mutate, encode, write, and sync Root Directory Index
    if let Some(rcur_root) = found_in_root_attr {
        // Re-read fresh root_mft to avoid TOCTOU drift (Sonnet F-06)
        file.read_exact_at(&mut root_mft, root_mft_off)?;
        let root_attr_off = usize::from(get_u16(&root_mft, 20)?);
        ntfs_fixup_decode(&mut root_mft, b"FILE", root_attr_off)?;

        let vlen = get_u32(&root_mft, rcur_root + 16)? as usize;
        let voff = usize::from(get_u16(&root_mft, rcur_root + 20)?);
        let node_slice = &mut root_mft[rcur_root + voff + 16..rcur_root + voff + vlen];
        index_rename(
            node_slice,
            target_inode,
            target_seq,
            &target_u16,
            true,
            force_order,
            "$INDEX_ROOT",
        )?;

        ntfs_fixup_encode(&mut root_mft, root_attr_off)?;
        file.write_all_at(&root_mft, root_mft_off)?;
        println!("[✓] Directory entry updated in $INDEX_ROOT (Inode 5)");
    } else if let Some(blk_off) = found_in_alloc_off {
        file.read_exact_at(&mut blk_buf, blk_off)?;
        let eoff_u32 = get_u32(&blk_buf, 0x18)?;
        let min_payload = 0x18usize.saturating_add(eoff_u32 as usize);
        ntfs_fixup_decode(&mut blk_buf, b"INDX", min_payload)?;

        index_rename(
            &mut blk_buf[0x18..],
            target_inode,
            target_seq,
            &target_u16,
            true,
            force_order,
            "INDX",
        )?;

        ntfs_fixup_encode(&mut blk_buf, min_payload)?;
        file.write_all_at(&blk_buf, blk_off)?;
        println!("[✓] Directory entry updated in $INDEX_ALLOCATION (block at 0x{:x})", blk_off);
    }

    file.sync_all()?; // Barrier 2: Flush all metadata and disk caches
    println!(
        "[✓] Switch completed safely with full NTFS compliance: TV will detect '{}'",
        target_name
    );
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("[!] Error: {e}");
            let code = match e {
                PatchError::LockBusy => 75,
                PatchError::CollationViolation(_) => 2,
                PatchError::LayoutMismatch(_) => 3,
                _ => 1,
            };
            ExitCode::from(code)
        }
    }
}

// ----------------------------------------------------------------- Unit Tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_name_lengths_and_112_byte_invariant() {
        let name_a_u16 = encode_ascii_utf16le(NAME_A); // 14 chars = 28 bytes
        let name_b_u16 = encode_ascii_utf16le(NAME_B); // 15 chars = 30 bytes

        assert_eq!(NAME_A.len(), 14);
        assert_eq!(name_a_u16.len(), 28);
        assert_eq!(NAME_B.len(), 15);
        assert_eq!(name_b_u16.len(), 30);

        // INDEX_ENTRY size: ((16 + 66 + name_bytes + 7) & !7)
        let size_a = (16 + 66 + name_a_u16.len() + 7) & !7;
        let size_b = (16 + 66 + name_b_u16.len() + 7) & !7;

        // Invariant: Both MUST equal exactly 112 bytes without subnode!
        assert_eq!(size_a, 112);
        assert_eq!(size_b, 112);

        // With subnode (fl & 1, +8 bytes):
        let size_a_sub = size_a + 8;
        let size_b_sub = size_b + 8;
        assert_eq!(size_a_sub, 120);
        assert_eq!(size_b_sub, 120);

        // MFT resident $FILE_NAME size: ((24 + 66 + name_bytes + 7) & !7)
        let mft_size_a = (24 + 66 + name_a_u16.len() + 7) & !7;
        let mft_size_b = (24 + 66 + name_b_u16.len() + 7) & !7;
        assert_eq!(mft_size_a, 120);
        assert_eq!(mft_size_b, 120);
    }

    #[test]
    fn test_collation_up16() {
        let a = encode_ascii_utf16le("TV AO VIVO.trp");
        let b = encode_ascii_utf16le("TV AO VIVO 2.tp");
        let a_lower = encode_ascii_utf16le("tv ao vivo.trp");

        // Case-insensitive comparison matches
        assert_eq!(coll_utf16_le(&a, &a_lower).unwrap(), std::cmp::Ordering::Equal);

        // Ordering between NAME_A and NAME_B:
        // Index 10: NAME_A has '.' (0x2E), NAME_B has ' ' (0x20).
        // Since ' ' (0x20) < '.' (0x2E), NAME_B < NAME_A!
        assert_eq!(coll_utf16_le(&b, &a).unwrap(), std::cmp::Ordering::Less);
        assert_eq!(coll_utf16_le(&a, &b).unwrap(), std::cmp::Ordering::Greater);
    }

    #[test]
    fn test_ntfs_fixup_roundtrip() {
        let mut rec = vec![0u8; 1024]; // 2 sectors
        rec[0..4].copy_from_slice(b"FILE");
        put_u16(&mut rec, 4, 48).unwrap(); // off = 48 (0x30)
        put_u16(&mut rec, 6, 3).unwrap();  // cnt = 3 (1 USN + 2 sector tails)
        put_u16(&mut rec, 48, 0x1234).unwrap(); // USN

        // Put original tail words at end of each sector
        put_u16(&mut rec, 510, 0x1234).unwrap();
        put_u16(&mut rec, 1022, 0x1234).unwrap();
        put_u16(&mut rec, 50, 0xAAAA).unwrap(); // saved tail 1
        put_u16(&mut rec, 52, 0xBBBB).unwrap(); // saved tail 2

        // Decode should restore 0xAAAA at 510 and 0xBBBB at 1022
        ntfs_fixup_decode(&mut rec, b"FILE", 64).unwrap();
        assert_eq!(get_u16(&rec, 510).unwrap(), 0xAAAA);
        assert_eq!(get_u16(&rec, 1022).unwrap(), 0xBBBB);

        // Encode should increment USN (0x1235), save tails in USA and put USN at tails
        ntfs_fixup_encode(&mut rec, 64).unwrap();
        assert_eq!(get_u16(&rec, 48).unwrap(), 0x1235);
        assert_eq!(get_u16(&rec, 510).unwrap(), 0x1235);
        assert_eq!(get_u16(&rec, 1022).unwrap(), 0x1235);
        assert_eq!(get_u16(&rec, 50).unwrap(), 0xAAAA);
        assert_eq!(get_u16(&rec, 52).unwrap(), 0xBBBB);
    }

    #[test]
    fn test_ntfs_fixup_torn_record_detection() {
        let mut rec = vec![0u8; 1024];
        rec[0..4].copy_from_slice(b"FILE");
        put_u16(&mut rec, 4, 48).unwrap();
        put_u16(&mut rec, 6, 3).unwrap();
        put_u16(&mut rec, 48, 0x1234).unwrap();

        put_u16(&mut rec, 510, 0x1234).unwrap();
        put_u16(&mut rec, 1022, 0x9999).unwrap(); // Torn sector 2!

        let err = ntfs_fixup_decode(&mut rec, b"FILE", 64);
        assert!(matches!(err, Err(PatchError::TornRecord { sector: 2, expected: 0x1234, found: 0x9999, .. })));
    }

    #[test]
    fn test_datarun_parser_and_bounds() {
        // Run 1: len=4 clusters, lcn delta=+100 -> lcn 100
        // Header: len_sz=1, off_sz=1 -> 0x11, len=0x04, delta=0x64
        // Run 2: len=2 clusters, lcn delta=-50 -> lcn 50
        // Header: len_sz=1, off_sz=1 -> 0x11, len=0x02, delta=0xCE (signed -50)
        // Run 3: sparse run len=8 clusters (off_sz=0)
        // Header: len_sz=1, off_sz=0 -> 0x01, len=0x08
        // Terminator: 0x00
        let runs_data = vec![
            0x11, 0x04, 0x64,
            0x11, 0x02, 0xCE,
            0x01, 0x08,
            0x00,
        ];

        let mut iter = DataRunIter::new(&runs_data);
        let r1 = iter.next_run().unwrap().unwrap();
        assert_eq!(r1, DataRun { lcn: Some(100), cluster_len: 4 });

        let r2 = iter.next_run().unwrap().unwrap();
        assert_eq!(r2, DataRun { lcn: Some(50), cluster_len: 2 });

        let r3 = iter.next_run().unwrap().unwrap();
        assert_eq!(r3, DataRun { lcn: None, cluster_len: 8 });

        assert_eq!(iter.next_run().unwrap(), None);
    }
}
