//! Host MVP for `Favoritos/Abertos/SBT.trp` and `Record.trp`.
//!
//! Host-only: no provider, network, Docker, USB or TV access. The test uses
//! two distinguishable synthetic MPEG-TS sources and drives the same layers a
//! future tablet worker would use:
//!
//! favorite ids -> manifest -> NTFS runlist extents -> probe detector ->
//! selection controller (source_epoch + framed data) -> published identity.
//!
//! It proves the host contracts, not physical playback. TV enumeration,
//! bootstrap decoding and `no_reconnect` remain pending hardware validation.

use fuse_ntfs::contract::TS_PKT_SZ;
use fuse_ntfs::favorites::detector::{DetectorConfig, ProbeDetector, ReadSpan};
use fuse_ntfs::favorites::digest::{sha256, sha256_hex};
use fuse_ntfs::favorites::extents::{ExtentMap, MapRegion};
use fuse_ntfs::favorites::manifest::VOLUME_128_GIB;
use fuse_ntfs::favorites::resolver::{
    resolve_favorites, CatalogChannel, ChannelKind, LiveCatalog, ResolverPolicy,
};
use fuse_ntfs::favorites::runtime::{
    FavoriteFile, FavoritesRuntime, RuntimeConfig, SourceTransport, TransportFailure,
};
use fuse_ntfs::favorites::selection::{
    DataFrame, SelectionController, SelectionError, SelectionRequest, SelectionState,
};
use fuse_ntfs::favorites::transport::{encode_frame, FrameVerifier};
use fuse_ntfs::vfs::ntfs_mft::{parse_runlist, RunPiece, CLUSTER_SIZE};

const MI: u64 = 1024 * 1024;
const K: u64 = 256 * 1024;
const SBT_ID: &str = "207198";
const RECORD_ID: &str = "214640";

#[test]
fn exporter_manifest_is_accepted_by_rust_and_matches_catalog_identity() {
    use fuse_ntfs::favorites::manifest::Manifest;
    use fuse_ntfs::favorites::resolver::stable_file_id;
    use std::{fs, process::Command};
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let temp = std::env::temp_dir().join(format!("favorites-export-{}", std::process::id()));
    fs::create_dir_all(&temp).unwrap();
    let layout = temp.join("layout.json");
    let output = temp.join("manifest.json");
    let export = temp.join("export.json");
    let _ = fs::remove_file(&output);
    fs::write(&layout, r#"{"files":[{"path":"/Abertos/SBT.trp","logical_size":4000000000},{"path":"/Abertos/Record.trp","logical_size":4000000000}]}"#).unwrap();
    fs::write(&export, r#"{"schema_version":1,"generation":1,"output_profile":"1080p30","identities":[{"identity":"sbt","category":"Abertos","filename":"SBT.trp","status":"approved","chosen":{"channel_id":"207198"},"candidates_tested":[{"channel_id":"207198"}]},{"identity":"record","category":"Abertos","filename":"Record.trp","status":"approved","chosen":{"channel_id":"214640"},"candidates_tested":[{"channel_id":"214640"}]}]}"#).unwrap();
    let status = Command::new("python3")
        .arg(root.join("scripts/build_live_favorites_manifest.py"))
        .args(["--layout"]).arg(&layout)
        .args(["--output"]).arg(&output)
        .args(["--export"]).arg(&export)
        .status().unwrap();
    assert!(status.success());
    let manifest = Manifest::from_json(&fs::read(&output).unwrap()).unwrap();
    for entry in &manifest.entries {
        assert_eq!(entry.file_id, stable_file_id("live", &entry.channel_id));
        assert_eq!(entry.folder, vec!["Abertos".to_string()]);
        assert_eq!(entry.path(), format!("Abertos/{}", entry.filename));
    }
    let _ = fs::remove_dir_all(temp);
}

struct FakeCatalog {
    channels: Vec<CatalogChannel>,
}

impl FakeCatalog {
    fn mvp() -> Self {
        Self {
            channels: vec![
                CatalogChannel {
                    namespace: "live".into(),
                    channel_id: SBT_ID.into(),
                    name: "SBT".into(),
                    category: vec!["Abertos".into()],
                    kind: ChannelKind::Live,
                },
                CatalogChannel {
                    namespace: "live".into(),
                    channel_id: RECORD_ID.into(),
                    name: "Record".into(),
                    category: vec!["Abertos".into()],
                    kind: ChannelKind::Live,
                },
            ],
        }
    }
}

impl LiveCatalog for FakeCatalog {
    fn channel(&self, namespace: &str, channel_id: &str) -> Option<CatalogChannel> {
        self.channels
            .iter()
            .find(|c| c.namespace == namespace && c.channel_id == channel_id)
            .cloned()
    }
}

/// Encodes a real NTFS runlist (4-byte length + 4-byte signed LCN delta) and
/// decodes it through the production parser, so the test does not fabricate
/// `RunPiece` values behind the parser's back.
fn runs(lcn: u64, clusters: u64) -> Vec<RunPiece> {
    let mut encoded = vec![0x44];
    encoded.extend_from_slice(&(clusters as u32).to_le_bytes());
    encoded.extend_from_slice(&(lcn as i32).to_le_bytes());
    encoded.push(0);
    parse_runlist(&encoded)
}

fn fake_ts(marker: u8, packets: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(packets * TS_PKT_SZ);
    for i in 0..packets {
        let mut packet = [0u8; TS_PKT_SZ];
        packet[0] = 0x47;
        packet[1] = if marker == 0xa1 { 0x01 } else { 0x02 };
        packet[2] = 0xff;
        packet[3] = 0x10 | (i & 0x0f) as u8;
        packet[4..].fill(marker);
        out.extend_from_slice(&packet);
    }
    out
}

fn frame(request: &SelectionRequest, sequence: u64, payload: Vec<u8>) -> DataFrame {
    DataFrame {
        session_id: request.session_id.clone(),
        manifest_id: request.manifest_id.clone(),
        file_id: request.file_id,
        channel_id: request.channel_id.clone(),
        request_generation: request.request_generation,
        source_epoch: request.source_epoch,
        sequence,
        payload,
    }
}

/// Feeds four 256 KiB opening requests through the real disk router. Only
/// `File` segments belonging to `file_id` become detector spans; metadata and
/// holes never reach the detector.
fn feed_opening_reads(
    map: &ExtentMap,
    detector: &mut ProbeDetector,
    file_id: u32,
    disk_start: u64,
    now_base: u64,
) -> Option<fuse_ntfs::favorites::detector::SelectionIntent> {
    let mut last = None;
    for i in 0..4u64 {
        let disk_offset = disk_start + i * K;
        let segments = map.route_read(disk_offset, K).expect("route");
        let spans: Vec<ReadSpan> = segments
            .iter()
            .filter_map(|segment| match segment.region {
                MapRegion::File {
                    file_id: id,
                    file_offset,
                    len,
                } if id == file_id => Some(ReadSpan::new(file_offset, len)),
                _ => None,
            })
            .collect();
        last = detector.observe_request(file_id, &spans, now_base + i * 100);
    }
    last
}

#[test]
fn mvp_sbt_record_manifest_extents_detector_and_epoch_commit() {
    // 1. Transport-only fixtures with fully allocated, small test files.
    // Their tagged TS payloads test identity, not PAT/PMT/IDR decodability.
    let mut policy = ResolverPolicy::mvp_two_channels();
    policy.default_logical_size = MI;
    let resolved = resolve_favorites(
        &[SBT_ID.into(), RECORD_ID.into()],
        &FakeCatalog::mvp(),
        7,
        &policy,
    )
    .expect("resolve SBT/Record");
    let manifest = resolved.manifest;
    manifest.validate().expect("manifest valid");
    assert_eq!(manifest.entries.len(), 2);
    let sbt = manifest
        .entries
        .iter()
        .find(|e| e.channel_id == SBT_ID)
        .expect("SBT");
    let record = manifest
        .entries
        .iter()
        .find(|e| e.channel_id == RECORD_ID)
        .expect("Record");
    assert_eq!(sbt.filename, "SBT.trp");
    assert_eq!(record.filename, "Record.trp");
    assert_eq!(sbt.path(), "Favoritos/Abertos/SBT.trp");
    assert_eq!(record.path(), "Favoritos/Abertos/Record.trp");
    assert_eq!(manifest.manifest_id.len(), 64);

    // 2. NTFS extent map derived from runlist pieces. SBT starts at LCN 256
    //    (1 MiB), Record at LCN 1024 (4 MiB); metadata occupies [0, 1 MiB).
    let mut map = ExtentMap::new(16 * MI).expect("map");
    map.add_metadata(0, MI).expect("metadata");
    map.add_file_runs(sbt.file_id, &runs(256, 256), sbt.logical_size)
        .expect("SBT extent");
    map.add_file_runs(record.file_id, &runs(1024, 256), record.logical_size)
        .expect("Record extent");

    // A read crossing metadata -> SBT keeps the file identity per segment.
    let crossing = map.route_read(MI - 8, 8 + K).expect("crossing");
    assert_eq!(crossing[0].region, MapRegion::Metadata { len: 8 });
    assert!(matches!(
        crossing[1].region,
        MapRegion::File {
            file_id,
            file_offset: 0,
            len
        } if file_id == sbt.file_id && len == K
    ));

    // 3. First channel: no implicit active file. Four distinct opening
    //    requests are needed (3 requests and >= 1 MiB union coverage).
    let mut detector = ProbeDetector::new(DetectorConfig::default());
    assert_eq!(detector.active(), None);
    let sbt_intent =
        feed_opening_reads(&map, &mut detector, sbt.file_id, MI, 0).expect("SBT intent");
    assert_eq!(sbt_intent.file_id, sbt.file_id);
    assert_eq!(sbt_intent.covered_bytes, MI);
    assert_eq!(sbt_intent.useful_requests, 4);

    // 4. Selection: verified data + source-ready + tablet ACK commit the
    //    exact SBT bytes. An ACK before data cannot publish.
    let mut selection = SelectionController::new();
    let sbt_bytes = fake_ts(0xa1, 8);
    let record_bytes = fake_ts(0xb2, 8);
    let sbt_req = selection
        .request("tablet-session", &manifest.manifest_id, sbt.file_id, SBT_ID)
        .expect("SBT request");
    selection
        .mark_source_ready(
            sbt_req.request_generation,
            sbt_req.source_epoch,
            (0, sbt_bytes.len() as u64),
            1000,
        )
        .expect("SBT source ready");
    assert_eq!(
        selection.tablet_ack(sbt_req.request_generation, sbt_req.source_epoch),
        Err(SelectionError::DataNotVerified)
    );
    selection
        .accept_data_frame(&frame(&sbt_req, 1, sbt_bytes.clone()))
        .expect("SBT data");
    assert_eq!(selection.state(), SelectionState::Ready);
    selection
        .tablet_ack(sbt_req.request_generation, sbt_req.source_epoch)
        .expect("SBT ack");
    let sbt_published = selection.commit(1100).expect("SBT commit");
    assert_eq!(sbt_published.channel_id, SBT_ID);
    assert_eq!(sbt_published.anchor, 0);
    assert_eq!(sbt_published.data_digest, sha256(&sbt_bytes));
    assert_ne!(sbt_published.data_digest, sha256(&record_bytes));

    // 5. SBT -> Record on the same volume: a new intent/epoch is prepared and
    //    the old active tuple stays visible until the new commit.
    detector.on_committed(sbt.file_id, 1100);
    let record_intent = feed_opening_reads(&map, &mut detector, record.file_id, 4 * MI, 3200)
        .expect("Record intent");
    assert_eq!(record_intent.file_id, record.file_id);

    let record_req = selection
        .request(
            "tablet-session",
            &manifest.manifest_id,
            record.file_id,
            RECORD_ID,
        )
        .expect("Record request");
    assert!(record_req.request_generation > sbt_req.request_generation);
    assert!(record_req.source_epoch > sbt_req.source_epoch);
    assert_eq!(selection.active().unwrap().channel_id, SBT_ID);

    // Stale SBT bytes cannot satisfy the Record preparation.
    let stale = frame(&sbt_req, 2, fake_ts(0xa1, 8));
    assert!(matches!(
        selection.accept_data_frame(&stale),
        Err(SelectionError::StaleGeneration { .. })
    ));
    let mut wrong = record_req.clone();
    wrong.channel_id = SBT_ID.into();
    let wrong_channel = frame(&wrong, 1, fake_ts(0xa1, 8));
    assert_eq!(
        selection.accept_data_frame(&wrong_channel),
        Err(SelectionError::IdentityMismatch("channel_id"))
    );

    selection
        .mark_source_ready(
            record_req.request_generation,
            record_req.source_epoch,
            (0, record_bytes.len() as u64),
            3600,
        )
        .expect("Record source ready");
    selection
        .accept_data_frame(&frame(&record_req, 1, record_bytes.clone()))
        .expect("Record data");
    selection
        .tablet_ack(record_req.request_generation, record_req.source_epoch)
        .expect("Record ack");
    let record_published = selection.commit(3700).expect("Record commit");
    assert_eq!(record_published.channel_id, RECORD_ID);
    assert_eq!(record_published.data_digest, sha256(&record_bytes));
    assert_ne!(record_published.data_digest, sbt_published.data_digest);
    assert_eq!(selection.state(), SelectionState::Active);
    assert_eq!(selection.active().unwrap(), &record_published);
    assert_eq!(sha256_hex(&record_bytes).len(), 64);
}

#[test]
fn metadata_and_unmapped_reads_never_become_detector_evidence() {
    let mut map = ExtentMap::new(8 * MI).expect("map");
    map.add_metadata(0, MI).expect("metadata");
    map.add_file_runs(7, &runs(512, 4), 4 * CLUSTER_SIZE as u64)
        .expect("file extent");

    let mut detector = ProbeDetector::new(DetectorConfig::default());
    detector.set_active(Some(99));

    // Repeated metadata/hole reads are not file evidence.
    for i in 0..8u64 {
        let segments = map.route_read(i * K, K).expect("metadata route");
        for segment in segments {
            if let MapRegion::File {
                file_id,
                file_offset,
                len,
            } = segment.region
            {
                let _ =
                    detector.observe_request(file_id, &[ReadSpan::new(file_offset, len)], i * 100);
            }
        }
    }
    assert_eq!(detector.candidate_count(), 0);
    assert_eq!(detector.active(), Some(99));

    // The virtual volume is far smaller than the 128 GiB production volume;
    // the manifest volume constant stays the production geometry.
    assert_eq!(VOLUME_128_GIB, 128 * 1024 * 1024 * 1024);
    assert!(map.volume_bytes() < VOLUME_128_GIB);
    let _ = CLUSTER_SIZE;
}

struct FrameTransport {
    token: String,
    verifier: Option<FrameVerifier>,
    wire: std::collections::VecDeque<Vec<u8>>,
    confirmation: Option<(u64, u64)>,
    cancelled: u64,
}

impl FrameTransport {
    fn new(token: &str) -> Self {
        Self {
            token: token.into(),
            verifier: None,
            wire: std::collections::VecDeque::new(),
            confirmation: None,
            cancelled: 0,
        }
    }
}

impl SourceTransport for FrameTransport {
    fn open(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure> {
        self.wire.clear();
        let payload = include_bytes!("../assets/live_favorites_bootstrap.ts");
        for (index, chunk) in payload.chunks((256 * 1024 / TS_PKT_SZ) * TS_PKT_SZ).enumerate() {
        let frame = DataFrame {
            session_id: request.session_id.clone(),
            manifest_id: request.manifest_id.clone(),
            file_id: request.file_id,
            channel_id: request.channel_id.clone(),
            request_generation: request.request_generation,
            source_epoch: request.source_epoch,
            sequence: index as u64 + 1,
            payload: chunk.to_vec(),
        };
        self.wire.push_back(
            encode_frame(&frame, frame.sequence, &self.token)
                .map_err(|error| TransportFailure(format!("{error:?}")))?,
        );
        }
        self.verifier = Some(
            FrameVerifier::new(request.clone(), &self.token)
                .map_err(|error| TransportFailure(format!("{error:?}")))?,
        );
        self.confirmation = Some((request.request_generation, request.source_epoch));
        Ok(())
    }

    fn poll(&mut self) -> Result<Option<DataFrame>, TransportFailure> {
        let Some(wire) = self.wire.pop_front() else {
            return Ok(None);
        };
        self.verifier
            .as_mut()
            .ok_or_else(|| TransportFailure("not open".into()))?
            .verify(&wire)
            .map(Some)
            .map_err(|error| TransportFailure(format!("{error:?}")))
    }

    fn cancel(&mut self, _request: &SelectionRequest) {
        self.cancelled += 1;
        self.wire.clear();
        self.verifier = None;
        self.confirmation = None;
    }

    fn confirmation(&self) -> Option<(u64, u64)> {
        self.confirmation
    }
}

#[test]
fn runtime_transport_and_fuse_path_commit_the_detected_file() {
    let mut encoded = vec![0x44];
    encoded.extend_from_slice(&(2048u32).to_le_bytes());
    encoded.extend_from_slice(&(256i32).to_le_bytes());
    encoded.push(0);
    let sbt_runs = parse_runlist(&encoded);
    let mut encoded = vec![0x44];
    encoded.extend_from_slice(&(2048u32).to_le_bytes());
    encoded.extend_from_slice(&(4096i32).to_le_bytes());
    encoded.push(0);
    let record_runs = parse_runlist(&encoded);
    let mut map = ExtentMap::new(32 * MI).unwrap();
    map.add_metadata(0, MI).unwrap();
    map.add_file_runs(7, &sbt_runs, 8 * MI).unwrap();
    map.add_file_runs(8, &record_runs, 8 * MI).unwrap();
    let runtime = FavoritesRuntime::new(
        map,
        vec![
            FavoriteFile {
                file_id: 7,
                channel_id: "sbt".into(),
                path: "Favoritos/Abertos/SBT.trp".into(),
                logical_size: 8 * MI,
            },
            FavoriteFile {
                file_id: 8,
                channel_id: "record".into(),
                path: "Favoritos/Abertos/Record.trp".into(),
                logical_size: 8 * MI,
            },
        ],
        {
            let mut config = RuntimeConfig::new("session", "manifest");
            config.detector_config.debounce_ms = 0;
            config
        },
    )
    .unwrap();

    // Four opening reads emulate the TV block-device walk; only then does the
    // worker get an intent. No network work happens inside serve_file.
    let mut buf = vec![0u8; 256 * 1024];
    for offset in [0u64, 256 * 1024, 512 * 1024, 768 * 1024] {
        runtime.serve_file(7, offset, &mut buf);
    }
    assert_eq!(runtime.pending_intent().unwrap().file_id, 7);
    assert_eq!(runtime.active(), None);

    let mut transport = FrameTransport::new("secret");
    let report = runtime.process(&mut transport, runtime.now_ms());
    assert!(report.opened, "{report:?}");
    assert_eq!(report.committed.as_ref().map(|p| p.file_id), Some(7));
    assert_eq!(runtime.active().unwrap().file_id, 7);
    assert_eq!(runtime.state(), SelectionState::Active);
    assert_eq!(transport.cancelled, 0);

    // A later intent for the other file cancels/reopens exactly once and
    // commits only the new epoch. The active file's earlier prefix is not
    // reused for the other identity.
    for offset in [0u64, 256 * 1024, 512 * 1024, 768 * 1024] {
        runtime.serve_file(8, offset, &mut buf);
    }
    let report = runtime.process(&mut transport, runtime.now_ms());
    assert_eq!(report.committed.as_ref().map(|p| p.file_id), Some(8));
    assert_eq!(transport.cancelled, 1);
    assert_eq!(runtime.active().unwrap().file_id, 8);

    // The FUSE-facing disk route still resolves each identity independently.
    let sbt = runtime.map().file_extents(7)[0];
    assert!(matches!(
        runtime.route(sbt.disk_start),
        MapRegion::File { file_id: 7, .. }
    ));
}

/// The deployment package is generated under `scratch/` (not committed). This
/// test deliberately **fails** — never silently skips — when the chosen
/// package or any of its artifacts is missing: a green run must mean the real
/// package passed the same verifiers the tablet startup path uses.
const CHOSEN_PACKAGE: &str = "../../scratch/mixed-staging/mixed-gen2-32abe7d7";
const CHOSEN_PACKAGE_FALLBACK: &str = "scratch/mixed-staging/mixed-gen2-32abe7d7";

const REQUIRED_ARTIFACTS: [&str; 6] = [
    "favorites_manifest.json",
    "favorites_layout.json",
    "favorites_package.json",
    "vod_allowlist.json",
    "SHA256SUMS",
    "favorites_template.sparse.gz",
];

/// `MIXED_STAGING_DIR` wins; otherwise the crate-relative path with the
/// repository-root fallback used by the previous test.
fn chosen_package_dir() -> std::path::PathBuf {
    use std::path::{Path, PathBuf};

    if let Ok(value) = std::env::var("MIXED_STAGING_DIR") {
        let path = PathBuf::from(value);
        assert!(
            path.is_dir(),
            "MIXED_STAGING_DIR={} não é um diretório",
            path.display()
        );
        return path;
    }
    for candidate in [Path::new(CHOSEN_PACKAGE), Path::new(CHOSEN_PACKAGE_FALLBACK)] {
        if candidate.is_dir() {
            return candidate.to_path_buf();
        }
    }
    panic!(
        "pacote escolhido ausente: {CHOSEN_PACKAGE} (defina MIXED_STAGING_DIR para outro pacote)"
    )
}

fn read_required(dir: &std::path::Path, name: &str) -> Vec<u8> {
    let path = dir.join(name);
    std::fs::read(&path).unwrap_or_else(|error| {
        panic!(
            "artefato obrigatório ausente ou ilegível: {} ({error})",
            path.display()
        )
    })
}

/// Verifies every `SHA256SUMS` entry against the actual bytes using the
/// crate's own SHA-256 (the crate has no external hashing dependency), and
/// proves the index covers every required artifact.
fn verify_sha256sums(dir: &std::path::Path) {
    let text = String::from_utf8(read_required(dir, "SHA256SUMS"))
        .expect("SHA256SUMS não é UTF-8");
    let mut checked = 0usize;
    for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
        let (expected, name) = line
            .split_once("  ")
            .unwrap_or_else(|| panic!("linha SHA256SUMS inválida: {line:?}"));
        assert_eq!(expected.len(), 64, "digest não canônico em {line:?}");
        assert!(
            expected
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "digest não canônico em {line:?}"
        );
        assert!(
            !name.starts_with('/') && !name.split('/').any(|part| part == ".."),
            "caminho inseguro no SHA256SUMS: {name}"
        );
        let bytes = std::fs::read(dir.join(name))
            .unwrap_or_else(|error| panic!("SHA256SUMS cita {name}, ilegível: {error}"));
        assert_eq!(sha256_hex(&bytes), expected, "hash divergente para {name}");
        checked += 1;
    }
    assert!(
        checked >= REQUIRED_ARTIFACTS.len(),
        "SHA256SUMS tem apenas {checked} entradas"
    );
    for name in REQUIRED_ARTIFACTS
        .iter()
        .copied()
        .filter(|name| *name != "SHA256SUMS")
    {
        assert!(
            text.lines()
                .any(|line| line.trim_end().ends_with(&format!("  {name}"))),
            "SHA256SUMS não cobre {name}"
        );
    }
}

/// Structural proof of the chosen package with the Rust startup verifiers:
/// `SHA256SUMS`, `Package::verify` (template archive + layout digests,
/// `package_id`, path coverage), `Manifest::from_json`/`validate`,
/// `Layout::parse`/`build_runtime_files` and `parse_allowlist`.
///
/// It does not mount anything: the physical NTFS enumeration by the Samsung
/// decoder remains hardware validation.
#[test]
fn chosen_staging_package_validates_with_startup_verifiers() {
    use fuse_ntfs::favorites::layout::{manifest_file_ids, Layout, Package};
    use fuse_ntfs::favorites::manifest::{EntryKind, Manifest};
    use fuse_ntfs::mixed_vod_http::{parse_allowlist, MixedVodHttp};

    let dir = chosen_package_dir();
    // Fail fast, naming the first missing artifact.
    for name in REQUIRED_ARTIFACTS {
        read_required(&dir, name);
    }

    // 1. Hashes and package digests, with the same `Package::verify` the
    //    startup path runs before touching `/dev/fuse`.
    verify_sha256sums(&dir);
    let manifest_bytes = read_required(&dir, "favorites_manifest.json");
    let layout_bytes = read_required(&dir, "favorites_layout.json");
    let package_bytes = read_required(&dir, "favorites_package.json");
    let archive_bytes = read_required(&dir, "favorites_template.sparse.gz");
    let allowlist_bytes = read_required(&dir, "vod_allowlist.json");

    let layout = Layout::parse(&layout_bytes).expect("layout inválido");
    let package = Package::parse(&package_bytes).expect("favorites_package.json inválido");
    package
        .verify(&archive_bytes, &layout_bytes, &layout)
        .expect("pacote não casa com o template e o layout");
    // The archive fields declared by the package must describe this archive.
    assert_eq!(package.template_sparse_bytes, archive_bytes.len() as u64);
    assert_eq!(package.template_sparse_sha256, sha256_hex(&archive_bytes));
    assert_eq!(package.layout_bytes, layout_bytes.len() as u64);
    assert_eq!(package.layout_sha256, sha256_hex(&layout_bytes));
    assert_eq!(package.volume_bytes, layout.volume_bytes);
    assert_eq!(layout.volume_bytes, 128 * 1024 * 1024 * 1024);

    // 2. Manifest identity and composition.
    let manifest = Manifest::from_json(&manifest_bytes).expect("manifesto inválido");
    manifest.validate().expect("manifesto não fecha");
    assert_eq!(manifest.manifest_id, manifest.recompute_id());
    assert_eq!(manifest.entries.len(), 122);
    let live = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == EntryKind::Live)
        .count();
    let vod = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == EntryKind::Vod)
        .count();
    assert_eq!((live, vod), (2, 120));

    // 3. Manifest <-> layout binding, through the startup call.
    let map = manifest_file_ids(&manifest_bytes).expect("identidades do manifesto inválidas");
    assert_eq!(map.manifest_id, manifest.manifest_id);
    assert_eq!(map.vod_files.len(), 120);
    assert_eq!(layout.files.len(), 122);
    assert_eq!(package.files.len(), layout.files.len());
    let (_extent_map, files) = layout
        .build_runtime_files(&map.files)
        .expect("layout não casa com o manifesto");
    assert_eq!(files.len(), 122);
    let vod_ids: std::collections::HashSet<u32> =
        map.vod_files.iter().map(|file| file.file_id).collect();
    assert_eq!(
        files
            .iter()
            .filter(|file| !vod_ids.contains(&file.file_id))
            .count(),
        2
    );

    // 4. The real `vod_allowlist.json` passes the startup verifier with sizes
    //    derived from the verified manifest, and the client pins that map.
    let sizes: std::collections::HashMap<String, u64> = map
        .vod_files
        .iter()
        .map(|file| (file.task_id.clone(), file.logical_size))
        .collect();
    assert_eq!(sizes.len(), 120);
    let allowlist =
        parse_allowlist(&allowlist_bytes, &sizes).expect("allowlist do pacote rejeitada");
    assert_eq!(allowlist.len(), 120);
    for file in &map.vod_files {
        let (size, revision) = allowlist
            .get(&file.task_id)
            .unwrap_or_else(|| panic!("task VOD {} fora da allowlist", file.task_id));
        assert_eq!(*size, file.logical_size);
        assert!(
            revision.starts_with("md5:")
                || revision.starts_with("fs:")
                || revision.starts_with("rev:")
        );
    }
    let endpoint: std::net::SocketAddr = "127.0.0.1:9".parse().unwrap();
    MixedVodHttp::new(
        endpoint,
        "0123456789abcdef0123456789abcdef".into(),
        std::time::Duration::from_secs(1),
    )
    .unwrap()
    .with_allowlist(allowlist)
    .expect("cliente VOD rejeitou a allowlist validada");
}
