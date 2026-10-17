#![forbid(unsafe_code)]

//! Sparse unpacker for thin-provisioned ext4 disk images.
//!
//! Streams chunks from a `SPARSE01` stream into a sparse file on disk,
//! guaranteeing true ext4 sparse holes, atomic staging, 64-bit bounds safety,
//! and complete durability.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, ErrorKind, Read};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};

pub const SPARSE_MAGIC: &[u8; 8] = b"SPARSE01";
pub const EOS_MARKER: u64 = 0xFFFF_FFFF_FFFF_FFFF;
pub const IO_BUF_SIZE: usize = 64 * 1024; // 64 KiB aligned to Linux pipe capacity
pub const MAX_VIRTUAL_SIZE: u64 = 16 * (1u64 << 40); // 16 TiB sanity limit for ext4

#[derive(Debug)]
pub enum UnpackError {
    Usage,
    EmptyOutputPath,
    InvalidMagic([u8; 8]),
    TruncatedHeader(&'static str),
    TruncatedPayload {
        offset: u64,
        expected: u32,
        remaining: u32,
    },
    MissingSentinel,
    ZeroVirtualSize,
    VirtualSizeTooLarge(u64),
    ZeroLengthChunk {
        offset: u64,
    },
    OffsetOverflow {
        offset: u64,
        len: u32,
    },
    ChunkOutOfBounds {
        offset: u64,
        len: u32,
        total_size: u64,
    },
    Io {
        context: &'static str,
        source: io::Error,
    },
}

impl fmt::Display for UnpackError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Usage => write!(f, "Usage: sparse_unpack <output_file>"),
            Self::EmptyOutputPath => write!(f, "Output path cannot be empty"),
            Self::InvalidMagic(got) => {
                write!(f, "Invalid sparse magic: expected b\"SPARSE01\", got {got:?}")
            }
            Self::TruncatedHeader(stage) => {
                write!(f, "Truncated stream while reading {stage}")
            }
            Self::TruncatedPayload {
                offset,
                expected,
                remaining,
            } => write!(
                f,
                "Truncated chunk payload at offset {offset:#x}: expected {expected} bytes, {remaining} bytes missing"
            ),
            Self::MissingSentinel => write!(
                f,
                "Stream reached EOF without 0xFFFFFFFFFFFFFFFF end-of-stream sentinel"
            ),
            Self::ZeroVirtualSize => write!(f, "Virtual file size cannot be 0"),
            Self::VirtualSizeTooLarge(sz) => {
                write!(f, "Virtual size {sz} exceeds sanity limit ({MAX_VIRTUAL_SIZE})")
            }
            Self::ZeroLengthChunk { offset } => {
                write!(f, "Invalid zero-length chunk at offset {offset:#x}")
            }
            Self::OffsetOverflow { offset, len } => {
                write!(
                    f,
                    "64-bit arithmetic overflow at offset {offset:#x} + length {len}"
                )
            }
            Self::ChunkOutOfBounds {
                offset,
                len,
                total_size,
            } => write!(
                f,
                "Chunk ({offset:#x}..+{len}) exceeds virtual file size ({total_size})"
            ),
            Self::Io { context, source } => write!(f, "I/O error during {context}: {source}"),
        }
    }
}

impl std::error::Error for UnpackError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// RAII guard that automatically removes the temporary staging file if not committed.
pub struct TempFileGuard {
    path: PathBuf,
    armed: bool,
}

impl TempFileGuard {
    pub fn new(path: PathBuf) -> Self {
        Self { path, armed: true }
    }

    pub fn commit(mut self, target: &Path) -> Result<(), UnpackError> {
        fs::rename(&self.path, target).map_err(|e| UnpackError::Io {
            context: "atomic rename staging file to target",
            source: e,
        })?;
        self.armed = false;
        Ok(())
    }
}

impl Drop for TempFileGuard {
    fn drop(&mut self) {
        if self.armed {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn read_exact_or_err<R: Read>(
    reader: &mut R,
    buf: &mut [u8],
    stage: &'static str,
) -> Result<(), UnpackError> {
    reader.read_exact(buf).map_err(|e| {
        if e.kind() == ErrorKind::UnexpectedEof {
            UnpackError::TruncatedHeader(stage)
        } else {
            UnpackError::Io {
                context: stage,
                source: e,
            }
        }
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnpackStats {
    pub chunks_count: u64,
    pub bytes_count: u64,
    pub total_size: u64,
}

/// Unpacks a `SPARSE01` stream from `reader` into `out_path` atomically.
pub fn unpack_stream<R: Read>(mut reader: R, out_path: &Path) -> Result<UnpackStats, UnpackError> {
    // 1. Validate 8-byte Magic Header
    let mut magic = [0u8; 8];
    read_exact_or_err(&mut reader, &mut magic, "magic header")?;
    if &magic != SPARSE_MAGIC {
        return Err(UnpackError::InvalidMagic(magic));
    }

    // 2. Read & Validate 64-bit Little-Endian Virtual Size
    let mut size_buf = [0u8; 8];
    read_exact_or_err(&mut reader, &mut size_buf, "total_size header")?;
    let total_size = u64::from_le_bytes(size_buf);
    if total_size == 0 {
        return Err(UnpackError::ZeroVirtualSize);
    }
    if total_size > MAX_VIRTUAL_SIZE {
        return Err(UnpackError::VirtualSizeTooLarge(total_size));
    }

    // 3. Staging path setup in same directory for atomic rename
    let parent_dir = match out_path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => Path::new("."),
    };
    let file_name = out_path
        .file_name()
        .ok_or(UnpackError::EmptyOutputPath)?
        .to_string_lossy();

    let tmp_path = parent_dir.join(format!(".{file_name}.sparse_tmp.{}", std::process::id()));

    let out_file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&tmp_path)
        .map_err(|e| UnpackError::Io {
            context: "open staging temp file",
            source: e,
        })?;

    let tmp_guard = TempFileGuard::new(tmp_path);

    // File::set_len invokes ftruncate64 on 32-bit Linux/Bionic
    out_file
        .set_len(total_size)
        .map_err(|e| UnpackError::Io {
            context: "ftruncate virtual size",
            source: e,
        })?;

    let mut buf = vec![0u8; IO_BUF_SIZE];
    let mut chunks_count: u64 = 0;
    let mut bytes_count: u64 = 0;

    // 4. Chunk streaming loop
    loop {
        let mut off_buf = [0u8; 8];
        let mut nread = 0;
        while nread < 8 {
            match reader.read(&mut off_buf[nread..]) {
                Ok(0) => {
                    if nread == 0 {
                        // Clean EOF before sentinel: upstream stream truncated
                        return Err(UnpackError::MissingSentinel);
                    } else {
                        // Mid-header truncation (1..7 bytes read)
                        return Err(UnpackError::TruncatedHeader("chunk_offset"));
                    }
                }
                Ok(n) => nread += n,
                Err(ref e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(e) => {
                    return Err(UnpackError::Io {
                        context: "read chunk_offset",
                        source: e,
                    });
                }
            }
        }

        let chunk_offset = u64::from_le_bytes(off_buf);
        if chunk_offset == EOS_MARKER {
            break;
        }

        let mut len_buf = [0u8; 4];
        read_exact_or_err(&mut reader, &mut len_buf, "chunk_len header")?;
        let chunk_len = u32::from_le_bytes(len_buf);

        if chunk_len == 0 {
            return Err(UnpackError::ZeroLengthChunk {
                offset: chunk_offset,
            });
        }

        let chunk_len_u64 = u64::from(chunk_len);
        let chunk_end = chunk_offset
            .checked_add(chunk_len_u64)
            .ok_or(UnpackError::OffsetOverflow {
                offset: chunk_offset,
                len: chunk_len,
            })?;

        if chunk_end > total_size {
            return Err(UnpackError::ChunkOutOfBounds {
                offset: chunk_offset,
                len: chunk_len,
                total_size,
            });
        }

        let mut rem = chunk_len;
        let mut cur_off = chunk_offset;

        while rem > 0 {
            let to_read = (rem as usize).min(IO_BUF_SIZE);
            let slice = &mut buf[..to_read];

            reader.read_exact(slice).map_err(|e| {
                if e.kind() == ErrorKind::UnexpectedEof {
                    UnpackError::TruncatedPayload {
                        offset: chunk_offset,
                        expected: chunk_len,
                        remaining: rem,
                    }
                } else {
                    UnpackError::Io {
                        context: "read chunk payload",
                        source: e,
                    }
                }
            })?;

            // FileExt::write_all_at invokes pwrite64 and retries EINTR
            out_file
                .write_all_at(slice, cur_off)
                .map_err(|e| UnpackError::Io {
                    context: "pwrite chunk payload",
                    source: e,
                })?;

            cur_off = cur_off.saturating_add(to_read as u64);
            rem = rem.saturating_sub(to_read as u32);
        }

        chunks_count = chunks_count.saturating_add(1);
        bytes_count = bytes_count.saturating_add(chunk_len_u64);
    }

    // 5. Durability barrier: sync file, commit rename, sync parent directory
    out_file
        .sync_all()
        .map_err(|e| UnpackError::Io {
            context: "fsync staging file",
            source: e,
        })?;
    drop(out_file);

    tmp_guard.commit(out_path)?;

    let dir_file = File::open(parent_dir).map_err(|e| UnpackError::Io {
        context: "open parent directory for sync",
        source: e,
    })?;
    dir_file.sync_all().map_err(|e| UnpackError::Io {
        context: "fsync parent directory",
        source: e,
    })?;

    Ok(UnpackStats {
        chunks_count,
        bytes_count,
        total_size,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn build_test_stream(total_size: u64, chunks: &[(u64, &[u8])], add_eos: bool) -> Vec<u8> {
        let mut stream = Vec::new();
        stream.extend_from_slice(SPARSE_MAGIC);
        stream.extend_from_slice(&total_size.to_le_bytes());

        for &(off, data) in chunks {
            stream.extend_from_slice(&off.to_le_bytes());
            stream.extend_from_slice(&(data.len() as u32).to_le_bytes());
            stream.extend_from_slice(data);
        }

        if add_eos {
            stream.extend_from_slice(&EOS_MARKER.to_le_bytes());
        }
        stream
    }

    #[test]
    fn test_valid_unpack_and_sparse_holes() {
        let dir = std::env::temp_dir().join(format!("sparse_test_{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let out_path = dir.join("test_out.bin");

        let chunk1 = b"HELLO_SECTOR_0";
        let chunk2 = b"SUPERBLOCK_EXT4_AT_1024";
        let stream_data = build_test_stream(
            1024 * 1024 * 1024, // 1 GiB virtual
            &[(0, chunk1), (1024, chunk2)],
            true,
        );

        let stats = unpack_stream(Cursor::new(stream_data), &out_path).expect("unpack failed");
        assert_eq!(stats.chunks_count, 2);
        assert_eq!(stats.bytes_count, (chunk1.len() + chunk2.len()) as u64);
        assert_eq!(stats.total_size, 1024 * 1024 * 1024);

        let metadata = fs::metadata(&out_path).expect("stat output file");
        assert_eq!(metadata.len(), 1024 * 1024 * 1024);

        // Verify content at offsets
        let f = File::open(&out_path).expect("open verified file");
        let mut read_buf1 = vec![0u8; chunk1.len()];
        let mut read_buf2 = vec![0u8; chunk2.len()];
        let mut hole_buf = vec![0u8; 100];

        f.read_exact_at(&mut read_buf1, 0).expect("read chunk1");
        assert_eq!(&read_buf1, chunk1);

        f.read_exact_at(&mut read_buf2, 1024).expect("read chunk2");
        assert_eq!(&read_buf2, chunk2);

        // Verify hole is zeroes
        f.read_exact_at(&mut hole_buf, 500).expect("read hole");
        assert_eq!(hole_buf, vec![0u8; 100]);

        let _ = fs::remove_file(&out_path);
        let _ = fs::remove_dir(&dir);
    }

    #[test]
    fn test_bad_magic() {
        let dir = std::env::temp_dir();
        let out_path = dir.join(format!("bad_magic_{}.bin", std::process::id()));
        let stream = b"NOTSPARS12345678";
        let err = unpack_stream(Cursor::new(stream), &out_path).unwrap_err();
        match err {
            UnpackError::InvalidMagic(m) => assert_eq!(&m, b"NOTSPARS"),
            _ => panic!("Expected InvalidMagic, got {err:?}"),
        }
        assert!(!out_path.exists());
    }

    #[test]
    fn test_missing_sentinel_eof() {
        let dir = std::env::temp_dir();
        let out_path = dir.join(format!("no_sentinel_{}.bin", std::process::id()));
        let chunk = b"DATA";
        // Stream truncated without EOS
        let stream_data = build_test_stream(10000, &[(0, chunk)], false);

        let err = unpack_stream(Cursor::new(stream_data), &out_path).unwrap_err();
        match err {
            UnpackError::MissingSentinel => {}
            _ => panic!("Expected MissingSentinel, got {err:?}"),
        }
        assert!(!out_path.exists(), "Temp staging file must be cleaned up");
    }

    #[test]
    fn test_out_of_bounds_chunk() {
        let dir = std::env::temp_dir();
        let out_path = dir.join(format!("oob_{}.bin", std::process::id()));
        let chunk = b"OVERFLOW_DATA";
        // chunk ends at 100 + 13 = 113 > total_size 100
        let stream_data = build_test_stream(100, &[(100, chunk)], true);

        let err = unpack_stream(Cursor::new(stream_data), &out_path).unwrap_err();
        match err {
            UnpackError::ChunkOutOfBounds { total_size, .. } => assert_eq!(total_size, 100),
            _ => panic!("Expected ChunkOutOfBounds, got {err:?}"),
        }
        assert!(!out_path.exists());
    }

    #[test]
    fn test_zero_length_chunk_rejected() {
        let dir = std::env::temp_dir();
        let out_path = dir.join(format!("zero_len_{}.bin", std::process::id()));
        let stream_data = build_test_stream(1000, &[(0, b"")], true);

        let err = unpack_stream(Cursor::new(stream_data), &out_path).unwrap_err();
        match err {
            UnpackError::ZeroLengthChunk { offset: 0 } => {}
            _ => panic!("Expected ZeroLengthChunk, got {err:?}"),
        }
        assert!(!out_path.exists());
    }

    #[test]
    fn test_bare_relative_path() {
        let bare_name = format!("test_bare_{}.bin", std::process::id());
        let out_path = Path::new(&bare_name);

        let stream_data = build_test_stream(4096, &[(0, b"BARE_PATH_TEST")], true);
        let stats = unpack_stream(Cursor::new(stream_data), out_path).expect("bare path unpack failed");
        assert_eq!(stats.chunks_count, 1);
        assert_eq!(stats.total_size, 4096);
        assert!(out_path.exists());

        let _ = fs::remove_file(out_path);
    }

    #[test]
    fn test_mid_header_truncated_chunk_offset() {
        let dir = std::env::temp_dir();
        let out_path = dir.join(format!("mid_hdr_{}.bin", std::process::id()));

        let mut stream_data = Vec::new();
        stream_data.extend_from_slice(SPARSE_MAGIC);
        stream_data.extend_from_slice(&1024u64.to_le_bytes());
        // Only 3 bytes of chunk_offset instead of 8
        stream_data.extend_from_slice(&[0x12, 0x34, 0x56]);

        let err = unpack_stream(Cursor::new(stream_data), &out_path).unwrap_err();
        match err {
            UnpackError::TruncatedHeader("chunk_offset") => {}
            _ => panic!("Expected TruncatedHeader('chunk_offset'), got {err:?}"),
        }
        assert!(!out_path.exists());
    }
}
