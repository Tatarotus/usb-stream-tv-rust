//! Virtual FAT32 geometry, template loader, and sector synthesizer.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

pub const BPS: u64 = 512;
pub const SPC: u64 = 8;
pub const RSV: u64 = 32;
pub const NFATS: u64 = 2;
pub const SPF: u64 = 8192;
pub const FILECLUS: u32 = 3;
pub const DEFAULT_FILE_SIZE: u64 = 1_800_000_000;
pub const DATA_SEC: u64 = RSV + NFATS * SPF; // 16416: cluster 2 (root dir)
pub const FILE_SEC: u64 = DATA_SEC + SPC; // 16424: cluster 3 (file data)
pub const TOTCLUS: u64 = 1_048_576; // 4.00 GiB virtual disk
pub const TOTSEC: u64 = DATA_SEC + (TOTCLUS - 2) * SPC; // 8,405,008 sectors
pub const DISK_SIZE: u64 = TOTSEC * BPS; // 4,303,364,096 bytes

#[derive(Debug, Clone)]
pub struct VodFileEntry {
    pub start_clus: u32,
    pub end_clus: u32,
    pub size: u64,
    pub path: String,
}

#[derive(Debug, Clone)]
pub struct FatTemplate {
    pub sectors: HashMap<u32, Box<[u8; 512]>>,
    pub vod_files: Vec<VodFileEntry>,
    pub file_size: u64,
    pub last_clus: u32,
    pub volume_id: u32,
}

fn generate_vsn() -> u32 {
    let mut rnd = [0u8; 4];
    if let Ok(mut f) = File::open("/dev/urandom") {
        let _ = f.read_exact(&mut rnd);
    }
    if rnd == [0; 4] {
        let t = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u32)
            .unwrap_or(0x1234_5678);
        rnd = t.to_le_bytes();
    }
    if rnd == [0; 4] {
        rnd[0] = 0x01;
    }
    u32::from_le_bytes(rnd)
}

impl Default for FatTemplate {
    fn default() -> Self {
        Self::new()
    }
}

impl FatTemplate {
    pub fn new() -> Self {
        let nfileclus = DEFAULT_FILE_SIZE.div_ceil(SPC * BPS);
        let last_clus = FILECLUS + (nfileclus as u32) - 1;
        Self {
            sectors: HashMap::new(),
            vod_files: Vec::new(),
            file_size: DEFAULT_FILE_SIZE,
            last_clus,
            volume_id: generate_vsn(),
        }
    }

    /// Loads sector template from `fat_template.bin` (516-byte records: 4-byte LE sec + 512-byte data).
    pub fn load<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let mut file = File::open(path)?;
        let mut sectors = HashMap::new();
        let mut vod_files = Vec::new();
        let mut rec = [0u8; 516];

        while let Ok(()) = file.read_exact(&mut rec) {
            let sec = u32::from_le_bytes([rec[0], rec[1], rec[2], rec[3]]);
            let mut dat = Box::new([0u8; 512]);
            dat.copy_from_slice(&rec[4..516]);

            // Scan root directory sectors (DATA_SEC .. DATA_SEC + SPC - 1)
            let sec_u64 = u64::from(sec);
            if (DATA_SEC..DATA_SEC + SPC).contains(&sec_u64) {
                for off in (0..=512 - 32).step_by(32) {
                    let entry = &dat[off..off + 32];
                    let first = entry[0];
                    if first == 0x00 {
                        break;
                    }
                    if first == 0xE5 {
                        continue;
                    }
                    let attr = entry[11];
                    if attr == 0x0F || (attr & 0x18) != 0 {
                        // Skip LFN, volume labels, and subdirectories
                        continue;
                    }

                    let highclus = u16::from_le_bytes([entry[20], entry[21]]);
                    let lowclus = u16::from_le_bytes([entry[26], entry[27]]);
                    let start_clus = (u32::from(highclus) << 16) | u32::from(lowclus);
                    let sz = u32::from_le_bytes([entry[28], entry[29], entry[30], entry[31]]);

                    if start_clus >= FILECLUS && sz > 0 && vod_files.len() < 16 {
                        let sz_u64 = u64::from(sz);
                        let n_clus = sz_u64.div_ceil(SPC * BPS);
                        let end_clus = start_clus + (if n_clus > 0 { n_clus as u32 - 1 } else { 0 });

                        vod_files.push(VodFileEntry {
                            start_clus,
                            end_clus,
                            size: sz_u64,
                            path: String::from_utf8_lossy(&dat[off..off + 11]).trim().to_string(),
                        });
                    }
                }
            }

            sectors.insert(sec, dat);
        }

        let file_size = if let Some(first) = vod_files.first() {
            first.size
        } else {
            DEFAULT_FILE_SIZE
        };

        let last_clus = if let Some(last) = vod_files.last() {
            last.end_clus
        } else {
            let nfileclus = file_size.div_ceil(SPC * BPS);
            FILECLUS + (nfileclus as u32) - 1
        };

        Ok(Self {
            sectors,
            vod_files,
            file_size,
            last_clus,
            volume_id: generate_vsn(),
        })
    }

    pub fn lookup(&self, sec: u64) -> Option<&[u8; 512]> {
        if let Ok(sec_u32) = u32::try_from(sec) {
            self.sectors.get(&sec_u32).map(|b| b.as_ref())
        } else {
            None
        }
    }

    pub fn synthesize_boot(out: &mut [u8; 512]) {
        out.fill(0);
        out[0..3].copy_from_slice(&[0xEB, 0x58, 0x90]); // BS_jmpBoot
        out[3..11].copy_from_slice(b"MSWIN4.1"); // BS_OEMName
        out[11..13].copy_from_slice(&(BPS as u16).to_le_bytes()); // BPB_BytsPerSec = 512
        out[13] = SPC as u8; // BPB_SecPerClus = 8
        out[14..16].copy_from_slice(&(RSV as u16).to_le_bytes()); // BPB_RsvdSecCnt = 32
        out[16] = NFATS as u8; // BPB_NumFATs = 2
        out[17..19].copy_from_slice(&0u16.to_le_bytes()); // BPB_RootEntCnt = 0
        out[19..21].copy_from_slice(&0u16.to_le_bytes()); // BPB_TotSec16 = 0
        out[21] = 0xF8; // BPB_Media
        out[22..24].copy_from_slice(&0u16.to_le_bytes()); // BPB_FATSz16 = 0
        out[24..26].copy_from_slice(&63u16.to_le_bytes()); // BPB_SecPerTrk = 63
        out[26..28].copy_from_slice(&255u16.to_le_bytes()); // BPB_NumHeads = 255
        out[28..32].copy_from_slice(&0u32.to_le_bytes()); // BPB_HiddSec = 0
        out[32..36].copy_from_slice(&(TOTSEC as u32).to_le_bytes()); // BPB_TotSec32
        out[36..40].copy_from_slice(&(SPF as u32).to_le_bytes()); // BPB_FATSz32 = 8192
        out[40..42].copy_from_slice(&0u16.to_le_bytes()); // BPB_ExtFlags = 0
        out[42..44].copy_from_slice(&0u16.to_le_bytes()); // BPB_FSVer = 0
        out[44..48].copy_from_slice(&2u32.to_le_bytes()); // BPB_RootClus = 2
        out[48..50].copy_from_slice(&1u16.to_le_bytes()); // BPB_FSInfo = 1
        out[50..52].copy_from_slice(&6u16.to_le_bytes()); // BPB_BkBootSec = 6
        out[64] = 0x80; // BS_DrvNum
        out[66] = 0x29; // BS_BootSig
        out[67..71].copy_from_slice(&0x1234_5678u32.to_le_bytes()); // BS_VolID (overwritten per-mount)
        out[71..82].copy_from_slice(b"NO NAME    "); // BS_VolLab
        out[82..90].copy_from_slice(b"FAT32   "); // BS_FilSysType
        out[510..512].copy_from_slice(&[0x55, 0xAA]); // Signature
    }

    pub fn synthesize_fsinfo(out: &mut [u8; 512]) {
        out.fill(0);
        out[0..4].copy_from_slice(&[0x52, 0x52, 0x61, 0x41]); // FSI_LeadSig "RRaA"
        out[484..488].copy_from_slice(&[0x72, 0x72, 0x41, 0x61]); // FSI_StrucSig "rrAa"
        out[488..492].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes()); // FSI_Free_Count
        out[492..496].copy_from_slice(&3u32.to_le_bytes()); // FSI_Nxt_Free = 3
        out[510..512].copy_from_slice(&[0x55, 0xAA]); // FSI_TrailSig
    }

    /// Synthesizes or retrieves a 512-byte metadata/FAT sector.
    pub fn read_metadata_sector(&self, sec: u64, out: &mut [u8; 512]) {
        // Sector 0 is Main Boot Sector, Sector 6 is Backup Boot Sector
        if sec == 0 || sec == 6 {
            if let Some(tpl) = self.lookup(sec) {
                out.copy_from_slice(tpl);
            } else {
                Self::synthesize_boot(out);
            }
            // Stamp per-mount randomized Volume Serial Number (VSN) at offset 67..71 to invalidate TV MStar FAT cache
            out[67..71].copy_from_slice(&self.volume_id.to_le_bytes());
            return;
        }

        // Sector 1 is FSInfo
        if sec == 1 {
            Self::synthesize_fsinfo(out);
            return;
        }

        if let Some(data) = self.lookup(sec) {
            out.copy_from_slice(data);
            return;
        }

        // Other reserved sectors < RSV are zeroed
        if sec < RSV {
            out.fill(0);
            return;
        }

        // FAT1: [32 .. 32+8192), FAT2: [8224 .. 8224+8192)
        if (RSV..DATA_SEC).contains(&sec) {
            let sno = if sec >= RSV + SPF {
                sec - (RSV + SPF)
            } else {
                sec - RSV
            };
            let c0 = (sno * 128) as u32;

            for i in 0..128 {
                let cl = c0 + i as u32;
                let v = if cl == 0 {
                    0x0FFF_FFF8
                } else if cl == 1 {
                    0xFFFF_FFFF
                } else if cl == 2 {
                    0x0FFF_FFFF // Root directory EOC
                } else if !self.vod_files.is_empty() {
                    let mut val = 0u32;
                    for f in &self.vod_files {
                        if cl >= f.start_clus && cl <= f.end_clus {
                            val = if cl == f.end_clus {
                                0x0FFF_FFFF
                            } else {
                                cl + 1
                            };
                            break;
                        }
                    }
                    val
                } else if cl < self.last_clus {
                    cl + 1
                } else if cl == self.last_clus {
                    0x0FFF_FFFF
                } else {
                    0
                };

                let bytes = v.to_le_bytes();
                out[i * 4..i * 4 + 4].copy_from_slice(&bytes);
            }
            return;
        }

        // Unallocated or un-templated metadata sector is zeroed
        out.fill(0);
    }

    /// Finds the VOD file entry matching a cluster and calculates file offset.
    pub fn find_vod_file(&self, clus: u64) -> Option<(usize, u64)> {
        for (i, f) in self.vod_files.iter().enumerate() {
            let sc = u64::from(f.start_clus);
            let ec = u64::from(f.end_clus);
            if (sc..=ec).contains(&clus) {
                let foff = (clus - sc) * (SPC * BPS);
                return Some((i, foff));
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_geometry_constants() {
        assert_eq!(DATA_SEC, 16416);
        assert_eq!(FILE_SEC, 16424);
        assert_eq!(TOTSEC, 8405008);
        assert_eq!(DISK_SIZE, 4303364096);
    }

    #[test]
    fn test_fat_sector_synthesis() {
        let tmpl = FatTemplate::new();
        let mut sec_buf = [0u8; 512];

        // Sector 32 is FAT1 sector 0
        tmpl.read_metadata_sector(32, &mut sec_buf);

        // Cluster 0 = 0x0FFFFFF8, Cluster 1 = 0xFFFFFFFF, Cluster 2 = 0x0FFFFFFF
        assert_eq!(u32::from_le_bytes([sec_buf[0], sec_buf[1], sec_buf[2], sec_buf[3]]), 0x0FFFFFF8);
        assert_eq!(u32::from_le_bytes([sec_buf[4], sec_buf[5], sec_buf[6], sec_buf[7]]), 0xFFFFFFFF);
        assert_eq!(u32::from_le_bytes([sec_buf[8], sec_buf[9], sec_buf[10], sec_buf[11]]), 0x0FFFFFFF);

        // Cluster 3 = 4 (linear chain)
        assert_eq!(u32::from_le_bytes([sec_buf[12], sec_buf[13], sec_buf[14], sec_buf[15]]), 4);
    }

    #[test]
    fn test_boot_sector_synthesis() {
        let tmpl = FatTemplate::new();
        let mut sec_buf = [0u8; 512];

        // Sector 0 (Boot) and Sector 6 (Backup)
        for &sec in &[0, 6] {
            tmpl.read_metadata_sector(sec, &mut sec_buf);
            assert_eq!(&sec_buf[0..3], &[0xEB, 0x58, 0x90]);
            assert_eq!(&sec_buf[3..11], b"MSWIN4.1");
            assert_eq!(&sec_buf[510..512], &[0x55, 0xAA]);
        }

        // Sector 1 (FSInfo)
        tmpl.read_metadata_sector(1, &mut sec_buf);
        assert_eq!(&sec_buf[0..4], b"RRaA");
        assert_eq!(&sec_buf[484..488], b"rrAa");
        assert_eq!(&sec_buf[510..512], &[0x55, 0xAA]);
    }
}
