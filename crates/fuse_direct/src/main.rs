//! fuse_direct: Raw /dev/fuse virtual FAT32 disk daemon for USB-Stream-TV.

use std::env;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use fuse_direct::fat32::{FatTemplate, DISK_SIZE, TOTSEC};
use fuse_direct::fuse_abi::*;
use fuse_direct::ring::LiveRing;
use fuse_direct::vod::VodEngine;
use fuse_direct::DiskServer;

const MNT_POINT: &str = "/data/local/tmp/vfat_mnt";
const DEF_FIFO: &str = "/data/local/tmp/live_pipe";
const DEF_TMPL: &str = "/data/local/tmp/fat_template.bin";
const FILE_NAME: &str = "tv_stream.img";

/// 8-byte aligned FUSE request/reply staging buffer.
/// The ARMv7-A kernel emits `LDRD`/`STRD` for 64-bit struct fields
/// (timespec, offsets, sizes) inside the FUSE out-handlers; an unaligned
/// staging buffer hard-faults the CPU on 32-bit ARM.
#[repr(C, align(8))]
struct AlignedFuseBuf([u8; 128 * 1024 + 4096]);

impl AlignedFuseBuf {
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

unsafe fn fuse_reply(fd: libc::c_int, buf: *const u8, len: usize) -> bool {
    let w = libc::write(fd, buf as *const libc::c_void, len);
    w >= 0
}

fn feeder_loop(fifo_path: String, ring: Arc<LiveRing>) {
    let mut buf = vec![0u8; 64 * 1024];
    while RUNNING.load(Ordering::SeqCst) {
        // Open FIFO with non-blocking flag or standard read
        let mut fifo = match OpenOptions::new()
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

        // Clear O_NONBLOCK for blocking reads
        unsafe {
            let fd = fifo.as_raw_fd();
            let flags = libc::fcntl(fd, libc::F_GETFL);
            if flags >= 0 {
                libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
            }
        }

        while RUNNING.load(Ordering::SeqCst) {
            match fifo.read(&mut buf) {
                Ok(0) => break, // EOF on pipe, re-open
                Ok(n) => {
                    ring.write_chunk(&buf[..n]);
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    }
}

fn prefetch_worker_loop(vod: Arc<VodEngine>) {
    while RUNNING.load(Ordering::SeqCst) {
        let target_off = {
            let mut state = vod.state.lock().unwrap_or_else(|e| e.into_inner());
            while !state.prefetch_in_progress && RUNNING.load(Ordering::SeqCst) {
                state = vod
                    .cv_prefetch
                    .wait(state)
                    .unwrap_or_else(|e| e.into_inner());
            }
            if !RUNNING.load(Ordering::SeqCst) {
                break;
            }
            state.target_prefetch_offset
        };

        if target_off != u64::MAX {
            let mut buf = vec![0u8; fuse_direct::vod::VOD_CHUNK_SZ];
            if let Ok(n) = vod.fetch_range(
                target_off,
                fuse_direct::vod::VOD_CHUNK_SZ,
                &mut buf,
                Duration::from_secs(3),
            ) {
                let mut state = vod.state.lock().unwrap_or_else(|e| e.into_inner());
                state.prefetch_buf[..n].copy_from_slice(&buf[..n]);
                state.prefetch_start = target_off;
                state.prefetch_len = n;
                state.prefetch_ready = true;
                state.prefetch_in_progress = false;
                vod.cv_done.notify_all();
            } else {
                let mut state = vod.state.lock().unwrap_or_else(|e| e.into_inner());
                state.prefetch_in_progress = false;
            }
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    install_signals();

    let args: Vec<String> = env::args().collect();
    let mnt = args.get(1).map(|s| s.as_str()).unwrap_or(MNT_POINT);
    let target = args.get(2).map(|s| s.as_str()).unwrap_or(DEF_FIFO);
    let template_path = args.get(3).map(|s| s.as_str()).unwrap_or(DEF_TMPL);

    // Load FAT template if exists, else construct clean default geometry
    let template = if Path::new(template_path).exists() {
        FatTemplate::load(template_path).unwrap_or_else(|_| FatTemplate::new())
    } else {
        FatTemplate::new()
    };

    let server = if let Some(url_part) = target.strip_prefix("http://") {
        let (host, port, path) = if let Some(slash) = url_part.find('/') {
            let hostport = &url_part[..slash];
            let p = &url_part[slash..];
            if let Some(colon) = hostport.find(':') {
                let h = &hostport[..colon];
                let pt = hostport[colon + 1..].parse::<u16>().unwrap_or(80);
                (h.to_string(), pt, p.to_string())
            } else {
                (hostport.to_string(), 80, p.to_string())
            }
        } else {
            (url_part.to_string(), 80, "/".to_string())
        };

        eprintln!("[✓] FUSE v3 iniciado em MODO VOD CLOUD: host={host} port={port} path={path}");
        let vod = VodEngine::new(host, port, path);
        let vod_clone = Arc::clone(&vod);
        thread::Builder::new()
            .name("vod_prefetch".into())
            .stack_size(1024 * 1024)
            .spawn(move || prefetch_worker_loop(vod_clone))?;

        DiskServer::new_vod(template, vod)
    } else if Path::new(target).exists() && (target.ends_with(".mp4") || target.ends_with(".ts")) {
        eprintln!("[✓] FUSE v3 iniciado em MODO VOD LOCAL: {target}");
        let file = File::open(target)?;
        DiskServer::new_local(template, file)
    } else {
        eprintln!("[✓] FUSE v3 iniciado em MODO TV AO VIVO (FIFO: {target})");
        let ring = Arc::new(LiveRing::new());
        let ring_clone = Arc::clone(&ring);
        let fifo_path = target.to_string();
        thread::Builder::new()
            .name("feeder".into())
            .stack_size(1024 * 1024)
            .spawn(move || feeder_loop(fifo_path, ring_clone))?;

        DiskServer::new_live(template, ring)
    };

    // Mount /dev/fuse
    let _ = fs::create_dir_all(mnt);
    let c_mnt = CString::new(mnt)?;
    unsafe {
        libc::umount2(c_mnt.as_ptr(), libc::MNT_DETACH);
    }

    let fuse_dev = CString::new("/dev/fuse")?;
    let fuse_fd = unsafe { libc::open(fuse_dev.as_ptr(), libc::O_RDWR) };
    if fuse_fd < 0 {
        eprintln!("Error: failed to open /dev/fuse");
        return Ok(());
    }

    let opts = format!("fd={fuse_fd},rootmode=040755,user_id=0,group_id=0,allow_other");
    let c_opts = CString::new(opts)?;
    let c_type = CString::new("fuse")?;

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
        eprintln!("Error: mount failed on {mnt}");
        unsafe { libc::close(fuse_fd) };
        return Ok(());
    }
    eprintln!("[✓] fuse_direct montado em {mnt}");

    // 128 KiB request/reply buffers, 8-byte aligned for ARMv7 LDRD/STRD
    let mut in_buf = AlignedFuseBuf::new_boxed();
    let mut out_buf = AlignedFuseBuf::new_boxed();

    while RUNNING.load(Ordering::SeqCst) {
        let n = unsafe {
            libc::read(
                fuse_fd,
                in_buf.0.as_mut_ptr() as *mut libc::c_void,
                in_buf.0.len(),
            )
        };

        if n < 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            break;
        }

        if (n as usize) < std::mem::size_of::<fuse_in_header>() {
            continue;
        }

        let inh = unsafe { &*(in_buf.0.as_ptr() as *const fuse_in_header) };
        let payload_offset = std::mem::size_of::<fuse_in_header>();

        if inh.opcode == FUSE_FORGET || inh.opcode == FUSE_BATCH_FORGET {
            continue;
        }

        let out_header_sz = std::mem::size_of::<fuse_out_header>();

        match inh.opcode {
            FUSE_INIT => {
                let ii = unsafe { &*(in_buf.0[payload_offset..].as_ptr() as *const fuse_init_in) };
                let reply_len = out_header_sz + std::mem::size_of::<fuse_init_out>();

                // Zero the reply struct region: max_background / congestion_threshold
                // are never written explicitly and would otherwise carry stale
                // MPEG-TS payload bytes into kernel-visible fields.
                out_buf.0[..reply_len].fill(0);

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = reply_len as u32;
                oh.error = 0;
                oh.unique = inh.unique;

                let io = unsafe {
                    &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_init_out)
                };
                io.major = 7;
                io.minor = ii.minor.min(26);
                io.max_readahead = 128 * 1024;
                io.flags = ii.flags & (FUSE_ASYNC_READ | FUSE_BIG_WRITES);
                io.max_write = 128 * 1024;

                let out_len = if ii.minor < 23 {
                    (out_header_sz + 24) as usize
                } else {
                    reply_len
                };
                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_len) };
            }

            FUSE_GETATTR => {
                let reply_len = out_header_sz + std::mem::size_of::<fuse_attr_out>();

                // Zero the reply struct region: attr timespec/uid/gid/rdev
                // fields must never carry stale FUSE_READ payload bytes.
                out_buf.0[..reply_len].fill(0);

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.unique = inh.unique;

                if inh.nodeid == FUSE_ROOT_ID {
                    oh.len = reply_len as u32;
                    oh.error = 0;
                    let ao = unsafe {
                        &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_attr_out)
                    };
                    ao.attr_valid = 10;
                    ao.attr.ino = FUSE_ROOT_ID;
                    ao.attr.mode = (libc::S_IFDIR | 0o755) as u32;
                    ao.attr.nlink = 2;
                    ao.attr.size = 4096;
                    ao.attr.blocks = DISK_SIZE / 512;
                    ao.attr.blksize = 4096;
                } else if inh.nodeid == FILE_INO {
                    oh.len = reply_len as u32;
                    oh.error = 0;
                    let ao = unsafe {
                        &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_attr_out)
                    };
                    ao.attr_valid = 10;
                    ao.attr.ino = FILE_INO;
                    ao.attr.mode = (libc::S_IFREG | 0o644) as u32;
                    ao.attr.nlink = 1;
                    ao.attr.size = DISK_SIZE;
                    ao.attr.blocks = DISK_SIZE / 512;
                    ao.attr.blksize = 4096;
                } else {
                    oh.len = out_header_sz as u32;
                    oh.error = -libc::ENOENT;
                }
                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), oh.len as usize) };
            }

            FUSE_LOOKUP => {
                // Zero the reply struct region so no stale MPEG-TS payload leaks
                // into entry_out/attr timespec fields.
                out_buf.0[..out_header_sz + std::mem::size_of::<fuse_entry_out>()].fill(0);

                let name_bytes = &in_buf.0[payload_offset..n as usize];
                let end = name_bytes
                    .iter()
                    .position(|&b| b == 0)
                    .unwrap_or(name_bytes.len());
                let is_disk = inh.nodeid == FUSE_ROOT_ID && &name_bytes[..end] == FILE_NAME.as_bytes();

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.unique = inh.unique;

                if is_disk {
                    let reply_len = out_header_sz + std::mem::size_of::<fuse_entry_out>();
                    oh.len = reply_len as u32;
                    oh.error = 0;

                    let eo = unsafe {
                        &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_entry_out)
                    };
                    eo.nodeid = FILE_INO;
                    eo.generation = 1;
                    eo.entry_valid = 10;
                    eo.attr_valid = 10;
                    eo.attr.ino = FILE_INO;
                    eo.attr.mode = (libc::S_IFREG | 0o644) as u32;
                    eo.attr.nlink = 1;
                    eo.attr.size = DISK_SIZE;
                    eo.attr.blocks = DISK_SIZE / 512;
                    eo.attr.blksize = 4096;
                    unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), reply_len) };
                } else {
                    oh.len = out_header_sz as u32;
                    oh.error = -libc::ENOENT;
                    unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_header_sz) };
                }
            }

            FUSE_OPEN | FUSE_OPENDIR => {
                let reply_len = out_header_sz + std::mem::size_of::<fuse_open_out>();

                // Zero the reply struct region (fh/open_flags/padding must be exact).
                out_buf.0[..reply_len].fill(0);

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = reply_len as u32;
                oh.error = 0;
                oh.unique = inh.unique;

                let oo = unsafe {
                    &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_open_out)
                };
                oo.fh = 1;
                // Waiver #FUSE-2013-MSTAR: FOPEN_DIRECT_IO is granted
                // unconditionally for the block image. The 2013 Samsung MStar
                // SCSI bridge freezes the decoded frame whenever the kernel page
                // cache serves stale sectors of the 4 GiB image; direct I/O forces
                // every sector through serve_disk(), so the live MPEG-TS feeder
                // and the anti-probe NULL-fill path remain authoritative.
                // FOPEN_NONSEEKABLE is deliberately OMITTED: the MStar bridge
                // issues pread64/lseek to walk the block image, and declaring the
                // file non-seekable would degrade the kernel to pure stream reads,
                // destroying the offset-based live-edge/re-anchor logic.
                oo.open_flags = if inh.opcode == FUSE_OPEN {
                    FOPEN_DIRECT_IO
                } else {
                    0
                };

                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), reply_len) };
            }

            FUSE_READDIR => {
                let rin = unsafe { &*(in_buf.0[payload_offset..].as_ptr() as *const fuse_read_in) };
                let off = rin.offset;
                let max_size = (rin.size as usize).min(out_buf.0.len() - out_header_sz);
                let mut used = 0;

                let entries = [
                    (FUSE_ROOT_ID, 1u64, ".", libc::DT_DIR),
                    (FUSE_ROOT_ID, 2u64, "..", libc::DT_DIR),
                    (FILE_INO, 3u64, FILE_NAME, libc::DT_REG),
                ];

                // Zero the whole dirent payload area: the name padding bytes
                // past each entry must not leak stale buffer content.
                out_buf.0[..out_header_sz + max_size].fill(0);

                let data_ptr = out_buf.0[out_header_sz..].as_mut_ptr();

                for (ino, next_off, nm, typ) in entries {
                    if next_off <= off {
                        continue;
                    }
                    let nl = nm.len();
                    let esz = (std::mem::size_of::<fuse_dirent>() + nl + 7) & !7;
                    if used + esz > max_size {
                        break;
                    }

                    let de = unsafe { &mut *(data_ptr.add(used) as *mut fuse_dirent) };
                    de.ino = ino;
                    de.off = next_off;
                    de.namelen = nl as u32;
                    de.typ = typ as u32;

                    unsafe {
                        std::ptr::copy_nonoverlapping(
                            nm.as_ptr(),
                            data_ptr.add(used + std::mem::size_of::<fuse_dirent>()),
                            nl,
                        );
                    }
                    used += esz;
                }

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = (out_header_sz + used) as u32;
                oh.error = 0;
                oh.unique = inh.unique;

                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_header_sz + used) };
            }

            FUSE_RELEASE | FUSE_RELEASEDIR => {
                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = out_header_sz as u32;
                oh.error = 0;
                oh.unique = inh.unique;
                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_header_sz) };
            }

            FUSE_STATFS => {
                let reply_len = out_header_sz + std::mem::size_of::<fuse_statfs_out>();

                // Zero the reply struct region: the kstatfs `spare` array is
                // never written explicitly and must not leak stale bytes.
                out_buf.0[..reply_len].fill(0);

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = reply_len as u32;
                oh.error = 0;
                oh.unique = inh.unique;

                let so = unsafe {
                    &mut *(out_buf.0[out_header_sz..].as_mut_ptr() as *mut fuse_statfs_out)
                };
                so.st.bsize = 4096;
                so.st.frsize = 4096;
                so.st.blocks = TOTSEC / 8;
                so.st.bfree = 0;
                so.st.bavail = 0;
                so.st.files = 2;
                so.st.ffree = 0;
                so.st.namelen = 255;

                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), reply_len) };
            }

            FUSE_READ => {
                let rin = unsafe { &*(in_buf.0[payload_offset..].as_ptr() as *const fuse_read_in) };
                let offset = rin.offset;
                let size = (rin.size as usize).min(128 * 1024);

                // serve_disk fully populates rdata (metadata / ring / NULL fill),
                // so no pre-zeroing of out_buf is required for FUSE_READ.
                let rdata = &mut out_buf.0[out_header_sz..out_header_sz + size];
                let deadline = Instant::now() + Duration::from_secs(4);

                server.serve_disk(rdata, offset, deadline);

                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = (out_header_sz + size) as u32;
                oh.error = 0;
                oh.unique = inh.unique;

                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_header_sz + size) };
            }

            _ => {
                let oh = unsafe { &mut *(out_buf.0.as_mut_ptr() as *mut fuse_out_header) };
                oh.len = out_header_sz as u32;
                oh.error = -libc::ENOSYS;
                oh.unique = inh.unique;
                unsafe { fuse_reply(fuse_fd, out_buf.0.as_ptr(), out_header_sz) };
            }
        }
    }

    eprintln!("[!] Encerrando fuse_direct...");
    unsafe {
        libc::umount2(c_mnt.as_ptr(), libc::MNT_DETACH);
        libc::close(fuse_fd);
    }
    Ok(())
}
