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

const MNT_POINT: &str = "/data/local/tmp/ntfs_mnt";
const DEF_TARGET: &str = "/data/local/tmp/live_pipe";
const DEF_TEMPLATE: &str = "/data/local/tmp/ntfs_template.bin";

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
                        Ok(0) | Err(_) => break,
                        Ok(_) => continue,
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
                        Ok(0) | Err(_) => break,
                        Ok(_) => continue,
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
