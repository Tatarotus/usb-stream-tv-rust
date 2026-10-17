//! Virtual filesystem layer: kernel protocol, NTFS metadata, catalog, and the
//! `/dev/fuse` request dispatcher.

pub mod catalog;
pub mod dispatcher;
pub mod fuse_abi;
pub mod ntfs_mft;
