//! Authenticated, epoch-bound framing between the source manager and feeder.
//!
//! A control ACK never authorizes bytes by itself. Media travels in explicit
//! frames carrying session, manifest, file, channel, request generation and
//! `source_epoch`; the header and payload are authenticated with HMAC-SHA256
//! keyed by a token loaded at runtime. The FUSE/USB side removes the framing
//! and only ever receives raw MPEG-TS.
//!
//! The transport is intentionally independent of TLS: it is designed to run
//! over a private/TLS link. A plain public link would expose the token and is
//! therefore rejected by the deployment documentation.

use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use crate::contract::TS_PKT_SZ;
use crate::favorites::digest::sha256;
use crate::favorites::runtime::{SourceTransport, TransportFailure};
use crate::favorites::selection::{DataFrame, SelectionRequest};

pub const FRAME_MAGIC: &[u8; 4] = b"TVFT";
pub const FRAME_VERSION: u8 = 1;
pub const FRAME_HEADER_LEN: usize = 46;
pub const FRAME_TAG_LEN: usize = 32;
pub const MAX_FRAME_PAYLOAD: usize = 256 * 1024;
pub const MAX_IDENTITY_LEN: usize = 512;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    EmptyToken,
    FrameTooShort,
    BadMagic,
    UnsupportedVersion(u8),
    BadLength,
    FrameTooLarge { size: usize },
    AuthenticatedTagMismatch,
    IdentityTooLong,
    PayloadNotTs,
    SameEpochReused,
    StaleGeneration { expected: u64, got: u64 },
    WrongEpoch { expected: u64, got: u64 },
    WrongSession,
    WrongManifest,
    WrongFile,
    WrongChannel,
    Replay { last: u64, got: u64 },
    SequenceGap { expected: u64, got: u64 },
    Io(String),
}

fn hmac_key(token: &str) -> Result<[u8; 32], TransportError> {
    if token.trim().is_empty() {
        return Err(TransportError::EmptyToken);
    }
    let mut material = b"tv-live-favorites/transport/v1\0".to_vec();
    material.extend_from_slice(token.as_bytes());
    Ok(sha256(&material))
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&sha256(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Vec::with_capacity(64 + data.len());
    let mut outer = Vec::with_capacity(64 + 32);
    for byte in block {
        inner.push(byte ^ 0x36);
        outer.push(byte ^ 0x5c);
    }
    inner.extend_from_slice(data);
    outer.extend_from_slice(&sha256(&inner));
    sha256(&outer)
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

fn put_u16(out: &mut Vec<u8>, value: u16) {
    out.extend_from_slice(&value.to_be_bytes());
}
fn put_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_be_bytes());
}
fn put_u64(out: &mut Vec<u8>, value: u64) {
    out.extend_from_slice(&value.to_be_bytes());
}

/// Encodes one framed media chunk. `sequence` starts at 1 for each epoch.
pub fn encode_frame(
    frame: &DataFrame,
    sequence: u64,
    token: &str,
) -> Result<Vec<u8>, TransportError> {
    let key = hmac_key(token)?;
    for value in [&frame.session_id, &frame.manifest_id, &frame.channel_id] {
        if value.len() > MAX_IDENTITY_LEN {
            return Err(TransportError::IdentityTooLong);
        }
    }
    if frame.payload.len() > MAX_FRAME_PAYLOAD {
        return Err(TransportError::FrameTooLarge {
            size: frame.payload.len(),
        });
    }
    if frame.payload.is_empty() || !frame.payload.len().is_multiple_of(TS_PKT_SZ) {
        return Err(TransportError::PayloadNotTs);
    }
    let mut out = Vec::with_capacity(
        FRAME_HEADER_LEN
            + frame.session_id.len()
            + frame.manifest_id.len()
            + frame.channel_id.len()
            + frame.payload.len()
            + FRAME_TAG_LEN,
    );
    out.extend_from_slice(FRAME_MAGIC);
    out.push(FRAME_VERSION);
    out.push(0);
    put_u16(&mut out, FRAME_HEADER_LEN as u16);
    put_u32(&mut out, frame.file_id);
    put_u64(&mut out, frame.request_generation);
    put_u64(&mut out, frame.source_epoch);
    put_u64(&mut out, sequence);
    put_u32(&mut out, frame.payload.len() as u32);
    put_u16(&mut out, frame.session_id.len() as u16);
    put_u16(&mut out, frame.manifest_id.len() as u16);
    put_u16(&mut out, frame.channel_id.len() as u16);
    out.extend_from_slice(frame.session_id.as_bytes());
    out.extend_from_slice(frame.manifest_id.as_bytes());
    out.extend_from_slice(frame.channel_id.as_bytes());
    out.extend_from_slice(&frame.payload);
    let tag = hmac_sha256(&key, &out);
    out.extend_from_slice(&tag);
    Ok(out)
}

fn read_u16(data: &[u8], offset: usize) -> u16 {
    u16::from_be_bytes([data[offset], data[offset + 1]])
}
fn read_u32(data: &[u8], offset: usize) -> u32 {
    u32::from_be_bytes(data[offset..offset + 4].try_into().unwrap())
}
fn read_u64(data: &[u8], offset: usize) -> u64 {
    u64::from_be_bytes(data[offset..offset + 8].try_into().unwrap())
}

/// Decodes and authenticates one media frame against the exact intent it is
/// supposed to satisfy. Any mismatch is terminal for that frame.
pub fn decode_frame(
    data: &[u8],
    token: &str,
    expected: &SelectionRequest,
) -> Result<(DataFrame, u64), TransportError> {
    let key = hmac_key(token)?;
    if data.len() < FRAME_HEADER_LEN + FRAME_TAG_LEN {
        return Err(TransportError::FrameTooShort);
    }
    if &data[..4] != FRAME_MAGIC {
        return Err(TransportError::BadMagic);
    }
    if data[4] != FRAME_VERSION {
        return Err(TransportError::UnsupportedVersion(data[4]));
    }
    if read_u16(data, 6) as usize != FRAME_HEADER_LEN {
        return Err(TransportError::BadLength);
    }
    let file_id = read_u32(data, 8);
    let generation = read_u64(data, 12);
    let epoch = read_u64(data, 20);
    let sequence = read_u64(data, 28);
    let payload_len = read_u32(data, 36) as usize;
    let session_len = read_u16(data, 40) as usize;
    let manifest_len = read_u16(data, 42) as usize;
    let channel_len = read_u16(data, 44) as usize;
    let identity_len = session_len
        .checked_add(manifest_len)
        .and_then(|v| v.checked_add(channel_len))
        .ok_or(TransportError::BadLength)?;
    let total = FRAME_HEADER_LEN
        .checked_add(identity_len)
        .and_then(|v| v.checked_add(payload_len))
        .and_then(|v| v.checked_add(FRAME_TAG_LEN))
        .ok_or(TransportError::BadLength)?;
    if total != data.len() {
        return Err(TransportError::BadLength);
    }
    if payload_len == 0 || !payload_len.is_multiple_of(TS_PKT_SZ) {
        return Err(TransportError::PayloadNotTs);
    }
    if payload_len > MAX_FRAME_PAYLOAD {
        return Err(TransportError::FrameTooLarge { size: payload_len });
    }
    let signed = &data[..data.len() - FRAME_TAG_LEN];
    let tag = hmac_sha256(&key, signed);
    if !constant_time_eq(&tag, &data[data.len() - FRAME_TAG_LEN..]) {
        return Err(TransportError::AuthenticatedTagMismatch);
    }

    let mut cursor = FRAME_HEADER_LEN;
    let session = std::str::from_utf8(&data[cursor..cursor + session_len])
        .map_err(|_| TransportError::BadLength)?
        .to_string();
    cursor += session_len;
    let manifest = std::str::from_utf8(&data[cursor..cursor + manifest_len])
        .map_err(|_| TransportError::BadLength)?
        .to_string();
    cursor += manifest_len;
    let channel = std::str::from_utf8(&data[cursor..cursor + channel_len])
        .map_err(|_| TransportError::BadLength)?
        .to_string();
    cursor += channel_len;
    let payload = data[cursor..cursor + payload_len].to_vec();

    if generation != expected.request_generation {
        return Err(TransportError::StaleGeneration {
            expected: expected.request_generation,
            got: generation,
        });
    }
    if epoch != expected.source_epoch {
        return Err(TransportError::WrongEpoch {
            expected: expected.source_epoch,
            got: epoch,
        });
    }
    if session != expected.session_id {
        return Err(TransportError::WrongSession);
    }
    if manifest != expected.manifest_id {
        return Err(TransportError::WrongManifest);
    }
    if file_id != expected.file_id {
        return Err(TransportError::WrongFile);
    }
    if channel != expected.channel_id {
        return Err(TransportError::WrongChannel);
    }
    let frame = DataFrame {
        session_id: session,
        manifest_id: manifest,
        file_id,
        channel_id: channel,
        request_generation: generation,
        source_epoch: epoch,
        sequence,
        payload,
    };
    Ok((frame, sequence))
}

/// Private loopback media link. The control preamble is line-oriented and the
/// media stream is the authenticated framing above; the server must bind to a
/// private interface and is never exposed publicly without TLS termination.
pub struct TcpFrameTransport {
    address: String,
    token: String,
    stream: Option<TcpStream>,
    verifier: Option<FrameVerifier>,
    confirmation: Option<(u64, u64)>,
    buffer: Vec<u8>,
    handshake_timeout: Duration,
}

impl TcpFrameTransport {
    pub fn new(address: &str, token: &str) -> Result<Self, TransportError> {
        hmac_key(token)?;
        Ok(Self {
            address: address.to_string(),
            token: token.to_string(),
            stream: None,
            verifier: None,
            confirmation: None,
            buffer: Vec::new(),
            handshake_timeout: Duration::from_secs(10),
        })
    }

    fn write_preamble(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure> {
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| TransportFailure("not connected".into()))?;
        let line = format!(
            "OPEN {} {} {} {} {} {} {}\n",
            request.request_generation,
            request.source_epoch,
            request.file_id,
            request.session_id.len(),
            request.manifest_id.len(),
            request.channel_id.len(),
            self.token.len()
        );
        stream
            .write_all(line.as_bytes())
            .and_then(|_| stream.write_all(request.session_id.as_bytes()))
            .and_then(|_| stream.write_all(request.manifest_id.as_bytes()))
            .and_then(|_| stream.write_all(request.channel_id.as_bytes()))
            .and_then(|_| stream.write_all(self.token.as_bytes()))
            .and_then(|_| stream.flush())
            .map_err(|error| TransportFailure(error.to_string()))?;
        let mut response = Vec::new();
        let mut byte = [0u8; 1];
        let deadline = Instant::now() + self.handshake_timeout;
        loop {
            let remain = deadline.saturating_duration_since(Instant::now());
            if remain.is_zero() {
                return Err(TransportFailure("media handshake timed out".into()));
            }
            stream
                .set_read_timeout(Some(remain))
                .map_err(|error| TransportFailure(error.to_string()))?;
            match stream.read(&mut byte) {
                Ok(1) => {
                    if byte[0] == b'\n' {
                        break;
                    }
                    if response.len() > 256 {
                        return Err(TransportFailure("response too long".into()));
                    }
                    response.push(byte[0]);
                }
                Ok(_) => return Err(TransportFailure("server closed during open".into())),
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(error) => return Err(TransportFailure(error.to_string())),
            }
        }
        let response = String::from_utf8_lossy(&response);
        let mut parts = response.split_whitespace();
        if parts.next() != Some("OK") {
            return Err(TransportFailure(response.into_owned()));
        }
        let generation = parts
            .next()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| TransportFailure("missing confirmation generation".into()))?;
        let epoch = parts
            .next()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| TransportFailure("missing confirmation epoch".into()))?;
        if parts.next().is_some() {
            return Err(TransportFailure("trailing confirmation fields".into()));
        }
        self.confirmation = Some((generation, epoch));
        Ok(())
    }

    fn fill(&mut self) -> Result<bool, TransportFailure> {
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| TransportFailure("not connected".into()))?;
        let mut temp = [0u8; 64 * 1024];
        match stream.read(&mut temp) {
            Ok(0) => Err(TransportFailure("media link closed".into())),
            Ok(n) => {
                self.buffer.extend_from_slice(&temp[..n]);
                Ok(true)
            }
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                Ok(false)
            }
            Err(error) => Err(TransportFailure(error.to_string())),
        }
    }

    fn extract(&mut self) -> Option<Vec<u8>> {
        if self.buffer.len() < FRAME_HEADER_LEN {
            return None;
        }
        let header = &self.buffer[..FRAME_HEADER_LEN];
        if &header[..4] != FRAME_MAGIC {
            return None;
        }
        let session_len = read_u16(header, 40) as usize;
        let manifest_len = read_u16(header, 42) as usize;
        let channel_len = read_u16(header, 44) as usize;
        let payload_len = read_u32(header, 36) as usize;
        let total = FRAME_HEADER_LEN
            .checked_add(session_len)?
            .checked_add(manifest_len)?
            .checked_add(channel_len)?
            .checked_add(payload_len)?
            .checked_add(FRAME_TAG_LEN)?;
        if total > FRAME_HEADER_LEN + MAX_FRAME_PAYLOAD + 3 * MAX_IDENTITY_LEN + FRAME_TAG_LEN {
            return None;
        }
        if self.buffer.len() < total {
            return None;
        }
        Some(self.buffer.drain(..total).collect())
    }
}

impl SourceTransport for TcpFrameTransport {
    fn open(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure> {
        self.stream = None;
        self.verifier = None;
        self.confirmation = None;
        self.buffer.clear();
        let stream = TcpStream::connect(&self.address)
            .map_err(|error| TransportFailure(error.to_string()))?;
        stream
            .set_read_timeout(Some(Duration::from_millis(250)))
            .map_err(|error| TransportFailure(error.to_string()))?;
        stream
            .set_write_timeout(Some(Duration::from_millis(2000)))
            .map_err(|error| TransportFailure(error.to_string()))?;
        self.stream = Some(stream);
        let verifier = FrameVerifier::new(request.clone(), &self.token)
            .map_err(|error| TransportFailure(format!("{error:?}")))?;
        self.write_preamble(request)?;
        self.stream
            .as_ref()
            .unwrap()
            .set_read_timeout(Some(Duration::from_millis(250)))
            .map_err(|error| TransportFailure(error.to_string()))?;
        self.verifier = Some(verifier);
        Ok(())
    }

    fn poll(&mut self) -> Result<Option<DataFrame>, TransportFailure> {
        if let Some(wire) = self.extract() {
            let verifier = self
                .verifier
                .as_mut()
                .ok_or_else(|| TransportFailure("not open".into()))?;
            return verifier
                .verify(&wire)
                .map(Some)
                .map_err(|error| TransportFailure(format!("{error:?}")));
        }
        if !self.fill()? {
            return Ok(None);
        }
        if let Some(wire) = self.extract() {
            let verifier = self
                .verifier
                .as_mut()
                .ok_or_else(|| TransportFailure("not open".into()))?;
            return verifier
                .verify(&wire)
                .map(Some)
                .map_err(|error| TransportFailure(format!("{error:?}")));
        }
        Ok(None)
    }

    fn confirmation(&self) -> Option<(u64, u64)> {
        self.confirmation
    }

    fn cancel(&mut self, request: &SelectionRequest) {
        if let Some(stream) = self.stream.as_mut() {
            let line = format!(
                "CANCEL {} {}\n",
                request.request_generation, request.source_epoch
            );
            let _ = stream.write_all(line.as_bytes());
            let _ = stream.flush();
        }
        self.stream = None;
        self.verifier = None;
        self.confirmation = None;
        self.buffer.clear();
    }
}

/// Stateful verifier for one epoch. It rejects replayed/out-of-order frames so
/// an old payload cannot be relabeled and delivered later.
#[derive(Debug)]
pub struct FrameVerifier {
    expected: SelectionRequest,
    token: String,
    last_sequence: u64,
}

impl FrameVerifier {
    pub fn new(expected: SelectionRequest, token: &str) -> Result<Self, TransportError> {
        hmac_key(token)?;
        Ok(Self {
            expected,
            token: token.to_string(),
            last_sequence: 0,
        })
    }

    pub fn last_sequence(&self) -> u64 {
        self.last_sequence
    }

    pub fn verify(&mut self, wire: &[u8]) -> Result<DataFrame, TransportError> {
        let (frame, sequence) = decode_frame(wire, &self.token, &self.expected)?;
        let expected = self
            .last_sequence
            .checked_add(1)
            .ok_or(TransportError::BadLength)?;
        if sequence == self.last_sequence {
            return Err(TransportError::Replay {
                last: self.last_sequence,
                got: sequence,
            });
        }
        if sequence < expected {
            return Err(TransportError::Replay {
                last: self.last_sequence,
                got: sequence,
            });
        }
        if sequence != expected {
            return Err(TransportError::SequenceGap {
                expected,
                got: sequence,
            });
        }
        self.last_sequence = sequence;
        Ok(frame)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlAuth {
    pub session_id: String,
    pub manifest_id: String,
}

/// Header-only authentication for the control API. The token is never read
/// from a query string, path or fixed default. `Authorization: Bearer ...` is
/// mandatory and compared in constant time; mount session and manifest must be
/// supplied as explicit headers.
pub fn authenticate_headers(
    configured_token: &str,
    headers: &str,
) -> Result<ControlAuth, ControlError> {
    if configured_token.trim().is_empty() {
        return Err(ControlError::EmptyToken);
    }
    let mut authorization = None;
    let mut session = None;
    let mut manifest = None;
    for line in headers.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        let value = value.trim();
        if key.eq_ignore_ascii_case("authorization") {
            authorization = Some(value.to_string());
        } else if key.eq_ignore_ascii_case("x-mount-session") {
            session = Some(value.to_string());
        } else if key.eq_ignore_ascii_case("x-manifest-id") {
            manifest = Some(value.to_string());
        }
    }
    let authorization = authorization.ok_or(ControlError::Unauthorized)?;
    let Some(token) = authorization.strip_prefix("Bearer ") else {
        return Err(ControlError::Unauthorized);
    };
    if !constant_time_eq(token.as_bytes(), configured_token.as_bytes()) {
        return Err(ControlError::Unauthorized);
    }
    let session_id = session
        .filter(|value| !value.trim().is_empty())
        .ok_or(ControlError::WrongSession)?;
    let manifest_id = manifest
        .filter(|value| !value.trim().is_empty())
        .ok_or(ControlError::WrongManifest)?;
    Ok(ControlAuth {
        session_id,
        manifest_id,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlOperation {
    Prepare,
    Confirm,
    Cancel,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlRequest {
    pub mount_session_id: String,
    pub manifest_id: String,
    pub file_id: u32,
    pub channel_id: String,
    pub request_generation: u64,
    pub operation: ControlOperation,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlError {
    EmptyToken,
    Unauthorized,
    WrongSession,
    WrongManifest,
    EmptyChannel,
    NotLive,
    StaleGeneration { expected: u64, got: u64 },
    NoGrant,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Grant {
    pub request: SelectionRequest,
    pub operation: ControlOperation,
}

/// Server-side source manager contract. One session owns one preparation; a
/// new generation supersedes the previous grant, which can no longer publish.
#[derive(Debug)]
pub struct SourceManager {
    configured_token: String,
    session_id: String,
    manifest_id: String,
    next_epoch: u64,
    next_generation: u64,
    grant: Option<Grant>,
}

impl SourceManager {
    pub fn new(token: &str, session_id: &str, manifest_id: &str) -> Result<Self, ControlError> {
        if token.trim().is_empty() {
            return Err(ControlError::EmptyToken);
        }
        Ok(Self {
            configured_token: token.to_string(),
            session_id: session_id.to_string(),
            manifest_id: manifest_id.to_string(),
            next_epoch: 0,
            next_generation: 0,
            grant: None,
        })
    }

    pub fn grant(&self) -> Option<&Grant> {
        self.grant.as_ref()
    }

    fn authorize(&self, token: &str, request: &ControlRequest) -> Result<(), ControlError> {
        if !constant_time_eq(token.as_bytes(), self.configured_token.as_bytes()) {
            return Err(ControlError::Unauthorized);
        }
        if request.mount_session_id != self.session_id {
            return Err(ControlError::WrongSession);
        }
        if request.manifest_id != self.manifest_id {
            return Err(ControlError::WrongManifest);
        }
        if request.channel_id.trim().is_empty() {
            return Err(ControlError::EmptyChannel);
        }
        Ok(())
    }

    /// Prepares one source. Repeating the current intent is idempotent; a
    /// different intent gets a fresh generation and epoch and invalidates the
    /// previous grant. A cancel retires the current preparation.
    pub fn prepare(
        &mut self,
        token: &str,
        mut request: ControlRequest,
    ) -> Result<Grant, ControlError> {
        self.authorize(token, &request)?;
        if let Some(grant) = &self.grant {
            if grant.request.file_id == request.file_id
                && grant.request.channel_id == request.channel_id
                && request.operation == ControlOperation::Prepare
            {
                return Ok(grant.clone());
            }
        }
        let generation =
            self.next_generation
                .checked_add(1)
                .ok_or(ControlError::StaleGeneration {
                    expected: self.next_generation,
                    got: u64::MAX,
                })?;
        let epoch = self
            .next_epoch
            .checked_add(1)
            .ok_or(ControlError::StaleGeneration {
                expected: self.next_epoch,
                got: u64::MAX,
            })?;
        self.next_generation = generation;
        self.next_epoch = epoch;
        request.request_generation = generation;
        request.operation = ControlOperation::Prepare;
        let grant = Grant {
            request: SelectionRequest {
                session_id: request.mount_session_id.clone(),
                manifest_id: request.manifest_id.clone(),
                file_id: request.file_id,
                channel_id: request.channel_id.clone(),
                request_generation: generation,
                source_epoch: epoch,
            },
            operation: ControlOperation::Prepare,
        };
        self.grant = Some(grant.clone());
        Ok(grant)
    }

    /// Confirms an epoch. A confirmation for a superseded generation is
    /// rejected; confirmation alone still does not authorize bytes.
    pub fn confirm(
        &mut self,
        token: &str,
        generation: u64,
        epoch: u64,
    ) -> Result<Grant, ControlError> {
        let grant = self.grant.as_ref().ok_or(ControlError::NoGrant)?;
        let mut request = ControlRequest {
            mount_session_id: grant.request.session_id.clone(),
            manifest_id: grant.request.manifest_id.clone(),
            file_id: grant.request.file_id,
            channel_id: grant.request.channel_id.clone(),
            request_generation: generation,
            operation: ControlOperation::Confirm,
        };
        self.authorize(token, &request)?;
        if grant.request.request_generation != generation {
            return Err(ControlError::StaleGeneration {
                expected: grant.request.request_generation,
                got: generation,
            });
        }
        if grant.request.source_epoch != epoch {
            return Err(ControlError::StaleGeneration {
                expected: grant.request.source_epoch,
                got: epoch,
            });
        }
        request.operation = ControlOperation::Confirm;
        Ok(grant.clone())
    }

    pub fn cancel(&mut self, token: &str, generation: u64) -> Result<(), ControlError> {
        let grant = self.grant.as_ref().ok_or(ControlError::NoGrant)?;
        let request = ControlRequest {
            mount_session_id: grant.request.session_id.clone(),
            manifest_id: grant.request.manifest_id.clone(),
            file_id: grant.request.file_id,
            channel_id: grant.request.channel_id.clone(),
            request_generation: generation,
            operation: ControlOperation::Cancel,
        };
        self.authorize(token, &request)?;
        if grant.request.request_generation != generation {
            return Err(ControlError::StaleGeneration {
                expected: grant.request.request_generation,
                got: generation,
            });
        }
        self.grant = None;
        Ok(())
    }

    /// The feeder-side check: a frame is acceptable only for the live grant.
    pub fn verify_media(&self, token: &str, wire: &[u8]) -> Result<DataFrame, TransportError> {
        let grant = self.grant.as_ref().ok_or(TransportError::WrongEpoch {
            expected: 0,
            got: 0,
        })?;
        decode_frame(wire, token, &grant.request).map(|(frame, _)| frame)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req() -> SelectionRequest {
        SelectionRequest {
            session_id: "session-a".into(),
            manifest_id: "manifest-a".into(),
            file_id: 7,
            channel_id: "sbt".into(),
            request_generation: 3,
            source_epoch: 4,
        }
    }

    fn frame(request: &SelectionRequest, sequence: u64, byte: u8) -> DataFrame {
        let mut payload = vec![0u8; TS_PKT_SZ * 2];
        for packet in payload.as_chunks_mut::<TS_PKT_SZ>().0 {
            packet[0] = 0x47;
            packet[1] = 0x1f;
            packet[2] = 0xff;
            packet[3] = 0x10;
            packet[4..].fill(byte);
        }
        DataFrame {
            session_id: request.session_id.clone(),
            manifest_id: request.manifest_id.clone(),
            file_id: request.file_id,
            channel_id: request.channel_id.clone(),
            request_generation: request.request_generation,
            source_epoch: request.source_epoch,
            sequence,
            payload,
        }
    }

    #[test]
    fn authenticated_frame_roundtrip_and_replay_rejection() {
        let request = req();
        let wire = encode_frame(&frame(&request, 1, 0xa1), 1, "secret").unwrap();
        let mut verifier = FrameVerifier::new(request.clone(), "secret").unwrap();
        let decoded = verifier.verify(&wire).unwrap();
        assert_eq!(decoded.payload[5], 0xa1);
        assert_eq!(
            verifier.verify(&wire),
            Err(TransportError::Replay { last: 1, got: 1 })
        );
        assert_eq!(
            decode_frame(&wire, "wrong", &request),
            Err(TransportError::AuthenticatedTagMismatch)
        );
        assert_eq!(
            encode_frame(&frame(&request, 1, 1), 1, ""),
            Err(TransportError::EmptyToken)
        );
    }

    #[test]
    fn old_payload_cannot_be_relabeled_for_new_epoch() {
        let old = SelectionRequest {
            request_generation: 1,
            source_epoch: 1,
            ..req()
        };
        let new = SelectionRequest {
            request_generation: 2,
            source_epoch: 2,
            ..req()
        };
        let wire = encode_frame(&frame(&old, 1, 0xa1), 1, "secret").unwrap();
        assert_eq!(
            decode_frame(&wire, "secret", &new),
            Err(TransportError::StaleGeneration {
                expected: 2,
                got: 1
            })
        );
        let mut forged = frame(&new, 1, 0xa1);
        forged.file_id = 99;
        let wire = encode_frame(&forged, 1, "secret").unwrap();
        assert_eq!(
            decode_frame(&wire, "secret", &new),
            Err(TransportError::WrongFile)
        );
    }

    #[test]
    fn sequence_gaps_are_rejected() {
        let request = req();
        let mut verifier = FrameVerifier::new(request.clone(), "secret").unwrap();
        let wire1 = encode_frame(&frame(&request, 1, 1), 1, "secret").unwrap();
        let wire3 = encode_frame(&frame(&request, 3, 3), 3, "secret").unwrap();
        verifier.verify(&wire1).unwrap();
        assert_eq!(
            verifier.verify(&wire3),
            Err(TransportError::SequenceGap {
                expected: 2,
                got: 3
            })
        );
    }

    #[test]
    fn manager_binds_session_manifest_epoch_and_cancels() {
        let mut mgr = SourceManager::new("secret", "s1", "m1").unwrap();
        let control = ControlRequest {
            mount_session_id: "s1".into(),
            manifest_id: "m1".into(),
            file_id: 7,
            channel_id: "sbt".into(),
            request_generation: 0,
            operation: ControlOperation::Prepare,
        };
        let grant = mgr.prepare("secret", control.clone()).unwrap();
        assert_eq!(grant.request.source_epoch, 1);
        assert!(mgr.prepare("wrong", control.clone()).is_err());
        let mut wrong = control.clone();
        wrong.mount_session_id = "other".into();
        assert_eq!(
            mgr.prepare("secret", wrong),
            Err(ControlError::WrongSession)
        );

        let wire = encode_frame(&frame(&grant.request, 1, 0xa1), 1, "secret").unwrap();
        assert_eq!(mgr.verify_media("secret", &wire).unwrap().file_id, 7);
        assert!(mgr.confirm("secret", 1, 2).is_err());

        let next = mgr
            .prepare(
                "secret",
                ControlRequest {
                    file_id: 8,
                    channel_id: "record".into(),
                    ..control
                },
            )
            .unwrap();
        assert_eq!(next.request.request_generation, 2);
        assert_eq!(
            mgr.verify_media("secret", &wire).unwrap_err(),
            TransportError::StaleGeneration {
                expected: 2,
                got: 1
            }
        );
        mgr.cancel("secret", 2).unwrap();
        assert!(mgr.grant().is_none());
        assert_eq!(
            mgr.verify_media("secret", &wire),
            Err(TransportError::WrongEpoch {
                expected: 0,
                got: 0
            })
        );
    }

    #[test]
    fn control_auth_requires_header_bearer_and_explicit_identity() {
        let headers = "Authorization: Bearer secret\r\nX-Mount-Session: s1\r\nX-Manifest-Id: m1\r\nContent-Length: 0\r\n";
        let auth = authenticate_headers("secret", headers).unwrap();
        assert_eq!(auth.session_id, "s1");
        assert_eq!(auth.manifest_id, "m1");
        assert_eq!(
            authenticate_headers("secret", "X-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n"),
            Err(ControlError::Unauthorized)
        );
        assert_eq!(
            authenticate_headers(
                "secret",
                "Authorization: Basic secret\r\nX-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n"
            ),
            Err(ControlError::Unauthorized)
        );
        assert_eq!(
            authenticate_headers("", headers),
            Err(ControlError::EmptyToken)
        );
    }

    #[test]
    fn tcp_loopback_link_receives_only_verified_epoch_frames() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let request = SelectionRequest {
            session_id: "session".into(),
            manifest_id: "manifest".into(),
            file_id: 7,
            channel_id: "sbt".into(),
            request_generation: 1,
            source_epoch: 1,
        };
        let wire = encode_frame(&frame(&request, 1, 0xa1), 1, "secret").unwrap();
        let expected = request.clone();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut line = Vec::new();
            let mut byte = [0u8; 1];
            loop {
                socket.read_exact(&mut byte).unwrap();
                if byte[0] == b'\n' {
                    break;
                }
                line.push(byte[0]);
            }
            let line = String::from_utf8(line).unwrap();
            let parts: Vec<&str> = line.split_whitespace().collect();
            assert_eq!(parts[0], "OPEN");
            let session_len: usize = parts[4].parse().unwrap();
            let manifest_len: usize = parts[5].parse().unwrap();
            let channel_len: usize = parts[6].parse().unwrap();
            let token_len: usize = parts[7].parse().unwrap();
            let mut identity = vec![0u8; session_len + manifest_len + channel_len + token_len];
            socket.read_exact(&mut identity).unwrap();
            assert_eq!(&identity[..session_len], expected.session_id.as_bytes());
            assert_eq!(
                &identity[session_len + manifest_len + channel_len..],
                b"secret"
            );
            // Chisel/private-link RTT can exceed the 250 ms polling budget.
            std::thread::sleep(Duration::from_millis(400));
            socket.write_all(b"OK 1 1\n").unwrap();
            socket.write_all(&wire).unwrap();
            socket.flush().unwrap();
            std::thread::sleep(std::time::Duration::from_millis(50));
        });

        let mut transport = TcpFrameTransport::new(&address, "secret").unwrap();
        transport.open(&request).unwrap();
        assert_eq!(transport.confirmation(), Some((1, 1)));
        let mut received = None;
        for _ in 0..20 {
            if let Some(frame) = transport.poll().unwrap() {
                received = Some(frame);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let received = received.expect("frame");
        assert_eq!(received.payload[5], 0xa1);
        transport.cancel(&request);
        server.join().unwrap();
    }

    #[test]
    fn silent_handshake_has_a_bounded_deadline() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let server = std::thread::spawn(move || {
            let (_socket, _) = listener.accept().unwrap();
            std::thread::sleep(Duration::from_millis(300));
        });
        let request = SelectionRequest {
            session_id: "session".into(),
            manifest_id: "manifest".into(),
            file_id: 7,
            channel_id: "sbt".into(),
            request_generation: 1,
            source_epoch: 1,
        };
        let mut transport = TcpFrameTransport::new(&address, "secret").unwrap();
        transport.handshake_timeout = Duration::from_millis(80);
        let started = Instant::now();
        assert!(transport.open(&request).is_err());
        assert!(started.elapsed() < Duration::from_millis(250));
        server.join().unwrap();
    }

    #[test]
    fn python_manager_wire_fixture_is_accepted_byte_for_byte() {
        let request = SelectionRequest {
            session_id: "session".into(),
            manifest_id: "manifest".into(),
            file_id: 7,
            channel_id: "sbt".into(),
            request_generation: 3,
            source_epoch: 4,
        };
        let wire = include_bytes!("../../tests/fixtures/python_frame.bin");
        let mut verifier = FrameVerifier::new(request, "secret").unwrap();
        let frame = verifier.verify(wire).unwrap();
        assert_eq!(frame.sequence, 1);
        assert_eq!(frame.payload.len(), 4 * TS_PKT_SZ);
        assert_eq!(frame.payload[5], 0xa1);
    }
}
