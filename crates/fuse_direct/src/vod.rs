//! Cloud VOD HTTP Dual-Cache (Pinned Head + Sliding Media Double-Buffer).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::ring::fill_null;

pub const VOD_HEAD_SZ: usize = 8 * 1024 * 1024; // 8 MB pinned head
pub const VOD_MEDIA_SZ: usize = 16 * 1024 * 1024; // 16 MB sliding buffer
pub const VOD_CHUNK_SZ: usize = 4 * 1024 * 1024; // 4 MB background prefetch chunk
pub const VOD_SYNC_MISS_SZ: usize = 1024 * 1024; // 1 MB sync miss chunk (keeps latency < 4s)

pub struct VodState {
    pub head_cache: Option<Box<[u8]>>,
    pub head_len: usize,

    pub media_buf: Box<[u8]>,
    pub media_start: u64,
    pub media_len: usize,

    pub prefetch_buf: Box<[u8]>,
    pub prefetch_start: u64,
    pub prefetch_len: usize,
    pub prefetch_ready: bool,
    pub prefetch_in_progress: bool,

    pub target_prefetch_offset: u64,
}

impl Default for VodState {
    fn default() -> Self {
        Self::new()
    }
}

impl VodState {
    pub fn new() -> Self {
        Self {
            head_cache: None,
            head_len: 0,
            media_buf: vec![0u8; VOD_MEDIA_SZ].into_boxed_slice(),
            media_start: u64::MAX,
            media_len: 0,
            prefetch_buf: vec![0u8; VOD_MEDIA_SZ].into_boxed_slice(),
            prefetch_start: u64::MAX,
            prefetch_len: 0,
            prefetch_ready: false,
            prefetch_in_progress: false,
            target_prefetch_offset: u64::MAX,
        }
    }

    pub fn swap_buffers(&mut self) {
        std::mem::swap(&mut self.media_buf, &mut self.prefetch_buf);
    }
}

pub struct VodEngine {
    pub host: String,
    pub port: u16,
    pub path: String,
    pub server_addr: Mutex<Option<SocketAddr>>,
    pub state: Mutex<VodState>,
    pub cv_prefetch: Condvar,
    pub cv_done: Condvar,
    pub abort_flag: AtomicBool,
}

impl VodEngine {
    pub fn new(host: String, port: u16, path: String) -> Arc<Self> {
        let addr = format!("{host}:{port}")
            .to_socket_addrs()
            .ok()
            .and_then(|mut iter| iter.next());

        Arc::new(Self {
            host,
            port,
            path,
            server_addr: Mutex::new(addr),
            state: Mutex::new(VodState::new()),
            cv_prefetch: Condvar::new(),
            cv_done: Condvar::new(),
            abort_flag: AtomicBool::new(false),
        })
    }

    /// Resolves server address with fallback to environment `SERVER_IP` or static IP.
    pub fn get_server_addr(&self) -> Option<SocketAddr> {
        let mut guard = self.server_addr.lock().unwrap_or_else(|e| e.into_inner());
        if guard.is_none() {
            let direct = format!("{}:{}", self.host, self.port)
                .to_socket_addrs()
                .ok()
                .and_then(|mut iter| iter.next());
            if direct.is_some() {
                *guard = direct;
            } else if let Ok(env_ip) = std::env::var("SERVER_IP") {
                *guard = format!("{}:{}", env_ip, self.port)
                    .to_socket_addrs()
                    .ok()
                    .and_then(|mut iter| iter.next());
            } else {
                *guard = format!("129.146.5.64:{}", self.port)
                    .to_socket_addrs()
                    .ok()
                    .and_then(|mut iter| iter.next());
            }
        }
        *guard
    }

    /// Fetches an HTTP range synchronously over a persistent TCP socket.
    pub fn fetch_range(
        &self,
        start: u64,
        len: usize,
        dst: &mut [u8],
        timeout: Duration,
    ) -> Result<usize, std::io::Error> {
        let addr = self.get_server_addr().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::AddrNotAvailable, "DNS resolution failed")
        })?;

        let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
        stream.set_nodelay(true)?;
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;

        let end = start + (len as u64).saturating_sub(1);
        let request = format!(
            "GET {} HTTP/1.1\r\nHost: {}:{}\r\nRange: bytes={}-{}\r\nConnection: close\r\n\r\n",
            self.path, self.host, self.port, start, end
        );

        stream.write_all(request.as_bytes())?;

        // Read HTTP response headers using buffered chunks
        let mut header_buf = Vec::with_capacity(4096);
        let mut chunk_buf = [0u8; 1024];
        let mut header_end_idx = None;

        while header_buf.len() < 4096 {
            let n = stream.read(&mut chunk_buf)?;
            if n == 0 {
                break;
            }
            let old_len = header_buf.len();
            header_buf.extend_from_slice(&chunk_buf[..n]);

            // Scan for \r\n\r\n
            let scan_start = old_len.saturating_sub(3);
            if let Some(pos) = header_buf[scan_start..].windows(4).position(|w| w == b"\r\n\r\n") {
                header_end_idx = Some(scan_start + pos + 4);
                break;
            }
        }

        let body_start = header_end_idx.ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "HTTP header truncated or exceeded 4096 bytes",
            )
        })?;

        // Validate the HTTP status line: only a full body (200) or a partial
        // content response (206) is acceptable. Anything else (404, 403, 416,
        // 3xx redirects, garbage) must fail loudly instead of feeding an error
        // page body into the MPEG-TS stream.
        if !(header_buf.starts_with(b"HTTP/1.1 206")
            || header_buf.starts_with(b"HTTP/1.0 206")
            || header_buf.starts_with(b"HTTP/1.1 200")
            || header_buf.starts_with(b"HTTP/1.0 200"))
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "HTTP server returned non-200/206 status",
            ));
        }

        // Any leftover bytes after \r\n\r\n in header_buf belong to the body
        let leftover = &header_buf[body_start..];
        let to_copy = leftover.len().min(dst.len()).min(len);
        if to_copy > 0 {
            dst[..to_copy].copy_from_slice(&leftover[..to_copy]);
        }

        let mut total_read = to_copy;
        let to_read = len.min(dst.len());

        while total_read < to_read {
            match stream.read(&mut dst[total_read..to_read]) {
                Ok(0) => break,
                Ok(n) => total_read += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }

        Ok(total_read)
    }

    /// Serves a byte slice from VOD head cache, active media cache, or prefetch buffer.
    pub fn serve_slice(
        &self,
        dst: &mut [u8],
        foff: u64,
        deadline: Instant,
    ) -> Result<(), std::io::Error> {
        let count = dst.len();
        let mut filled = 0;

        // 1. Check if covered by Pinned Head Cache
        {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if state.head_cache.is_none() {
                // Initialize head cache on first read
                let mut head = vec![0u8; VOD_HEAD_SZ].into_boxed_slice();
                drop(state); // Drop lock during network I/O
                let head_deadline = deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(3500))
                    .max(Duration::from_millis(800));
                if let Ok(n) = self.fetch_range(0, VOD_HEAD_SZ, &mut head, head_deadline) {
                    let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                    state.head_cache = Some(head);
                    state.head_len = n;
                }
            }
        }

        // Read from head cache if within range
        {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(ref head) = state.head_cache {
                if foff < state.head_len as u64 {
                    let start = foff as usize;
                    let avail = state.head_len.saturating_sub(start);
                    let copy_len = count.min(avail);
                    dst[..copy_len].copy_from_slice(&head[start..start + copy_len]);
                    filled = copy_len;
                    if filled == count {
                        return Ok(());
                    }
                }
            }
        }

        let rem_dst = &mut dst[filled..];
        let rem_foff = foff + filled as u64;
        let rem_count = rem_dst.len();

        // 2. Check active Media Sliding Cache
        {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if rem_foff >= state.media_start
                && rem_foff + rem_count as u64 <= state.media_start + state.media_len as u64
            {
                let start = (rem_foff - state.media_start) as usize;
                rem_dst.copy_from_slice(&state.media_buf[start..start + rem_count]);

                // Trigger lookahead prefetch if nearing end of media buffer
                if (rem_foff - state.media_start) as usize + VOD_CHUNK_SZ >= state.media_len
                    && !state.prefetch_in_progress
                {
                    state.target_prefetch_offset = state.media_start + state.media_len as u64;
                    state.prefetch_in_progress = true;
                    self.cv_prefetch.notify_one();
                }
                return Ok(());
            }

            // 3. Check if ready in Prefetch Buffer -> Double-Buffer Swap!
            if state.prefetch_ready
                && rem_foff >= state.prefetch_start
                && rem_foff + rem_count as u64 <= state.prefetch_start + state.prefetch_len as u64
            {
                state.swap_buffers();
                state.media_start = state.prefetch_start;
                state.media_len = state.prefetch_len;
                state.prefetch_ready = false;

                let start = (rem_foff - state.media_start) as usize;
                rem_dst.copy_from_slice(&state.media_buf[start..start + rem_count]);
                return Ok(());
            }
        }

        // 4. Synchronous Cache Miss: fetch 1 MB chunk to satisfy SCSI request within deadline
        let miss_sz = VOD_SYNC_MISS_SZ;
        let mut temp_buf = vec![0u8; miss_sz];
        let timeout = deadline
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(1200));
        let read_n = self.fetch_range(rem_foff, miss_sz, &mut temp_buf, timeout)?;

        let copy_len = rem_count.min(read_n);
        rem_dst[..copy_len].copy_from_slice(&temp_buf[..copy_len]);
        if copy_len < rem_count {
            fill_null(&mut rem_dst[copy_len..]);
        }

        // Install fetched data into active media cache
        {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            state.media_buf[..read_n].copy_from_slice(&temp_buf[..read_n]);
            state.media_start = rem_foff;
            state.media_len = read_n;
        }

        Ok(())
    }
}
