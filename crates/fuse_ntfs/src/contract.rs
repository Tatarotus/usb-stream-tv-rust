//! Shared contract between the FUSE VFS layer and the streaming backends.
//!
//! This module is the stable seam between the NTFS virtual-filesystem
//! dispatcher (`vfs`) and the live/VOD ingest engine (`engine`). Both sides
//! depend only on the constants and trait defined here, so the engine can be
//! developed, audited, and replaced independently of the kernel ABI layer.

/// Size in bytes of one MPEG-TS transport stream packet.
pub const TS_PKT_SZ: usize = 188;

/// Capacity of the live ring buffer in bytes (128 MiB). The ring is circular
/// and self-recovering: the network feeder never throttles, and a reader that
/// falls behind `ring_old` is transparently re-anchored to the live target.
pub const RINGSZ: usize = 128 * 1024 * 1024;

/// Distance behind the live write head (`g_s_write`) where playback anchors.
///
/// This is the live delay the viewer sees at every open and every restart: at
/// the measured 5.3 Mbit/s source, 16 MiB was ~27 s behind live, so a Return
/// that reopens the file landed at the same relative position and the picture
/// looked like "the same scene as the first open" (RC-001 hardware window,
/// iterations 9/10).
///
/// 12 MiB halves that to ~18 s while keeping the whole strict-contract suite
/// unchanged. The host sweep (`integration_rc001_lead_sweep`, 5.3 Mbit/s, 8 s
/// upstream interruption, ring wrap) shows 12 MiB and 16 MiB behave identically
/// under starvation (0 NULL bytes) and 12 MiB keeps 6 MiB of margin.
///
/// 8 MiB also passed the sweep with 3.4 MiB of margin and is the smallest lead
/// `PaceTier::Fast900` can sustain (below `PACE_LEAD_FAST` = 8 MiB the token
/// bucket drops to 600 KiB/s, slower than the stream, so the cushion would
/// self-correct back to ~8 MiB). It is NOT selected yet because two strict
/// contracts (test 8 wrap, test 14 large bookmark) pre-buffer 9-12 MiB and play
/// a static ring: their playable window is exactly one lead, so an 8 MiB lead
/// makes them run past the live edge. Those fixtures need a live-feeder
/// redesign, which must be reviewed before the lead is reduced further.
pub const LEADBACK: u64 = 12 * 1024 * 1024;

/// Minimum lead the reader must hold over the live edge before a confirmed
/// resume (bookmark / jump) is allowed to re-anchor the stream.
pub const RESUME_MIN_LEAD: u64 = 2 * 1024 * 1024;

/// File offset at/above which reads are treated as probe candidates.
pub const PROBE_ISOLATION_FOFF: u64 = 8 * 1024 * 1024;

/// Idle time after the last read at/above which the next read is a player
/// reopen (Return / resume) rather than steady playback. Shared by the
/// `restart_at_zero` cold restart and by the qualified-return probe exception.
pub const RETURN_IDLE_MS: u64 = 1500;

/// Margin past the active end beyond which a read is an isolated probe.
pub const PROBE_OVERFALL: u64 = 2 * 1024 * 1024;

/// Margin added on top of the resume target when re-anchoring, so the reader
/// restarts slightly behind the requested bookmark and never outruns it.
pub const RESUME_TARGET_MARGIN: u64 = 2 * 1024 * 1024;

/// Slack multiple applied to the *measured* PTS cadence when deciding whether a
/// PES timestamp inside a delivered block may become the splice reference.
///
/// The horizon is `cadence_ticks_per_byte * delivered_bytes * this`, i.e. it is
/// derived from the stream's own cadence and the bytes actually delivered —
/// never from a fixed wall-clock window, so a legitimate cadence change (or a
/// new epoch) is always accepted once its bytes have been delivered, while a
/// single corrupt/future PES inside an otherwise normal block is rejected.
/// Rejected candidates update neither the reference nor the cadence estimator.
pub const PTS_CADENCE_SLACK: u64 = 8;

/// (A) recovery: how many consecutive DELIVERED BLOCKS may be rejected while
/// still advancing forward before the cadence estimator is re-measured from the
/// bytes those blocks carried. It counts deliveries, not time and not ticks:
/// corruption does not repeat in an ordered way, a real timeline change does.
pub const PTS_REJECT_STREAK: u8 = 4;

/// (B) How many consecutive reads a qualified Return intent may stay armed. It
/// counts delivered reads of the SAME chain: a read that does not continue the
/// chain (a probe, a jump, an abandoned chain) cancels the intent immediately,
/// so it can never disable probe isolation or leak the return context into an
/// unrelated seam.
pub const RETURN_CHAIN_READS: u32 = 16;

/// Backend abstraction implemented by the streaming engine.
///
/// * `serve_live` feeds the live channel identified by `file_id`.
/// * `serve_vod` feeds an on-demand stream identified by its HTTP URL.
///
/// Both methods must fully populate `dst` (padding with MPEG-TS null packets
/// when the source is exhausted) so the 27 MHz STC PLL of the 2013 MStar
/// decoder never starves on a short reply.
pub trait StreamBackend: Send + Sync {
    /// Fills `dst` with live MPEG-TS bytes for `file_id`, starting at the
    /// stream offset `foff`. Returns the number of bytes written.
    fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], deadline_ms: u64) -> usize;

    /// Fills `dst` with VOD bytes for `url`, starting at `foff`.
    /// Returns the number of bytes written, or an I/O error when the source
    /// cannot be reached (the caller then falls back to null packets).
    fn serve_vod(
        &self,
        url: &str,
        foff: u64,
        dst: &mut [u8],
        deadline_ms: u64,
    ) -> Result<usize, std::io::Error>;

    /// Optional notification when the kernel opens the backing storage file.
    fn on_open(&self) {}
}
