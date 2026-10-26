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
    let _ = fs::remove_file(&output);
    fs::write(&layout, r#"{"files":[{"path":"/Favoritos/Abertos/SBT.trp","logical_size":4000000000},{"path":"/Favoritos/Abertos/Record.trp","logical_size":4000000000}]}"#).unwrap();
    let status = Command::new("python3")
        .arg(root.join("scripts/build_live_favorites_manifest.py"))
        .arg(&layout).arg(&output).args(["--sbt", SBT_ID, "--record", RECORD_ID])
        .status().unwrap();
    assert!(status.success());
    let manifest = Manifest::from_json(&fs::read(&output).unwrap()).unwrap();
    for entry in &manifest.entries {
        assert_eq!(entry.file_id, stable_file_id("live", &entry.channel_id));
        assert_eq!(entry.path(), format!("Favoritos/Abertos/{}", entry.filename));
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
