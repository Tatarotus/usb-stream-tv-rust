//! fuse_ntfs: high-performance raw `/dev/fuse` live NTFS engine for
//! USB-Stream-TV.
//!
//! The crate serves a 128 GiB thin-provisioned NTFS volume directly through
//! the kernel FUSE device, without any backing filesystem write path. The
//! Samsung MStar 2013 SCSI bridge reads the volume over the USB gadget; the
//! MPEG-TS payload of the stream file is synthesized on demand by the
//! streaming engine (`engine`), while filesystem metadata (VBR, $MFT, index
//! nodes) is synthesized or served from a sparse template (`vfs::ntfs_mft`).
//!
//! Module map:
//! * [`contract`] — constants + `StreamBackend` seam shared with the engine.
//! * [`engine`] — live/VOD streaming engine (ring, anchors, probes).
//! * [`observability`] — bounded, loss-accounted `USBTV_OBS` diagnostics.
//! * [`vfs`] — kernel FUSE ABI, NTFS metadata layer, virtual catalog, and the
//!   `/dev/fuse` protocol dispatcher.

pub mod contract;
pub mod engine;
// Host-side favorites contracts. No runtime activation until transport,
// template and physical validation are integrated.
pub mod favorites;
pub mod observability;
pub mod vfs;

pub use contract::{LEADBACK, RESUME_MIN_LEAD, RESUME_TARGET_MARGIN, RINGSZ, StreamBackend, TS_PKT_SZ};
pub use vfs::{catalog, dispatcher, fuse_abi, ntfs_mft};
