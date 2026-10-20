"""Host-only tests for the pure TASK-006 Phase B TS readiness probe."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ts_candidate_probe import (  # noqa: E402
    NEED_MORE, READY, REJECT, TsCandidateProbe, mpeg_crc32,
)

PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
AUDIO_PID = 0x0101


def _crc(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 4
    section = bytes([table_id, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF]) + body
    return section + _crc(section).to_bytes(4, "big")


def _pat_section(pmt_pid: int = PMT_PID) -> bytes:
    return _section(0x00, b"\x00\x01\xc1\x00\x00" +
                    b"\x00\x01" + bytes([0xE0 | ((pmt_pid >> 8) & 0x1F), pmt_pid & 0xFF]))


def _pmt_section(audio: str = "ac3", video_pid: int = VIDEO_PID,
                 video_type: int = 0x1B) -> bytes:
    body = b"\x00\x01\xc1\x00\x00" + bytes([0xE0 | ((video_pid >> 8) & 0x1F), video_pid & 0xFF]) + b"\xf0\x00"
    body += bytes([video_type, 0xE0 | ((video_pid >> 8) & 0x1F), video_pid & 0xFF, 0xF0, 0x00])
    if audio == "ac3":
        body += bytes([0x81, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, 0x00])
    elif audio == "private_ac3":
        desc = b"\x6a\x04\x40\x00\x00\x00"
        body += bytes([0x06, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, len(desc)]) + desc
    elif audio == "private_no_desc":
        body += bytes([0x06, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, 0x00])
    elif audio == "aac":
        body += bytes([0x0F, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, 0x00])
    elif audio == "eac3":
        body += bytes([0x87, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, 0x00])
    elif audio in ("mixed_aac_ac3", "mixed_ac3_aac"):
        aac_pid = AUDIO_PID + 1
        aac = bytes([0x0F, 0xE0 | ((aac_pid >> 8) & 0x1F), aac_pid & 0xFF, 0xF0, 0x00])
        ac3 = bytes([0x81, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, 0x00])
        body += aac + ac3 if audio == "mixed_aac_ac3" else ac3 + aac
    elif audio == "private_reg_ac3":
        desc = b"\x05\x04AC-3"
        body += bytes([0x06, 0xE0 | ((AUDIO_PID >> 8) & 0x1F), AUDIO_PID & 0xFF, 0xF0, len(desc)]) + desc
    return _section(0x02, body)


def _ts_packet(pid: int, payload: bytes, pusi: bool, cc: int = 0) -> bytes:
    if len(payload) > 184:
        raise ValueError("payload too large")
    header = bytes([
        0x47,
        (0x40 if pusi else 0x00) | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (0x10 if len(payload) == 184 else 0x30) | (cc & 0x0F),
    ])
    if len(payload) == 184:
        return header + payload
    adaptation_length = 183 - len(payload)
    return header + bytes([adaptation_length, 0x00]) + b"\xff" * (adaptation_length - 1) + payload


def _psi_packets(pid: int, section: bytes, split=None) -> bytes:
    if split is None:
        return _ts_packet(pid, b"\x00" + section, True, 0)
    return (_ts_packet(pid, b"\x00" + section[:split], True, 0) +
            _ts_packet(pid, section[split:], False, 1))


def _pes(es: bytes) -> bytes:
    return b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + es


def _pes_packets(pid: int, pes: bytes, first_size: int = 184) -> bytes:
    chunks = []
    if first_size and len(pes) > first_size:
        chunks.append(pes[:first_size])
        rest = pes[first_size:]
    else:
        rest = pes
    chunks.extend(rest[i:i + 184] for i in range(0, len(rest), 184))
    if not chunks:
        chunks = [b""]
    return b"".join(_ts_packet(pid, chunk, index == 0, index)
                    for index, chunk in enumerate(chunks))


def _keyframe_es() -> bytes:
    return (b"\x00\x00\x00\x01\x09\xf0"
            b"\x00\x00\x00\x01\x67\x42\x00\x1e\x95\xa8\x14\x01\x6e\x9b\x80"
            b"\x00\x00\x00\x01\x68\xce\x3c\x80"
            b"\x00\x00\x00\x01\x65\x88\x84\x00\x0a\xff\xff")


def _nonkey_es() -> bytes:
    return b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x41\x9a\x22\x00\xff"


def _stream(audio="ac3", keyframe=True, extra_nonkey=False,
            psi_recovery=False, split_psi=None, first_pes_size=184,
            video_type=0x1B) -> bytes:
    pat = _pat_section()
    pmt = _pmt_section(audio, video_type=video_type)
    packets = _psi_packets(PAT_PID, pat, split_psi) + _psi_packets(PMT_PID, pmt, split_psi)
    if extra_nonkey:
        packets += _pes_packets(VIDEO_PID, _pes(_nonkey_es() + b"FIRSTNONKEYMARKER"))
    if psi_recovery:
        packets += _psi_packets(PAT_PID, pat) + _psi_packets(PMT_PID, pmt)
    packets += _pes_packets(VIDEO_PID, _pes(_keyframe_es() if keyframe else _nonkey_es()), first_pes_size)
    packets += _pes_packets(VIDEO_PID, _pes(_nonkey_es()))
    return packets


def _feed_chunks(data: bytes, sizes=(4096,)):
    probe = TsCandidateProbe()
    result = None
    index = 0
    while index < len(data):
        size = sizes[index % len(sizes)]
        result = probe.feed(data[index:index + size])
        index += size
        if result.status != NEED_MORE:
            return probe, result
    return probe, result or probe.feed(b"")


def test_ready_with_81_audio_and_fragmented_reads():
    data = _stream("ac3")
    for sizes in ((1,), (7, 188), (188,), (333, 4096)):
        probe, result = _feed_chunks(data, sizes)
        assert result.status == READY, (sizes, result)
        assert result.video_pid == VIDEO_PID
        assert result.audio_pid == AUDIO_PID
        assert result.audio_kind == "ac3"
        assert result.selected[:188] == _ts_packet(PAT_PID, b"\x00" + _pat_section(), True, 0)
        assert len(result.selected) % 188 == 0


def test_private_06_with_ac3_descriptor_is_accepted():
    _, result = _feed_chunks(_stream("private_ac3"))
    assert result.status == READY
    assert result.audio_kind == "ac3"


def test_mixed_audio_reports_the_ac3_pid_in_both_orders():
    for variant in ("mixed_aac_ac3", "mixed_ac3_aac"):
        _, result = _feed_chunks(_stream(variant))
        assert result.status == READY, (variant, result)
        assert result.audio_kind == "ac3"
        assert result.audio_pid == AUDIO_PID


def test_private_registration_descriptor_ac3_is_accepted():
    _, result = _feed_chunks(_stream("private_reg_ac3"))
    assert result.status == READY
    assert result.audio_kind == "ac3"
    assert result.audio_pid == AUDIO_PID


def test_single_oversized_chunk_is_rejected_before_copy():
    class NoExtend(bytearray):
        def extend(self, data):
            raise AssertionError("oversized chunk was copied before rejection")

    probe = TsCandidateProbe(max_bytes=188 * 4)
    probe._buf = NoExtend()
    result = probe.feed(b"\x47" * (188 * 4 + 1))
    assert result.status == REJECT
    assert result.reason == "buffer_limit"


def test_ready_exposes_absolute_offsets_and_trailing_bytes():
    data = _stream()
    probe = TsCandidateProbe()
    result = probe.feed(data)
    assert result.status == READY
    assert result.selected_start == 0
    assert result.selected_end == len(result.selected)
    assert result.selected + result.trailing == data

    prefix = b"leading-garbage" * 7
    probe = TsCandidateProbe()
    result = probe.feed(prefix + data)
    assert result.status == READY
    assert result.selected_start == len(prefix)
    assert result.selected_end == len(prefix) + len(result.selected)
    assert result.selected + result.trailing == (prefix + data)[len(prefix):]


def test_private_06_without_ac3_descriptor_is_rejected():
    probe, result = _feed_chunks(_stream("private_no_desc"))
    assert result.status in {REJECT, NEED_MORE}
    assert probe.finish().reason == "unsupported_audio"


def test_eac3_audio_is_rejected_as_unsupported():
    probe, result = _feed_chunks(_stream("eac3"))
    assert result.status in {REJECT, NEED_MORE}
    assert probe.finish().reason == "unsupported_audio"


def test_video_only_is_accepted_with_explicit_none_audio():
    _, result = _feed_chunks(_stream("none"))
    assert result.status == READY
    assert result.audio_kind == "none"
    assert result.audio_pid is None


def test_aac_audio_is_rejected():
    probe, result = _feed_chunks(_stream("aac"))
    assert result.status in {REJECT, NEED_MORE}
    assert probe.finish().reason == "unsupported_audio"


def test_non_h264_pmt_is_rejected():
    probe, result = _feed_chunks(_stream(video_type=0x02))
    assert result.status in {REJECT, NEED_MORE}
    assert probe.finish().reason == "no_h264"


def test_empty_and_packet_without_psi_are_rejected():
    empty = TsCandidateProbe()
    assert empty.finish().reason == "empty"
    no_psi = TsCandidateProbe()
    no_psi.feed(b"".join(_ts_packet(VIDEO_PID, b"\x00" * 184, True, i) for i in range(3)))
    assert no_psi.finish().reason == "missing_pat"


def test_no_keyframe_is_rejected():
    probe, result = _feed_chunks(_stream(keyframe=False))
    assert result.status == REJECT or result.status == NEED_MORE
    final = probe.finish()
    assert final.status == REJECT
    assert final.reason in {"no_keyframe", "incomplete_access_unit"}


def test_keyframe_without_sps_or_pps_is_rejected():
    es = b"\x00\x00\x00\x01\x68\xce\x3c\x80\x00\x00\x00\x01\x65\x88\x84\x00\x0a"
    data = (_psi_packets(PAT_PID, _pat_section()) +
            _psi_packets(PMT_PID, _pmt_section()) +
            _pes_packets(VIDEO_PID, _pes(es)) +
            _pes_packets(VIDEO_PID, _pes(_nonkey_es())))
    probe, result = _feed_chunks(data)
    assert result.status == REJECT or result.status == NEED_MORE
    assert probe.finish().reason in {"no_keyframe", "incomplete_access_unit"}


def test_non_idr_before_idr_in_same_access_unit_is_rejected():
    es = (b"\x00\x00\x00\x01\x67\x42\x00\x1e\x95\xa8"
          b"\x00\x00\x00\x01\x68\xce\x3c\x80"
          b"\x00\x00\x00\x01\x41\x9a\x22\x00"
          b"\x00\x00\x00\x01\x65\x88\x84\x00\x0a")
    data = (_psi_packets(PAT_PID, _pat_section()) +
            _psi_packets(PMT_PID, _pmt_section()) +
            _pes_packets(VIDEO_PID, _pes(es)) +
            _pes_packets(VIDEO_PID, _pes(_nonkey_es())))
    probe, result = _feed_chunks(data)
    assert result.status == REJECT or result.status == NEED_MORE
    assert probe.finish().reason in {"no_keyframe", "incomplete_access_unit"}


def test_keyframe_after_video_without_fresh_psi_is_rejected():
    probe, result = _feed_chunks(_stream(extra_nonkey=True))
    assert result.status == REJECT or result.status == NEED_MORE
    final = probe.finish()
    assert final.status == REJECT
    assert final.reason in {"no_keyframe", "psi_not_adjacent", "incomplete_access_unit"}


def test_fresh_psi_before_keyframe_recovers_selection():
    _, result = _feed_chunks(_stream(extra_nonkey=True, psi_recovery=True))
    assert result.status == READY
    assert result.selected[:188] == _ts_packet(PAT_PID, b"\x00" + _pat_section(), True, 0)
    assert b"FIRSTNONKEYMARKER" not in result.selected


def test_fragmented_pat_pmt_and_split_pes_header():
    data = _stream(split_psi=4, first_pes_size=5)
    _, result = _feed_chunks(data, (3, 188, 17))
    assert result.status == READY


def test_false_sync_prefix_is_skipped():
    prefix = b"\x47\x00\x47\x00" * 23
    _, result = _feed_chunks(prefix + _stream())
    assert result.status == READY
    assert result.selected[:188] == _ts_packet(PAT_PID, b"\x00" + _pat_section(), True, 0)


def test_truncated_packet_is_rejected():
    data = _stream()[:188 * 3 + 50]
    probe = TsCandidateProbe()
    probe.feed(data)
    assert probe.finish().reason == "truncated"


def test_no_sync_is_rejected():
    probe = TsCandidateProbe()
    probe.feed(b"not a transport stream" * 20)
    assert probe.finish().reason == "no_sync"


def test_bad_pat_crc_is_rejected():
    pat = bytearray(_pat_section())
    pat[-1] ^= 0xFF
    data = (_psi_packets(PAT_PID, bytes(pat)) +
            _psi_packets(PMT_PID, _pmt_section()) +
            _pes_packets(VIDEO_PID, _pes(_keyframe_es())) +
            _pes_packets(VIDEO_PID, _pes(_nonkey_es())))
    probe = TsCandidateProbe()
    probe.feed(data)
    assert probe.finish().reason == "missing_pat"


def test_buffer_limit_is_rejected():
    probe = TsCandidateProbe(max_bytes=188 * 4)
    result = probe.feed(_stream() * 4)
    assert result.status == REJECT
    assert result.reason == "buffer_limit"


def test_mpeg_crc_known_section():
    assert mpeg_crc32(_pat_section()) == 0


def test_module_import_is_side_effect_free():
    source = (REPO_ROOT / "ts_candidate_probe.py").read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "import socket", "import threading"):
        assert forbidden not in source


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_real_ffmpeg_transcode_profile_and_selected_region_decodes(tmp_path):
    fixture = tmp_path / "transcode.ts"
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=2:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:flags=bicubic,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-bf", "0", "-threads", "0", "-b:v", "2200k", "-maxrate", "2600k",
        "-bufsize", "4400k", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-profile:v", "main", "-level", "4.1",
        "-x264-params", "repeat-headers=1:slices=1:bframes=0:aq-mode=2:aq-strength=1.0",
        "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-flags", "low_delay", "-pcr_period", "20", "-muxdelay", "0", "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-mpegts_pmt_start_pid", "4096", "-streamid", "0:256", "-streamid", "1:257",
        "-f", "mpegts", str(fixture),
    ]
    subprocess.run(command, check=True, capture_output=True)
    _assert_real_fixture_decodes(fixture, tmp_path / "selected.ts")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_real_ffmpeg_eco_copy_profile(tmp_path):
    source = tmp_path / "source.ts"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=2:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast",
        "-tune", "zerolatency", "-bf", "0", "-g", "30", "-keyint_min", "30",
        "-sc_threshold", "0", "-profile:v", "main", "-level", "4.1",
        "-x264-params", "repeat-headers=1:slices=1:bframes=0",
        "-c:a", "ac3", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-f", "mpegts", str(source),
    ], check=True, capture_output=True)
    fixture = tmp_path / "eco.ts"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
        "-bsf:v", "dump_extra=freq=keyframe",
        "-af", "aresample=async=1000:first_pts=0:min_hard_comp=0.100000",
        "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-flags", "low_delay", "-pcr_period", "20", "-muxdelay", "0.7", "-muxpreload", "0.7",
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-mpegts_pmt_start_pid", "4096", "-streamid", "0:256", "-streamid", "1:257",
        "-f", "mpegts", str(fixture),
    ], check=True, capture_output=True)
    _assert_real_fixture_decodes(fixture, tmp_path / "selected_eco.ts")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_real_ffmpeg_video_only_profile(tmp_path):
    fixture = tmp_path / "video_only.ts"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=2:size=640x360:rate=30",
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-tune", "zerolatency", "-bf", "0", "-g", "30", "-keyint_min", "30",
        "-sc_threshold", "0", "-profile:v", "main", "-level", "4.1",
        "-x264-params", "repeat-headers=1:slices=1:bframes=0:aq-mode=2:aq-strength=1.0",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-flags", "low_delay", "-pcr_period", "20", "-muxdelay", "0", "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-mpegts_pmt_start_pid", "4096", "-streamid", "0:256",
        "-f", "mpegts", str(fixture),
    ], check=True, capture_output=True)
    _assert_real_fixture_decodes(fixture, tmp_path / "selected_video_only.ts", audio=False)


def _assert_real_fixture_decodes(fixture: Path, selected: Path, audio=True):
    data = fixture.read_bytes()
    probe, result = _feed_chunks(data, (1, 7, 188, 333, 4096))
    assert result.status == READY, result
    assert result.video_pid == VIDEO_PID
    if audio:
        assert result.audio_pid == AUDIO_PID
        assert result.audio_kind == "ac3"
    else:
        assert result.audio_pid is None
        assert result.audio_kind == "none"
    assert len(result.selected) % 188 == 0
    assert (((result.selected[1] & 0x1F) << 8) | result.selected[2]) == PAT_PID
    fixture_streams = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type,id",
        "-of", "compact", str(fixture),
    ], check=True, capture_output=True, text=True)
    assert "codec_name=h264" in fixture_streams.stdout
    assert "id=0x100" in fixture_streams.stdout
    if audio:
        assert "codec_name=ac3" in fixture_streams.stdout
        assert "id=0x101" in fixture_streams.stdout
    selected.write_bytes(result.selected)
    video = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,id", "-of", "compact", str(selected),
    ], check=True, capture_output=True, text=True)
    assert "codec_name=h264" in video.stdout
    assert "id=0x100" in video.stdout
    frames = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-read_intervals", "%+#1", "-show_entries", "frame=key_frame,pict_type",
        "-of", "compact", str(selected),
    ], check=True, capture_output=True, text=True)
    assert "key_frame=1" in frames.stdout
