//! TASK-004: anchor state extracted from `live_engine.rs` to keep the engine
//! root within the 500-line budget. Behaviour is unchanged; `gen` is the
//! flush/open generation token used to reject stale publication/commits.

#[derive(Debug, Clone)]
pub struct AnchorState {
    pub active: bool,
    pub file_id: u32,
    pub base_foff: u64,
    pub base_spos: u64,
    pub last_fend: u64,
    pub epoch_seq: u64,
    /// Anchor-generation token: incremented by a flush/open and by every
    /// reanchor publication. A read that captured an older value must not
    /// publish an anchor or commit progress.
    pub gen: u64,
    pub probe_consec: u8,
    pub last_probe_off: u64,
    pub last_read_ms: u64,
}

impl Default for AnchorState {
    fn default() -> Self {
        Self {
            active: false,
            file_id: 0,
            base_foff: 0,
            base_spos: 0,
            last_fend: 0,
            epoch_seq: 0,
            gen: 0,
            probe_consec: 0,
            last_probe_off: u64::MAX,
            last_read_ms: 0,
        }
    }
}
