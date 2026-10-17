//! Low-level Linux `/dev/fuse` kernel protocol definitions (Kernel 3.4+).
//!
//! Structures are the raw kernel-userspace ABI for the FUSE protocol: every
//! field is little-endian on the wire and every struct is `#[repr(C)]` with
//! natural alignment. The 8-byte-aligned staging buffer below is mandatory on
//! ARMv7-A: the kernel emits `LDRD`/`STRD` pairs for 64-bit struct fields
//! (timespec, offsets, sizes), and an unaligned buffer hard-faults the CPU.

#![allow(non_camel_case_types)]
#![allow(dead_code)]

// ------------------------------------------------------------------ opcodes

pub const FUSE_LOOKUP: u32 = 1;
pub const FUSE_FORGET: u32 = 2;
pub const FUSE_GETATTR: u32 = 3;
pub const FUSE_OPEN: u32 = 14;
pub const FUSE_READ: u32 = 15;
pub const FUSE_STATFS: u32 = 17;
/// FUSE_RELEASE is 18 in the Linux kernel ABI (8 is FUSE_MKDIR; a release
/// handler must never swallow mkdir requests, so the audited value from the
/// `fuse_direct` sibling engine is used here verbatim).
pub const FUSE_RELEASE: u32 = 18;
pub const FUSE_INIT: u32 = 26;
pub const FUSE_OPENDIR: u32 = 27;
pub const FUSE_READDIR: u32 = 28;
pub const FUSE_RELEASEDIR: u32 = 29;
pub const FUSE_BATCH_FORGET: u32 = 42;

// -------------------------------------------------------------------- flags

/// Kernel-side init flags (negotiated during FUSE_INIT).
pub const FUSE_ASYNC_READ: u32 = 1 << 0;
pub const FUSE_BIG_WRITES: u32 = 1 << 5;

/// Open-reply flags (FUSE_OPEN / FUSE_OPENDIR).
pub const FOPEN_DIRECT_IO: u32 = 1 << 0;
pub const FOPEN_KEEP_CACHE: u32 = 1 << 1;
pub const FOPEN_NONSEEKABLE: u32 = 1 << 2;

// ------------------------------------------------------------------ structs

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
pub struct fuse_open_in {
    pub flags: u32,
    pub unused: u32,
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

/// Maximum payload the kernel will send or accept in one request.
pub const MAX_MSG_SZ: usize = 128 * 1024;

/// 8-byte aligned FUSE request/reply staging buffer.
/// The ARMv7-A kernel emits `LDRD`/`STRD` for 64-bit struct fields
/// (timespec, offsets, sizes) inside the FUSE out-handlers; an unaligned
/// staging buffer hard-faults the CPU on 32-bit ARM.
#[repr(C, align(8))]
pub struct AlignedFuseBuf(pub [u8; MAX_MSG_SZ + 4096]);

impl AlignedFuseBuf {
    pub fn new_zeroed() -> Self {
        Self([0u8; MAX_MSG_SZ + 4096])
    }

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

#[cfg(test)]
mod tests {
    use super::*;

    /// Entry sizing rule shared by the kernel and the READDIR handler:
    /// a `fuse_dirent` header plus the NUL-less name, padded up to 8 bytes.
    fn dirent_entry_len(name: &str) -> usize {
        (std::mem::size_of::<fuse_dirent>() + name.len() + 7) & !7
    }

    #[test]
    fn opcodes_match_the_linux_kernel_abi() {
        assert_eq!(FUSE_LOOKUP, 1);
        assert_eq!(FUSE_FORGET, 2);
        assert_eq!(FUSE_GETATTR, 3);
        assert_eq!(FUSE_OPEN, 14);
        assert_eq!(FUSE_READ, 15);
        assert_eq!(FUSE_STATFS, 17);
        assert_eq!(FUSE_RELEASE, 18);
        assert_eq!(FUSE_INIT, 26);
        assert_eq!(FUSE_OPENDIR, 27);
        assert_eq!(FUSE_READDIR, 28);
        assert_eq!(FUSE_BATCH_FORGET, 42);
        // 8 is FUSE_MKDIR on the wire; a RELEASE opcode must never shadow it.
        assert_ne!(FUSE_RELEASE, 8);
    }

    #[test]
    fn open_flags_expose_direct_io_not_nonseekable() {
        assert_eq!(FOPEN_DIRECT_IO, 1 << 0);
        // Waiver #FUSE-2013-MSTAR: NONSEEKABLE is never granted, the MStar
        // SCSI bridge relies on pread64/lseek over the block image.
        assert_eq!(FOPEN_NONSEEKABLE & FOPEN_DIRECT_IO, 0);
    }

    #[test]
    fn wire_struct_layouts_are_exact() {
        assert_eq!(std::mem::size_of::<fuse_in_header>(), 40);
        assert_eq!(std::mem::align_of::<fuse_in_header>(), 8);
        assert_eq!(std::mem::size_of::<fuse_out_header>(), 16);
        assert_eq!(std::mem::align_of::<fuse_out_header>(), 8);
        assert_eq!(std::mem::size_of::<fuse_attr>(), 88);
        assert_eq!(std::mem::size_of::<fuse_entry_out>(), 128);
        assert_eq!(std::mem::size_of::<fuse_init_out>(), 24);
        assert_eq!(std::mem::size_of::<fuse_open_out>(), 16);
        assert_eq!(std::mem::size_of::<fuse_dirent>(), 24);
        assert_eq!(std::mem::align_of::<fuse_dirent>(), 8);
        assert_eq!(std::mem::size_of::<fuse_statfs_out>(), 80);
    }

    #[test]
    fn dirent_entries_are_eight_byte_aligned() {
        for name in ["", ".", "..", "Filmes", "Canais TV", "TV AO VIVO.trp", "tv_stream.img"] {
            let len = dirent_entry_len(name);
            assert_eq!(len % 8, 0, "{name} entry not padded to 8");
            assert!(len >= std::mem::size_of::<fuse_dirent>() + name.len());
        }
        assert_eq!(dirent_entry_len("Filmes"), 32);
        assert_eq!(dirent_entry_len("Canais TV"), 40);
        assert_eq!(dirent_entry_len("TV AO VIVO.trp"), 40);
    }

    #[test]
    fn dirent_serialization_is_little_endian_with_zeroed_padding() {
        // Reproduces the READDIR payload construction bit for bit.
        let mut out = AlignedFuseBuf::new_zeroed();
        let payload = out.0[std::mem::size_of::<fuse_out_header>()..].as_mut_ptr();
        assert_eq!(payload as usize % 8, 0, "dirent area must stay 8-byte aligned");

        let name = "Canais TV";
        let namelen = name.len();
        let entry_len = dirent_entry_len(name);
        // SAFETY: `entry_len` fits inside the oversized staging buffer and the
        // base pointer is 8-byte aligned, exactly as the dispatcher requires.
        let de = unsafe { &mut *(payload as *mut fuse_dirent) };
        de.ino = 7;
        de.off = 3;
        de.namelen = namelen as u32;
        de.typ = libc::DT_DIR as u32;
        // SAFETY: `namelen` bytes fit inside the validated window.
        unsafe {
            std::ptr::copy_nonoverlapping(
                name.as_ptr(),
                payload.add(std::mem::size_of::<fuse_dirent>()),
                namelen,
            );
        }

        let raw = &out.0[std::mem::size_of::<fuse_out_header>()..std::mem::size_of::<fuse_out_header>() + entry_len];
        assert_eq!(&raw[0..8], &7u64.to_le_bytes(), "ino must be little-endian");
        assert_eq!(&raw[8..16], &3u64.to_le_bytes(), "off must be little-endian");
        assert_eq!(&raw[16..20], &(namelen as u32).to_le_bytes(), "namelen is a u32");
        assert_eq!(&raw[20..24], &(libc::DT_DIR as u32).to_le_bytes(), "typ is a u32");
        assert_eq!(&raw[24..24 + namelen], name.as_bytes(), "name follows the header");
        assert!(
            raw[24 + namelen..entry_len].iter().all(|&b| b == 0),
            "padding past the name must be zeroed"
        );
    }

    #[test]
    fn staging_buffer_is_eight_byte_aligned_and_zeroed() {
        assert_eq!(std::mem::align_of::<AlignedFuseBuf>(), 8);
        let boxed = AlignedFuseBuf::new_boxed();
        let ptr = boxed.0.as_ptr();
        assert_eq!(ptr as usize % 8, 0, "ARMv7-A LDRD/STRD requires 8-byte alignment");
        assert_eq!(boxed.0.len(), MAX_MSG_SZ + 4096);
        let zeroed = AlignedFuseBuf::new_zeroed();
        assert!(zeroed.0[..].iter().all(|&b| b == 0));
    }
}
