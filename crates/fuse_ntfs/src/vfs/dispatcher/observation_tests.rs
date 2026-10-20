//! Raw `/dev/fuse` protocol observation tests (TASK-003).
//!
//! A `SOCK_SEQPACKET` pair stands in for the kernel device so request/reply
//! framing matches `/dev/fuse` (one message per read/write). The session runs
//! on a real `FuseSession`; only the fd is synthetic.

use std::os::unix::io::RawFd;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

use super::*;
use crate::observability::{
    Config, ManualClock, MemorySink, Mode, Observer, Worker, MAX_LINE_BYTES,
};
use crate::vfs::catalog::LIVE_INO_BASE;

const REQ_HDR: usize = 40;

struct ZeroBackend;

impl StreamBackend for ZeroBackend {
    fn serve_live(&self, _file_id: u32, _foff: u64, dst: &mut [u8], _deadline: u64) -> usize {
        dst.fill(0x47);
        dst.len()
    }

    fn serve_vod(
        &self,
        _url: &str,
        _foff: u64,
        dst: &mut [u8],
        _deadline: u64,
    ) -> io::Result<usize> {
        dst.fill(0x47);
        Ok(dst.len())
    }
}

fn pair() -> (RawFd, RawFd) {
    let mut sv = [0i32; 2];
    let rc = unsafe { libc::socketpair(libc::AF_UNIX, libc::SOCK_SEQPACKET, 0, sv.as_mut_ptr()) };
    assert_eq!(rc, 0, "socketpair: {}", io::Error::last_os_error());
    (sv[0], sv[1])
}

fn header(opcode: u32, unique: u64, nodeid: u64, payload: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(REQ_HDR + payload);
    v.extend_from_slice(&((REQ_HDR + payload) as u32).to_ne_bytes());
    v.extend_from_slice(&opcode.to_ne_bytes());
    v.extend_from_slice(&unique.to_ne_bytes());
    v.extend_from_slice(&nodeid.to_ne_bytes());
    for _ in 0..4 {
        v.extend_from_slice(&0u32.to_ne_bytes()); // uid, gid, pid, padding
    }
    v
}

fn open_request(unique: u64, nodeid: u64) -> Vec<u8> {
    let mut v = header(FUSE_OPEN, unique, nodeid, 8);
    v.extend_from_slice(&0u32.to_ne_bytes()); // flags
    v.extend_from_slice(&0u32.to_ne_bytes()); // unused
    v
}

fn read_request(unique: u64, nodeid: u64, offset: u64, size: u32) -> Vec<u8> {
    let payload = std::mem::size_of::<fuse_read_in>();
    let mut v = header(FUSE_READ, unique, nodeid, payload);
    v.extend_from_slice(&1u64.to_ne_bytes()); // fh
    v.extend_from_slice(&offset.to_ne_bytes());
    v.extend_from_slice(&size.to_ne_bytes());
    v.extend_from_slice(&0u32.to_ne_bytes()); // read_flags
    v.extend_from_slice(&0u64.to_ne_bytes()); // lock_owner
    v.extend_from_slice(&0u32.to_ne_bytes()); // flags
    v.extend_from_slice(&0u32.to_ne_bytes()); // padding
    assert_eq!(v.len(), REQ_HDR + payload);
    v
}

fn release_request(unique: u64, nodeid: u64) -> Vec<u8> {
    // The dispatcher ignores the RELEASE payload; a bare header is valid there.
    header(FUSE_RELEASE, unique, nodeid, 0)
}

fn send(fd: RawFd, msg: &[u8]) {
    let n = unsafe { libc::write(fd, msg.as_ptr() as *const libc::c_void, msg.len()) };
    assert_eq!(
        n,
        msg.len() as isize,
        "write: {}",
        io::Error::last_os_error()
    );
}

/// Waits (bounded) for one reply message and returns its length.
fn recv_reply(fd: RawFd, buf: &mut [u8]) -> usize {
    let mut pfd = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    let start = Instant::now();
    loop {
        let ready = unsafe { libc::poll(&mut pfd, 1, 100) };
        if ready > 0 {
            break;
        }
        assert!(
            start.elapsed() < Duration::from_secs(2),
            "no reply within 2s"
        );
    }
    let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
    assert!(n > 0, "read reply: {}", io::Error::last_os_error());
    n as usize
}

fn start_session(
    dev: RawFd,
    obs: Observer,
) -> (
    Arc<NtfsServer>,
    Arc<AtomicBool>,
    std::thread::JoinHandle<io::Result<()>>,
) {
    let server = Arc::new(
        NtfsServer::new(
            NtfsTemplate::synthesized(),
            Catalog::new(1024),
            Arc::new(ZeroBackend),
        )
        .with_observer(obs),
    );
    let running = Arc::new(AtomicBool::new(true));
    let handle = {
        let srv = Arc::clone(&server);
        let run = Arc::clone(&running);
        std::thread::spawn(move || FuseSession::new(dev, srv).run(&run))
    };
    (server, running, handle)
}

fn observer(sink: Arc<MemorySink>) -> (Observer, Worker) {
    let cfg = Config {
        mode: Mode::Basic,
        rate_per_s: 64,
        burst: 64,
    };
    let started = Observer::start(cfg, Arc::new(ManualClock::new(90_000)), Box::new(sink))
        .expect("worker spawns");
    (started.observer, started.worker)
}

#[test]
fn raw_protocol_open_read_release_is_observed() {
    let (dev, host) = pair();
    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let (server, running, handle) = start_session(dev, obs);

    let mut buf = [0u8; 4096];
    send(host, &open_request(1, FILE_INO));
    let n = recv_reply(host, &mut buf);
    assert_eq!(n, OUT_HDR_SZ + std::mem::size_of::<fuse_open_out>());

    send(host, &read_request(2, LIVE_INO_BASE, 0, 376));
    let n = recv_reply(host, &mut buf);
    assert_eq!(n, OUT_HDR_SZ + 376);

    send(host, &release_request(3, FILE_INO));
    let n = recv_reply(host, &mut buf);
    assert_eq!(n, OUT_HDR_SZ);

    running.store(false, std::sync::atomic::Ordering::SeqCst);
    unsafe {
        libc::shutdown(host, libc::SHUT_RDWR);
        libc::close(host);
    }
    handle.join().expect("session thread").expect("clean loop");
    drop(server);
    assert!(worker.shutdown(2_000), "observer drained");

    let lines = sink.lines();
    let has = |needle: &str| lines.iter().any(|l| l.contains(needle));
    assert!(has("component=fuse"), "{lines:?}");
    assert!(has("event=fuse_open_request") && has("reason=backing_image"));
    assert!(has("event=fuse_open_result") && has("fh=1"));
    assert!(has("event=fuse_read_result") && has("len=376"));
    assert!(has("event=fuse_read_result") && has("reason=sampled"));
    assert!(has("event=fuse_release") && has("reason=backing_image"));
    assert!(has("event=fuse_loop_exit"));
    // Every line is schema-shaped and bounded.
    for l in &lines {
        assert!(l.starts_with("USBTV_OBS version=1 "), "{l}");
        assert!(l.len() <= MAX_LINE_BYTES, "{l}");
        assert!(l.contains(" clock=boottime boot_ms="), "{l}");
    }
}

#[test]
fn unknown_node_open_is_denied_and_labelled() {
    let (dev, host) = pair();
    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let (server, running, handle) = start_session(dev, obs);

    let mut buf = [0u8; 512];
    send(host, &open_request(7, 0xDEAD));
    let n = recv_reply(host, &mut buf);
    assert_eq!(n, OUT_HDR_SZ);
    // Negative reply: the errno lives in the first signed field of the header.
    let errno = i32::from_ne_bytes([buf[4], buf[5], buf[6], buf[7]]);
    assert_eq!(errno, -libc::ENOENT);

    running.store(false, std::sync::atomic::Ordering::SeqCst);
    unsafe {
        libc::shutdown(host, libc::SHUT_RDWR);
        libc::close(host);
    }
    handle.join().expect("session").expect("clean loop");
    drop(server);
    assert!(worker.shutdown(2_000));

    let lines = sink.lines();
    assert!(
        lines
            .iter()
            .any(|l| l.contains("event=fuse_open_result") && l.contains("reason=open_invalid_node")),
        "denied open records an outcome: {lines:?}"
    );
    assert!(
        !lines
            .iter()
            .any(|l| l.contains("event=fuse_open_result") && l.contains("fh=")),
        "denied opens never report a granted FH: {lines:?}"
    );
    assert!(
        !lines.iter().any(|l| l.contains("reason=backing_image")),
        "unknown nodes are not classified as the backing image: {lines:?}"
    );
}

/// The read result must be usable to reproduce the request: size, offset,
/// handle and transport correlation are all present, and `real_bytes` matches
/// the payload actually returned.
#[test]
fn read_result_carries_request_geometry_handle_and_returned_bytes() {
    let (dev, host) = pair();
    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let (server, running, handle) = start_session(dev, obs);

    let mut buf = [0u8; 4096];
    send(host, &read_request(77, FILE_INO, 512, 376));
    let n = recv_reply(host, &mut buf);
    assert_eq!(n, OUT_HDR_SZ + 376);

    running.store(false, std::sync::atomic::Ordering::SeqCst);
    unsafe {
        libc::shutdown(host, libc::SHUT_RDWR);
        libc::close(host);
    }
    handle.join().expect("session").expect("clean loop");
    drop(server);
    assert!(worker.shutdown(2_000));

    let lines = sink.lines();
    let read = lines
        .iter()
        .find(|l| l.contains("event=fuse_read_result"))
        .unwrap_or_else(|| panic!("read result emitted: {lines:?}"));
    for needle in [
        "transport_request_unique=77",
        "nodeid=",
        "fh=1",
        "foff=512",
        "len=376",
        "real_bytes=376",
        "reason=sampled",
    ] {
        assert!(read.contains(needle), "missing {needle}: {read}");
    }
}

/// A reply that cannot be written must preserve the transport `errno`, the
/// expected versus actual length and the originating request.
#[test]
fn reply_write_error_preserves_errno_expected_and_correlation() {
    // `/dev/null` opened read-only: `write(2)` deterministically fails EBADF
    // without raising SIGPIPE (which a closed pipe peer would).
    let path = b"/dev/null\0";
    let fd = unsafe { libc::open(path.as_ptr() as *const libc::c_char, libc::O_RDONLY) };
    assert!(fd >= 0, "open /dev/null: {}", io::Error::last_os_error());

    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let server = NtfsServer::new(
        NtfsTemplate::synthesized(),
        Catalog::new(1024),
        Arc::new(ZeroBackend),
    )
    .with_observer(obs);

    let mut out = AlignedFuseBuf::new_boxed();
    let req = read_request(91, FILE_INO, 0, 376);
    FuseSession::new(fd, Arc::new(server)).dispatch(&req, &mut out);
    unsafe { libc::close(fd) };

    assert!(worker.shutdown(2_000), "observer drained");
    let lines = sink.lines();
    let failed = lines
        .iter()
        .find(|l| l.contains("event=fuse_reply_write_result"))
        .unwrap_or_else(|| panic!("write error emitted: {lines:?}"));
    assert!(failed.contains("reason=write_error"), "{failed}");
    assert!(
        failed.contains(&format!("errno={}", libc::EBADF)),
        "{failed}"
    );
    assert!(
        failed.contains(&format!("len={}", OUT_HDR_SZ + 376)),
        "{failed}"
    );
    assert!(failed.contains("real_bytes=0"), "{failed}");
    assert!(failed.contains("transport_request_unique=91"), "{failed}");
}

/// An unexpected device-read failure ends the loop as unclean, not silently.
#[test]
fn unexpected_device_read_error_is_reported_as_loop_unclean() {
    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let server = Arc::new(
        NtfsServer::new(
            NtfsTemplate::synthesized(),
            Catalog::new(1024),
            Arc::new(ZeroBackend),
        )
        .with_observer(obs),
    );

    // fd = -1 makes `read(2)` fail EBADF: neither retryable nor ENODEV.
    let err = FuseSession::new(-1, server)
        .run(&AtomicBool::new(true))
        .expect_err("invalid fd must surface");
    assert_eq!(err.raw_os_error(), Some(libc::EBADF));

    assert!(worker.shutdown(2_000), "observer drained");
    let lines = sink.lines();
    let has = |needle: &str| lines.iter().any(|l| l.contains(needle));
    assert!(has("event=fuse_device_read_error"), "{lines:?}");
    assert!(has(&format!("errno={}", libc::EBADF)), "{lines:?}");
    assert!(
        has("event=fuse_loop_exit") && has("reason=loop_unclean"),
        "{lines:?}"
    );
}

fn stream_pair_with_restricted_buffer() -> (RawFd, RawFd) {
    let mut sv = [0i32; 2];
    let rc = unsafe { libc::socketpair(libc::AF_UNIX, libc::SOCK_STREAM, 0, sv.as_mut_ptr()) };
    assert_eq!(rc, 0, "socketpair: {}", io::Error::last_os_error());
    let (w, r) = (sv[0], sv[1]);

    let sndbuf: libc::c_int = 4096;
    let rc = unsafe {
        libc::setsockopt(
            w,
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            &sndbuf as *const _ as *const libc::c_void,
            std::mem::size_of_val(&sndbuf) as libc::socklen_t,
        )
    };
    assert_eq!(rc, 0);

    let flags = unsafe { libc::fcntl(w, libc::F_GETFL, 0) };
    unsafe { libc::fcntl(w, libc::F_SETFL, flags | libc::O_NONBLOCK) };

    let dummy = [0x55u8; 256];
    loop {
        let n = unsafe { libc::write(w, dummy.as_ptr() as *const libc::c_void, dummy.len()) };
        if n < 0 {
            let err = io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EWOULDBLOCK)
                || err.raw_os_error() == Some(libc::EAGAIN)
            {
                break;
            }
        }
    }
    let dummy1 = [0x55u8; 1];
    loop {
        let n = unsafe { libc::write(w, dummy1.as_ptr() as *const libc::c_void, 1) };
        if n < 0 {
            break;
        }
    }

    let mut drain_buf = [0u8; 1000];
    let drained = unsafe {
        libc::read(
            r,
            drain_buf.as_mut_ptr() as *mut libc::c_void,
            drain_buf.len(),
        )
    };
    assert!(drained > 0, "drained bytes from peer");

    (w, r)
}

/// A positive partial write returns positive bytes < len without setting errno.
/// The event must record errno=0, expected len, actual real_bytes, and the request unique.
#[test]
fn positive_short_write_preserves_zero_errno_and_expected_len() {
    let (dev, host) = stream_pair_with_restricted_buffer();
    let sink = Arc::new(MemorySink::new());
    let (obs, worker) = observer(Arc::clone(&sink));
    let server = NtfsServer::new(
        NtfsTemplate::synthesized(),
        Catalog::new(1024),
        Arc::new(ZeroBackend),
    )
    .with_observer(obs);

    let mut out = AlignedFuseBuf::new_boxed();
    let req = read_request(99, FILE_INO, 0, 65536);
    FuseSession::new(dev, Arc::new(server)).dispatch(&req, &mut out);

    unsafe {
        libc::close(dev);
        libc::close(host);
    }

    assert!(worker.shutdown(2_000), "observer drained");
    let lines = sink.lines();
    let short_write = lines
        .iter()
        .find(|l| l.contains("event=fuse_reply_write_result"))
        .unwrap_or_else(|| panic!("reply write result emitted: {lines:?}"));

    assert!(short_write.contains("reason=short_write"), "{short_write}");
    assert!(
        short_write.contains("errno=0"),
        "positive short write must have errno=0: {short_write}"
    );
    assert!(
        short_write.contains(&format!("len={}", OUT_HDR_SZ + 65536)),
        "{short_write}"
    );
    assert!(
        short_write.contains("transport_request_unique=99"),
        "{short_write}"
    );

    let real_bytes = short_write
        .split("real_bytes=")
        .nth(1)
        .and_then(|r| r.split_whitespace().next())
        .and_then(|v| v.parse::<usize>().ok())
        .expect("real_bytes parsed");
    assert!(real_bytes > 0, "positive bytes written: {real_bytes}");
    assert!(
        real_bytes < OUT_HDR_SZ + 65536,
        "partial write strictly less than expected: {real_bytes}"
    );
}
