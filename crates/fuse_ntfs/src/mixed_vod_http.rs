//! Bounded, authenticated byte-range reader for the private mixed VOD proxy.
//!
//! The proxy is reached only through an explicitly configured loopback socket.
//! Every response is validated before it can enter the cache or reach FUSE.
//! No credentials, URLs or media bytes are logged.
//!
//! When the package ships a `vod_allowlist.json` the reader is additionally
//! pinned to the trusted content revision of every VOD task: only allowlisted
//! task ids with the exact allowlisted size are served, the outbound request
//! carries the canonical `X-Vod-Revision` value the server and proxy enforce,
//! and cache entries are keyed by that revision so a changed object can never
//! be answered from the previous generation.

use std::collections::{HashMap, VecDeque};
use std::io::{self, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

const BLOCK: u64 = 1024 * 1024;
const MAX_ENTRIES: usize = 4;
const MAX_INFLIGHT: usize = 2;
const MAX_READ: usize = 4 * 1024 * 1024;
const MAX_HEADER: usize = 16 * 1024;

/// Upper bound for `vod_allowlist.json`; the manifest itself is bounded by
/// [`crate::favorites::manifest::DEFAULT_MAX_ENTRIES`], so a larger list is
/// necessarily malformed or hostile.
pub const MAX_ALLOWLIST_TASKS: usize = 4096;

struct Entry {
    task: String,
    total: u64,
    start: u64,
    /// Trusted content revision this block was fetched under (`None` when no
    /// allowlist was configured). Part of the cache identity.
    revision: Option<String>,
    data: Vec<u8>,
}

/// One reader has at most four 1 MiB cache entries and two simultaneous
/// network fetches. The cache lock is never held during network I/O.
pub struct MixedVodHttp {
    endpoint: SocketAddr,
    token: String,
    timeout: Duration,
    /// Optional `task_id -> (size, revision)` pin copied from the package
    /// allowlist. `None` keeps the legacy size-only behaviour.
    allowlist: Option<HashMap<String, (u64, String)>>,
    cache: Mutex<VecDeque<Entry>>,
    inflight: Mutex<usize>,
    available: Condvar,
}

struct Permit<'a>(&'a MixedVodHttp);
impl Drop for Permit<'_> {
    fn drop(&mut self) {
        let mut count = self.0.inflight.lock().unwrap_or_else(|e| e.into_inner());
        *count -= 1;
        self.0.available.notify_one();
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn invalid_input(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

/// Canonical task identity: exactly eight lowercase hex digits.
fn valid_task_id(task: &str) -> bool {
    task.len() == 8
        && task
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Canonical trusted revision: `md5:`/`fs:`/`rev:` followed by 32 lowercase
/// hex digits. Mirrors `vod_revision.REVISION_RE` on the server side.
fn valid_revision(value: &str) -> bool {
    let digest = value
        .strip_prefix("md5:")
        .or_else(|| value.strip_prefix("fs:"))
        .or_else(|| value.strip_prefix("rev:"));
    match digest {
        Some(digest) => {
            digest.len() == 32
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }
        None => false,
    }
}

/// Parses the package's `vod_allowlist.json` with the crate's own JSON reader.
///
/// This is the single startup verifier used before the mount (see
/// `main.rs`); it fails closed on any ambiguity:
///
/// * `tasks` must be a non-empty array of at most [`MAX_ALLOWLIST_TASKS`]
///   entries;
/// * every `task_id` is exactly eight lowercase hex digits;
/// * every `size` is strictly positive and equals the manifest size of the
///   same task (`sizes` is derived from the verified manifest);
/// * every `revision` is canonical (`md5:`/`fs:`/`rev:` + 32 lowercase hex);
/// * duplicate task ids are rejected.
///
/// The returned map is exactly the shape accepted by
/// [`MixedVodHttp::with_allowlist`], so a package that passes here is pinned
/// to the trusted content revision of every VOD task.
pub fn parse_allowlist(
    json: &[u8],
    sizes: &HashMap<String, u64>,
) -> io::Result<HashMap<String, (u64, String)>> {
    use crate::favorites::layout::{parse, Json};

    let json = parse(json).map_err(|_| invalid_input("JSON inválido"))?;
    let tasks = json
        .get("tasks")
        .and_then(Json::as_array)
        .ok_or_else(|| invalid_input("campo 'tasks' ausente"))?;
    if tasks.is_empty() || tasks.len() > MAX_ALLOWLIST_TASKS {
        return Err(invalid_input("lista 'tasks' vazia ou excessiva"));
    }
    let mut allowlist = HashMap::with_capacity(tasks.len());
    for task in tasks {
        let task_id = task
            .get("task_id")
            .and_then(Json::as_str)
            .ok_or_else(|| invalid_input("task_id inválido"))?;
        let size = task
            .get("size")
            .and_then(Json::as_u64)
            .ok_or_else(|| invalid_input("size inválido"))?;
        let revision = task
            .get("revision")
            .and_then(Json::as_str)
            .ok_or_else(|| invalid_input("revision inválida"))?;
        if !valid_task_id(task_id) {
            return Err(invalid_input("task_id inválido"));
        }
        if size == 0 {
            return Err(invalid_input("size inválido"));
        }
        if !valid_revision(revision) {
            return Err(invalid_input("revision inválida"));
        }
        let expected = sizes
            .get(task_id)
            .ok_or_else(|| invalid_input("task fora do manifesto"))?;
        if *expected != size {
            return Err(invalid_input("tamanho diverge do manifesto"));
        }
        if allowlist
            .insert(task_id.to_string(), (size, revision.to_string()))
            .is_some()
        {
            return Err(invalid_input("identificador duplicado"));
        }
    }
    Ok(allowlist)
}

fn remaining(deadline: Instant) -> io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "VOD read deadline expired"))
}

impl MixedVodHttp {
    pub fn new(endpoint: SocketAddr, token: String, timeout: Duration) -> io::Result<Self> {
        if !endpoint.ip().is_loopback() || endpoint.port() == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "VOD proxy must be loopback",
            ));
        }
        if !(32..=512).contains(&token.len()) || !token.bytes().all(|b| (0x21..=0x7e).contains(&b))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid private token",
            ));
        }
        if timeout.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid VOD timeout",
            ));
        }
        Ok(Self {
            endpoint,
            token,
            timeout,
            allowlist: None,
            cache: Mutex::new(VecDeque::new()),
            inflight: Mutex::new(0),
            available: Condvar::new(),
        })
    }

    /// Pins the reader to the package's trusted content revisions.
    ///
    /// Every entry must have a canonical 8-digit lowercase hex task id, a
    /// strictly positive size and a canonical revision (`md5:`/`fs:`/`rev:` +
    /// 32 lowercase hex). Duplicated task ids fail closed. Any invalid entry
    /// returns [`io::ErrorKind::InvalidInput`] and leaves the reader unchanged.
    pub fn with_allowlist<I>(mut self, allowlist: I) -> io::Result<Self>
    where
        I: IntoIterator<Item = (String, (u64, String))>,
    {
        let mut checked: HashMap<String, (u64, String)> = HashMap::new();
        for (task, (size, revision)) in allowlist {
            if !valid_task_id(&task) || size == 0 || !valid_revision(&revision) {
                return Err(invalid_input("invalid VOD allowlist entry"));
            }
            if checked.insert(task, (size, revision)).is_some() {
                return Err(invalid_input("duplicate VOD allowlist entry"));
            }
        }
        self.allowlist = Some(checked);
        Ok(self)
    }

    pub fn read(
        &self,
        task_id: &str,
        offset: u64,
        dst: &mut [u8],
        logical_size: u64,
    ) -> io::Result<usize> {
        self.read_with_timeout(task_id, offset, dst, logical_size, self.timeout)
    }

    /// Returns at most the bytes remaining before EOF. On any failure `dst`
    /// remains unchanged, including if a later block of a multi-block read fails.
    pub fn read_with_timeout(
        &self,
        task_id: &str,
        offset: u64,
        dst: &mut [u8],
        logical_size: u64,
        timeout: Duration,
    ) -> io::Result<usize> {
        if !valid_task_id(task_id) {
            return Err(invalid_input("invalid VOD task identity"));
        }
        if dst.len() > MAX_READ || timeout.is_zero() {
            return Err(invalid_input("invalid VOD read length or timeout"));
        }
        // Fail closed before any syscall when the package pins revisions: a
        // task outside the allowlist or a mismatched size must never be
        // forwarded to the proxy nor cached.
        let revision: Option<&str> = match &self.allowlist {
            Some(allowlist) => match allowlist.get(task_id) {
                Some((size, revision)) if *size == logical_size => Some(revision.as_str()),
                Some(_) => return Err(invalid_input("VOD logical size does not match allowlist")),
                None => return Err(invalid_input("VOD task is not allowlisted")),
            },
            None => None,
        };
        if dst.is_empty() || offset >= logical_size {
            return Ok(0);
        }
        let len = (logical_size - offset).min(dst.len() as u64) as usize;
        let deadline = Instant::now()
            .checked_add(timeout.min(self.timeout))
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid VOD deadline"))?;
        let mut result = vec![0u8; len];
        let mut done = 0;
        while done < len {
            remaining(deadline)?;
            let pos = offset + done as u64;
            let start = pos / BLOCK * BLOCK;
            let block_len = (logical_size - start).min(BLOCK) as usize;
            let within = (pos - start) as usize;
            let take = (len - done).min(block_len - within);
            let hit = {
                let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
                let idx = cache.iter().position(|e| {
                    e.task == task_id
                        && e.total == logical_size
                        && e.start == start
                        && e.revision.as_deref() == revision
                });
                if let Some(idx) = idx {
                    let entry = cache.remove(idx).expect("cache index exists");
                    result[done..done + take].copy_from_slice(&entry.data[within..within + take]);
                    cache.push_back(entry);
                    true
                } else {
                    false
                }
            };
            if !hit {
                let data = self.fetch(task_id, start, block_len, logical_size, revision, deadline)?;
                result[done..done + take].copy_from_slice(&data[within..within + take]);
                let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
                if let Some(idx) = cache.iter().position(|e| {
                    e.task == task_id
                        && e.total == logical_size
                        && e.start == start
                        && e.revision.as_deref() == revision
                }) {
                    cache.remove(idx);
                }
                while cache.len() >= MAX_ENTRIES {
                    cache.pop_front();
                }
                cache.push_back(Entry {
                    task: task_id.to_owned(),
                    total: logical_size,
                    start,
                    revision: revision.map(str::to_owned),
                    data,
                });
            }
            done += take;
        }
        remaining(deadline)?;
        dst[..len].copy_from_slice(&result);
        Ok(len)
    }

    fn permit(&self, deadline: Instant) -> io::Result<Permit<'_>> {
        let mut count = self.inflight.lock().unwrap_or_else(|e| e.into_inner());
        while *count >= MAX_INFLIGHT {
            let (next, result) = self
                .available
                .wait_timeout(count, remaining(deadline)?)
                .unwrap_or_else(|e| e.into_inner());
            count = next;
            if result.timed_out() {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "VOD proxy is busy"));
            }
        }
        *count += 1;
        Ok(Permit(self))
    }

    fn fetch(
        &self,
        task: &str,
        start: u64,
        len: usize,
        total: u64,
        revision: Option<&str>,
        deadline: Instant,
    ) -> io::Result<Vec<u8>> {
        let _permit = self.permit(deadline)?;
        let end = start
            .checked_add(len as u64 - 1)
            .ok_or_else(|| invalid("invalid byte range"))?;
        let mut stream = TcpStream::connect_timeout(&self.endpoint, remaining(deadline)?)?;
        stream.set_write_timeout(Some(remaining(deadline)?))?;
        // The revision value is never logged; exactly one header line is added
        // when the package pinned the task to a trusted content revision.
        let revision_header = match revision {
            Some(value) => format!("X-Vod-Revision: {value}\r\n"),
            None => String::new(),
        };
        let request = format!("GET /vod/{task} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {}\r\nRange: bytes={start}-{end}\r\nAccept-Encoding: identity\r\nConnection: close\r\n{revision_header}\r\n", self.endpoint, self.token);
        let mut pending = request.as_bytes();
        while !pending.is_empty() {
            stream.set_write_timeout(Some(remaining(deadline)?))?;
            match stream.write(pending) {
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "VOD request write failed",
                    ))
                }
                Ok(n) => pending = &pending[n..],
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        let mut reader = BufReader::new(stream);
        let mut header = Vec::new();
        loop {
            if header.len() >= MAX_HEADER {
                return Err(invalid("VOD response headers too large"));
            }
            reader
                .get_mut()
                .set_read_timeout(Some(remaining(deadline)?))?;
            let mut b = [0u8; 1];
            reader.read_exact(&mut b)?;
            header.push(b[0]);
            if header.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        let text =
            std::str::from_utf8(&header).map_err(|_| invalid("invalid VOD response headers"))?;
        let mut lines = text.split("\r\n");
        let status = lines.next().ok_or_else(|| invalid("missing HTTP status"))?;
        if !(status.starts_with("HTTP/1.1 206 ")
            || status.starts_with("HTTP/1.0 206 ")
            || status == "HTTP/1.1 206"
            || status == "HTTP/1.0 206")
        {
            return Err(invalid("VOD proxy did not return partial content"));
        }
        let mut content_length = None;
        let mut content_range = None;
        for line in lines.filter(|s| !s.is_empty()) {
            let (name, value) = line
                .split_once(':')
                .ok_or_else(|| invalid("invalid HTTP header"))?;
            let value = value.trim();
            if name.eq_ignore_ascii_case("content-length") {
                if content_length
                    .replace(
                        value
                            .parse::<usize>()
                            .map_err(|_| invalid("invalid content length"))?,
                    )
                    .is_some()
                {
                    return Err(invalid("duplicate content length"));
                }
            } else if name.eq_ignore_ascii_case("content-range") {
                if content_range.replace(value).is_some() {
                    return Err(invalid("duplicate content range"));
                }
            } else if name.eq_ignore_ascii_case("transfer-encoding")
                || (name.eq_ignore_ascii_case("content-encoding")
                    && !value.eq_ignore_ascii_case("identity"))
            {
                return Err(invalid("unsupported VOD response encoding"));
            }
        }
        if content_length != Some(len)
            || content_range != Some(format!("bytes {start}-{end}/{total}").as_str())
        {
            return Err(invalid("VOD range response does not match request"));
        }
        let mut data = vec![0u8; len];
        let mut filled = 0;
        while filled < len {
            reader
                .get_mut()
                .set_read_timeout(Some(remaining(deadline)?))?;
            let n = reader.read(&mut data[filled..])?;
            if n == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "truncated VOD response",
                ));
            }
            filled += n;
        }
        Ok(data)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::thread;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn server<F>(count: usize, handler: F) -> (SocketAddr, thread::JoinHandle<()>)
    where
        F: Fn(usize, String, &mut TcpStream) + Send + 'static,
    {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            for i in 0..count {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut bytes = Vec::new();
                while !bytes.ends_with(b"\r\n\r\n") {
                    let mut b = [0];
                    stream.read_exact(&mut b).unwrap();
                    bytes.push(b[0]);
                }
                handler(i, String::from_utf8(bytes).unwrap(), &mut stream);
            }
        });
        (addr, handle)
    }

    fn range(request: &str) -> (u64, u64) {
        let value = request
            .lines()
            .find_map(|l| l.strip_prefix("Range: bytes="))
            .unwrap();
        let (start, end) = value.split_once('-').unwrap();
        (start.parse().unwrap(), end.parse().unwrap())
    }

    fn respond(stream: &mut TcpStream, start: u64, end: u64, total: u64, byte: u8) {
        let len = (end - start + 1) as usize;
        write!(stream, "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{end}/{total}\r\nContent-Length: {len}\r\n\r\n").unwrap();
        stream.write_all(&vec![byte; len]).unwrap();
    }

    #[test]
    fn reads_cache_hits_and_eof_without_extra_request() {
        let requests = Arc::new(AtomicUsize::new(0));
        let counted = requests.clone();
        let (addr, handle) = server(1, move |_, req, stream| {
            counted.fetch_add(1, Ordering::SeqCst);
            assert!(req.starts_with("GET /vod/1234abcd HTTP/1.1\r\n"));
            assert!(req.contains(&format!("Authorization: Bearer {TOKEN}\r\n")));
            let (start, end) = range(&req);
            assert_eq!((start, end), (0, 999));
            respond(stream, start, end, 1000, 0x5a);
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
        let mut first = [0u8; 10];
        assert_eq!(client.read("1234abcd", 10, &mut first, 1000).unwrap(), 10);
        assert_eq!(first, [0x5a; 10]);
        let mut tail = [0x44; 10];
        assert_eq!(client.read("1234abcd", 997, &mut tail, 1000).unwrap(), 3);
        assert_eq!(&tail[..3], &[0x5a; 3]);
        assert_eq!(&tail[3..], &[0x44; 7]);
        assert_eq!(client.read("1234abcd", 1000, &mut tail, 1000).unwrap(), 0);
        handle.join().unwrap();
        assert_eq!(requests.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn rejects_mismatched_second_block_without_modifying_destination() {
        let total = BLOCK + 10;
        let (addr, handle) = server(2, move |i, req, stream| {
            let (start, end) = range(&req);
            if i == 0 {
                respond(stream, start, end, total, 1);
            } else {
                respond(stream, start, end, total + 1, 2);
            }
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
        let mut dst = [0x42; 16];
        assert!(client.read("1234abcd", BLOCK - 8, &mut dst, total).is_err());
        assert_eq!(dst, [0x42; 16]);
        handle.join().unwrap();
    }

    #[test]
    fn rejects_short_body_and_non_range_status() {
        for bad_response in [
            "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-9/10\r\nContent-Length: 10\r\n\r\nabc",
            "HTTP/1.1 200 OK\r\nContent-Range: bytes 0-9/10\r\nContent-Length: 10\r\n\r\n0123456789",
            "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-9/10\r\nContent-Length: 10\r\nContent-Length: 10\r\n\r\n0123456789",
        ] {
            let (addr, handle) = server(1, move |_, _, stream| { stream.write_all(bad_response.as_bytes()).unwrap(); });
            let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
            let mut dst = [0x42; 10];
            assert!(client.read("1234abcd", 0, &mut dst, 10).is_err());
            assert_eq!(dst, [0x42; 10]);
            handle.join().unwrap();
        }
    }

    #[test]
    fn rejects_non_loopback_bad_token_and_invalid_task() {
        let remote: SocketAddr = "192.0.2.1:80".parse().unwrap();
        assert!(MixedVodHttp::new(remote, TOKEN.into(), Duration::from_secs(1)).is_err());
        let local: SocketAddr = "127.0.0.1:1234".parse().unwrap();
        assert!(MixedVodHttp::new(local, format!("{TOKEN}\r\n"), Duration::from_secs(1)).is_err());
        let client = MixedVodHttp::new(local, TOKEN.into(), Duration::from_secs(1)).unwrap();
        assert!(client.read("../bad00", 0, &mut [0; 1], 1).is_err());
    }

    #[test]
    fn cache_is_scoped_to_task_identity() {
        let (addr, handle) = server(2, |i, req, stream| {
            assert!(req.starts_with(if i == 0 {
                "GET /vod/1234abcd "
            } else {
                "GET /vod/1234abce "
            }));
            let (start, end) = range(&req);
            respond(stream, start, end, 32, (i + 1) as u8);
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
        let mut dst = [0; 2];
        client.read("1234abcd", 0, &mut dst, 32).unwrap();
        assert_eq!(dst, [1, 1]);
        client.read("1234abce", 0, &mut dst, 32).unwrap();
        assert_eq!(dst, [2, 2]);
        handle.join().unwrap();
    }

    #[test]
    fn stalled_proxy_times_out_without_modifying_destination() {
        let (addr, handle) = server(1, |_, _, _| thread::sleep(Duration::from_millis(150)));
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_millis(30)).unwrap();
        let mut dst = [0x42; 8];
        assert!(client.read("1234abcd", 0, &mut dst, 8).is_err());
        assert_eq!(dst, [0x42; 8]);
        handle.join().unwrap();
    }

    #[test]
    fn parallel_misses_do_not_hold_the_cache_lock_during_io() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let rendezvous = Arc::new(std::sync::Barrier::new(2));
        let server = thread::spawn(move || {
            let mut workers = Vec::new();
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                let ready = rendezvous.clone();
                workers.push(thread::spawn(move || {
                    let mut req = Vec::new();
                    while !req.ends_with(b"\r\n\r\n") {
                        let mut b = [0];
                        stream.read_exact(&mut b).unwrap();
                        req.push(b[0]);
                    }
                    ready.wait();
                    respond(&mut stream, 0, 15, 16, 7);
                }));
            }
            for worker in workers {
                worker.join().unwrap();
            }
        });
        let client =
            Arc::new(MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap());
        let mut workers = Vec::new();
        for task in ["1234abcd", "1234abce"] {
            let client = client.clone();
            workers.push(thread::spawn(move || {
                let mut dst = [0; 16];
                assert_eq!(client.read(task, 0, &mut dst, 16).unwrap(), 16);
                assert_eq!(dst, [7; 16]);
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }
        server.join().unwrap();
    }

    #[test]
    fn cache_isolates_tasks_and_sizes() {
        let (addr, handle) = server(3, |i, req, stream| {
            let (start, end) = range(&req);
            let total = if i == 2 { 11 } else { 10 };
            let task = if i == 1 { "deadbeef" } else { "1234abcd" };
            assert!(req.starts_with(&format!("GET /vod/{task} ")));
            respond(stream, start, end, total, (i + 1) as u8);
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
        for (task, size, expected) in [
            ("1234abcd", 10, 1u8),
            ("deadbeef", 10, 2u8),
            ("1234abcd", 11, 3u8),
            ("1234abcd", 10, 1u8),
        ] {
            let mut dst = [0];
            assert_eq!(client.read(task, 0, &mut dst, size).unwrap(), 1);
            assert_eq!(dst, [expected]);
        }
        handle.join().unwrap();
    }

    fn revision(prefix: &str, hex: char) -> String {
        format!("{prefix}:{}", hex.to_string().repeat(32))
    }

    /// A listener that must never accept a connection; the test asserts the
    /// accept queue stays empty after a rejected read.
    fn assert_no_connection(listener: &TcpListener) {
        listener.set_nonblocking(true).unwrap();
        match listener.accept() {
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
            _ => panic!("unexpected network connection for a rejected VOD read"),
        }
    }

    #[test]
    fn allowlisted_request_carries_exactly_one_revision_header() {
        let rev = revision("md5", 'a');
        let expected = rev.clone();
        let (addr, handle) = server(1, move |_, req, stream| {
            assert_eq!(req.matches("X-Vod-Revision:").count(), 1);
            assert!(req.contains(&format!("X-Vod-Revision: {expected}\r\n")));
            respond(stream, 0, 9, 10, 0x11);
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, rev))])
            .unwrap();
        let mut dst = [0u8; 10];
        assert_eq!(client.read("1234abcd", 0, &mut dst, 10).unwrap(), 10);
        assert_eq!(dst, [0x11; 10]);
        handle.join().unwrap();
    }

    #[test]
    fn task_absent_from_allowlist_is_rejected_without_network() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, revision("md5", 'b')))])
            .unwrap();
        let mut dst = [0x42u8; 4];
        let error = client.read("deadbeef", 0, &mut dst, 10).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(dst, [0x42; 4]);
        assert_no_connection(&listener);
    }

    #[test]
    fn allowlisted_size_mismatch_is_rejected_without_network() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, revision("fs", 'c')))])
            .unwrap();
        let mut dst = [0x42u8; 4];
        let error = client.read("1234abcd", 0, &mut dst, 11).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(dst, [0x42; 4]);
        assert_no_connection(&listener);
    }

    #[test]
    fn changed_revision_fetches_new_bytes_with_new_header() {
        let rev_a = revision("md5", 'a');
        let rev_b = revision("rev", 'd');
        let first = rev_a.clone();
        let second = rev_b.clone();
        let (addr, handle) = server(2, move |i, req, stream| {
            let expected = if i == 0 { &first } else { &second };
            assert!(req.contains(&format!("X-Vod-Revision: {expected}\r\n")));
            respond(stream, 0, 9, 10, (i + 1) as u8);
        });
        let mut bytes = [0u8; 10];
        let client_a = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, rev_a))])
            .unwrap();
        assert_eq!(client_a.read("1234abcd", 0, &mut bytes, 10).unwrap(), 10);
        assert_eq!(bytes, [1u8; 10]);
        let client_b = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, rev_b))])
            .unwrap();
        assert_eq!(client_b.read("1234abcd", 0, &mut bytes, 10).unwrap(), 10);
        assert_eq!(bytes, [2u8; 10]);
        handle.join().unwrap();
    }

    #[test]
    fn with_allowlist_rejects_invalid_entries() {
        let endpoint: SocketAddr = "127.0.0.1:1234".parse().unwrap();
        let valid = revision("md5", 'e');
        let cases: Vec<Vec<(String, (u64, String))>> = vec![
            // uppercase task id
            vec![("1234ABCD".to_string(), (10u64, valid.clone()))],
            // short task id
            vec![("1234abc".to_string(), (10u64, valid.clone()))],
            // non-hex task id
            vec![("1234abcz".to_string(), (10u64, valid.clone()))],
            // zero size
            vec![("1234abcd".to_string(), (0u64, valid.clone()))],
            // uppercase digest
            vec![("1234abcd".to_string(), (10u64, revision("md5", 'E')))],
            // unknown prefix
            vec![("1234abcd".to_string(), (10u64, format!("sha:{}", "e".repeat(32))))],
            // digest too short
            vec![("1234abcd".to_string(), (10u64, "md5:abc".to_string()))],
            // duplicate id
            vec![
                ("1234abcd".to_string(), (10u64, valid.clone())),
                ("1234abcd".to_string(), (10u64, valid.clone())),
            ],
        ];
        for entries in cases {
            let client = MixedVodHttp::new(endpoint, TOKEN.into(), Duration::from_secs(1)).unwrap();
            match client.with_allowlist(entries) {
                Ok(_) => panic!("invalid allowlist entry was accepted"),
                Err(error) => assert_eq!(error.kind(), io::ErrorKind::InvalidInput),
            }
        }
        // A fully valid map is accepted.
        let client = MixedVodHttp::new(endpoint, TOKEN.into(), Duration::from_secs(1))
            .unwrap()
            .with_allowlist(vec![("1234abcd".to_string(), (10u64, valid))]);
        assert!(client.is_ok());
    }

    #[test]
    fn without_allowlist_no_revision_header_and_happy_path_works() {
        let (addr, handle) = server(1, move |_, req, stream| {
            assert!(!req.contains("X-Vod-Revision"));
            let (start, end) = range(&req);
            respond(stream, start, end, 10, 0x22);
        });
        let client = MixedVodHttp::new(addr, TOKEN.into(), Duration::from_secs(2)).unwrap();
        let mut dst = [0u8; 10];
        assert_eq!(client.read("1234abcd", 0, &mut dst, 10).unwrap(), 10);
        assert_eq!(dst, [0x22; 10]);
        handle.join().unwrap();
    }

    fn sizes(entries: &[(String, u64, String)]) -> HashMap<String, u64> {
        entries
            .iter()
            .map(|(task, size, _)| (task.clone(), *size))
            .collect()
    }

    fn allowlist_json(entries: &[(String, u64, String)]) -> Vec<u8> {
        let body = entries
            .iter()
            .map(|(task, size, revision)| {
                format!("{{\"task_id\":\"{task}\",\"size\":{size},\"revision\":\"{revision}\"}}")
            })
            .collect::<Vec<_>>()
            .join(",");
        format!("{{\"tasks\":[{body}]}}").into_bytes()
    }

    fn entry(task: &str, size: u64, revision: &str) -> (String, u64, String) {
        (task.to_string(), size, revision.to_string())
    }

    /// Raised for every rejection; `parse_allowlist` must never return
    /// `InvalidData` or a partially verified map.
    fn allowlist_error(json: &[u8], sizes: &HashMap<String, u64>) -> io::Error {
        match parse_allowlist(json, sizes) {
            Ok(_) => panic!("invalid allowlist was accepted"),
            Err(error) => {
                assert_eq!(error.kind(), io::ErrorKind::InvalidInput, "{error}");
                error
            }
        }
    }

    #[test]
    fn parse_allowlist_accepts_a_manifest_bound_allowlist() {
        let md5 = revision("md5", 'a');
        let fs = revision("fs", 'b');
        let entries = vec![
            entry("1234abcd", 100, &md5),
            entry("deadbeef", 200, &fs),
        ];
        let parsed = parse_allowlist(&allowlist_json(&entries), &sizes(&entries)).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed["1234abcd"], (100u64, md5.clone()));
        assert_eq!(parsed["deadbeef"], (200u64, fs.clone()));

        // The startup path feeds exactly this map to `with_allowlist`; the
        // client must accept it without a second, divergent rule set.
        let endpoint: SocketAddr = "127.0.0.1:9".parse().unwrap();
        let client = MixedVodHttp::new(endpoint, TOKEN.into(), Duration::from_secs(1))
            .unwrap()
            .with_allowlist(parsed)
            .unwrap();
        assert_eq!(client.allowlist.as_ref().map(HashMap::len), Some(2));
    }

    #[test]
    fn parse_allowlist_rejects_missing_revision() {
        let json = br#"{"tasks":[{"task_id":"1234abcd","size":10}]}"#;
        let sizes = HashMap::from([("1234abcd".to_string(), 10u64)]);
        let error = allowlist_error(json, &sizes);
        assert!(error.to_string().contains("revision"), "{error}");
    }

    #[test]
    fn parse_allowlist_rejects_size_that_diverges_from_the_manifest() {
        let rev = revision("md5", 'c');
        let entries = vec![entry("1234abcd", 101, &rev)];
        let manifest = HashMap::from([("1234abcd".to_string(), 100u64)]);
        let error = allowlist_error(&allowlist_json(&entries), &manifest);
        assert!(error.to_string().contains("manifesto"), "{error}");

        // A zero size is invalid even when the manifest agrees.
        let zero = vec![entry("1234abcd", 0, &rev)];
        let error = allowlist_error(&allowlist_json(&zero), &sizes(&zero));
        assert!(error.to_string().contains("size"), "{error}");
    }

    #[test]
    fn parse_allowlist_rejects_duplicate_ids() {
        let rev = revision("rev", 'd');
        let entries = vec![entry("1234abcd", 10, &rev), entry("1234abcd", 10, &rev)];
        let error = allowlist_error(&allowlist_json(&entries), &sizes(&entries));
        assert!(error.to_string().contains("duplicado"), "{error}");
    }

    #[test]
    fn parse_allowlist_rejects_non_canonical_revisions_and_task_ids() {
        for bad in [
            format!("sha:{}", "e".repeat(32)),
            revision("md5", 'E'),
            format!("md5:{}", "e".repeat(31)),
            format!("md5:{}", "e".repeat(33)),
            String::new(),
        ] {
            let entries = vec![entry("1234abcd", 10, &bad)];
            let error = allowlist_error(&allowlist_json(&entries), &sizes(&entries));
            assert!(error.to_string().contains("revision"), "{error}");
        }
        let rev = revision("md5", 'e');
        for bad in ["1234ABCD", "1234abc", "1234abcz", "123456789"] {
            let entries = vec![entry(bad, 10, &rev)];
            let sizes = HashMap::from([(bad.to_string(), 10u64)]);
            let error = allowlist_error(&allowlist_json(&entries), &sizes);
            assert!(error.to_string().contains("task_id"), "{error}");
        }
    }

    #[test]
    fn parse_allowlist_rejects_task_absent_from_the_manifest() {
        let rev = revision("fs", 'f');
        let entries = vec![entry("1234abcd", 10, &rev)];
        let error = allowlist_error(&allowlist_json(&entries), &HashMap::new());
        assert!(error.to_string().contains("fora do manifesto"), "{error}");
    }

    #[test]
    fn parse_allowlist_rejects_missing_empty_or_oversized_task_lists() {
        let rev = revision("md5", 'a');
        let error = allowlist_error(b"{}", &HashMap::new());
        assert!(error.to_string().contains("tasks"), "{error}");
        let error = allowlist_error(b"{\"tasks\":[]}", &HashMap::new());
        assert!(error.to_string().contains("vazia"), "{error}");

        let oversized: Vec<(String, u64, String)> = (0..=MAX_ALLOWLIST_TASKS)
            .map(|index| entry(&format!("{index:08x}"), 1, &rev))
            .collect();
        assert_eq!(oversized.len(), MAX_ALLOWLIST_TASKS + 1);
        let error = allowlist_error(&allowlist_json(&oversized), &sizes(&oversized));
        assert!(error.to_string().contains("excessiva"), "{error}");

        // Exactly the bound is accepted.
        let limit: Vec<(String, u64, String)> = (0..MAX_ALLOWLIST_TASKS)
            .map(|index| entry(&format!("{index:08x}"), 1, &rev))
            .collect();
        let parsed = parse_allowlist(&allowlist_json(&limit), &sizes(&limit)).unwrap();
        assert_eq!(parsed.len(), MAX_ALLOWLIST_TASKS);
    }
}
