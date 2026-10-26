//! Favorites Live MVP: manifest, resolver, NTFS extent routing, probe
//! detector and the selection/epoch contract.
//!
//! These modules are **host-validated building blocks**, not an enabled
//! runtime mode. Nothing here changes the default Single Live or VOD path:
//!
//! * the existing `Catalog` owns host FUSE nodes, while the TV reads sectors
//!   of `tv_stream.img`; [`extents::ExtentMap`] supplies the missing
//!   disk-byte → file identity mapping;
//! * [`detector::ProbeDetector`] only emits an intent and never mutates the
//!   active anchor/ring;
//! * [`selection::SelectionController`] publishes an active tuple only after
//!   verified data from the matching `source_epoch` exists.
//!
//! Activation requires a validated NTFS template, a mode/script change and a
//! physical window, which are intentionally out of scope for this host MVP.

pub mod bootstrap;
pub mod detector;
pub mod digest;
pub mod extents;
pub mod layout;
pub mod manifest;
pub mod name;
pub mod resolver;
pub mod runtime;
pub mod selection;
pub mod startup;
pub mod transport;

pub use detector::{CandidateView, DetectorConfig, ProbeDetector, ReadSpan, SelectionIntent};
pub use extents::{ExtentMap, FileExtent, MapError, MapRegion, Segment, Span};
pub use manifest::{
    CapacityPolicy, CapacityUsage, EntryKind, Manifest, ManifestEntry, ManifestError,
    DEFAULT_LOGICAL_SIZE, LAYOUT_VERSION, SCHEMA_VERSION, VOLUME_128_GIB,
};
pub use resolver::{
    resolve_favorites, CatalogChannel, ChannelKind, LiveCatalog, Rejection, RejectionReason,
    Resolution, ResolutionError, ResolverPolicy,
};
pub use selection::{
    DataFrame, Published, SelectionController, SelectionError, SelectionRequest, SelectionState,
};
