#!/usr/bin/env python3
"""
test_probe_sar.py - Validação unitária e de integração para probe_is_h264() com SAR anamórfico.
Verifica que streams H.264 com SAR anamórfico (64:45, 160:159, etc.) retornam False,
forçando transcode 1080p square e prevenindo modo ECO/copy.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import unittest
from unittest import mock
import json
import subprocess
import threading
import atexit
from http.server import HTTPServer, SimpleHTTPRequestHandler

import server
from server import probe_is_h264, CHANNEL_CODEC_CACHE


# Encerramento higiênico do HUB
def _cleanup_hub():
    try:
        if hasattr(server, "HUB") and server.HUB:
            server.HUB.running = False
            if hasattr(server.HUB, "proc") and server.HUB.proc:
                server.HUB.proc.terminate()
    except Exception:
        pass

atexit.register(_cleanup_hub)


class TestProbeIsH264Mocked(unittest.TestCase):
    """Testes unitários com simulação controlada do payload JSON retornado pelo ffprobe."""

    def setUp(self):
        CHANNEL_CODEC_CACHE.clear()

    def _mock_ffprobe(self, stream_dict):
        payload = json.dumps({"streams": [stream_dict]}).encode("utf-8")
        return payload

    def test_anamorphic_sar_64_45_rejected(self):
        """Streams com SAR 64:45 (ex: NTSC anamórfico 720x480) devem retornar False."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": "64:45"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_64_45.ts"
            result = probe_is_h264(url)
            self.assertFalse(result, "Stream com SAR 64:45 deve ser rejeitado pelo probe_is_h264")
            self.assertIn(url, CHANNEL_CODEC_CACHE)
            self.assertFalse(CHANNEL_CODEC_CACHE[url])

    def test_anamorphic_sar_160_159_rejected(self):
        """Streams com SAR 160:159 (ex: 704x480) devem retornar False."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": "160:159"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_160_159.ts"
            result = probe_is_h264(url)
            self.assertFalse(result, "Stream com SAR 160:159 deve ser rejeitado pelo probe_is_h264")
            self.assertIn(url, CHANNEL_CODEC_CACHE)
            self.assertFalse(CHANNEL_CODEC_CACHE[url])

    def test_square_sar_1_1_accepted(self):
        """Streams H.264 sem B-frames e <=31fps com SAR 1:1 devem retornar True."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": "1:1"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_square.ts"
            result = probe_is_h264(url)
            self.assertTrue(result, "Stream com SAR 1:1 deve ser aceito pelo probe_is_h264")
            self.assertTrue(CHANNEL_CODEC_CACHE[url])

    def test_square_sar_slash_format_accepted(self):
        """Streams com SAR '1/1' devem retornar True."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "29.97/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": "1/1"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_square_slash.ts"
            result = probe_is_h264(url)
            self.assertTrue(result, "Stream com SAR 1/1 deve ser aceito pelo probe_is_h264")

    def test_empty_sar_accepted_if_standard(self):
        """Streams sem campo SAR explícito ou vazio devem ser aceitos se H.264 standard."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": ""
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_no_sar.ts"
            result = probe_is_h264(url)
            self.assertTrue(result, "Stream sem SAR deve ser aceito se h264 standard")

    def test_b_frames_rejected_even_with_square_sar(self):
        """Streams com B-frames (>0) devem ser rejeitados mesmo com SAR 1:1."""
        mock_stream = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 2,
            "sample_aspect_ratio": "1:1"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_bframes.ts"
            result = probe_is_h264(url)
            self.assertFalse(result, "Stream com B-frames deve ser rejeitado")

    def test_hevc_rejected(self):
        """Streams HEVC / H.265 devem ser rejeitados."""
        mock_stream = {
            "codec_name": "hevc",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "has_b_frames": 0,
            "sample_aspect_ratio": "1:1"
        }
        with mock.patch("subprocess.check_output", return_value=self._mock_ffprobe(mock_stream)):
            url = "http://stream.local/live_hevc.ts"
            result = probe_is_h264(url)
            self.assertFalse(result, "Stream HEVC deve ser rejeitado")


class TestProbeIsH264RealMedia(unittest.TestCase):
    """Testes de integração com arquivos reais codificados via FFmpeg e servidos via HTTP."""

    TMP_DIR = "/tmp/usb_stream_sar_tests"
    HTTP_PORT = 18889
    httpd = None
    server_thread = None

    @classmethod
    def setUpClass(cls):
        os.makedirs(cls.TMP_DIR, exist_ok=True)

        # 1. Gera vídeo H.264 anamórfico 64:45 sem B-frames
        p1 = os.path.join(cls.TMP_DIR, "anamorphic_64_45.ts")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=720x480:rate=30",
            "-vf", "setsar=sar=64/45", "-c:v", "libx264", "-bf", "0", "-f", "mpegts", p1
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        # 2. Gera vídeo H.264 anamórfico 160:159 sem B-frames
        p2 = os.path.join(cls.TMP_DIR, "anamorphic_160_159.ts")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=704x480:rate=30",
            "-vf", "setsar=sar=160/159", "-c:v", "libx264", "-bf", "0", "-f", "mpegts", p2
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        # 3. Gera vídeo H.264 square 1:1 sem B-frames
        p3 = os.path.join(cls.TMP_DIR, "square_1_1.ts")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=1280x720:rate=30",
            "-vf", "setsar=sar=1/1", "-c:v", "libx264", "-bf", "0", "-f", "mpegts", p3
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        # Servidor HTTP local para fornecer URLs http:// reais ao probe_is_h264
        class CustomHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=cls.TMP_DIR, **kwargs)

            def log_message(self, format, *args):
                pass  # silencia logs de requisição

        cls.httpd = HTTPServer(("127.0.0.1", cls.HTTP_PORT), CustomHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()

    def setUp(self):
        CHANNEL_CODEC_CACHE.clear()

    def test_real_anamorphic_64_45_stream(self):
        url = f"http://127.0.0.1:{self.HTTP_PORT}/anamorphic_64_45.ts"
        result = probe_is_h264(url)
        self.assertFalse(result, "Arquivo real com SAR 64:45 deve retornar False")

    def test_real_anamorphic_160_159_stream(self):
        url = f"http://127.0.0.1:{self.HTTP_PORT}/anamorphic_160_159.ts"
        result = probe_is_h264(url)
        self.assertFalse(result, "Arquivo real com SAR 160:159 deve retornar False")

    def test_real_square_1_1_stream(self):
        url = f"http://127.0.0.1:{self.HTTP_PORT}/square_1_1.ts"
        result = probe_is_h264(url)
        self.assertTrue(result, "Arquivo real com SAR 1:1 e 0 B-frames deve retornar True")


if __name__ == "__main__":
    unittest.main(verbosity=2)
