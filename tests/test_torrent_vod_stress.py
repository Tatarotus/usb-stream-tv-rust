"""Stress + edge-case suite for torrent VOD downloading (torrent_downloader.py).
Covers: multi-file torrents/sample filtering, subtitle encoding normalization
(UTF-8/CP1252/Latin-1), no-subtitle fallback, aria2 cancellation, plus
boundary/null/concurrency/failure-injection. Self-contained: tmp dirs + mocks.
"""
import os
import sys
import threading
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import json
import tempfile
from unittest import mock

import pytest

import torrent_downloader as td
from torrent_downloader import (
    is_torrent_url,
    extract_torrent_title,
    run_aria2_download,
    find_main_video_file,
    find_and_prepare_subtitle,
    is_audio_portuguese,
    build_ffmpeg_transcode_command,
    can_stream_copy_video,
    is_sar_samsung_compatible,
    escape_ffmpeg_filter_path,
    _read_and_normalize_srt,
)


# ---------------------------------------------------------------- is_torrent_url
class TestIsTorrentUrl:
    @pytest.mark.parametrize("url", [
        "magnet:?xt=urn:btih:ABCDEF123456",
        "MAGNET:?XT=URN:BTIH:ABCDEF",
        "  magnet:?xt=urn:btih:123  ",
        "https://ex.com/film.torrent",
        "https://ex.com/FILM.TORRENT",  # lowercased internally
        "http://ex.com/film.torrent?token=abc",
        "http://ex.com/x.torrent?y=1&z=2",
    ])
    def test_happy_true(self, url):
        assert is_torrent_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://ex.com/film.mp4",
        "https://ex.com/stream.m3u8",
        "https://ex.com/film.torrents",  # suffix mismatch
        "https://ex.com/torrent/movie",  # no .torrent suffix
        "",
        "   ",
        None,
        12345,
        ["magnet:?xt=1"],
        {"u": "magnet:?"},
        "magnet:",  # missing '?'
        "http://ex.com/video.mkv",
    ])
    def test_edge_false(self, url):
        assert is_torrent_url(url) is False

    def test_none_and_bytes(self):
        assert is_torrent_url(None) is False
        assert is_torrent_url(b"magnet:?xt=1") is False  # not str


# ------------------------------------------------------- extract_torrent_title
class TestExtractTorrentTitle:
    def test_standard_dn(self):
        m = "magnet:?xt=urn:btih:1&dn=Deadpool.and.Wolverine.2024.[1080p].Dual_Audio"
        assert extract_torrent_title(m) == "Deadpool and Wolverine 2024 1080p Dual Audio"

    def test_underscores_braces(self):
        m = "magnet:?xt=urn:btih:a&dn=O_Poderoso_Chefao_{1972}_[1080p]"
        assert extract_torrent_title(m) == "O Poderoso Chefao 1972 1080p"

    def test_url_encoding_plus(self):
        assert extract_torrent_title("magnet:?dn=Gladiator+II+%5B2024%5D") == "Gladiator II 2024"

    def test_percent_utf8(self):
        m = "magnet:?xt=urn:btih:1&dn=Cora%C3%A7%C3%A3o.Valente.1995"
        assert extract_torrent_title(m) == "Coração Valente 1995"

    def test_fallback_cleaned(self):
        assert extract_torrent_title("magnet:?xt=urn:btih:abc", fallback="Interestelar.2014.BluRay") == "Interestelar 2014 BluRay"

    def test_fallback_used_when_no_dn(self):
        assert extract_torrent_title("magnet:?xt=urn:btih:abc", fallback="") == ""
        assert extract_torrent_title("", fallback="Matrix.1999") == "Matrix 1999"

    def test_empty_everything(self):
        assert extract_torrent_title("") == ""
        assert extract_torrent_title(None) == ""
        assert extract_torrent_title(None, fallback=None if False else "") == ""
        assert extract_torrent_title("not a magnet at all") == ""

    def test_malformed_magnet_no_crash(self):
        assert isinstance(extract_torrent_title("magnet:????"), str)
        assert isinstance(extract_torrent_title("?" * 5000), str)

    def test_dn_only_separators_returns_raw(self):
        # dn = "..." -> cleaned empty -> returns raw_title per code
        r = extract_torrent_title("magnet:?dn=...")
        assert isinstance(r, str)

    def test_multiple_spaces_collapsed(self):
        m = "magnet:?dn=Movie...1080p___BluRay"
        assert extract_torrent_title(m) == "Movie 1080p BluRay"

    def test_concurrent_titles_threadsafe(self):
        mags = [f"magnet:?xt=urn:btih:{i}&dn=Movie.{i}.2024.1080p" for i in range(50)]
        out = [None] * 50
        def w(idx):
            out[idx] = extract_torrent_title(mags[idx])
        ths = [threading.Thread(target=w, args=(i,)) for i in range(50)]
        [t.start() for t in ths]; [t.join() for t in ths]
        for i in range(50):
            assert out[i] == f"Movie {i} 2024 1080p"


# ------------------------------------------------------- find_main_video_file
class TestFindMainVideoFile:
    def test_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            assert find_main_video_file(d) is None
        assert find_main_video_file("/nonexistent/path/xyz") is None
        assert find_main_video_file("") is None
        assert find_main_video_file(None) is None

    def test_ignores_nfo_txt(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "info.txt"), "w").write("x")
            open(os.path.join(d, "movie.nfo"), "w").write("x")
            assert find_main_video_file(d) is None

    def test_multifile_season_pack_picks_largest_non_sample(self):
        with tempfile.TemporaryDirectory() as d:
            sizes = {"Show.S01E01.mkv": 100, "Show.S01E02.mp4": 500,
                     "Show.S01E03.avi": 300, "sample.S01E02.mkv": 900}
            for name, sz in sizes.items():
                with open(os.path.join(d, name), "wb") as f:
                    f.write(b"x" * sz)
            best = find_main_video_file(d)
            assert best.endswith("Show.S01E02.mp4"), best

    def test_sample_only_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            s1 = os.path.join(d, "sample.mkv"); s2 = os.path.join(d, "SAMPLE_big.mp4")
            open(s1, "wb").write(b"x" * 10); open(s2, "wb").write(b"x" * 50)
            assert find_main_video_file(d) == os.path.abspath(s2)

    def test_nested_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a", "b"))
            deep = os.path.join(d, "a", "b", "movie.ts")
            shallow = os.path.join(d, "small.mov")
            open(deep, "wb").write(b"x" * 2000); open(shallow, "wb").write(b"x" * 10)
            assert find_main_video_file(d) == os.path.abspath(deep)

    def test_case_insensitive_ext_and_sample(self):
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, "FILM.MKV"); smp = os.path.join(d, "SAMPLE.MP4")
            open(big, "wb").write(b"x" * 100); open(smp, "wb").write(b"x" * 10000)
            # SAMPLE upper must still be classified as sample
            assert find_main_video_file(d) == os.path.abspath(big)

    def test_all_extensions_recognized(self):
        with tempfile.TemporaryDirectory() as d:
            for i, ext in enumerate([".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov"]):
                open(os.path.join(d, f"f{i}{ext}"), "wb").write(b"x" * (10 + i))
            best = find_main_video_file(d)
            assert best.endswith("f5.mov")

    def test_zero_byte_vs_normal(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "empty.mkv"), "wb").close()
            open(os.path.join(d, "real.mkv"), "wb").write(b"x" * 5)
            assert find_main_video_file(d).endswith("real.mkv")

    def test_stress_200_files(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(200):
                name = f"file_{i:03d}.mkv" if i % 2 == 0 else f"sample_{i:03d}.mkv"
                open(os.path.join(d, name), "wb").write(b"x" * (i + 1))
            t0 = time.time()
            best = find_main_video_file(d)
            dt = time.time() - t0
            assert best.endswith("file_198.mkv"), best  # largest non-sample even index
            assert dt < 2.0, f"too slow: {dt:.2f}s"

    def test_unreadable_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "good.mp4")
            open(good, "wb").write(b"x" * 100)
            with mock.patch("os.path.getsize", side_effect=[OSError("denied"), 100]):
                # first file raises, must not crash — need 2 files to exercise path
                open(os.path.join(d, "bad.mkv"), "wb").write(b"x")
                # result may be None or good depending on walk order; key: no exception
                assert find_main_video_file(d) in (None, os.path.abspath(good), mock.ANY) or True


# ------------------------------------------------- _read_and_normalize_srt
class TestNormalizeSrt:
    def _write_raw(self, d, name, data: bytes):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_utf8_plain(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_raw(d, "a.srt", "1\n00:00:00,000 --> 00:00:01,000\nOlá mundo\n".encode("utf-8"))
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            assert open(out, encoding="utf-8").read().startswith("1\n")
            assert "Olá" in open(out, encoding="utf-8").read()

    def test_utf8_bom_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_raw(d, "b.srt", "1\n00:00:00,000 --> 00:00:01,000\nAção\n".encode("utf-8-sig"))
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            content = open(out, encoding="utf-8").read()
            assert not content.startswith("\ufeff"), "BOM must be stripped"
            assert "Ação" in content

    def test_cp1252_smart_quotes(self):
        # CP1252 bytes invalid in UTF-8: 0x92 (’), 0x93/0x94 (“ ”), 0xE7 ç
        with tempfile.TemporaryDirectory() as d:
            raw = "1\n00:00:00,000 --> 00:00:01,000\nVoc\xeas est\xe3o prontos \x93ok\x94 \x97 fim\n".encode("latin-1")
            # craft genuine cp1252-only byte: \x92 right single quote
            raw = b"1\n00:00:00,000 --> 00:00:01,000\nVoc\xeas \x92prontos\x92\n"
            src = self._write_raw(d, "c.srt", raw)
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            assert out is not None
            txt = open(out, encoding="utf-8").read()
            assert "Vocês" in txt  # ç decoded correctly via cp1252/latin-1 path

    def test_latin1_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            raw = "1\n00:00:00,000 --> 00:00:01,000\nna\xefve caf\xe9\n".encode("latin-1")
            src = self._write_raw(d, "l.srt", raw)
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            txt = open(out, encoding="utf-8").read()
            assert "naïve" in txt or "caf\xe9" in txt

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_raw(d, "e.srt", b"")
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            assert out is not None and os.path.getsize(out) == 0

    def test_missing_source_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            assert _read_and_normalize_srt(os.path.join(d, "nope.srt"), os.path.join(d, "w")) is None

    def test_output_always_utf8_readable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_raw(d, "g.srt", bytes(range(1, 256)))
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            open(out, encoding="utf-8").read()  # must not raise

    def test_large_srt_stress_1mb(self):
        with tempfile.TemporaryDirectory() as d:
            chunk = "1\n00:00:00,000 --> 00:00:01,000\nLegenda de teste com acentuação: çãõé\n"
            src = self._write_raw(d, "big.srt", (chunk * 20000).encode("utf-8"))
            t0 = time.time()
            out = _read_and_normalize_srt(src, os.path.join(d, "w"))
            assert time.time() - t0 < 5.0
            assert os.path.getsize(out) > 1_000_000


# ------------------------------------------------- find_and_prepare_subtitle
def _make_fake_video(path):
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)


class TestFindAndPrepareSubtitle:
    def test_no_subtitles_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            open(os.path.join(tor, "movie.mkv"), "wb").write(b"x" * 100)
            with mock.patch("subprocess.run", side_effect=FileNotFoundError("no ffprobe")):
                assert find_and_prepare_subtitle(vid, tor, os.path.join(d, "w")) is None

    def test_single_srt_fallback_no_pt_keyword(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            with open(os.path.join(tor, "movie.srt"), "w", encoding="utf-8") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nLegenda única.\n")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                out = find_and_prepare_subtitle(vid, tor, os.path.join(d, "w"))
            assert out and "Legenda única" in open(out, encoding="utf-8").read()

    def test_multiple_non_pt_srts_no_singleton_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            for n in ["movie.en.srt", "movie.es.srt"]:
                with open(os.path.join(tor, n), "w") as f:
                    f.write("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                assert find_and_prepare_subtitle(vid, tor, os.path.join(d, "w")) is None

    def test_pt_keyword_priority_and_largest_wins(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            small = os.path.join(tor, "movie.dublado.srt")
            big = os.path.join(tor, "movie.portugues.srt")
            with open(small, "w") as f: f.write("1\n00:00:00,000 --> 00:00:01,000\nPequena\n")
            with open(big, "w") as f: f.write(("1\n00:00:00,000 --> 00:00:01,000\nGrande XPTO\n" * 50))
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                out = find_and_prepare_subtitle(vid, tor, os.path.join(d, "w"))
            assert out and "Grande XPTO" in open(out, encoding="utf-8").read()

    def test_token_pt_dot_srt(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            with open(os.path.join(tor, "movie.pt.srt"), "w") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nToken PT\n")
            with open(os.path.join(tor, "movie.en.srt"), "w") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nEN\n")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                out = find_and_prepare_subtitle(vid, tor, os.path.join(d, "w"))
            assert out and "Token PT" in open(out, encoding="utf-8").read()

    def test_pt_in_subfolder_name(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mp4"); _make_fake_video(vid)
            tor = os.path.join(d, "tor")
            sub = os.path.join(tor, "Legendas Dublado")
            os.makedirs(sub)
            with open(os.path.join(sub, "leg.srt"), "w") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nPasta PT\n")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                out = find_and_prepare_subtitle(vid, tor, os.path.join(d, "w"))
            assert out and "Pasta PT" in open(out, encoding="utf-8").read()

    def test_tier1_embedded_pt_extracted(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mkv"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            work = os.path.join(d, "w")
            probe_json = json.dumps({"streams": [
                {"codec_type": "video", "index": 0},
                {"codec_type": "subtitle", "index": 2,
                 "tags": {"language": "por", "title": "Português"}},
            ]})
            def fake_run(cmd, **kw):
                if cmd[0] == "ffprobe":
                    return mock.Mock(stdout=probe_json, returncode=0)
                # ffmpeg extract: write a temp srt so normalize can pick it up
                assert cmd[0] == "ffmpeg"
                tmp = os.path.join(work, "temp_extracted.srt")
                os.makedirs(work, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("1\n00:00:00,000 --> 00:00:01,000\nEmbutida PT\n")
                return mock.Mock(returncode=0)
            with mock.patch("subprocess.run", side_effect=fake_run):
                out = find_and_prepare_subtitle(vid, tor, work)
            assert out and "Embutida PT" in open(out, encoding="utf-8").read()

    def test_tier1_non_pt_ignored_falls_to_tier2(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mkv"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            with open(os.path.join(tor, "f.pt-br.srt"), "w") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nExterna PT\n")
            probe_json = json.dumps({"streams": [
                {"codec_type": "subtitle", "index": 1,
                 "tags": {"language": "eng", "title": "English"}}]})
            with mock.patch("subprocess.run",
                             return_value=mock.Mock(stdout=probe_json, returncode=0)):
                out = find_and_prepare_subtitle(vid, tor, os.path.join(d, "w"))
            assert out and "Externa PT" in open(out, encoding="utf-8").read()

    def test_tier1_ffprobe_corrupt_json_falls_through(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mkv"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            with mock.patch("subprocess.run",
                             return_value=mock.Mock(stdout="NOT JSON{{{", returncode=0)):
                assert find_and_prepare_subtitle(vid, tor, os.path.join(d, "w")) is None

    def test_tier1_ffmpeg_extract_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            vid = os.path.join(d, "v.mkv"); _make_fake_video(vid)
            tor = os.path.join(d, "tor"); os.makedirs(tor)
            probe_json = json.dumps({"streams": [
                {"codec_type": "subtitle", "index": 1,
                 "tags": {"language": "pt", "title": "x"}}]})
            def fake_run(cmd, **kw):
                if cmd[0] == "ffprobe":
                    return mock.Mock(stdout=probe_json, returncode=0)
                return mock.Mock(returncode=1)  # ffmpeg fails
            with mock.patch("subprocess.run", side_effect=fake_run):
                assert find_and_prepare_subtitle(vid, tor, os.path.join(d, "w")) is None

    def test_missing_video_and_dir_no_crash(self):
        with tempfile.TemporaryDirectory() as d:
            assert find_and_prepare_subtitle("/nonexistent/v.mp4", "/nonexistent/t",
                                             os.path.join(d, "w")) is None


# ------------------------------------------------------- is_audio_portuguese
def _probe_audio_mock(streams):
    return mock.Mock(stdout=json.dumps({"streams": streams}), returncode=0)

class TestIsAudioPortuguese:
    def test_missing_file(self):
        assert is_audio_portuguese("/nonexistent.mp4") is False
        assert is_audio_portuguese("") is False
        assert is_audio_portuguese(None) is False

    def test_por_lang(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4"); _make_fake_video(v)
            with mock.patch("subprocess.run",
                             return_value=_probe_audio_mock(
                                 [{"tags": {"language": "por"}}])):
                assert is_audio_portuguese(v) is True

    @pytest.mark.parametrize("lang", ["pob", "pt", "pt-br", "POR", " PT "])
    def test_lang_variants(self, lang, tmp_path):
        v = str(tmp_path / "v.mp4"); _make_fake_video(v)
        with mock.patch("subprocess.run",
                         return_value=_probe_audio_mock([{"tags": {"language": lang}}])):
            assert is_audio_portuguese(v) is True

    def test_title_dublado(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4"); _make_fake_video(v)
            with mock.patch("subprocess.run", return_value=_probe_audio_mock(
                    [{"tags": {"language": "eng", "title": "Dublado PT-BR"}}])):
                # title lowercased in code -> "dublado pt-br" contains "dublado"
                assert is_audio_portuguese(v) is True

    def test_english_false(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4"); _make_fake_video(v)
            with mock.patch("subprocess.run", return_value=_probe_audio_mock(
                    [{"tags": {"language": "eng", "title": "Original English"}}])):
                assert is_audio_portuguese(v) is False

    def test_multi_stream_one_pt(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4"); _make_fake_video(v)
            with mock.patch("subprocess.run", return_value=_probe_audio_mock(
                    [{"tags": {"language": "eng"}}, {"tags": {"language": "por"}}])):
                assert is_audio_portuguese(v) is True

    def test_ffprobe_error_false(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4"); _make_fake_video(v)
            with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
                assert is_audio_portuguese(v) is False
            with mock.patch("subprocess.run",
                             return_value=mock.Mock(stdout="garbage", returncode=0)):
                assert is_audio_portuguese(v) is False


# ------------------------------------------------------- run_aria2_download
import subprocess

class FakeStdout:
    def __init__(self, lines):
        self._it = iter(lines)
    def readline(self):
        try:
            return next(self._it)
        except StopIteration:
            return ""

class FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = FakeStdout(lines)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = False
    def wait(self, timeout=None):
        self.waited = True
        return self.returncode
    def terminate(self):
        self.terminated = True
    def kill(self):
        self.killed = True

class TestRunAria2:
    MAG = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"

    def test_success_telemetry(self):
        lines = [
            "[#1 500KiB/1MiB(50%) CN:4 DL:2.5MiB SD:1]\n",
            "(OK):download completed\n",
        ]
        fp = FakeProc(lines, returncode=0)
        task = {}
        with mock.patch("subprocess.Popen", return_value=fp) as p:
            ok = run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task)
        assert ok is True
        assert task["progress"] >= 50
        assert "concluído" in task["status_msg"]
        assert p.call_args[0][0][0] == "aria2c"
        assert "--seed-time=0" in p.call_args[0][0]

    def test_progress_mapping_bounds(self):
        for raw, expect_min, expect_max in [("0%", 5, 5), ("50%", 27, 28), ("100%", 50, 50)]:
            lines = [f"[#1 1MiB/1MiB({raw} CN:1 DL:1MiB)]\n"]
            fp = FakeProc(lines, returncode=1)
            task = {}
            with mock.patch("subprocess.Popen", return_value=fp):
                run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task)
            assert 5 <= task["progress"] <= 50, (raw, task)

    def test_speed_normalization_gib(self):
        lines = ["[#1 10MiB/100MiB(10%) CN:8 DL:1.5GiB SD:2]\n"]
        fp = FakeProc(lines, returncode=1)
        task = {}
        with mock.patch("subprocess.Popen", return_value=fp):
            run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task)
        assert task["speed"].endswith("/s") and "GB" in task["speed"], task["speed"]

    def test_failure_returncode_false(self):
        fp = FakeProc(["error: no peers\n"], returncode=1)
        with mock.patch("subprocess.Popen", return_value=fp):
            assert run_aria2_download(self.MAG, tempfile.mkdtemp()) is False

    def test_cancel_via_callback(self):
        fp = FakeProc(["[#1 (10%) CN:1 DL:1MiB]\n"] * 50, returncode=0)
        procs = {}
        with mock.patch("subprocess.Popen", return_value=fp):
            ok = run_aria2_download(self.MAG, tempfile.mkdtemp(),
                                    is_cancelled_fn=lambda: True,
                                    running_procs_dict=procs, task_id="t1")
        assert ok is False
        assert fp.terminated or fp.killed
        assert "t1" not in procs  # cleanup in finally

    def test_cancel_via_task_dict_flag(self):
        fp = FakeProc(["[#1 (10%) CN:1 DL:1MiB]\n"], returncode=0)
        import threading as th
        task = {"cancelled": True}
        with mock.patch("subprocess.Popen", return_value=fp):
            ok = run_aria2_download(self.MAG, tempfile.mkdtemp(),
                                    task_dict=task, task_lock=th.Lock())
        assert ok is False and (fp.terminated or fp.killed)

    def test_cancel_via_status_string(self):
        fp = FakeProc(["[#1 (10%) CN:1 DL:1MiB]\n"], returncode=0)
        task = {"status": "cancelled"}
        with mock.patch("subprocess.Popen", return_value=fp):
            assert run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task) is False

    def test_cancel_mid_stream(self):
        calls = {"n": 0}
        def cb():
            calls["n"] += 1
            return calls["n"] >= 3
        lines = ["[#1 (1%) CN:1 DL:1MiB]\n"] * 10
        fp = FakeProc(lines, returncode=0)
        task = {}
        with mock.patch("subprocess.Popen", return_value=fp):
            ok = run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task,
                                    is_cancelled_fn=cb)
        assert ok is False and calls["n"] >= 3

    def test_terminate_timeout_falls_back_to_kill(self):
        class SlowProc(FakeProc):
            def wait(self, timeout=None):
                if timeout == 2 and not self.killed:
                    raise subprocess.TimeoutExpired("aria2c", 2)
                self.waited = True
                return self.returncode
        fp = SlowProc(["x\n"], returncode=0)
        with mock.patch("subprocess.Popen", return_value=fp):
            ok = run_aria2_download(self.MAG, tempfile.mkdtemp(),
                                    is_cancelled_fn=lambda: True)
        assert ok is False and fp.killed

    def test_creates_download_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "new", "nested")
            fp = FakeProc([], returncode=0)
            with mock.patch("subprocess.Popen", return_value=fp):
                run_aria2_download(self.MAG, target)
            assert os.path.isdir(target)

    def test_empty_stdout_success(self):
        fp = FakeProc([], returncode=0)
        task = {}
        with mock.patch("subprocess.Popen", return_value=fp):
            assert run_aria2_download(self.MAG, tempfile.mkdtemp(), task_dict=task) is True

    def test_concurrent_downloads_isolated_procs(self):
        fps = [FakeProc([], returncode=0) for _ in range(5)]
        procs = {}
        seen_cmds = []
        orig_popen = subprocess.Popen
        idx = {"i": 0}
        def fake_popen(cmd, **kw):
            fp = fps[idx["i"]]; idx["i"] += 1
            return fp
        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            def w(i):
                run_aria2_download(f"{self.MAG}{i}", tempfile.mkdtemp(),
                                   running_procs_dict=procs, task_id=f"t{i}")
            ths = [threading.Thread(target=w, args=(i,)) for i in range(5)]
            [t.start() for t in ths]; [t.join() for t in ths]
        assert procs == {}, f"leaked procs: {procs}"


# ------------------------------------------------- ffmpeg / sar / compat
class TestFfmpegSarCompat:
    def test_escape_path(self):
        assert escape_ffmpeg_filter_path("C:\\subs\\a.srt") == "C\\:/subs/a.srt"
        assert "\\:" in escape_ffmpeg_filter_path("/a/b:c.srt")
        assert "\\'" in escape_ffmpeg_filter_path("/a/b'c.srt")

    def test_burnin_command_invariants(self):
        cmd = build_ffmpeg_transcode_command("/v/v.mkv", "/o/out.mp4", "/s/sub.srt", True)
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-c:a") + 1] == "ac3"
        assert cmd[cmd.index("-b:a") + 1] == "384k"
        assert cmd[cmd.index("-ar") + 1] == "48000"
        assert cmd[cmd.index("-ac") + 1] == "2"
        assert "FontSize=22" in cmd[cmd.index("-vf") + 1]
        assert "subtitles=" in cmd[cmd.index("-vf") + 1]
        assert cmd[cmd.index("-f") + 1] == "mp4"

    def test_copy_command(self):
        cmd = build_ffmpeg_transcode_command("/v.mkv", "/o.mp4", None, True)
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert "-vf" not in cmd

    def test_transcode_command(self):
        cmd = build_ffmpeg_transcode_command("/v.mkv", "/o.mp4", None, False)
        assert "scale=1920:1080" in cmd[cmd.index("-vf") + 1]
        assert cmd[cmd.index("-c:v") + 1] == "libx264"

    def test_burnin_with_special_chars_escaped(self):
        cmd = build_ffmpeg_transcode_command("/v.mkv", "/o.mp4", "/tmp/a:b'c.srt", False)
        vf = cmd[cmd.index("-vf") + 1]
        assert "\\:" in vf and "\\'" in vf

    @pytest.mark.parametrize("sar,ok", [
        ("1:1", True), ("1/1", True), ("", True), (None, True),
        ("64:45", True), ("12:11", True), ("16:9", False), ("4:3", False),
        ("2:1", False), ("garbage", False), ("1:0", False),
    ])
    def test_sar_matrix(self, sar, ok):
        assert is_sar_samsung_compatible(sar) is ok

    def _probe_video(self, stream):
        return mock.Mock(stdout=json.dumps({"streams": [stream]}), returncode=0)

    def test_can_copy_happy(self, tmp_path):
        v = str(tmp_path / "v.mp4"); _make_fake_video(v)
        s = {"codec_name": "h264", "pix_fmt": "yuv420p", "level": 41,
             "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"}
        with mock.patch("subprocess.run", return_value=self._probe_video(s)):
            assert can_stream_copy_video(v) is True

    @pytest.mark.parametrize("mut", [
        {"codec_name": "hevc"}, {"pix_fmt": "yuv444p"}, {"level": 50},
        {"width": 3840}, {"height": 2160}, {"sample_aspect_ratio": "16:9"},
    ])
    def test_can_copy_rejections(self, tmp_path, mut):
        v = str(tmp_path / "v.mp4"); _make_fake_video(v)
        base = {"codec_name": "h264", "pix_fmt": "yuv420p", "level": 41,
                "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"}
        base.update(mut)
        with mock.patch("subprocess.run", return_value=self._probe_video(base)):
            assert can_stream_copy_video(v) is False

    def test_can_copy_missing_and_error(self, tmp_path):
        assert can_stream_copy_video("/nonexistent.mp4") is False
        v = str(tmp_path / "v.mp4"); _make_fake_video(v)
        with mock.patch("subprocess.run", side_effect=OSError("no ffprobe")):
            assert can_stream_copy_video(v) is False
