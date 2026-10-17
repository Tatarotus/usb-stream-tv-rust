//! stream_fetcher v3 (Rust port of stream_fetcher.c v2)
//!
//! GET http://HOST:PORT/PATH (HTTP/1.0, raw MPEG-TS) -> aligns to 188-byte
//! packets -> writes into a FIFO read by the FUSE layer.
//!
//! Usage: stream_fetcher [fifo_path] [host] [port] [path]
//! Env:   SERVER_HOST overrides host; SERVER_IP is the DNS-failure fallback.

mod ts_align;

use socket2::{Domain, Protocol, Socket, TcpKeepalive, Type};
use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream, ToSocketAddrs};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant};

use ts_align::{Sink, SinkError, TsAligner, TS_PKT_SZ};

/// 21 TS packets = 3948 bytes <= PIPE_BUF (4096): atomic pipe writes.
const TS_WR_CHUNK: usize = TS_PKT_SZ * 21;
const IO_TIMEOUT_MS: i32 = 2000;
const HEARTBEAT_PATH: &str = "/data/local/tmp/fetcher_heartbeat.ts";
const LOCK_PATH: &str = "/data/local/tmp/stream_fetcher.lock";
const F_SETPIPE_SZ: libc::c_int = 1031;
const DEFAULT_FALLBACK_IP: Ipv4Addr = Ipv4Addr::new(129, 146, 5, 64);

static RUNNING: AtomicBool = AtomicBool::new(true);

extern "C" fn on_signal(_: libc::c_int) {
    RUNNING.store(false, Ordering::SeqCst);
}

fn running() -> bool {
    RUNNING.load(Ordering::SeqCst)
}

fn install_signals() {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = on_signal as *const () as usize;
        libc::sigemptyset(&mut sa.sa_mask);
        sa.sa_flags = 0; // no SA_RESTART: poll()/write() must return EINTR
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        libc::signal(libc::SIGCHLD, libc::SIG_IGN);
    }
}

fn last_errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// poll() one fd; Ok(revents) or Ok(0) on timeout. EINTR is surfaced as Interrupted.
fn poll_one(fd: RawFd, events: i16, timeout_ms: i32) -> std::io::Result<i16> {
    let mut p = libc::pollfd { fd, events, revents: 0 };
    let r = unsafe { libc::poll(&mut p, 1, timeout_ms) };
    if r < 0 {
        Err(std::io::Error::last_os_error())
    } else if r == 0 {
        Ok(0)
    } else {
        Ok(p.revents)
    }
}

// ---------------------------------------------------------------- resolver

struct Resolver {
    cached: Option<SocketAddrV4>,
    fails: u32,
}

impl Resolver {
    fn new() -> Self {
        Self { cached: None, fails: 0 }
    }

    fn resolve(&mut self, host: &str, port: u16) -> SocketAddrV4 {
        if let (Some(a), true) = (self.cached, self.fails < 3) {
            return a;
        }
        if let Ok(ip) = host.parse::<Ipv4Addr>() {
            return self.store(SocketAddrV4::new(ip, port));
        }
        if let Ok(mut it) = (host, port).to_socket_addrs() {
            if let Some(a) = it.find_map(|a| match a {
                SocketAddr::V4(v4) => Some(v4),
                _ => None,
            }) {
                return self.store(a);
            }
        }
        if let Some(a) = self.cached {
            eprintln!("[!] Falha no DNS para {}, mantendo IP em cache: {}", host, a.ip());
            self.fails = 0; // Cooldown: do not block in getaddrinfo() on every subsequent retry
            return a;
        }
        let fb_ip = std::env::var("SERVER_IP")
            .ok()
            .and_then(|s| s.parse::<Ipv4Addr>().ok())
            .unwrap_or(DEFAULT_FALLBACK_IP);
        eprintln!("[!] Falha no DNS para {} sem cache prévio. Usando fallback: {}", host, fb_ip);
        let a = SocketAddrV4::new(fb_ip, port);
        self.cached = Some(a);
        self.fails = 0;
        a
    }

    fn store(&mut self, a: SocketAddrV4) -> SocketAddrV4 {
        self.cached = Some(a);
        self.fails = 0;
        a
    }
}

// -------------------------------------------------------------- FIFO sink

struct FifoSink {
    fd: OwnedFd,
    last_hb: Option<Instant>,
    hb_file: Option<File>,
}

impl FifoSink {
    fn new(fd: OwnedFd) -> Self {
        let hb_file = OpenOptions::new()
            .create(true)
            .write(true)
            .mode(0o644)
            .open(HEARTBEAT_PATH)
            .ok();
        Self {
            fd,
            last_hb: None,
            hb_file,
        }
    }

    fn heartbeat(&mut self) {
        let due = self.last_hb.map_or(true, |t| t.elapsed() >= Duration::from_secs(10));
        if !due {
            return;
        }
        // Touch heartbeat file to signal liveness to watchdog.
        // Rate-limited to 10s to minimize eMMC ext4 journal commit wear on Tab 3.
        // Raw SYS_utimensat is used because Bionic libc.so on Android 4.4 (API 19)
        // does not export the `futimens` C wrapper (added in API 21).
        if self.hb_file.is_none() {
            self.hb_file = OpenOptions::new()
                .create(true)
                .write(true)
                .mode(0o644)
                .open(HEARTBEAT_PATH)
                .ok();
        }
        if let Some(ref f) = self.hb_file {
            let ret = unsafe {
                libc::syscall(
                    libc::SYS_utimensat,
                    f.as_raw_fd(),
                    std::ptr::null::<libc::c_char>(),
                    std::ptr::null::<libc::timespec>(),
                    0,
                )
            };
            if ret < 0 {
                eprintln!("[!] heartbeat SYS_utimensat: {}", last_errno());
            }
        }
        self.last_hb = Some(Instant::now());
    }
}

impl Sink for FifoSink {
    fn write_run(&mut self, run: &[u8]) -> Result<(), SinkError> {
        let fd = self.fd.as_raw_fd();
        let mut written = 0usize;
        while written < run.len() && running() {
            let n = (run.len() - written).min(TS_WR_CHUNK);
            match poll_one(fd, libc::POLLOUT, IO_TIMEOUT_MS) {
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return Err(SinkError),
                Ok(0) => {
                    eprintln!("[!] FIFO write backpressure (>2.0s sem dreno pelo FUSE)");
                    return Err(SinkError);
                }
                Ok(re) if re & (libc::POLLERR | libc::POLLHUP) != 0 => {
                    eprintln!("[!] FIFO leitor desconectado (POLLHUP/POLLERR)");
                    return Err(SinkError);
                }
                Ok(_) => {}
            }
            let w = unsafe {
                libc::write(fd, run[written..].as_ptr() as *const libc::c_void, n)
            };
            if w < 0 {
                match last_errno() {
                    libc::EINTR | libc::EAGAIN => continue,
                    libc::EPIPE => return Err(SinkError),
                    e => {
                        eprintln!("[!] write fifo: errno {}", e);
                        return Err(SinkError);
                    }
                }
            }
            written += w as usize;
        }
        self.heartbeat();
        if written < run.len() {
            return Err(SinkError); // shutting down
        }
        Ok(())
    }
}

// ----------------------------------------------------------------- helpers

fn take_singleton_lock() -> File {
    let f = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .mode(0o660)
        .custom_flags(libc::O_CLOEXEC)
        .open(LOCK_PATH);
    match f {
        Ok(f) => {
            if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } < 0 {
                eprintln!("[!] Another stream_fetcher is already running (locked). Exiting.");
                std::process::exit(0);
            }
            let _ = f.set_len(0);
            let s = format!("{}\n", std::process::id());
            let _ = unsafe {
                libc::pwrite(f.as_raw_fd(), s.as_ptr() as *const libc::c_void, s.len(), 0)
            };
            f
        }
        Err(e) => {
            eprintln!("[!] Fatal: open stream_fetcher.lock failed: {}. Exiting to prevent split-brain.", e);
            std::process::exit(1);
        }
    }
}

fn ensure_fifo(path: &str) {
    use std::os::unix::fs::FileTypeExt;
    let ok = std::fs::metadata(path).map(|m| m.file_type().is_fifo()).unwrap_or(false);
    if ok {
        return;
    }
    let _ = std::fs::remove_file(path);
    let c = match CString::new(path) {
        Ok(c) => c,
        Err(_) => {
            eprintln!("[!] Fatal: FIFO path contains interior NUL byte: {}", path);
            std::process::exit(1);
        }
    };
    if unsafe { libc::mkfifo(c.as_ptr(), 0o660) } == 0 {
        println!("[✓] Self-created FIFO pipe: {}", path);
    } else {
        eprintln!("[!] mkfifo failed: {}", std::io::Error::last_os_error());
    }
}

/// Open the FIFO write-only + O_NONBLOCK, waiting for a reader (ENXIO).
fn open_fifo(path: &str) -> Option<OwnedFd> {
    let c = match CString::new(path) {
        Ok(c) => c,
        Err(_) => {
            eprintln!("[!] Fatal: FIFO path contains interior NUL byte: {}", path);
            return None;
        }
    };
    while running() {
        let fd = unsafe { libc::open(c.as_ptr(), libc::O_WRONLY | libc::O_NONBLOCK | libc::O_CLOEXEC) };
        if fd >= 0 {
            let res = unsafe { libc::fcntl(fd, F_SETPIPE_SZ, 1024 * 1024) };
            if res < 0 {
                let _ = unsafe { libc::fcntl(fd, F_SETPIPE_SZ, 512 * 1024) };
            }
            return Some(unsafe { OwnedFd::from_raw_fd(fd) });
        }
        match last_errno() {
            libc::ENXIO => sleep(Duration::from_millis(250)),
            libc::EINTR => return None,
            _ => {
                eprintln!("[!] open fifo: {}", std::io::Error::last_os_error());
                sleep(Duration::from_secs(1));
            }
        }
    }
    None
}

fn connect(addr: SocketAddrV4) -> std::io::Result<TcpStream> {
    let s = Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?;
    let to = Some(Duration::from_secs(2));
    s.set_read_timeout(to)?;
    s.set_write_timeout(to)?;
    let _ = s.set_recv_buffer_size(512 * 1024);
    s.connect_timeout(&SocketAddr::V4(addr).into(), Duration::from_secs(2))?;
    let ka = TcpKeepalive::new()
        .with_time(Duration::from_secs(3))
        .with_interval(Duration::from_secs(2))
        .with_retries(3);
    let _ = s.set_tcp_keepalive(&ka);
    let _ = s.set_nodelay(true);
    Ok(s.into())
}

enum Header {
    Ok { body: Vec<u8> },
    Retry { pause_ms: u64 },
}

fn trim_ascii_bytes(mut b: &[u8]) -> &[u8] {
    while let Some((&first, rest)) = b.split_first() {
        if first == b' ' || first == b'\t' || first == b'\r' || first == b'\n' {
            b = rest;
        } else {
            break;
        }
    }
    while let Some((&last, rest)) = b.split_last() {
        if last == b' ' || last == b'\t' || last == b'\r' || last == b'\n' {
            b = rest;
        } else {
            break;
        }
    }
    b
}

fn read_http_header(sock: &mut TcpStream) -> Header {
    let mut buf = vec![0u8; 8192];
    let mut hlen = 0usize;
    let mut hdr_end: Option<usize> = None;
    let start = Instant::now();
    while hlen < buf.len() && running() {
        if start.elapsed() >= Duration::from_secs(4) {
            eprintln!("[!] Total HTTP header read timeout exceeded (4.0s). Reconnecting...");
            return Header::Retry { pause_ms: 250 };
        }
        match poll_one(sock.as_raw_fd(), libc::POLLIN, IO_TIMEOUT_MS) {
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) | Ok(0) => break,
            Ok(_) => {}
        }
        match sock.read(&mut buf[hlen..]) {
            Ok(0) => break,
            Ok(n) => {
                let scan = hlen.saturating_sub(3);
                hlen += n;
                if let Some(p) = buf[scan..hlen].windows(4).position(|w| w == b"\r\n\r\n") {
                    hdr_end = Some(scan + p);
                    break;
                }
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    let end = match hdr_end {
        Some(e) => e,
        None => {
            eprintln!("[!] Failed to read HTTP headers (timeout/EOF). Reconnecting...");
            return Header::Retry { pause_ms: 250 };
        }
    };
    let head = &buf[..end];
    if !(head.starts_with(b"HTTP/1.") && head.len() >= 12 && &head[8..12] == b" 200") {
        eprintln!("[!] Bad HTTP response status (not 200 OK). Reconnecting...");
        return Header::Retry { pause_ms: 250 };
    }
    // Inspect headers specifically for Transfer-Encoding: chunked
    for line in head.split(|&b| b == b'\n') {
        let line = if line.ends_with(b"\r") { &line[..line.len() - 1] } else { line };
        if let Some(colon) = line.iter().position(|&b| b == b':') {
            let name = trim_ascii_bytes(&line[..colon]);
            if name.eq_ignore_ascii_case(b"transfer-encoding") {
                let val = line[colon + 1..].to_ascii_lowercase();
                if val.windows(7).any(|w| w == b"chunked") {
                    eprintln!("[!] Servidor retornou Transfer-Encoding chunked para HTTP/1.0. Reconectando...");
                    return Header::Retry { pause_ms: 250 };
                }
            }
        }
    }
    Header::Ok { body: buf[end + 4..hlen].to_vec() }
}

// -------------------------------------------------------------------- main

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let fifo_path = args.get(1).cloned().unwrap_or_else(|| "/data/local/tmp/live_pipe".into());
    let mut host = args.get(2).cloned().unwrap_or_else(|| "tv.smre.run.place".into());
    let port: u16 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(80);
    let path = args.get(4).cloned().unwrap_or_else(|| "/stream".into());
    if let Ok(h) = std::env::var("SERVER_HOST") {
        if !h.is_empty() {
            host = h;
        }
    }

    let _lock = take_singleton_lock(); // held for process lifetime
    ensure_fifo(&fifo_path);
    install_signals();

    println!(
        "[*] stream_fetcher v3 (rust) starting: target {}:{}{} -> fifo {}",
        host, port, path, fifo_path
    );

    let host_hdr = if port == 80 { host.clone() } else { format!("{}:{}", host, port) };
    let request = format!(
        "GET {} HTTP/1.0\r\nHost: {}\r\nUser-Agent: USBStreamTV/2.0\r\nConnection: close\r\n\r\n",
        path, host_hdr
    );

    let mut resolver = Resolver::new();
    let mut aligner = TsAligner::new();
    let mut raw = vec![0u8; 65536];
    let mut consecutive_fails = 0u32;

    while running() {
        println!("[*] Opening FIFO {} (waiting for reader)...", fifo_path);
        let fd = match open_fifo(&fifo_path) {
            Some(fd) => fd,
            None => break,
        };
        println!("[✓] FIFO connected!");
        let mut sink = FifoSink::new(fd);
        aligner.reset();

        'conn: while running() {
            let addr = resolver.resolve(&host, port);
            println!("[*] Connecting to {}:{}...", addr.ip(), port);
            let mut sock = match connect(addr) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("[!] connect: {}", e);
                    resolver.fails += 1;
                    consecutive_fails += 1;
                    let pause = if consecutive_fails > 2 { 500 } else { 100 };
                    sleep(Duration::from_millis(pause));
                    continue;
                }
            };

            if let Err(e) = sock.write_all(request.as_bytes()) {
                eprintln!("[!] send: {}", e);
                resolver.fails += 1;
                consecutive_fails += 1;
                sleep(Duration::from_millis(100));
                continue;
            }
            println!("[✓] HTTP request sent. Reading response headers...");

            let body = match read_http_header(&mut sock) {
                Header::Ok { body } => {
                    consecutive_fails = 0;
                    resolver.fails = 0;
                    body
                }
                Header::Retry { pause_ms } => {
                    resolver.fails += 1;
                    consecutive_fails += 1;
                    let sleep_ms = pause_ms.max(if consecutive_fails > 3 { 1000 } else { 250 });
                    sleep(Duration::from_millis(sleep_ms));
                    continue;
                }
            };
            aligner.reset();

            if !body.is_empty() && aligner.feed(&body, &mut sink).is_err() {
                break 'conn; // reopen FIFO
            }
            println!("[✓] Stream connected! Forwarding aligned MPEG-TS packets into FIFO...");

            let mut last_log = Instant::now();
            while running() {
                match poll_one(sock.as_raw_fd(), libc::POLLIN, IO_TIMEOUT_MS) {
                    Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                    Err(_) | Ok(0) => {
                        eprintln!("[!] Socket timeout (2.0s sem dados). Reconectando...");
                        break;
                    }
                    Ok(_) => {}
                }
                let n = match sock.read(&mut raw) {
                    Ok(0) => {
                        eprintln!("[!] Server connection closed cleanly. Reconectando...");
                        break;
                    }
                    Ok(n) => n,
                    Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                    Err(e) => {
                        eprintln!("[!] Server connection closed ({})", e);
                        break;
                    }
                };
                if aligner.feed(&raw[..n], &mut sink).is_err() {
                    break 'conn; // FIFO problem: reopen it
                }
                if last_log.elapsed() >= Duration::from_secs(10) {
                    println!(
                        "[*] Streaming active: {:.2} MB valid TS (resync drops: {} bytes)",
                        aligner.total_valid_bytes as f64 / (1024.0 * 1024.0),
                        aligner.total_resync_drops
                    );
                    last_log = Instant::now();
                }
            }
            // socket dropped here; reconnect immediately without a fixed sleep
        }
        // sink (and its FIFO fd) dropped here
    }
    println!("[*] stream_fetcher exiting cleanly.");
}
