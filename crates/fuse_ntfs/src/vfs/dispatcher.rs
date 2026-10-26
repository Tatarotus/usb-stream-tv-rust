//! `/dev/fuse` protocol dispatcher for the virtual NTFS volume.
//!
//! Implements the kernel-userspace FUSE protocol (Linux 3.4+) on a raw
//! `/dev/fuse` file descriptor: INIT handshake (with the minor < 23
//! truncation for the old tablet kernel), LOOKUP / GETATTR / OPEN / READ /
//! READDIR / STATFS / RELEASE, and FORGET suppression.
//!
//! Read routing (`NtfsServer::serve_read`):
//! * `tv_stream.img` (the whole-disk node) maps disk offsets to metadata
//!   sectors (VBR, $MFT, ... — VSN-stamped) or to the stream data area, which
//!   is served by the engine through [`StreamBackend`];
//! * live channel files feed `serve_live(file_id, ...)`;
//! * VOD files feed `serve_vod(url, ...)`.
//!
//! Zero-panic contract: every request is bounds-checked before any pointer
//! cast, reply payload areas are always zero-initialized before fields are
//! written, and all mutex poisoning is recovered.

use std::io;
use std::os::unix::io::RawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::contract::{StreamBackend, TS_PKT_SZ};
use crate::favorites::extents::MapRegion;
use crate::favorites::runtime::FavoritesRuntime;
use crate::observability::{EventKind, Observer, Reason, UNSET};
use crate::vfs::catalog::{Catalog, NodeKind};
use crate::vfs::fuse_abi::*;
use crate::vfs::ntfs_mft::{
    self, DiskRegion, NtfsTemplate, VolumeLayout, BPS, FILE_INO, ROOT_INO,
};

pub use crate::engine::ring::{fill_null, fill_null_at, NULL_PKT};

mod observation;

/// Default read deadline handed to the backend: the MStar SCSI bridge aborts
/// a request after 3.5–4.0 s of silence.
pub const DEFAULT_DEADLINE_MS: u64 = 3500;

/// Observation label for an OPEN/RELEASE target (TASK-003).
fn open_reason(nodeid: u64) -> Reason {
    if nodeid == FILE_INO {
        Reason::BackingImage
    } else if nodeid == ROOT_INO {
        Reason::OpenRoot
    } else {
        Reason::OpenNode
    }
}

/// Size of the fixed reply header.
pub const OUT_HDR_SZ: usize = std::mem::size_of::<fuse_out_header>();
/// Size of the fixed request header.
const IN_HDR_SZ: usize = std::mem::size_of::<fuse_in_header>();

/// The VFS service bound to a backend. Shared by the session loop.
pub struct NtfsServer {
    pub template: NtfsTemplate,
    pub catalog: Catalog,
    pub layout: VolumeLayout,
    pub backend: Arc<dyn StreamBackend>,
    pub deadline_ms: u64,
    pub obs: Observer,
    /// Opt-in favorites route. `None` preserves the Single Live/VOD path.
    pub favorites: Option<Arc<FavoritesRuntime>>,
}

impl NtfsServer {
    pub fn new(
        template: NtfsTemplate,
        catalog: Catalog,
        backend: Arc<dyn StreamBackend>,
    ) -> Self {
        let layout = template.layout.clone();
        Self {
            template,
            catalog,
            layout,
            backend,
            deadline_ms: DEFAULT_DEADLINE_MS,
            obs: Observer::disabled(),
            favorites: None,
        }
    }

    /// Attaches a bounded observation channel (TASK-003). Playback and reply
    /// behavior are unchanged; the default is a disabled observer.
    pub fn with_observer(mut self, obs: Observer) -> Self {
        self.obs = obs;
        self
    }

    /// Enables the opt-in favorites disk map. Callers must have validated the
    /// template/layout/bootstrap package first; unknown files still fail
    /// closed (NULL packets), never fall back to `catalog.active_node()`.
    pub fn with_favorites(mut self, favorites: Arc<FavoritesRuntime>) -> Self {
        self.favorites = Some(favorites);
        self
    }

    /// Serves one FUSE_READ payload for `nodeid`, always fully populating
    /// `dst` (null-padded when the backend cannot deliver). Returns the
    /// number of bytes written.
    pub fn serve_read(&self, nodeid: u64, offset: u64, dst: &mut [u8]) -> usize {
        if dst.is_empty() {
            return 0;
        }
        let node = match self.catalog.node(nodeid) {
            Some(n) => n,
            None => {
                fill_null_at(dst, offset);
                return dst.len();
            }
        };
        match node.kind {
            NodeKind::DiskImage => self.serve_disk(offset, dst),
            NodeKind::Live { file_id } => {
                // Feed the detector first: a >= 1 MiB contiguous read run on
                // a non-active channel switches the active file_id.
                let _ = self.catalog.note_read(nodeid, offset, dst.len() as u64);
                self.serve_live_filled(file_id, offset, dst)
            }
            NodeKind::Vod { ref url } => match self.backend.serve_vod(
                url,
                offset,
                dst,
                self.deadline_ms,
            ) {
                Ok(n) if n > 0 => self.ts_trim(offset, n, dst),
                _ => {
                    fill_null_at(dst, offset);
                    dst.len()
                }
            },
            NodeKind::Root | NodeKind::Directory => {
                dst.fill(0);
                dst.len()
            }
        }
    }

    /// Guarantees the backend reply never truncates an MPEG-TS packet: a
    /// partial trailing packet is null-padded at the correct phase of the
    /// absolute stream offset, an empty reply becomes a full null fill.
    fn ts_trim(&self, foff: u64, n: usize, dst: &mut [u8]) -> usize {
        let valid = if n >= dst.len() {
            dst.len()
        } else {
            let end_off = foff.saturating_add(n as u64);
            let rem = (end_off % TS_PKT_SZ as u64) as usize;
            n.saturating_sub(rem)
        };
        if valid < dst.len() {
            fill_null_at(&mut dst[valid..], foff + valid as u64);
        }
        dst.len()
    }

    fn serve_live_filled(&self, file_id: u32, foff: u64, dst: &mut [u8]) -> usize {
        let n = self
            .backend
            .serve_live(file_id, foff, dst, self.deadline_ms);
        self.ts_trim(foff, n, dst)
    }

    /// Serves a read of the whole-disk node: metadata sectors come from the
    /// NTFS template (VSN-stamped boot sectors), data bytes from the engine.
    ///
    /// The stream file is fragmented (see [`crate::vfs::ntfs_mft::DiskExtent`]):
    /// each iteration classifies the current offset, and stream chunks are
    /// clamped to the extent end so one backend call never bridges a hole
    /// between two extents — mirroring `avail_in_ext` in the C daemon.
    fn serve_disk(&self, disk_off: u64, dst: &mut [u8]) -> usize {
        let mut done = 0usize;
        let mut cur = disk_off;
        // `vbr.total_sectors` is the NTFS BPB count, which EXCLUDES the backup
        // boot sector: that sector lives one LBA past it and must still be
        // served (VSN-stamped) for the anti-cache contract, so the servable
        // volume ends one sector after `backup_vbr_byte()`.
        let vol_end = self.template.vbr.total_sectors.saturating_add(1).saturating_mul(BPS as u64);

        while done < dst.len() {
            if cur >= vol_end {
                dst[done..].fill(0);
                break;
            }
            if let Some(favorites) = &self.favorites {
                match favorites.route(cur) {
                    MapRegion::File {
                        file_id,
                        file_offset,
                        len,
                    } => {
                        let rem = (dst.len() - done) as u64;
                        let chunk = rem.min(len) as usize;
                        if chunk == 0 {
                            dst[done..].fill(0);
                            break;
                        }
                        let n = favorites.serve_file(
                            file_id,
                            file_offset,
                            &mut dst[done..done + chunk],
                        );
                        if n == 0 {
                            dst[done..].fill(0);
                            break;
                        }
                        done += n;
                        cur += n as u64;
                    }
                    MapRegion::Metadata { .. } | MapRegion::Unmapped { .. } => {
                        // Still template-backed: VBR/MFT/index sectors live in
                        // sparse holes that the layout does not enumerate.
                        // This never routes to an active channel.
                        let before = done;
                        self.serve_template_chunk(cur, dst, &mut done);
                        cur += (done - before) as u64;
                    }
                }
            } else {
                match self.template.route(cur) {
                    DiskRegion::Data(foff) => {
                        let run = self.template.data_run_len(cur);
                        let rem = (dst.len() - done) as u64;
                        let chunk = rem.min(run) as usize;
                        if chunk == 0 {
                            dst[done..].fill(0);
                            break;
                        }
                        let n = self.serve_stream(foff, &mut dst[done..done + chunk]);
                        done += n;
                        cur += n as u64;
                    }
                    DiskRegion::Metadata(_) => {
                        let before = done;
                        self.serve_template_chunk(cur, dst, &mut done);
                        cur += (done - before) as u64;
                    }
                }
            }
        }
        dst.len()
    }

    fn serve_template_chunk(&self, cur: u64, dst: &mut [u8], done: &mut usize) {
        let sec = cur / BPS as u64;
        let sec_off = (cur % BPS as u64) as usize;
        let rem = (dst.len() - *done) as u64;
        let chunk = rem.min((BPS - sec_off) as u64) as usize;
        let mut sec_buf = [0u8; BPS];
        self.template.read_sector(sec, &mut sec_buf);
        dst[*done..*done + chunk].copy_from_slice(&sec_buf[sec_off..sec_off + chunk]);
        *done += chunk;
    }

    /// Routes stream-data reads through the active catalog entry.
    fn serve_stream(&self, foff: u64, dst: &mut [u8]) -> usize {
        match self.catalog.active_node() {
            Some(node) => match node.kind {
                NodeKind::Live { file_id } => self.serve_live_filled(file_id, foff, dst),
                NodeKind::Vod { ref url } => {
                    match self.backend.serve_vod(url, foff, dst, self.deadline_ms) {
                        Ok(n) if n > 0 => self.ts_trim(foff, n, dst),
                        _ => {
                            fill_null_at(dst, foff);
                            dst.len()
                        }
                    }
                }
                _ => {
                    fill_null_at(dst, foff);
                    dst.len()
                }
            },
            None => {
                fill_null_at(dst, foff);
                dst.len()
            }
        }
    }

    /// Resolves a FUSE_LOOKUP name (NUL-terminated by the kernel, but
    /// defensively bounded when not).
    pub fn resolve_lookup(&self, parent: u64, name: &[u8]) -> Option<crate::vfs::catalog::Node> {
        let end = name.iter().position(|&b| b == 0).unwrap_or(name.len());
        let name = std::str::from_utf8(&name[..end]).ok()?;
        self.catalog.lookup(parent, name)
    }

    /// Children of `ino` for READDIR, in stable order.
    pub fn readdir_entries(&self, ino: u64) -> Vec<(u64, String, u32)> {
        let mut out: Vec<(u64, String, u32)> = Vec::new();
        out.push((ino, ".".to_string(), libc::DT_DIR as u32));
        let parent = self
            .catalog
            .node(ino)
            .map(|n| n.parent)
            .unwrap_or(ROOT_INO);
        out.push((parent, "..".to_string(), libc::DT_DIR as u32));
        for child in self.catalog.children(ino) {
            out.push((child.ino, child.name.clone(), child.dirent_type()));
        }
        out
    }
}

/// A `/dev/fuse` session: the raw request/reply loop bound to an
/// [`NtfsServer`].
pub struct FuseSession {
    fd: RawFd,
    server: Arc<NtfsServer>,
}

impl FuseSession {
    pub fn new(fd: RawFd, server: Arc<NtfsServer>) -> Self {
        Self { fd, server }
    }

    /// Runs the request loop until `running` clears or the device errors.
    /// `running` is flipped by the signal handler in `main`.
    pub fn run(&self, running: &AtomicBool) -> io::Result<()> {
        // 8-byte aligned staging buffers for ARMv7-A LDRD/STRD safety.
        let mut in_buf = AlignedFuseBuf::new_boxed();
        let mut out_buf = AlignedFuseBuf::new_boxed();

        // A cleared `running` flag is an orderly shutdown; only an unexpected
        // device-read failure is unclean (reported before the error returns).
        let mut exit = Reason::LoopClean;
        while running.load(Ordering::SeqCst) {
            let n = unsafe {
                libc::read(
                    self.fd,
                    in_buf.0.as_mut_ptr() as *mut libc::c_void,
                    in_buf.0.len(),
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                match err.raw_os_error() {
                    Some(libc::EINTR) | Some(libc::EAGAIN) | Some(libc::ENOENT) => {
                        continue;
                    }
                    Some(libc::ENODEV) => {
                        // Filesystem was unmounted cleanly.
                        exit = Reason::LoopClean;
                        break;
                    }
                    _ => {
                        self.report_device_error(err.raw_os_error().unwrap_or(0));
                        self.report_loop_exit(Reason::LoopUnclean);
                        return Err(err);
                    }
                }
            }
            if (n as usize) < IN_HDR_SZ {
                continue;
            }
            let payload_end = (n as usize).min(in_buf.0.len());
            self.dispatch(&in_buf.0[..payload_end], &mut out_buf);
        }
        self.report_loop_exit(exit);
        Ok(())
    }

    /// Parses one request and writes its reply.
    fn dispatch(&self, req: &[u8], out: &mut AlignedFuseBuf) {
        let Some(header_bytes) = req.get(..IN_HDR_SZ) else {
            return;
        };
        // SAFETY: `AlignedFuseBuf` is 8-byte aligned and `req` is a prefix of
        // it of at least IN_HDR_SZ bytes.
        let inh: &fuse_in_header =
            unsafe { &*(header_bytes.as_ptr() as *const fuse_in_header) };
        let payload = &req[IN_HDR_SZ..];

        // FORGET / BATCH_FORGET are no-reply notifications.
        if inh.opcode == FUSE_FORGET || inh.opcode == FUSE_BATCH_FORGET {
            return;
        }

        match inh.opcode {
            FUSE_INIT => self.handle_init(inh, payload, out),
            FUSE_GETATTR => self.handle_getattr(inh, out),
            FUSE_LOOKUP => self.handle_lookup(inh, payload, out),
            FUSE_OPEN | FUSE_OPENDIR => self.handle_open(inh, out),
            FUSE_READ => self.handle_read(inh, payload, out),
            FUSE_WRITE => self.handle_write(inh, payload, out),
            FUSE_READDIR => self.handle_readdir(inh, payload, out),
            FUSE_RELEASE | FUSE_RELEASEDIR => self.handle_release(inh, out),
            FUSE_STATFS => self.handle_statfs(inh, out),
            _ => self.reply_error(inh, out, -libc::ENOSYS),
        }
    }

    // ------------------------------------------------------------ helpers

    fn handle_release(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let why = open_reason(inh.nodeid);
        self.report(EventKind::FuseRelease, why, inh, UNSET, UNSET);
        self.reply_empty(inh, out);
    }

    /// Writes a successful reply of exactly `len` bytes, capturing the
    /// transport errno and the originating request for diagnostics.
    fn reply(&self, inh: &fuse_in_header, out: &AlignedFuseBuf, len: usize) {
        let w = unsafe {
            libc::write(
                self.fd,
                out.0.as_ptr() as *const libc::c_void,
                len,
            )
        };
        if w < 0 || w as usize != len {
            // errno is only meaningful for a failed write; a positive short
            // write carries no new error code.
            let errno = if w < 0 {
                io::Error::last_os_error().raw_os_error().unwrap_or(0)
            } else {
                0
            };
            self.report_write_error(inh, w, len, errno);
        }
    }

    /// Header-only positive reply.
    fn reply_empty(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ, 0);
        self.reply(inh, out, OUT_HDR_SZ);
    }

    /// Header-only negative reply with an errno.
    fn reply_error(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, errno: i32) {
        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ, errno);
        self.reply(inh, out, OUT_HDR_SZ);
    }

    /// Fills the common reply header fields.
    fn write_header(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, len: usize, error: i32) {
        // SAFETY: 8-byte aligned buffer, OUT_HDR_SZ bytes available.
        let oh = unsafe { &mut *(out.0.as_mut_ptr() as *mut fuse_out_header) };
        oh.len = len as u32;
        oh.error = error;
        oh.unique = inh.unique;
    }

    /// Fills a `fuse_attr` for regular files.
    fn fill_file_attr(attr: &mut fuse_attr, ino: u64, size: u64) {
        attr.ino = ino;
        attr.size = size;
        attr.blocks = size / 512;
        attr.mode = (libc::S_IFREG | 0o644) as u32;
        attr.nlink = 1;
        attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
    }

    /// Writes an attr_out reply for `attr`.
    fn reply_attr(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf, attr: &fuse_attr) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_attr_out>();
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);
        // SAFETY: 8-byte aligned, reply_len validated against the buffer.
        let ao = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_attr_out) };
        ao.attr_valid = 10;
        ao.attr = *attr;
        self.reply(inh, out, reply_len);
    }

    // ------------------------------------------------------------ handlers

    fn handle_init(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < 8 {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        let minor = u32::from_ne_bytes(payload[4..8].try_into().unwrap_or([0; 4]));
        let flags = if payload.len() >= 16 {
            u32::from_ne_bytes(payload[12..16].try_into().unwrap_or([0; 4]))
        } else {
            0
        };
        // The Samsung tablet ships kernel 3.4.0, whose FUSE client negotiates
        // minor 7.18: it understands the reply only up through `max_write`,
        // so the body must be truncated at exactly 24 payload bytes whenever
        // the negotiated minor is < 23 (anything longer makes the kernel read
        // past the declared reply length). This mirrors the audited C daemon
        // (`fuse_ntfs.c` INIT handshake) and the `fuse_direct` sibling.
        let out_len = if minor < 23 {
            OUT_HDR_SZ + 24
        } else {
            OUT_HDR_SZ + std::mem::size_of::<fuse_init_out>()
        };

        // Zero the reply struct region: max_background / congestion_threshold
        // are never written explicitly and would otherwise carry stale
        // MPEG-TS payload bytes into kernel-visible fields.
        out.0[..out_len].fill(0);
        self.write_header(inh, out, out_len, 0);

        // SAFETY: aligned + sized as above.
        let io = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_init_out) };
        io.major = 7;
        io.minor = minor.min(26);
        io.max_readahead = MAX_MSG_SZ as u32;
        io.flags = flags & (FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        io.max_write = MAX_MSG_SZ as u32;

        self.reply(inh, out, out_len);
    }

    fn handle_getattr(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let mut attr: fuse_attr = unsafe { std::mem::zeroed() };
        match inh.nodeid {
            ROOT_INO => {
                attr.ino = ROOT_INO;
                attr.mode = (libc::S_IFDIR | 0o755) as u32;
                attr.nlink = 2;
                attr.size = 4096;
                attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                self.reply_attr(inh, out, &attr);
            }
            FILE_INO => {
                Self::fill_file_attr(&mut attr, FILE_INO, ntfs_mft::VOLUME_BYTES);
                self.reply_attr(inh, out, &attr);
            }
            ino => match self.server.catalog.node(ino) {
                Some(node) if node.is_dir() => {
                    attr.ino = node.ino;
                    attr.mode = (libc::S_IFDIR | 0o755) as u32;
                    attr.nlink = 2;
                    attr.size = 4096;
                    attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                    self.reply_attr(inh, out, &attr);
                }
                Some(node) => {
                    Self::fill_file_attr(&mut attr, node.ino, node.size);
                    self.reply_attr(inh, out, &attr);
                }
                None => self.reply_error(inh, out, -libc::ENOENT),
            },
        }
    }

    fn handle_lookup(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_entry_out>();
        // Zero the reply struct region so no stale payload leaks into
        // entry_out/attr timespec fields.
        out.0[..reply_len].fill(0);

        match self.server.resolve_lookup(inh.nodeid, payload) {
            Some(node) => {
                self.write_header(inh, out, reply_len, 0);
                // SAFETY: aligned + sized as above.
                let eo =
                    unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_entry_out) };
                eo.nodeid = node.ino;
                eo.generation = 1;
                eo.entry_valid = 10;
                eo.attr_valid = 10;
                if node.is_dir() {
                    eo.attr.ino = node.ino;
                    eo.attr.mode = (libc::S_IFDIR | 0o755) as u32;
                    eo.attr.nlink = 2;
                    eo.attr.size = 4096;
                } else {
                    Self::fill_file_attr(&mut eo.attr, node.ino, node.size);
                }
                eo.attr.blksize = ntfs_mft::CLUSTER_SIZE as u32;
                self.reply(inh, out, reply_len);
            }
            None => self.reply_error(inh, out, -libc::ENOENT),
        }
    }

    fn handle_open(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let why = open_reason(inh.nodeid);
        self.report(EventKind::FuseOpenRequest, why, inh, UNSET, UNSET);
        if inh.opcode == FUSE_OPEN {
            self.server.backend.on_open();
        }
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_open_out>();

        // Unknown nodes are rejected before granting a handle.
        if inh.nodeid != ROOT_INO && self.server.catalog.node(inh.nodeid).is_none() {
            let why = Reason::OpenInvalidNode;
            self.report(EventKind::FuseOpenResult, why, inh, UNSET, UNSET);
            self.reply_error(inh, out, -libc::ENOENT);
            return;
        }

        // Zero the reply struct region (fh/open_flags/padding must be exact).
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);
        // SAFETY: aligned + sized as above.
        let oo = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_open_out) };
        oo.fh = 1;
        // Waiver #FUSE-2013-MSTAR: FOPEN_DIRECT_IO is granted unconditionally
        // for the block image. The 2013 Samsung MStar SCSI bridge freezes the
        // decoded frame whenever the kernel page cache serves stale sectors
        // of the 128 GiB image; direct I/O forces every sector through
        // serve_read(), so the live feeder and the anti-probe NULL-fill path
        // remain authoritative. FOPEN_NONSEEKABLE is deliberately OMITTED:
        // the MStar bridge issues pread64/lseek to walk the block image, and
        // declaring the file non-seekable would degrade the kernel to pure
        // stream reads, destroying the offset-based live-edge/re-anchor logic.
        oo.open_flags = if inh.opcode == FUSE_OPEN {
            FOPEN_DIRECT_IO
        } else {
            0
        };
        let why = open_reason(inh.nodeid);
        self.report(EventKind::FuseOpenResult, why, inh, oo.fh, UNSET);
        self.reply(inh, out, reply_len);
    }

    fn handle_write(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        let size = if payload.len() >= std::mem::size_of::<fuse_write_in>() {
            let win = unsafe { &*(payload.as_ptr() as *const fuse_write_in) };
            win.size
        } else {
            0
        };
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_write_out>();
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);
        let wout = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_write_out) };
        wout.size = size;
        self.reply(inh, out, reply_len);
    }

    fn handle_read(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < std::mem::size_of::<fuse_read_in>() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        // SAFETY: payload inside the aligned request buffer, sized above.
        let rin = unsafe { &*(payload.as_ptr() as *const fuse_read_in) };
        let size = (rin.size as usize).min(MAX_MSG_SZ);
        if OUT_HDR_SZ + size > out.0.len() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }

        // serve_read fully populates the payload (metadata / ring / NULL
        // fill), so no pre-zeroing of out_buf is required for FUSE_READ.
        let rdata = &mut out.0[OUT_HDR_SZ..OUT_HDR_SZ + size];
        let written = self.server.serve_read(inh.nodeid, rin.offset, rdata);

        // The result records the request's size, offset and handle as well as
        // the bytes actually returned; a short reply stays observable.
        self.report_read_result(inh, rin, written);

        out.0[..OUT_HDR_SZ].fill(0);
        self.write_header(inh, out, OUT_HDR_SZ + written, 0);
        self.reply(inh, out, OUT_HDR_SZ + written);
    }

    fn handle_readdir(&self, inh: &fuse_in_header, payload: &[u8], out: &mut AlignedFuseBuf) {
        if payload.len() < std::mem::size_of::<fuse_read_in>() {
            self.reply_error(inh, out, -libc::EINVAL);
            return;
        }
        // SAFETY: as above.
        let rin = unsafe { &*(payload.as_ptr() as *const fuse_read_in) };
        let off = rin.offset;
        let max_size = (rin.size as usize).min(out.0.len() - OUT_HDR_SZ);

        let entries = self.server.readdir_entries(inh.nodeid);

        // Zero the whole dirent payload area: the name padding bytes past
        // each entry must not leak stale buffer content.
        out.0[..OUT_HDR_SZ + max_size].fill(0);

        let mut used = 0usize;
        let data_ptr = out.0[OUT_HDR_SZ..].as_mut_ptr();

        for (idx, (ino, name, typ)) in entries.iter().enumerate() {
            let next_off = idx as u64 + 1;
            if next_off <= off {
                continue;
            }
            let nl = name.len();
            let esz = (std::mem::size_of::<fuse_dirent>() + nl + 7) & !7;
            if used + esz > max_size {
                break;
            }
            // SAFETY: data_ptr is 8-byte aligned, `used + esz <= max_size`.
            let de = unsafe { &mut *(data_ptr.add(used) as *mut fuse_dirent) };
            de.ino = *ino;
            de.off = next_off;
            de.namelen = nl as u32;
            de.typ = *typ;
            // SAFETY: name bytes fit inside the validated `esz` window.
            unsafe {
                std::ptr::copy_nonoverlapping(
                    name.as_ptr(),
                    data_ptr.add(used + std::mem::size_of::<fuse_dirent>()),
                    nl,
                );
            }
            used += esz;
        }

        let reply_len = OUT_HDR_SZ + used;
        self.write_header(inh, out, reply_len, 0);
        self.reply(inh, out, reply_len);
    }

    fn handle_statfs(&self, inh: &fuse_in_header, out: &mut AlignedFuseBuf) {
        let reply_len = OUT_HDR_SZ + std::mem::size_of::<fuse_statfs_out>();

        // Zero the reply struct region: the kstatfs `spare` array is never
        // written explicitly and must not leak stale bytes.
        out.0[..reply_len].fill(0);
        self.write_header(inh, out, reply_len, 0);

        let vbr = &self.server.template.vbr;
        // SAFETY: aligned + sized as above.
        let so = unsafe { &mut *(out.0[OUT_HDR_SZ..].as_mut_ptr() as *mut fuse_statfs_out) };
        so.st.bsize = vbr.cluster_size as u32;
        so.st.frsize = vbr.cluster_size as u32;
        let total_clusters = vbr.total_sectors / u64::from(vbr.sectors_per_cluster);
        so.st.blocks = total_clusters;
        so.st.bfree = 0;
        so.st.bavail = 0;
        so.st.files = self.server.catalog.len() as u64;
        so.st.ffree = 0;
        so.st.namelen = 255;

        self.reply(inh, out, reply_len);
    }

    // -------------------------------------------------- observation (TASK-003)
    //
    // The reporters live in `dispatcher/observation.rs`; this module keeps only
    // the call sites, so the dispatch hot path stays readable and in budget.
}

/// Binds a session to a mounted `/dev/fuse` fd. Thin wrapper so `main` keeps
/// the process-lifecycle concerns while the protocol stays here.
pub fn run(fd: RawFd, server: Arc<NtfsServer>, running: &AtomicBool) -> io::Result<()> {
    FuseSession::new(fd, server).run(running)
}

#[cfg(test)]
mod observation_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::StreamBackend;
    use crate::vfs::catalog::Catalog;
    use crate::vfs::ntfs_mft::NtfsTemplate;
    use std::io;

    /// Backend that reports how it was called and fills deterministic data.
    struct ProbeBackend {
        live_calls: std::sync::Mutex<Vec<(u32, u64, usize)>>,
    }

    impl ProbeBackend {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                live_calls: std::sync::Mutex::new(Vec::new()),
            })
        }
    }

    impl StreamBackend for ProbeBackend {
        fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], _d: u64) -> usize {
            self.live_calls
                .lock()
                .unwrap()
                .push((file_id, foff, dst.len()));
            for (i, b) in dst.iter_mut().enumerate() {
                *b = (i % 251) as u8;
            }
            dst.len()
        }
        fn serve_vod(&self, _url: &str, _foff: u64, dst: &mut [u8], _d: u64) -> io::Result<usize> {
            for (_i, b) in dst.iter_mut().enumerate() {
                *b = 0xEE;
            }
            Ok(dst.len())
        }
    }

    fn server() -> (Arc<NtfsServer>, Arc<ProbeBackend>) {
        let backend = ProbeBackend::new();
        let server = Arc::new(NtfsServer::new(
            NtfsTemplate::synthesized(),
            Catalog::new(1024),
            backend.clone(),
        ));
        (server, backend)
    }

    #[test]
    fn test_fill_null_packet_shape() {
        let mut buf = [0u8; TS_PKT_SZ * 3];
        fill_null(&mut buf);
        for p in 0..3 {
            let o = p * TS_PKT_SZ;
            assert_eq!(buf[o], 0x47);
            assert_eq!(buf[o + 1], 0x1F);
            assert_eq!(buf[o + 2], 0xFF);
            assert_eq!(buf[o + 3], 0x10);
        }
        // Partial tails stay packet-aligned.
        let mut small = [0u8; 100];
        fill_null(&mut small);
        assert_eq!(small[0], 0x47);
        assert_eq!(small[99], 0xFF);
    }

    #[test]
    fn test_serve_read_disk_metadata() {
        let (srv, _b) = server();
        let mut buf = [0u8; BPS as usize];
        let n = srv.serve_read(FILE_INO, 0, &mut buf);
        assert_eq!(n, BPS as usize);
        assert_eq!(&buf[3..11], b"NTFS    ");
        assert_eq!(buf[510], 0x55);
    }

    #[test]
    fn test_serve_read_disk_data_routes_to_backend() {
        let (srv, b) = server();
        let mut buf = [0u8; TS_PKT_SZ * 8];
        let off = srv.layout.data_start_byte + 4096;
        let n = srv.serve_read(FILE_INO, off, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0], (0, 4096, TS_PKT_SZ * 8));
    }

    #[test]
    fn test_serve_read_partial_metadata_sector() {
        let (srv, _b) = server();
        // Mid-boot-sector read must stay sector aligned.
        let mut buf = [0u8; 16];
        let n = srv.serve_read(FILE_INO, 100, &mut buf);
        assert_eq!(n, 16);
        // Sector 0 offset 100 still inside the NTFS OEM field area (all zero
        // after 0x0B), while a read spanning into the metadata payload works.
        let mut buf2 = [0u8; 16];
        srv.serve_read(FILE_INO, 0, &mut buf2);
        assert_eq!(&buf2[0..3], &[0xEB, 0x52, 0x90]);
    }

    #[test]
    fn test_serve_read_live_channel_file() {
        let (srv, b) = server();
        // "Canais TV/TV AO VIVO 2.tp" maps to file_id 1.
        let ino = crate::vfs::catalog::LIVE_INO_BASE + 1;
        let mut buf = [0u8; TS_PKT_SZ * 4];
        let n = srv.serve_read(ino, 0, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls[0].0, 1);
    }

    #[test]
    fn test_serve_read_vod_file() {
        let (srv, _b) = server();
        let ino = srv.catalog.add_vod_file("http://vps/x.ts", "Filme.ts", 1024);
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(ino, 0, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        assert_eq!(buf[0], 0xEE);
    }

    #[test]
    fn test_serve_read_unknown_node_nulls() {
        let (srv, b) = server();
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(4242, 0, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        assert_eq!(buf[0], 0x47);
        assert!(b.live_calls.lock().unwrap().is_empty());
    }

    #[test]
    fn test_ts_trim_pads_partial_packet() {
        let (srv, _b) = server();
        // A backend reply of 100 bytes (less than one TS packet) is fully
        // null-filled by the hardened ts_trim.
        let mut dst = [0u8; TS_PKT_SZ * 2];
        let n = srv.ts_trim(0, 100, &mut dst);
        assert_eq!(n, TS_PKT_SZ * 2);
        assert_eq!(dst[0], 0x47);
        assert_eq!(dst[1], 0x1F);
    }

    #[test]
    fn test_ts_trim_zero_fills() {
        let (srv, _b) = server();
        let mut dst = [0u8; TS_PKT_SZ];
        assert_eq!(srv.ts_trim(0, 0, &mut dst), TS_PKT_SZ);
        assert_eq!(dst[0], 0x47);
    }

    #[test]
    fn test_readdir_entries() {
        let (srv, _b) = server();
        let entries = srv.readdir_entries(ROOT_INO);
        let names: Vec<&str> = entries.iter().map(|(_, n, _)| n.as_str()).collect();
        assert!(names.contains(&"."));
        assert!(names.contains(&".."));
        assert!(names.contains(&"tv_stream.img"));
        assert!(names.contains(&"Canais TV"));
        assert!(names.contains(&"Filmes"));
        assert!(names.contains(&"Favoritos"));

        let canais = srv
            .catalog
            .lookup(ROOT_INO, "Canais TV")
            .expect("dir");
        let sub = srv.readdir_entries(canais.ino);
        let sub_names: Vec<&str> = sub.iter().map(|(_, n, _)| n.as_str()).collect();
        assert!(sub_names.contains(&"TV AO VIVO.trp"));
        assert!(sub_names.contains(&"TV AO VIVO 2.tp"));
    }

    /// ITER#2 regression (#7): a directory listing must never expose a
    /// zero-length name (the kernel treats that as a corrupt dirent).
    #[test]
    fn readdir_has_no_empty_name_entry() {
        let (srv, _b) = server();
        for (ino, name, _typ) in srv.readdir_entries(ROOT_INO) {
            assert!(!name.is_empty(), "empty dirent name for ino {ino}");
        }
    }

    #[test]
    fn test_resolve_lookup_bounds() {
        let (srv, _b) = server();
        // NUL-terminated name.
        let mut name = b"tv_stream.img".to_vec();
        name.push(0);
        let node = srv.resolve_lookup(ROOT_INO, &name).expect("resolves");
        assert_eq!(node.ino, FILE_INO);
        // Unterminated name still resolves (kernel always terminates).
        let node2 = srv
            .resolve_lookup(ROOT_INO, b"tv_stream.img")
            .expect("resolves");
        assert_eq!(node2.ino, FILE_INO);
        // Invalid UTF-8 fails closed.
        assert!(srv.resolve_lookup(ROOT_INO, &[0xFF, 0xFE]).is_none());
    }

    #[test]
    fn test_disk_read_whole_extent() {
        let (srv, _b) = server();
        // Read spanning metadata -> data in one request.
        let start = srv.layout.data_start_byte - 256;
        let mut buf = [0u8; 512 + TS_PKT_SZ * 2];
        let n = srv.serve_read(FILE_INO, start, &mut buf);
        assert_eq!(n, buf.len());
        // First 256 bytes: tail of the MFT metadata area (zero-filled hole).
        // Following bytes: ring data from the backend.
        assert_ne!(buf[300], 0);
    }

    /// Production-shaped stream: data starts right behind `$MFT` (cluster 23)
    /// and the file is fragmented into two extents with a hole between them.
    fn fragmented_server() -> (Arc<NtfsServer>, Arc<ProbeBackend>) {
        let layout = ntfs_mft::VolumeLayout {
            data_start_byte: 94_208,
            data_size: 128_000_000_000,
            live_file_inode: 64,
            extents: vec![
                ntfs_mft::DiskExtent { disk_start_byte: 94_208, disk_end_byte: 94_208 + 8192, file_start: 0 },
                ntfs_mft::DiskExtent { disk_start_byte: 1_000_000, disk_end_byte: 1_000_000 + 4096, file_start: 60_000 },
            ],
        };
        let backend = ProbeBackend::new();
        let server = Arc::new(NtfsServer::new(
            NtfsTemplate::with_layout(ntfs_mft::NtfsTemplate::default_vbr(), layout),
            Catalog::new(1024),
            backend.clone(),
        ));
        (server, backend)
    }

    #[test]
    fn test_serve_disk_backup_vbr_through_read() {
        let (srv, b) = server();
        let sec = srv.template.vbr.total_sectors;
        let mut buf = [0u8; BPS as usize];
        let n = srv.serve_read(FILE_INO, sec * BPS as u64, &mut buf);
        assert_eq!(n, BPS as usize);
        assert_eq!(&buf[3..11], b"NTFS    ");
        // The randomized VSN is stamped at offset 0x48 of both boot sectors.
        let vsn = u64::from_le_bytes(buf[ntfs_mft::VSN_OFFSET..ntfs_mft::VSN_OFFSET + 8].try_into().unwrap());
        assert_eq!(vsn, srv.template.serial);
        assert!(b.live_calls.lock().unwrap().is_empty());
    }

    #[test]
    fn test_serve_disk_routes_multi_extent_data() {
        let (srv, b) = fragmented_server();
        // A read landing on the second extent must be inverted to its file
        // offset (60_000), not served as a linear disk offset.
        let mut buf = [0u8; TS_PKT_SZ];
        let n = srv.serve_read(FILE_INO, 1_000_000, &mut buf);
        assert_eq!(n, TS_PKT_SZ);
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0], (0, 60_000, TS_PKT_SZ));
        drop(calls);

        // The hole between the extents is metadata: zeros, no backend call.
        let mut hole = [0xFF; TS_PKT_SZ];
        let n = srv.serve_read(FILE_INO, 94_208 + 8192, &mut hole);
        assert_eq!(n, TS_PKT_SZ);
        assert!(hole.iter().all(|&x| x == 0));
        assert_eq!(b.live_calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_serve_disk_clamps_stream_chunk_to_extent_end() {
        let (srv, b) = fragmented_server();
        // A read straddling the first extent end serves 8192 stream bytes and
        // the remainder from the hole (metadata), never one bridged call.
        let mut buf = [0u8; TS_PKT_SZ * 8];
        let n = srv.serve_read(FILE_INO, 94_208 + 8192 - TS_PKT_SZ as u64 * 4, &mut buf);
        assert_eq!(n, buf.len());
        let calls = b.live_calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].2, TS_PKT_SZ * 4);
        drop(calls);
        // The trailing hole bytes are zeros.
        assert!(buf[TS_PKT_SZ * 4..].iter().all(|&x| x == 0));
    }

    /// Builds a raw INIT request in `buf`; returns the total request length.
    fn init_request(buf: &mut AlignedFuseBuf, minor: u32, flags: u32) -> usize {
        let req_len = IN_HDR_SZ + 16;
        buf.0[..req_len].fill(0);
        // SAFETY: 8-byte aligned buffer, req_len >= IN_HDR_SZ + 16.
        let ih = unsafe { &mut *(buf.0.as_mut_ptr() as *mut fuse_in_header) };
        ih.len = req_len as u32;
        ih.opcode = FUSE_INIT;
        ih.unique = 7;
        let ii = unsafe { &mut *(buf.0[IN_HDR_SZ..].as_mut_ptr() as *mut fuse_init_in) };
        ii.major = 7;
        ii.minor = minor;
        ii.max_readahead = MAX_MSG_SZ as u32;
        ii.flags = flags;
        req_len
    }

    fn dispatch_init(req: &mut AlignedFuseBuf, minor: u32, flags: u32) -> Box<AlignedFuseBuf> {
        let (srv, _b) = server();
        // fd = -1: `reply` discards the EBADF from the failed write.
        let session = FuseSession::new(-1, srv);
        let req_len = init_request(req, minor, flags);
        let mut out = AlignedFuseBuf::new_boxed();
        session.dispatch(&req.0[..req_len], &mut out);
        out
    }

    fn out_len(out: &AlignedFuseBuf) -> usize {
        u32::from_ne_bytes(out.0[0..4].try_into().unwrap()) as usize
    }

    fn out_err(out: &AlignedFuseBuf) -> i32 {
        i32::from_ne_bytes(out.0[4..8].try_into().unwrap())
    }

    #[test]
    fn test_init_reply_truncated_for_legacy_minor() {
        let mut req = AlignedFuseBuf::new_boxed();
        let out = dispatch_init(&mut req, 18, FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        // Tablet kernel 3.4.0 negotiates 7.18 (< 23): body capped at 24.
        assert_eq!(out_len(&out), OUT_HDR_SZ + 24);
        assert_eq!(out_err(&out), 0);
        // SAFETY: 8-byte aligned buffer, reply covers the whole struct.
        let io = unsafe { &*(out.0[OUT_HDR_SZ..].as_ptr() as *const fuse_init_out) };
        assert_eq!(io.major, 7);
        assert_eq!(io.minor, 18);
        assert_eq!(io.max_readahead, MAX_MSG_SZ as u32);
        assert_eq!(io.flags, FUSE_ASYNC_READ | FUSE_BIG_WRITES);
        assert_eq!(io.max_write, MAX_MSG_SZ as u32);
        // Zero-filled reply region: no stale MPEG-TS bytes leak into
        // kernel-visible fields.
        assert_eq!(io.max_background, 0);
        assert_eq!(io.congestion_threshold, 0);
    }

    #[test]
    fn test_init_reply_caps_minor_at_26() {
        let mut req = AlignedFuseBuf::new_boxed();
        let out = dispatch_init(&mut req, 31, FUSE_ASYNC_READ);
        assert_eq!(
            out_len(&out),
            OUT_HDR_SZ + std::mem::size_of::<fuse_init_out>()
        );
        assert_eq!(out_err(&out), 0);
        // SAFETY: as above.
        let io = unsafe { &*(out.0[OUT_HDR_SZ..].as_ptr() as *const fuse_init_out) };
        assert_eq!(io.major, 7);
        assert_eq!(io.minor, 26);
        assert_eq!(io.flags, FUSE_ASYNC_READ);
    }

    #[test]
    fn test_init_rejects_short_payload() {
        let (srv, _b) = server();
        let session = FuseSession::new(-1, srv);
        let mut req = AlignedFuseBuf::new_boxed();
        req.0[..IN_HDR_SZ].fill(0);
        // SAFETY: 8-byte aligned buffer, IN_HDR_SZ bytes available.
        let ih = unsafe { &mut *(req.0.as_mut_ptr() as *mut fuse_in_header) };
        ih.len = IN_HDR_SZ as u32;
        ih.opcode = FUSE_INIT;
        ih.unique = 9;
        let mut out = AlignedFuseBuf::new_boxed();
        session.dispatch(&req.0[..IN_HDR_SZ], &mut out);
        assert_eq!(out_len(&out), OUT_HDR_SZ);
        assert_eq!(out_err(&out), -libc::EINVAL);
    }

    #[test]
    fn favorites_route_ignores_global_active_and_serves_bootstrap() {
        use crate::favorites::extents::ExtentMap;
        use crate::favorites::runtime::{FavoriteFile, FavoritesRuntime, RuntimeConfig};
        use crate::vfs::ntfs_mft::parse_runlist;

        let mut encoded = vec![0x44];
        encoded.extend_from_slice(&(1024u32).to_le_bytes());
        encoded.extend_from_slice(&(256i32).to_le_bytes());
        encoded.push(0);
        let runs = parse_runlist(&encoded);
        let mut map = ExtentMap::new(8 * 1024 * 1024).unwrap();
        map.add_metadata(0, 1024 * 1024).unwrap();
        map.add_file_runs(7, &runs, 1 * 1024 * 1024).unwrap();
        let runtime = Arc::new(
            FavoritesRuntime::new(
                map,
                vec![FavoriteFile {
                    file_id: 7,
                    channel_id: "sbt".into(),
                    path: "Favoritos/Abertos/SBT.trp".into(),
                    logical_size: 1024 * 1024,
                }],
                RuntimeConfig::new("session", "manifest"),
            )
            .unwrap(),
        );
        let backend = ProbeBackend::new();
        let server = Arc::new(
            NtfsServer::new(
                NtfsTemplate::synthesized(),
                Catalog::new(1024),
                backend.clone(),
            )
            .with_favorites(runtime.clone()),
        );
        server.catalog.set_active(1);
        let disk_start = 1024 * 1024u64;
        let bootstrap = runtime.bootstrap(7).unwrap();
        let mut buf = vec![0u8; TS_PKT_SZ * 4];
        assert_eq!(server.serve_read(FILE_INO, disk_start, &mut buf), buf.len());
        assert_eq!(&buf[..], &bootstrap.bytes()[..buf.len()]);
        assert!(backend.live_calls.lock().unwrap().is_empty());

        // A read in the unallocated hole still comes from the template and
        // never from the active catalog channel.
        let mut hole = vec![0xFFu8; 512];
        server.serve_read(FILE_INO, 2 * 1024 * 1024, &mut hole);
        assert!(backend.live_calls.lock().unwrap().is_empty());
    }
}
