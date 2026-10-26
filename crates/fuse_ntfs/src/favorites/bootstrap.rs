//! Finite, per-file MPEG-TS bootstrap and bootstrap→Live splice validation.
//!
//! The embedded slate is generated offline by
//! `scripts/generate_favorites_bootstrap.py`; the runtime never invokes
//! ffmpeg. Its bytes are immutable once served. A per-file private section
//! binds the prefix to `file_id` + channel identity, so a caller cannot pass
//! channel A's bootstrap off as channel B's merely by relabeling metadata.
//!
//! The validator checks structure that the driver depends on: PAT/PMT with a
//! valid CRC, H.264 Constrained Baseline with SPS/PPS/IDR, no B-slices,
//! AC3 384 kbit/s stereo 48 kHz, coherent continuity counters and a bounded
//! PCR interval. It is not a decoder and does not replace physical playback
//! validation.
//!
//! [`splice_live`] rewrites the first continuity counters, PCR, PTS and DTS of
//! an active epoch so the transition from the immutable prefix is coherent.
//! If the live prefix lacks the required timing fields the splice fails and
//! the caller must keep the previous source published.

use crate::contract::TS_PKT_SZ;
use crate::favorites::digest::{fnv1a32, sha256};

/// 30 fps, two seconds, H.264 Constrained Baseline + AC3 48 kHz stereo.
pub const BOOTSTRAP_TEMPLATE: &[u8] = include_bytes!("../../assets/live_favorites_bootstrap.ts");

pub const IDENTITY_PID: u16 = 0x1FF0;
pub const IDENTITY_MAGIC: &[u8; 6] = b"TVFAV1";

const PCR_MOD: u64 = (1u64 << 33) * 300;
const WRAP33: u64 = 1u64 << 33;
const PCR_20MS: u64 = 27_000 * 20;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootstrapError {
    Empty,
    NotAligned,
    BadSync { packet: usize },
    MissingPat,
    MalformedPat,
    MissingPmt,
    MalformedPmt,
    BadCrc(&'static str),
    MissingVideo,
    MissingAudio,
    VideoProfile,
    MissingSps,
    MissingPps,
    MissingIdr,
    BSlice,
    SliceHeader,
    MultipleSlices,
    AudioNotAc3,
    AudioNotStereo48k384k,
    NoPcr,
    PcrNonMonotonic,
    PcrIntervalTooLarge { ticks: u64 },
    ContinuityMismatch { pid: u16, expected: u8, got: u8 },
    MissingIdentity,
    IdentityMismatch { expected: u32, got: u32 },
    IdentityChannelMismatch,
    SpliceTimingMissing(&'static str),
    SplicePcrMissing,
    SplicePtsMissing,
    Overflow,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BootstrapSummary {
    pub bytes: usize,
    pub packets: usize,
    pub video_pid: u16,
    pub audio_pid: u16,
    pub pcr_pid: u16,
    pub idr_count: u32,
    pub pcr_interval_max: u64,
}

#[derive(Debug, Clone)]
pub struct Bootstrap {
    bytes: Vec<u8>,
    file_id: u32,
    channel_id: String,
}

impl Bootstrap {
    /// Builds the immutable prefix for one favorite. The shared slate bytes
    /// are unchanged; only the trailing identity section differs per file.
    pub fn for_file(file_id: u32, channel_id: &str) -> Self {
        let mut bytes = BOOTSTRAP_TEMPLATE.to_vec();
        bytes.extend_from_slice(&identity_packet(file_id, channel_id));
        Self {
            bytes,
            file_id,
            channel_id: channel_id.to_string(),
        }
    }

    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    pub fn len(&self) -> usize {
        self.bytes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    pub fn file_id(&self) -> u32 {
        self.file_id
    }

    pub fn channel_id(&self) -> &str {
        &self.channel_id
    }

    pub fn validate(&self) -> Result<BootstrapSummary, BootstrapError> {
        validate(&self.bytes, Some((self.file_id, &self.channel_id)))
    }
}

fn crc32_mpeg(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= u32::from(byte) << 24;
        for _ in 0..8 {
            crc = if crc & 0x8000_0000 != 0 {
                (crc << 1) ^ 0x04C1_1DB7
            } else {
                crc << 1
            };
        }
    }
    crc
}

fn channel_tag(channel_id: &str) -> [u8; 16] {
    let digest = sha256(channel_id.as_bytes());
    let mut out = [0u8; 16];
    out.copy_from_slice(&digest[..16]);
    out
}

fn identity_packet(file_id: u32, channel_id: &str) -> [u8; TS_PKT_SZ] {
    let tag = channel_tag(channel_id);
    let mut private = Vec::with_capacity(64);
    private.extend_from_slice(IDENTITY_MAGIC);
    private.extend_from_slice(&file_id.to_be_bytes());
    private.extend_from_slice(&tag);
    let section_length = private.len() + 4;
    let mut section = Vec::with_capacity(3 + section_length);
    section.push(0x80);
    section.push(0x30 | ((section_length >> 8) as u8 & 0x0F));
    section.push(section_length as u8);
    section.extend_from_slice(&private);
    let crc = crc32_mpeg(&section);
    section.extend_from_slice(&crc.to_be_bytes());

    let mut packet = [0xFFu8; TS_PKT_SZ];
    packet[0] = 0x47;
    packet[1] = 0x40 | ((IDENTITY_PID >> 8) as u8 & 0x1F);
    packet[2] = IDENTITY_PID as u8;
    packet[3] = 0x10;
    packet[4] = 0;
    packet[5..5 + section.len()].copy_from_slice(&section);
    packet
}

fn parse_pat(payload: &[u8]) -> Option<(u16, u16)> {
    if payload.len() < 8 || payload[0] != 0 {
        return None;
    }
    let section = &payload[1..];
    if section.len() < 12 || section[0] != 0x00 {
        return None;
    }
    let section_length = ((section[1] as usize & 0x0F) << 8) | section[2] as usize;
    let end = 3 + section_length;
    if end > section.len() || section_length < 13 || (section_length - 9) % 4 != 0 {
        return None;
    }
    let crc_ok = crc32_mpeg(&section[..end - 4])
        == u32::from_be_bytes(section[end - 4..end].try_into().ok()?);
    if !crc_ok {
        return None;
    }
    let program_count = section_length.saturating_sub(9) / 4;
    if program_count == 0 || section.len() < 12 + (program_count - 1) * 4 {
        return None;
    }
    let program = ((section[8] as u16) << 8) | section[9] as u16;
    let pmt_pid = (((section[10] as u16 & 0x1F) << 8) | section[11] as u16) & 0x1FFF;
    if program == 0 {
        return None;
    }
    Some((program, pmt_pid))
}

fn parse_pmt(payload: &[u8], pmt_pid: u16) -> Option<(u16, u16, u16)> {
    if payload.len() < 8 || payload[0] != 0 {
        return None;
    }
    let section = &payload[1..];
    if section.len() < 12 || section[0] != 0x02 {
        return None;
    }
    let section_length = ((section[1] as usize & 0x0F) << 8) | section[2] as usize;
    let end = 3 + section_length;
    if end > section.len() || section_length < 13 {
        return None;
    }
    if crc32_mpeg(&section[..end - 4]) != u32::from_be_bytes(section[end - 4..end].try_into().ok()?)
    {
        return None;
    }
    let pcr_pid = (((section[8] as u16 & 0x1F) << 8) | section[9] as u16) & 0x1FFF;
    let program_info_len = (((section[10] as usize & 0x0F) << 8) | section[11] as usize) & 0x0FFF;
    let mut cursor = 12 + program_info_len;
    if cursor > end - 4 {
        return None;
    }
    let mut video = None;
    let mut audio = None;
    while cursor + 5 <= end - 4 {
        let stream_type = section[cursor];
        let elementary_pid =
            (((section[cursor + 1] as u16 & 0x1F) << 8) | section[cursor + 2] as u16) & 0x1FFF;
        let es_info_len =
            (((section[cursor + 3] as usize & 0x0F) << 8) | section[cursor + 4] as usize) & 0x0FFF;
        if cursor + 5 + es_info_len > end - 4 {
            return None;
        }
        match stream_type {
            0x1B if video.is_none() => video = Some(elementary_pid),
            0x81 if audio.is_none() => audio = Some(elementary_pid),
            _ => {}
        }
        cursor += 5 + es_info_len;
    }
    if cursor != end - 4 {
        return None;
    }
    let _ = pmt_pid;
    Some((pcr_pid, video?, audio?))
}

fn payload(packet: &[u8]) -> Option<&[u8]> {
    if packet.len() != TS_PKT_SZ || packet[0] != 0x47 {
        return None;
    }
    let afc = (packet[3] >> 4) & 3;
    if afc & 1 == 0 {
        return None;
    }
    let mut offset = 4;
    if afc & 2 != 0 {
        offset = 5 + packet[4] as usize;
    }
    if offset >= TS_PKT_SZ {
        None
    } else {
        Some(&packet[offset..])
    }
}

fn pcr_value(packet: &[u8]) -> Option<u64> {
    let afc = (packet[3] >> 4) & 3;
    if afc & 2 == 0 || packet[4] < 7 || packet[4] > 183 || packet[5] & 0x10 == 0 {
        return None;
    }
    let b = &packet[6..12];
    let base = (u64::from(b[0]) << 25)
        | (u64::from(b[1]) << 17)
        | (u64::from(b[2]) << 9)
        | (u64::from(b[3]) << 1)
        | (u64::from(b[4]) >> 7);
    let ext = ((u64::from(b[4]) & 1) << 8) | u64::from(b[5]);
    if ext >= 300 || b[4] & 0x7E != 0x7E {
        return None;
    }
    Some(base * 300 + ext)
}

fn write_pcr(packet: &mut [u8], value: u64) {
    let value = value % PCR_MOD;
    let base = value / 300;
    let ext = value % 300;
    packet[6] = (base >> 25) as u8;
    packet[7] = (base >> 17) as u8;
    packet[8] = (base >> 9) as u8;
    packet[9] = (base >> 1) as u8;
    packet[10] = ((base as u8 & 1) << 7) | 0x7E | ((ext >> 8) as u8 & 1);
    packet[11] = ext as u8;
}

fn timestamp(packet: &[u8], payload: &[u8]) -> Option<(u8, u64)> {
    let afc = (packet[3] >> 4) & 3;
    let mut offset = 4;
    if afc & 2 != 0 {
        offset = 5 + packet[4] as usize;
    }
    if offset >= TS_PKT_SZ || offset + 9 > TS_PKT_SZ {
        return None;
    }
    let p = payload;
    if p.len() < 9 || p[0..3] != [0, 0, 1] {
        return None;
    }
    let sid = p[3];
    let cursor = 9;
    if offset + cursor + 5 > TS_PKT_SZ {
        return None;
    }
    // `payload` is already sliced at `offset`, so cursor is relative to it.
    if p.len() < cursor + 5 {
        return None;
    }
    let flags = p[7];
    if flags & 0x80 == 0 {
        return None;
    }
    let b = &p[cursor..cursor + 5];
    let prefix = if flags & 0xc0 == 0xc0 { 3 } else { 2 };
    if b[0] >> 4 != prefix || b[0] & 1 == 0 || b[2] & 1 == 0 || b[4] & 1 == 0 {
        return None;
    }
    let pts = ((u64::from(b[0] & 0x0E)) << 29)
        | (u64::from(b[1]) << 22)
        | (u64::from(b[2] & 0xFE) << 14)
        | (u64::from(b[3]) << 7)
        | (u64::from(b[4]) >> 1);
    Some((sid, pts))
}

fn write_timestamp(packet: &mut [u8], payload_offset: usize, prefix: u8, value: u64) {
    let value = value % WRAP33;
    let start = payload_offset + 9;
    if start + 5 > packet.len() {
        return;
    }
    packet[start] = (prefix << 4) | ((value >> 29) as u8 & 0x0E) | 0x01;
    packet[start + 1] = (value >> 22) as u8;
    packet[start + 2] = (((value >> 14) as u8) & 0xFE) | 0x01;
    packet[start + 3] = (value >> 7) as u8;
    packet[start + 4] = ((value << 1) as u8) | 0x01;
}

fn check_pes_timestamps(packet: &[u8]) -> Result<(), BootstrapError> {
    if packet[1] & 0x40 == 0 {
        return Ok(());
    }
    let p = payload(packet).ok_or(BootstrapError::SpliceTimingMissing("PES payload"))?;
    if p.len() < 9 || p[..3] != [0, 0, 1] {
        return Err(BootstrapError::SpliceTimingMissing("PES header"));
    }
    let flags = p[7] >> 6;
    let length = match flags {
        0 => 0,
        2 => 5,
        3 => 10,
        _ => return Err(BootstrapError::SpliceTimingMissing("PES flags")),
    };
    if usize::from(p[8]) < length || 9 + usize::from(p[8]) > p.len() {
        // Our FFmpeg contract never splits the short PES header. Fail closed
        // instead of exposing a timestamp that cannot be rewritten atomically.
        return Err(BootstrapError::SpliceTimingMissing("split PES header"));
    }
    let valid = |b: &[u8], prefix: u8| {
        b[0] >> 4 == prefix && b[0] & 1 == 1 && b[2] & 1 == 1 && b[4] & 1 == 1
    };
    if length >= 5 && !valid(&p[9..14], if flags == 3 { 3 } else { 2 }) {
        return Err(BootstrapError::SpliceTimingMissing("PTS markers"));
    }
    if length == 10 && !valid(&p[14..19], 1) {
        return Err(BootstrapError::SpliceTimingMissing("DTS markers"));
    }
    Ok(())
}

struct BitReader<'a> {
    data: &'a [u8],
    bit: usize,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, bit: 0 }
    }

    fn bit(&mut self) -> Option<u8> {
        if self.bit >= self.data.len() * 8 {
            return None;
        }
        let byte = self.data[self.bit / 8];
        let value = (byte >> (7 - (self.bit % 8))) & 1;
        self.bit += 1;
        Some(value)
    }

    fn bits(&mut self, count: u32) -> Option<u32> {
        let mut value = 0u32;
        for _ in 0..count {
            value = (value << 1) | u32::from(self.bit()?);
        }
        Some(value)
    }

    fn ue(&mut self) -> Option<u32> {
        let mut zeros = 0u32;
        while self.bit()? == 0 {
            zeros += 1;
            if zeros > 31 {
                return None;
            }
        }
        if zeros == 0 {
            return Some(0);
        }
        let rest = self.bits(zeros)?;
        Some((1u32 << zeros) - 1 + rest)
    }
}

fn check_sps(nal: &[u8]) -> Result<(), BootstrapError> {
    if nal.len() < 4 {
        return Err(BootstrapError::MissingSps);
    }
    let profile = nal[1];
    if profile != 66 && profile != 77 && profile != 88 {
        return Err(BootstrapError::VideoProfile);
    }
    let mut r = BitReader::new(&nal[4..]);
    let _sps_id = r.ue().ok_or(BootstrapError::MissingSps)?;
    let _log2_frame_num = r.ue().ok_or(BootstrapError::MissingSps)?;
    let poc_type = r.ue().ok_or(BootstrapError::MissingSps)?;
    if poc_type == 0 {
        let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
    } else if poc_type == 1 {
        let _ = r.bit().ok_or(BootstrapError::MissingSps)?;
        let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
        let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
        let count = r.ue().ok_or(BootstrapError::MissingSps)?;
        for _ in 0..count {
            let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
        }
    }
    let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
    let _ = r.bit().ok_or(BootstrapError::MissingSps)?;
    let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
    let _ = r.ue().ok_or(BootstrapError::MissingSps)?;
    let frame_mbs_only = r.bit().ok_or(BootstrapError::MissingSps)?;
    if frame_mbs_only != 1 {
        return Err(BootstrapError::VideoProfile);
    }
    Ok(())
}

fn check_h264(es: &[u8], summary: &mut BootstrapSummary) -> Result<(), BootstrapError> {
    let mut sps = false;
    let mut pps = false;
    let mut idr = false;
    let mut slices = 0usize;
    let mut cursor = 0usize;
    while cursor + 3 <= es.len() {
        let mut start = None;
        let mut index = cursor;
        while index + 3 <= es.len() {
            if es[index] == 0 && es[index + 1] == 0 && es[index + 2] == 1 {
                start = Some(index + 3);
                break;
            }
            index += 1;
        }
        let Some(nal_start) = start else { break };
        if nal_start >= es.len() {
            break;
        }
        let next = es[nal_start..]
            .windows(3)
            .position(|w| w == [0, 0, 1])
            .map(|offset| nal_start + offset)
            .unwrap_or(es.len());
        let mut nal = &es[nal_start..next];
        while nal.last() == Some(&0) {
            nal = &nal[..nal.len() - 1];
        }
        if nal.is_empty() {
            cursor = next;
            continue;
        }
        let nal_type = nal[0] & 0x1F;
        match nal_type {
            7 => {
                check_sps(nal)?;
                sps = true;
            }
            8 => pps = true,
            5 => {
                let mut r = BitReader::new(&nal[1..]);
                if r.ue().ok_or(BootstrapError::SliceHeader)? != 0 {
                    return Err(BootstrapError::MultipleSlices);
                }
                idr = true;
                slices += 1;
                summary.idr_count += 1;
            }
            1 => {
                slices += 1;
                let mut r = BitReader::new(&nal[1..]);
                if r.ue().ok_or(BootstrapError::SliceHeader)? != 0 {
                    return Err(BootstrapError::MultipleSlices);
                }
                let slice_type = r.ue().ok_or(BootstrapError::SliceHeader)? % 5;
                if slice_type == 1 {
                    return Err(BootstrapError::BSlice);
                }
            }
            _ => {}
        }
        cursor = next;
    }
    if !sps {
        return Err(BootstrapError::MissingSps);
    }
    if !pps {
        return Err(BootstrapError::MissingPps);
    }
    if !idr {
        return Err(BootstrapError::MissingIdr);
    }
    let _ = slices;
    Ok(())
}

fn check_ac3(payload: &[u8]) -> Result<(), BootstrapError> {
    if payload.len() < 6 || payload[0] != 0x0B || payload[1] != 0x77 {
        return Err(BootstrapError::AudioNotAc3);
    }
    let fscod = payload[4] >> 6;
    let frmsizecod = payload[4] & 0x3F;
    if payload.len() < 7 {
        return Err(BootstrapError::AudioNotAc3);
    }
    let acmod = payload[6] >> 5;
    if fscod != 0 || acmod != 2 || (frmsizecod >> 1) != 14 {
        return Err(BootstrapError::AudioNotStereo48k384k);
    }
    Ok(())
}

fn identity_of(packet: &[u8]) -> Option<(u32, [u8; 16])> {
    if ((packet[1] as u16 & 0x1F) << 8 | packet[2] as u16) != IDENTITY_PID {
        return None;
    }
    if packet[1] & 0x40 == 0 {
        return None;
    }
    let p = payload(packet)?;
    if p.first() != Some(&0) {
        return None;
    }
    let section = &p[1..];
    if section.len() < 33 || section[0] != 0x80 {
        return None;
    }
    let section_length = ((section[1] as usize & 0x0F) << 8) | section[2] as usize;
    if section_length < 30 || 3 + section_length > section.len() {
        return None;
    }
    let end = 3 + section_length;
    if crc32_mpeg(&section[..end - 4]) != u32::from_be_bytes(section[end - 4..end].try_into().ok()?)
    {
        return None;
    }
    let private = &section[3..end - 4];
    if private.len() < 26 || &private[..6] != IDENTITY_MAGIC {
        return None;
    }
    let file_id = u32::from_be_bytes(private[6..10].try_into().ok()?);
    let mut tag = [0u8; 16];
    tag.copy_from_slice(&private[10..26]);
    Some((file_id, tag))
}

/// Validates a finite bootstrap and, when supplied, its per-file identity.
pub fn validate(
    data: &[u8],
    expected: Option<(u32, &str)>,
) -> Result<BootstrapSummary, BootstrapError> {
    if data.is_empty() {
        return Err(BootstrapError::Empty);
    }
    if !data.len().is_multiple_of(TS_PKT_SZ) {
        return Err(BootstrapError::NotAligned);
    }
    let mut summary = BootstrapSummary {
        bytes: data.len(),
        packets: data.len() / TS_PKT_SZ,
        video_pid: 0,
        audio_pid: 0,
        pcr_pid: 0,
        idr_count: 0,
        pcr_interval_max: 0,
    };
    let mut pmt_pid = None;
    let mut video_pid = None;
    let mut audio_pid = None;
    let mut pcr_pid = None;
    let mut video = Vec::new();
    let mut audio = Vec::new();
    let mut last_cc: [Option<u8>; 8192] = [None; 8192];
    let mut last_pcr: Option<u64> = None;
    let mut id_file = None;
    let mut id_tag = None;

    for (index, packet) in data.as_chunks::<TS_PKT_SZ>().0.iter().enumerate() {
        if packet[0] != 0x47 {
            return Err(BootstrapError::BadSync { packet: index });
        }
        if packet[1] & 0x80 != 0 {
            return Err(BootstrapError::BadSync { packet: index });
        }
        let pid = ((u16::from(packet[1]) & 0x1F) << 8) | u16::from(packet[2]);
        let afc = (packet[3] >> 4) & 3;
        if afc == 0 {
            return Err(BootstrapError::BadSync { packet: index });
        }
        let has_payload = afc & 1 != 0;
        let cc = packet[3] & 0x0F;
        // Null packets have no continuity requirement (ISO/IEC 13818-1).
        if has_payload && pid != 0x1fff {
            let slot = &mut last_cc[pid as usize & 0x1FFF];
            if let Some(previous) = *slot {
                if cc != (previous + 1) & 0x0F {
                    return Err(BootstrapError::ContinuityMismatch {
                        pid,
                        expected: (previous + 1) & 0x0F,
                        got: cc,
                    });
                }
            }
            *slot = Some(cc);
        }

        if pid == 0 {
            if let Some(p) = payload(packet) {
                if let Some((_program, parsed_pmt)) = parse_pat(p) {
                    pmt_pid = Some(parsed_pmt);
                } else if p.first() == Some(&0) {
                    return Err(BootstrapError::MalformedPat);
                }
            }
        } else if Some(pid) == pmt_pid {
            if let Some(p) = payload(packet) {
                if let Some((parsed_pcr, parsed_video, parsed_audio)) = parse_pmt(p, pid) {
                    pcr_pid = Some(parsed_pcr);
                    video_pid = Some(parsed_video);
                    audio_pid = Some(parsed_audio);
                } else {
                    return Err(BootstrapError::MalformedPmt);
                }
            }
        } else if pid == IDENTITY_PID {
            if let Some((file_id, tag)) = identity_of(packet) {
                id_file = Some(file_id);
                id_tag = Some(tag);
            }
        }

        if let Some(v) = pcr_value(packet) {
            if let Some(previous) = last_pcr {
                if v <= previous {
                    return Err(BootstrapError::PcrNonMonotonic);
                }
                let gap = v - previous;
                summary.pcr_interval_max = summary.pcr_interval_max.max(gap);
                if gap > PCR_20MS {
                    return Err(BootstrapError::PcrIntervalTooLarge { ticks: gap });
                }
            }
            last_pcr = Some(v);
        }

        if Some(pid) == video_pid {
            check_pes_timestamps(packet)?;
            if let Some(p) = payload(packet) {
                if packet[1] & 0x40 != 0 {
                    if let Some(start) = p.iter().position(|&b| b == 0) {
                        if p.len() > start + 9 && p[start..start + 3] == [0, 0, 1] {
                            let header_len = p[start + 8] as usize;
                            let es = start + 9 + header_len;
                            if es < p.len() {
                                video.extend_from_slice(&p[es..]);
                                video.extend_from_slice(&[0, 0, 0]);
                            }
                        }
                    }
                } else {
                    video.extend_from_slice(p);
                }
            }
        }
        if Some(pid) == audio_pid {
            check_pes_timestamps(packet)?;
            if let Some(p) = payload(packet) {
                if packet[1] & 0x40 != 0 {
                    if p.len() >= 9 && p[0..3] == [0, 0, 1] {
                        let header_len = p[8] as usize;
                        let es = 9 + header_len;
                        if es < p.len() {
                            audio.extend_from_slice(&p[es..]);
                        }
                    }
                } else if audio.len() < 64 {
                    audio.extend_from_slice(p);
                }
            }
        }
    }

    summary.video_pid = video_pid.ok_or(BootstrapError::MissingVideo)?;
    summary.audio_pid = audio_pid.ok_or(BootstrapError::MissingAudio)?;
    summary.pcr_pid = pcr_pid.ok_or(BootstrapError::NoPcr)?;
    if last_pcr.is_none() {
        return Err(BootstrapError::NoPcr);
    }
    if pmt_pid.is_none() {
        return Err(BootstrapError::MissingPmt);
    }
    check_h264(&video, &mut summary)?;
    check_ac3(&audio)?;

    if let Some((expected_id, expected_channel)) = expected {
        let got_file = id_file.ok_or(BootstrapError::MissingIdentity)?;
        if got_file != expected_id {
            return Err(BootstrapError::IdentityMismatch {
                expected: expected_id,
                got: got_file,
            });
        }
        let tag = id_tag.ok_or(BootstrapError::MissingIdentity)?;
        if tag != channel_tag(expected_channel) {
            return Err(BootstrapError::IdentityChannelMismatch);
        }
    }

    let _ = fnv1a32(b"bootstrap");
    Ok(summary)
}

fn payload_offset(packet: &[u8]) -> Option<usize> {
    let afc = (packet[3] >> 4) & 3;
    if afc & 1 == 0 {
        return None;
    }
    let mut offset = 4;
    if afc & 2 != 0 {
        offset = 5 + packet[4] as usize;
    }
    (offset < TS_PKT_SZ).then_some(offset)
}

fn find_pcr(data: &[u8], reverse: bool) -> Option<u64> {
    if reverse {
        data.as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .rev()
            .find_map(|packet| pcr_value(packet))
    } else {
        data.as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .find_map(|packet| pcr_value(packet))
    }
}

fn find_timestamp(data: &[u8], reverse: bool) -> Option<u64> {
    let packets: Vec<&[u8]> = data
        .as_chunks::<TS_PKT_SZ>()
        .0
        .iter()
        .map(|packet| packet.as_slice())
        .collect();
    let mut check = |packet: &&[u8]| -> Option<u64> {
        if packet[1] & 0x40 == 0 {
            return None;
        }
        let p = payload(packet)?;
        let (sid, pts) = timestamp(packet, p)?;
        (0xE0..=0xEF).contains(&sid).then_some(pts)
    };
    if reverse {
        packets.iter().rev().find_map(&mut check)
    } else {
        packets.iter().find_map(&mut check)
    }
}

/// Rewrites one active-epoch prefix so it continues the immutable bootstrap:
/// per-PID continuity counters restart after the bootstrap's last values, PCR
/// continues at +20 ms, and PTS/DTS share one 33-bit offset (preserving A/V
/// phase). Returns an error instead of emitting a discontinuous splice.
pub fn splice_live(bootstrap: &[u8], live: &[u8]) -> Result<Vec<u8>, BootstrapError> {
    SpliceState::new(bootstrap, live)?.rewrite(live)
}

/// Transformation owned by one authenticated source epoch. Every subsequent
/// chunk must pass through the same state: resetting at a frame boundary would
/// jump back to the original PCR/PTS and continuity counters.
#[derive(Clone)]
pub struct SpliceState {
    last_cc: [Option<u8>; 8192],
    pcr_offset: u64,
    pts_offset: u64,
    video_pid: u16,
    audio_pid: u16,
    pcr_pid: u16,
    pmt_pid: u16,
    last_pcr: Option<u64>,
}

impl SpliceState {
    pub fn video_pid(&self) -> u16 {
        self.video_pid
    }
    pub fn pmt_pid(&self) -> u16 {
        self.pmt_pid
    }
    pub fn new(bootstrap: &[u8], live: &[u8]) -> Result<Self, BootstrapError> {
        if live.is_empty() || !live.len().is_multiple_of(TS_PKT_SZ) {
            return Err(BootstrapError::SpliceTimingMissing("live prefix alignment"));
        }
        let summary = validate(bootstrap, None)?;
        let live_summary = validate(live, None)?;
        if (summary.video_pid, summary.audio_pid, summary.pcr_pid)
            != (
                live_summary.video_pid,
                live_summary.audio_pid,
                live_summary.pcr_pid,
            )
        {
            return Err(BootstrapError::MalformedPmt);
        }
        let pmt_pid = bootstrap
            .as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .filter(|packet| ((u16::from(packet[1]) & 0x1f) << 8 | u16::from(packet[2])) == 0)
            .find_map(|packet| payload(packet).and_then(parse_pat).map(|(_, pid)| pid))
            .ok_or(BootstrapError::MissingPat)?;
        let boot_pcr = find_pcr(bootstrap, true).ok_or(BootstrapError::SplicePcrMissing)?;
        let live_pcr = find_pcr(live, false).ok_or(BootstrapError::SplicePcrMissing)?;
        find_timestamp(bootstrap, true).ok_or(BootstrapError::SplicePtsMissing)?;
        find_timestamp(live, false).ok_or(BootstrapError::SplicePtsMissing)?;
        let target_pcr = boot_pcr.saturating_add(PCR_20MS);
        let pcr_offset = (target_pcr + PCR_MOD - (live_pcr % PCR_MOD)) % PCR_MOD;
        // Apply the same clock translation to PCR and PES timestamps. Choosing
        // these independently changes the decoder's CPB delay at the splice.
        let pts_offset = (pcr_offset / 300) % WRAP33;
        let pcr_offset = pts_offset * 300;

        let mut last_cc: [Option<u8>; 8192] = [None; 8192];
        for packet in bootstrap.as_chunks::<TS_PKT_SZ>().0 {
            let pid = ((u16::from(packet[1]) & 0x1F) << 8) | u16::from(packet[2]);
            if (packet[3] >> 4) & 1 != 0 {
                last_cc[pid as usize & 0x1FFF] = Some(packet[3] & 0x0F);
            }
        }

        Ok(Self {
            last_cc,
            pcr_offset,
            pts_offset,
            video_pid: summary.video_pid,
            audio_pid: summary.audio_pid,
            pcr_pid: summary.pcr_pid,
            pmt_pid,
            last_pcr: Some(boot_pcr),
        })
    }

    /// A replacement source for the same virtual file must continue the last
    /// published epoch, rather than reset the decoder clock to the short slate.
    /// Seed from the writer endpoint so every already-issued old response is
    /// earlier than the first replacement PCR, including an in-flight read.
    pub fn successor(&self, live: &[u8]) -> Result<Self, BootstrapError> {
        let summary = validate(live, None)?;
        if (summary.video_pid, summary.audio_pid, summary.pcr_pid)
            != (self.video_pid, self.audio_pid, self.pcr_pid)
        {
            return Err(BootstrapError::MalformedPmt);
        }
        let previous = self.last_pcr.ok_or(BootstrapError::SplicePcrMissing)?;
        let first = find_pcr(live, false).ok_or(BootstrapError::SplicePcrMissing)?;
        find_timestamp(live, false).ok_or(BootstrapError::SplicePtsMissing)?;
        let target = (previous + PCR_20MS) % PCR_MOD;
        let delta = (target + PCR_MOD - first) % PCR_MOD;
        let mut next = self.clone();
        next.pts_offset = delta / 300;
        next.pcr_offset = next.pts_offset * 300;
        Ok(next)
    }

    pub fn rewrite(&mut self, live: &[u8]) -> Result<Vec<u8>, BootstrapError> {
        if live.is_empty() || !live.len().is_multiple_of(TS_PKT_SZ) {
            return Err(BootstrapError::NotAligned);
        }
        // Validate the whole chunk before changing state (transactional failure).
        for (index, packet) in live.as_chunks::<TS_PKT_SZ>().0.iter().enumerate() {
            let afc = (packet[3] >> 4) & 3;
            if packet[0] != 0x47
                || packet[1] & 0x80 != 0
                || afc == 0
                || (afc & 2 != 0
                    && (packet[4] as usize > 183
                        || (packet[4] > 0 && packet[5] & 0x10 != 0 && packet[4] < 7)))
            {
                return Err(BootstrapError::BadSync { packet: index });
            }
            let pid = ((u16::from(packet[1]) & 0x1f) << 8) | u16::from(packet[2]);
            if pid == self.video_pid || pid == self.audio_pid {
                check_pes_timestamps(packet)?;
            }
            if pid == 0 {
                if let Some(p) = payload(packet) {
                    if parse_pat(p).map(|(_, pid)| pid) != Some(self.pmt_pid) {
                        return Err(BootstrapError::MalformedPat);
                    }
                }
            } else if pid == self.pmt_pid {
                if let Some(p) = payload(packet) {
                    if parse_pmt(p, pid) != Some((self.pcr_pid, self.video_pid, self.audio_pid)) {
                        return Err(BootstrapError::MalformedPmt);
                    }
                }
            }
        }
        let mut out = live.to_vec();
        for packet in out.as_chunks_mut::<TS_PKT_SZ>().0 {
            let pid = ((u16::from(packet[1]) & 0x1F) << 8) | u16::from(packet[2]);
            let afc = (packet[3] >> 4) & 3;
            if afc & 1 != 0 {
                let slot = pid as usize & 0x1FFF;
                let previous = self.last_cc[slot];
                let cc = if let Some(previous) = previous {
                    (previous + 1) & 0x0F
                } else {
                    packet[3] & 0x0F
                };
                packet[3] = (packet[3] & 0xF0) | cc;
                self.last_cc[slot] = Some(cc);
            } else if let Some(cc) = self.last_cc[pid as usize & 0x1FFF] {
                packet[3] = (packet[3] & 0xF0) | cc;
            }
            if let Some(value) = pcr_value(packet) {
                let shifted = value.saturating_add(self.pcr_offset) % PCR_MOD;
                write_pcr(packet, shifted);
                self.last_pcr = Some(shifted);
            }
            if packet[1] & 0x40 != 0 {
                if let Some(offset) = payload_offset(packet) {
                    let decoded = {
                        let p = &packet[offset..];
                        if p.len() >= 14 && p[0..3] == [0, 0, 1] {
                            let flags = p[7];
                            let mut values = (flags, None, None);
                            if flags & 0x80 != 0 {
                                let b = &p[9..14];
                                let pts = ((u64::from(b[0] & 0x0E)) << 29)
                                    | (u64::from(b[1]) << 22)
                                    | (u64::from(b[2] & 0xFE) << 14)
                                    | (u64::from(b[3]) << 7)
                                    | (u64::from(b[4]) >> 1);
                                values.1 = Some(pts);
                            }
                            if flags & 0x40 != 0 && p.len() >= 19 {
                                let b = &p[14..19];
                                let dts = ((u64::from(b[0] & 0x0E)) << 29)
                                    | (u64::from(b[1]) << 22)
                                    | (u64::from(b[2] & 0xFE) << 14)
                                    | (u64::from(b[3]) << 7)
                                    | (u64::from(b[4]) >> 1);
                                values.2 = Some(dts);
                            }
                            values
                        } else {
                            (0, None, None)
                        }
                    };
                    if let Some(pts) = decoded.1 {
                        let prefix = if decoded.0 & 0x40 != 0 {
                            0b0011
                        } else {
                            0b0010
                        };
                        write_timestamp(packet, offset, prefix, pts.wrapping_add(self.pts_offset));
                    }
                    if let Some(dts) = decoded.2 {
                        write_timestamp(
                            packet,
                            offset + 5,
                            0b0001,
                            dts.wrapping_add(self.pts_offset),
                        );
                    }
                }
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_template_is_structurally_valid() {
        let summary = validate(BOOTSTRAP_TEMPLATE, None).expect("template");
        assert_eq!(summary.bytes % TS_PKT_SZ, 0);
        assert!(summary.idr_count >= 2);
        assert!(summary.pcr_interval_max <= PCR_20MS);
        assert_eq!(summary.pcr_pid, summary.video_pid);
    }

    #[test]
    fn per_file_identity_is_required_and_channel_bound() {
        let a = Bootstrap::for_file(7, "sbt");
        let b = Bootstrap::for_file(8, "record");
        assert_eq!(a.validate().unwrap().bytes, a.len());
        assert_eq!(b.validate().unwrap().bytes, b.len());
        assert_ne!(a.bytes(), b.bytes());
        assert_eq!(
            validate(a.bytes(), Some((8, "sbt"))),
            Err(BootstrapError::IdentityMismatch {
                expected: 8,
                got: 7
            })
        );
        assert_eq!(
            validate(a.bytes(), Some((7, "record"))),
            Err(BootstrapError::IdentityChannelMismatch)
        );
    }

    #[test]
    fn validator_rejects_non_ts_and_missing_identity() {
        assert_eq!(validate(&[], None), Err(BootstrapError::Empty));
        let mut broken = BOOTSTRAP_TEMPLATE.to_vec();
        broken[0] = 0;
        assert_eq!(
            validate(&broken, None),
            Err(BootstrapError::BadSync { packet: 0 })
        );
        assert_eq!(
            validate(BOOTSTRAP_TEMPLATE, Some((1, "x"))),
            Err(BootstrapError::MissingIdentity)
        );
    }

    #[test]
    fn splice_continues_pcr_and_cc() {
        let boot = Bootstrap::for_file(1, "one");
        // Reuse the asset as a synthetic live prefix; the splice must rewrite
        // it rather than concatenate raw timing.
        let live = BOOTSTRAP_TEMPLATE;
        let spliced = splice_live(boot.bytes(), live).expect("splice");
        let boot_last_pcr = find_pcr(boot.bytes(), true).unwrap();
        let live_last_pcr = find_pcr(&spliced, true).unwrap();
        assert!(live_last_pcr > boot_last_pcr);
        validate_live_prefix(&spliced).expect("valid live prefix");
    }

    #[test]
    fn splice_state_is_continuous_across_transport_chunks() {
        let boot = Bootstrap::for_file(1, "one");
        let live = BOOTSTRAP_TEMPLATE;
        let split = (live.len() / 2 / TS_PKT_SZ) * TS_PKT_SZ;
        let mut state = SpliceState::new(boot.bytes(), live).unwrap();
        let mut chunks = state.rewrite(&live[..split]).unwrap();
        chunks.extend(state.rewrite(&live[split..]).unwrap());
        assert_eq!(chunks, splice_live(boot.bytes(), live).unwrap());
        let mut pes_count = 0;
        for packet in chunks.as_chunks::<TS_PKT_SZ>().0 {
            if packet[1] & 0x40 != 0 {
                if let Some(p) = payload(packet) {
                    if p.len() >= 14 && p[..3] == [0, 0, 1] && p[7] & 0x80 != 0 {
                        assert_eq!(p[9] & 1, 1, "PTS marker bit");
                        assert_eq!(p[11] & 1, 1);
                        assert_eq!(p[13] & 1, 1);
                        pes_count += 1;
                    }
                }
            }
        }
        assert!(pes_count > 1);
        validate(&chunks, None).unwrap();
    }

    #[test]
    fn reconnect_successor_continues_the_published_clock_and_counters() {
        let boot = Bootstrap::for_file(1, "one");
        let mut state = SpliceState::new(boot.bytes(), BOOTSTRAP_TEMPLATE).unwrap();
        let mut combined = boot.bytes().to_vec();
        let initial = state.rewrite(BOOTSTRAP_TEMPLATE).unwrap();
        combined.extend(&initial);
        let mut previous_pts = find_timestamp(&initial, true).unwrap();
        for _ in 0..3 {
            let previous_pcr = state.last_pcr.unwrap();
            state = state.successor(BOOTSTRAP_TEMPLATE).unwrap();
            let replacement = state.rewrite(BOOTSTRAP_TEMPLATE).unwrap();
            let first_pcr = find_pcr(&replacement, false).unwrap();
            assert!(first_pcr > previous_pcr);
            assert!(first_pcr - previous_pcr <= PCR_20MS);
            assert!(find_timestamp(&replacement, false).unwrap() > previous_pts);
            previous_pts = find_timestamp(&replacement, true).unwrap();
            combined.extend(replacement);
        }
        validate(&combined, None).expect("clock and CC remain continuous across fresh epochs");
    }

    #[test]
    fn ac3_channel_mode_is_read_after_bsid_and_bsmod() {
        let mut audio = [0x0b, 0x77, 0, 0, 28, 0x40, 0x40];
        assert!(check_ac3(&audio).is_ok());
        audio[6] = 0x20; // mono, while byte5 (bsid) is unchanged
        assert_eq!(
            check_ac3(&audio),
            Err(BootstrapError::AudioNotStereo48k384k)
        );
    }

    #[test]
    fn psi_rejects_truncation_eac3_and_descriptor_overflow() {
        let pat = BOOTSTRAP_TEMPLATE
            .as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .find(|packet| ((u16::from(packet[1]) & 0x1f) << 8 | u16::from(packet[2])) == 0)
            .unwrap();
        let p = payload(pat).unwrap();
        let (_, pmt_pid) = parse_pat(p).unwrap();
        let mut truncated = p.to_vec();
        truncated[2] = (truncated[2] & 0xf0) | 0xf;
        truncated[3] = 0xff;
        assert!(parse_pat(&truncated).is_none());
        let packet = BOOTSTRAP_TEMPLATE
            .as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .find(|packet| ((u16::from(packet[1]) & 0x1f) << 8 | u16::from(packet[2])) == pmt_pid)
            .unwrap();
        let mut p = payload(packet).unwrap().to_vec();
        let section = &p[1..];
        let end = 3 + (((section[1] as usize & 0xf) << 8) | section[2] as usize);
        let mut cursor = 12 + (((section[10] as usize & 0xf) << 8) | section[11] as usize);
        while cursor + 5 <= end - 4 && p[1 + cursor] != 0x81 {
            let len = ((p[1 + cursor + 3] as usize & 0xf) << 8) | p[1 + cursor + 4] as usize;
            cursor += 5 + len;
        }
        assert_eq!(p[1 + cursor], 0x81);
        p[1 + cursor] = 0x87;
        let crc = crc32_mpeg(&p[1..1 + end - 4]);
        p[1 + end - 4..1 + end].copy_from_slice(&crc.to_be_bytes());
        assert!(parse_pmt(&p, pmt_pid).is_none());
        p[1 + cursor] = 0x81;
        p[11] = (p[11] & 0xf0) | 0xf;
        p[12] = 0xff;
        let crc = crc32_mpeg(&p[1..1 + end - 4]);
        p[1 + end - 4..1 + end].copy_from_slice(&crc.to_be_bytes());
        assert!(parse_pmt(&p, pmt_pid).is_none());
    }

    #[test]
    fn failed_timestamp_validation_does_not_advance_splice_state() {
        let boot = Bootstrap::for_file(1, "one");
        let mut state = SpliceState::new(boot.bytes(), BOOTSTRAP_TEMPLATE).unwrap();
        let packet = BOOTSTRAP_TEMPLATE
            .as_chunks::<TS_PKT_SZ>()
            .0
            .iter()
            .find(|packet| {
                packet[1] & 0x40 != 0
                    && payload(*packet)
                        .is_some_and(|p| p.len() >= 14 && p[..3] == [0, 0, 1] && p[7] & 0x80 != 0)
            })
            .unwrap();
        let mut broken = packet.to_vec();
        let offset = payload_offset(packet).unwrap();
        broken[offset + 9] &= !1;
        assert!(state.rewrite(&broken).is_err());
        let expected = splice_live(boot.bytes(), BOOTSTRAP_TEMPLATE).unwrap();
        assert_eq!(state.rewrite(BOOTSTRAP_TEMPLATE).unwrap(), expected);
    }

    #[test]
    #[ignore = "requires operator-supplied live capture"]
    fn captured_live_source_validates_and_splices_across_frames() {
        let path = std::env::var("FAVORITES_LIVE_SAMPLE").expect("sample path");
        let bytes = std::fs::read(path).unwrap();
        let summary = validate(&bytes, None).expect("captured source must meet Samsung invariants");
        eprintln!("live capture validated: {summary:?}");
        let bootstrap = Bootstrap::for_file(1, "captured");
        let mut state = SpliceState::new(bootstrap.bytes(), &bytes).unwrap();
        let mut combined = bootstrap.bytes().to_vec();
        for frame in bytes.chunks(TS_PKT_SZ * 349) {
            combined.extend(state.rewrite(frame).unwrap());
        }
        validate(&combined, None).expect("combined splice remains continuous");
        if let Ok(output) = std::env::var("FAVORITES_SPLICED_SAMPLE") {
            std::fs::write(output, combined).unwrap();
        }
    }

    fn validate_live_prefix(data: &[u8]) -> Result<(), BootstrapError> {
        // Reuse the main validator without identity requirements.
        validate(data, None).map(|_| ())
    }
}
