use std::sync::Mutex;

use super::ntfs_mft::{FILE_INO, FILE_NAME, ROOT_INO};

pub const DIR_CANAIS: &str = "Canais TV";
pub const DIR_FILMES: &str = "Filmes";
pub const DIR_FAVORITOS: &str = "Favoritos";
pub const LIVE_NAME_PRIMARY: &str = "TV AO VIVO.trp";
pub const LIVE_NAME_ALTERNATE: &str = "TV AO VIVO 2.tp";
pub const SWITCH_THRESHOLD: u64 = 1024 * 1024;
pub const DIR_INO_CANAIS: u64 = 3;
pub const DIR_INO_FILMES: u64 = 4;
pub const DIR_INO_FAVORITOS: u64 = 5;
pub const LIVE_INO_BASE: u64 = 0x1000;
pub const VOD_INO_BASE: u64 = 0x10000;
pub const STREAM_INO: u64 = FILE_INO;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NodeKind {
    Root,
    Directory,
    DiskImage,
    Live { file_id: u32 },
    Vod { url: String },
}

#[derive(Debug, Clone)]
pub struct Node {
    pub ino: u64,
    pub parent: u64,
    pub name: String,
    pub kind: NodeKind,
    pub size: u64,
}

impl Node {
    pub fn is_dir(&self) -> bool {
        matches!(self.kind, NodeKind::Root | NodeKind::Directory)
    }
    pub fn dirent_type(&self) -> u32 {
        if self.is_dir() { libc::DT_DIR as u32 } else { libc::DT_REG as u32 }
    }
}

#[derive(Debug, Default)]
struct CatalogState {
    nodes: Vec<Node>,
    active_file_id: u32,
    watch_ino: u64,
    watch_end: u64,
    watch_bytes: u64,
}

pub struct Catalog {
    state: Mutex<CatalogState>,
}

impl Default for Catalog {
    fn default() -> Self {
        Self::new(1024 * 1024 * 1024)
    }
}

impl Catalog {
    pub fn new(live_size: u64) -> Self {
        let mut nodes = Vec::with_capacity(8);
        nodes.push(Node { ino: ROOT_INO, parent: ROOT_INO, name: String::new(), kind: NodeKind::Root, size: 4096 });
        nodes.push(Node { ino: FILE_INO, parent: ROOT_INO, name: FILE_NAME.to_string(), kind: NodeKind::DiskImage, size: crate::vfs::ntfs_mft::VOLUME_BYTES });
        for (ino, name) in [(DIR_INO_CANAIS, DIR_CANAIS), (DIR_INO_FILMES, DIR_FILMES), (DIR_INO_FAVORITOS, DIR_FAVORITOS)] {
            nodes.push(Node { ino, parent: ROOT_INO, name: name.to_string(), kind: NodeKind::Directory, size: 4096 });
        }
        let cat = Self { state: Mutex::new(CatalogState { nodes, active_file_id: 0, watch_ino: 0, watch_end: 0, watch_bytes: 0 }) };
        cat.add_live_channel(0, LIVE_NAME_PRIMARY, live_size);
        cat.add_live_channel(1, LIVE_NAME_ALTERNATE, live_size);
        cat
    }
    fn lock(&self) -> std::sync::MutexGuard<'_, CatalogState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    pub fn node(&self, ino: u64) -> Option<Node> {
        self.lock().nodes.iter().find(|n| n.ino == ino).cloned()
    }
    pub fn lookup(&self, parent: u64, name: &str) -> Option<Node> {
        let st = self.lock();
        st.nodes.iter().find(|n| n.parent == parent && n.name == name).cloned()
    }
    pub fn children(&self, ino: u64) -> Vec<Node> {
        // The root node carries `parent == ROOT_INO`, so a naive parent match
        // lists the root as its own child with an empty name — an invalid FUSE
        // dirent. Exclude the node itself (ITER#2 fix #7).
        self.lock()
            .nodes
            .iter()
            .filter(|n| n.parent == ino && n.ino != ino)
            .cloned()
            .collect()
    }
    pub fn len(&self) -> usize {
        self.lock().nodes.len()
    }
    pub fn is_empty(&self) -> bool { self.len() == 0 }
    pub fn active_file_id(&self) -> u32 { self.lock().active_file_id }
    pub fn active_node(&self) -> Option<Node> {
        let st = self.lock();
        let id = st.active_file_id;
        st.nodes.iter().find(|n| matches!(&n.kind, NodeKind::Live { file_id } if *file_id == id)).cloned()
    }
    pub fn set_active(&self, file_id: u32) -> Option<Node> {
        let mut st = self.lock();
        st.active_file_id = file_id;
        st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0;
        st.nodes.iter().find(|n| matches!(&n.kind, NodeKind::Live { file_id: f } if *f == file_id)).cloned()
    }
    pub fn add_live_channel(&self, file_id: u32, name: &str, size: u64) -> u64 {
        let ino = LIVE_INO_BASE + u64::from(file_id);
        self.lock().nodes.push(Node { ino, parent: DIR_INO_CANAIS, name: name.to_string(), kind: NodeKind::Live { file_id }, size });
        ino
    }
    pub fn add_vod_file(&self, url: &str, name: &str, size: u64) -> u64 {
        let mut st = self.lock();
        let ino = VOD_INO_BASE + st.nodes.len() as u64;
        st.nodes.push(Node { ino, parent: DIR_INO_FILMES, name: name.to_string(), kind: NodeKind::Vod { url: url.to_string() }, size });
        ino
    }
    pub fn note_read(&self, ino: u64, offset: u64, len: u64) -> Option<u32> {
        let mut st = self.lock();
        let kind = match st.nodes.iter().find(|n| n.ino == ino) { Some(n) => n.kind.clone(), None => return None };
        let NodeKind::Live { file_id } = kind else {
            st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0; return None;
        };
        if file_id == st.active_file_id { st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0; return None; }
        let end = offset.saturating_add(len);
        if st.watch_ino == ino && offset == st.watch_end { st.watch_bytes = st.watch_bytes.saturating_add(len); }
        else { st.watch_ino = ino; st.watch_bytes = len; }
        st.watch_end = end;
        if st.watch_bytes >= SWITCH_THRESHOLD {
            st.active_file_id = file_id;
            st.watch_ino = 0; st.watch_end = 0; st.watch_bytes = 0;
            Some(file_id)
        } else { None }
    }
    pub fn getattr(&self, ino: u64, stream_len: u64) -> Option<crate::vfs::fuse_abi::fuse_attr> {
        let node = self.node(ino)?;
        let mut attr = crate::vfs::fuse_abi::fuse_attr::default();
        attr.ino = node.ino;
        attr.size = if matches!(node.kind, NodeKind::DiskImage) { crate::vfs::ntfs_mft::VOLUME_BYTES } else { node.size };
        if ino == STREAM_INO { attr.size = stream_len; }
        attr.mode = if node.is_dir() { 0o040000 | 0o755 } else { 0o100000 | 0o644 } as u32;
        attr.nlink = if node.is_dir() { 2 } else { 1 };
        attr.blksize = 4096;
        attr.blocks = attr.size.div_ceil(512);
        Some(attr)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Size handed to every synthetic live channel in the catalog.
    const LIVE_SIZE: u64 = 4 * 1024 * 1024;

    fn cat() -> Catalog {
        Catalog::new(LIVE_SIZE)
    }

    #[test]
    fn root_tree_holds_disk_image_and_category_directories() {
        let c = cat();
        let root = c.node(ROOT_INO).expect("root node");
        assert_eq!(root.kind, NodeKind::Root);
        assert!(root.is_dir());

        let names: Vec<String> = c
            .children(ROOT_INO)
            .iter()
            .map(|n| n.name.clone())
            .collect();
        assert!(names.contains(&FILE_NAME.to_string()));
        assert!(names.contains(&DIR_CANAIS.to_string()));
        assert!(names.contains(&DIR_FILMES.to_string()));
        assert!(names.contains(&DIR_FAVORITOS.to_string()));

        let disk = c.lookup(ROOT_INO, FILE_NAME).expect("disk image");
        assert_eq!(disk.kind, NodeKind::DiskImage);
        assert_eq!(disk.ino, FILE_INO);
        assert_eq!(disk.size, crate::vfs::ntfs_mft::VOLUME_BYTES);
        assert_eq!(disk.dirent_type(), libc::DT_REG as u32);

        let canais = c.lookup(ROOT_INO, DIR_CANAIS).expect("canais");
        assert_eq!(canais.ino, DIR_INO_CANAIS);
        assert_eq!(canais.kind, NodeKind::Directory);
        assert_eq!(canais.dirent_type(), libc::DT_DIR as u32);
        assert_eq!(
            c.lookup(ROOT_INO, DIR_FILMES).expect("filmes").ino,
            DIR_INO_FILMES
        );
        assert_eq!(
            c.lookup(ROOT_INO, DIR_FAVORITOS).expect("favoritos").ino,
            DIR_INO_FAVORITOS
        );

        // Lookups must be scoped to the parent directory.
        assert!(c.lookup(DIR_INO_CANAIS, FILE_NAME).is_none());
        assert!(c.lookup(ROOT_INO, "nao-existe").is_none());
        assert!(c.node(9999).is_none());
    }

    /// ITER#2 regression (#7): the root node has `parent == ROOT_INO`, so a
    /// naive `parent == ino` filter lists it as its own child with an empty
    /// name, which surfaces as an invalid zero-length FUSE dirent.
    #[test]
    fn root_is_not_listed_as_its_own_child() {
        let c = cat();
        let kids = c.children(ROOT_INO);
        assert!(
            kids.iter().all(|n| n.ino != ROOT_INO),
            "root must not appear among its own children"
        );
        assert!(
            kids.iter().all(|n| !n.name.is_empty()),
            "no child may have an empty name"
        );
    }

    #[test]
    fn live_channels_register_under_canais_tv() {
        let c = cat();
        assert_eq!(c.len(), 7);

        let primary = c.lookup(DIR_INO_CANAIS, LIVE_NAME_PRIMARY).expect("primary");
        assert_eq!(primary.ino, LIVE_INO_BASE);
        assert_eq!(primary.kind, NodeKind::Live { file_id: 0 });
        assert_eq!(primary.size, LIVE_SIZE);
        assert_eq!(primary.parent, DIR_INO_CANAIS);

        let alternate = c.lookup(DIR_INO_CANAIS, LIVE_NAME_ALTERNATE).expect("alternate");
        assert_eq!(alternate.ino, LIVE_INO_BASE + 1);
        assert_eq!(alternate.kind, NodeKind::Live { file_id: 1 });

        let kids: Vec<String> = c
            .children(DIR_INO_CANAIS)
            .iter()
            .map(|n| n.name.clone())
            .collect();
        assert!(kids.contains(&LIVE_NAME_PRIMARY.to_string()));
        assert!(kids.contains(&LIVE_NAME_ALTERNATE.to_string()));
    }

    #[test]
    fn active_file_id_defaults_to_the_primary_channel() {
        let c = cat();
        assert_eq!(c.active_file_id(), 0);
        let active = c.active_node().expect("active node");
        assert_eq!(active.ino, LIVE_INO_BASE);
        assert_eq!(active.kind, NodeKind::Live { file_id: 0 });
    }

    #[test]
    fn set_active_switches_channel_and_drops_pending_watch() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;
        // Half a threshold already pending on the inactive channel.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD / 2).is_none());
        // An explicit switch resets the detector, so the pending run never
        // completes the switch on its own.
        let node = c.set_active(1).expect("alternate node");
        assert_eq!(node.ino, alt);
        assert_eq!(c.active_file_id(), 1);
        assert_eq!(c.active_node().expect("active").ino, alt);
        // Reads of the now-active channel never re-trigger a switch.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD / 2).is_none());
        assert!(c.note_read(alt, SWITCH_THRESHOLD / 2, SWITCH_THRESHOLD / 2).is_none());
        assert_eq!(c.active_file_id(), 1);
        // Switching back re-arms tracking on the other channel.
        assert_eq!(c.set_active(0).expect("primary").ino, LIVE_INO_BASE);
        assert_eq!(c.active_file_id(), 0);
    }

    #[test]
    fn channel_switch_needs_a_contiguous_threshold_run() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;

        // Chunks below half the threshold must accumulate contiguously to
        // complete the switch; the third one tips it over. The `+ 1` guards
        // against integer truncation leaving the sum one byte short.
        let chunk = SWITCH_THRESHOLD / 3 + 1;
        assert!(c.note_read(alt, 0, chunk).is_none());
        assert!(c.note_read(alt, chunk, chunk).is_none());
        assert_eq!(c.active_file_id(), 0);
        let switched = c.note_read(alt, chunk * 2, chunk);
        assert_eq!(switched, Some(1));
        assert_eq!(c.active_file_id(), 1);
        assert_eq!(c.active_node().expect("active").ino, alt);

        // The detector resets after a switch: the active channel is inert.
        assert!(c.note_read(alt, SWITCH_THRESHOLD + SWITCH_THRESHOLD, SWITCH_THRESHOLD).is_none());
        assert_eq!(c.active_file_id(), 1);

        // A single read of exactly the threshold on the now-inactive primary
        // channel switches back immediately.
        assert!(c.note_read(LIVE_INO_BASE, 3 * SWITCH_THRESHOLD / 2, SWITCH_THRESHOLD).is_some());
    }

    #[test]
    fn channel_switch_ignores_seeks_below_the_threshold() {
        let c = cat();
        let alt = LIVE_INO_BASE + 1;
        let mut off = 0u64;
        let step = SWITCH_THRESHOLD / 4;
        for i in 0..8 {
            // Every fourth read rewinds to zero: the contiguous run is broken
            // so the accumulator must never reach the threshold.
            let seek = i % 4 == 3;
            let at = if seek { 0 } else { off };
            assert!(c.note_read(alt, at, step).is_none(), "iteration {i}");
            if !seek {
                off += step;
            }
        }
        assert_eq!(c.active_file_id(), 0);
        // A clean contiguous run still flips the channel afterwards.
        assert!(c.note_read(alt, 0, SWITCH_THRESHOLD).is_some());
        assert_eq!(c.active_file_id(), 1);
    }

    #[test]
    fn note_read_ignores_non_live_nodes() {
        let c = cat();
        assert!(c.note_read(ROOT_INO, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(DIR_INO_CANAIS, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(STREAM_INO, 0, 8 * SWITCH_THRESHOLD).is_none());
        let vod = c.add_vod_file("http://vps/movie.ts", "Filme.ts", 1024);
        assert!(c.note_read(vod, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert!(c.note_read(4242, 0, 8 * SWITCH_THRESHOLD).is_none());
        assert_eq!(c.active_file_id(), 0);
    }

    #[test]
    fn add_vod_file_registers_under_filmes() {
        let c = cat();
        let url = "http://example.invalid/movie.ts";
        let ino = c.add_vod_file(url, "Filme.ts", 2 * 1024 * 1024);
        assert!(ino >= VOD_INO_BASE);

        let node = c.node(ino).expect("vod node");
        assert_eq!(node.parent, DIR_INO_FILMES);
        assert_eq!(node.kind, NodeKind::Vod { url: url.to_string() });
        assert_eq!(node.size, 2 * 1024 * 1024);
        assert!(!node.is_dir());
        assert_eq!(node.dirent_type(), libc::DT_REG as u32);

        let found = c.lookup(DIR_INO_FILMES, "Filme.ts").expect("lookup");
        assert_eq!(found.ino, ino);
        assert!(c.children(DIR_INO_FILMES).iter().any(|n| n.ino == ino));
    }

    #[test]
    fn add_live_channel_appends_channel_nodes() {
        let c = cat();
        let ino = c.add_live_channel(7, "TV EXTRA.ts", LIVE_SIZE);
        assert_eq!(ino, LIVE_INO_BASE + 7);
        let node = c.node(ino).expect("extra channel");
        assert_eq!(node.kind, NodeKind::Live { file_id: 7 });
        assert_eq!(node.parent, DIR_INO_CANAIS);
    }

    #[test]
    fn getattr_maps_node_kinds_to_fuse_attributes() {
        let c = cat();

        let root = c.getattr(ROOT_INO, 0).expect("root attr");
        assert_eq!(root.mode, (libc::S_IFDIR | 0o755) as u32);
        assert_eq!(root.nlink, 2);
        assert_eq!(root.size, 4096);
        assert_eq!(root.blksize, 4096);
        assert_eq!(root.blocks, 4096 / 512);

        let canais = c.getattr(DIR_INO_CANAIS, 0).expect("dir attr");
        assert_eq!(canais.mode, (libc::S_IFDIR | 0o755) as u32);
        assert_eq!(canais.nlink, 2);

        let live = c.getattr(LIVE_INO_BASE, LIVE_SIZE).expect("live attr");
        assert_eq!(live.mode, (libc::S_IFREG | 0o644) as u32);
        assert_eq!(live.nlink, 1);
        assert_eq!(live.size, LIVE_SIZE);
        assert_eq!(live.ino, LIVE_INO_BASE);

        // The block image node reports the live engine length, which keeps the
        // TV reading a growing file instead of a stale 128 GiB projection.
        let stream = c.getattr(STREAM_INO, LIVE_SIZE).expect("stream attr");
        assert_eq!(stream.size, LIVE_SIZE);
        assert_eq!(stream.mode, (libc::S_IFREG | 0o644) as u32);

        assert!(c.getattr(9999, 0).is_none());
    }

    #[test]
    fn default_catalog_is_not_empty() {
        let c = Catalog::default();
        assert!(!c.is_empty());
        assert!(c.lookup(DIR_INO_CANAIS, LIVE_NAME_PRIMARY).is_some());
    }
}
