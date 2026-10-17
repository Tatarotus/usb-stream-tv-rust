//! Low-level Linux /dev/fuse kernel protocol definitions (Kernel 3.4+).

#![allow(non_camel_case_types)]
#![allow(dead_code)]

pub const FUSE_LOOKUP: u32 = 1;
pub const FUSE_FORGET: u32 = 2;
pub const FUSE_GETATTR: u32 = 3;
pub const FUSE_OPEN: u32 = 14;
pub const FUSE_READ: u32 = 15;
pub const FUSE_STATFS: u32 = 17;
pub const FUSE_RELEASE: u32 = 18;
pub const FUSE_OPENDIR: u32 = 21;
pub const FUSE_INIT: u32 = 26;
pub const FUSE_READDIR: u32 = 28;
pub const FUSE_RELEASEDIR: u32 = 29;
pub const FUSE_BATCH_FORGET: u32 = 42;

pub const FUSE_ROOT_ID: u64 = 1;
pub const FILE_INO: u64 = 2;

pub const FUSE_ASYNC_READ: u32 = 1 << 0;
pub const FUSE_BIG_WRITES: u32 = 1 << 5;

pub const FOPEN_DIRECT_IO: u32 = 1 << 0;
pub const FOPEN_KEEP_CACHE: u32 = 1 << 1;
pub const FOPEN_NONSEEKABLE: u32 = 1 << 2;

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_in_header {
    pub len: u32,
    pub opcode: u32,
    pub unique: u64,
    pub nodeid: u64,
    pub uid: u32,
    pub gid: u32,
    pub pid: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_out_header {
    pub len: u32,
    pub error: i32,
    pub unique: u64,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_init_in {
    pub major: u32,
    pub minor: u32,
    pub max_readahead: u32,
    pub flags: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_init_out {
    pub major: u32,
    pub minor: u32,
    pub max_readahead: u32,
    pub flags: u32,
    pub max_background: u16,
    pub congestion_threshold: u16,
    pub max_write: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_attr {
    pub ino: u64,
    pub size: u64,
    pub blocks: u64,
    pub atime: u64,
    pub mtime: u64,
    pub ctime: u64,
    pub atimensec: u32,
    pub mtimensec: u32,
    pub ctimensec: u32,
    pub mode: u32,
    pub nlink: u32,
    pub uid: u32,
    pub gid: u32,
    pub rdev: u32,
    pub blksize: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_attr_out {
    pub attr_valid: u64,
    pub attr_valid_nsec: u32,
    pub dummy: u32,
    pub attr: fuse_attr,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_entry_out {
    pub nodeid: u64,
    pub generation: u64,
    pub entry_valid: u64,
    pub attr_valid: u64,
    pub entry_valid_nsec: u32,
    pub attr_valid_nsec: u32,
    pub attr: fuse_attr,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_open_out {
    pub fh: u64,
    pub open_flags: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_read_in {
    pub fh: u64,
    pub offset: u64,
    pub size: u32,
    pub read_flags: u32,
    pub lock_owner: u64,
    pub flags: u32,
    pub padding: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct fuse_dirent {
    pub ino: u64,
    pub off: u64,
    pub namelen: u32,
    pub typ: u32,
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_kstatfs {
    pub blocks: u64,
    pub bfree: u64,
    pub bavail: u64,
    pub files: u64,
    pub ffree: u64,
    pub bsize: u32,
    pub namelen: u32,
    pub frsize: u32,
    pub padding: u32,
    pub spare: [u32; 6],
}

#[repr(C)]
#[derive(Debug, Copy, Clone, Default)]
pub struct fuse_statfs_out {
    pub st: fuse_kstatfs,
}
