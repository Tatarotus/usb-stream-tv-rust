#!/usr/bin/env python3
"""
test_torrent_vod.py - Suíte de testes unitários para processamento e ingestão de Torrents VOD.

Valida os seguintes componentes de torrent_downloader.py:
1. Detecção de URLs de torrent e links magnéticos (is_torrent_url).
2. Extração e sanitização do título do torrent a partir de magnet links (extract_torrent_title).
3. Seleção heurística do arquivo principal de vídeo ignorando amostras/extras (find_main_video_file).
4. Busca e normalização de legendas em português (CP1252/ISO-8859-1 -> UTF-8 limpo) (find_and_prepare_subtitle).
5. Detecção de faixas de áudio dubladas/em português via ffprobe (is_audio_portuguese).
6. Construção de comandos FFmpeg para conformidade estrita com o chipset Samsung MStar 2013:
   - Burn-in de legendas via libass (-vf subtitles=...).
   - Transcodificação H.264 High@L4.1 + AC-3 Dolby Digital estéreo 48kHz.
   - Stream copy rápido (-c:v copy + -c:a ac3) quando não há legendas e o vídeo é compatível.
   - Regras de compatibilidade de SAR e hardware Samsung PL51F4000.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest import mock

# Inclusão da raiz do repositório no PYTHONPATH
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from torrent_downloader import (
    is_torrent_url,
    extract_torrent_title,
    find_main_video_file,
    find_and_prepare_subtitle,
    is_audio_portuguese,
    can_stream_copy_video,
    is_sar_samsung_compatible,
    escape_ffmpeg_filter_path,
    build_ffmpeg_transcode_command,
)


class TestMagnetLinkDetection(unittest.TestCase):
    """Validação da função is_torrent_url para diversos esquemas e formatos de URLs."""

    def test_valid_magnet_links(self):
        """Verifica se links magnéticos válidos em diferentes formatos são reconhecidos."""
        valid_magnets = [
            "magnet:?xt=urn:btih:dacf434d28c89c8a9e9a4f4d2f8319e34e567890&dn=Ubuntu+22.04",
            "magnet:?dn=Test+Movie&xt=urn:btih:1234567890abcdef1234567890abcdef12345678",
            "MAGNET:?XT=URN:BTIH:ABCD1234EF567890ABCD1234EF567890ABCD1234",
            "  magnet:?xt=urn:btih:abcdef1234567890abcdef1234567890abcdef12  ",
            "magnet:?xt=urn:btih:3b245504d603a1fc6c9e05f63d09a0f0a514d2a1&tr=udp%3A%2F%2Ftracker.opentrackr.org",
        ]
        for mag in valid_magnets:
            with self.subTest(magnet=mag):
                self.assertTrue(is_torrent_url(mag), f"Deveria identificar como magnet link: {mag}")

    def test_valid_torrent_urls(self):
        """Verifica se URLs de arquivos .torrent (http, https, file, query strings) são reconhecidas."""
        valid_torrents = [
            "http://example.com/movies/feature.torrent",
            "https://archive.org/download/public_domain_film/film.torrent",
            "https://tracker.org/download.php?file=release.torrent",
            "http://torrents.local/linux.torrent?token=abc",
            "file:///tmp/downloaded_media.torrent",
            "https://releases.ubuntu.com/22.04/ubuntu-22.04.3-desktop-amd64.iso.torrent",
        ]
        for tor in valid_torrents:
            with self.subTest(torrent=tor):
                self.assertTrue(is_torrent_url(tor), f"Deveria identificar como .torrent: {tor}")

    def test_invalid_and_non_torrent_urls(self):
        """Verifica se streams de vídeo HTTP normais, HLS, TS e YouTube são rejeitados."""
        invalid_urls = [
            "http://stream.local:8080/live/ch1/video.mp4",
            "http://iptv.provider.com/live/username/password/12345.m3u8",
            "http://server.net/channels/101.ts",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "http://example.com/movie.mkv",
            "https://ftp.gnu.org/gnu/emacs/emacs-29.1.tar.gz",
            "http://not-a-torrent.org/torrent/view/1234",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertFalse(is_torrent_url(url), f"Não deveria identificar como torrent: {url}")

    def test_empty_and_invalid_types(self):
        """Verifica o tratamento defensivo para strings vazias, None e tipos incorretos."""
        invalid_inputs = ["", "   ", None, 12345, [], {}, 0.0]
        for inp in invalid_inputs:
            with self.subTest(input=inp):
                self.assertFalse(is_torrent_url(inp), f"Entrada inválida {inp!r} deve retornar False")


class TestTorrentTitleExtraction(unittest.TestCase):
    """Validação da extração e higienização do nome do torrent (extract_torrent_title)."""

    def test_standard_magnet_title(self):
        """Verifica a extração básica do parâmetro dn= com pontos e underscores convertidos."""
        mag = "magnet:?xt=urn:btih:12345&dn=Avatar.The.Way.of.Water"
        title = extract_torrent_title(mag)
        self.assertEqual(title, "Avatar The Way of Water")

        mag_under = "magnet:?xt=urn:btih:12345&dn=O_Auto_da_Compadecida"
        title_under = extract_torrent_title(mag_under)
        self.assertEqual(title_under, "O Auto da Compadecida")

    def test_url_encoded_magnet_title(self):
        """Verifica se caracteres especiais codificados (%20, acentos, parênteses) são decodificados."""
        mag_spaces = "magnet:?xt=urn:btih:12345&dn=Dune%20Part%20Two%202024%201080p"
        self.assertEqual(extract_torrent_title(mag_spaces), "Dune Part Two 2024 1080p")

        mag_accents = "magnet:?xt=urn:btih:12345&dn=O%20Poderoso%20Chef%C3%A3o%20%281972%29"
        self.assertEqual(extract_torrent_title(mag_accents), "O Poderoso Chefão (1972)")

        mag_plus = "magnet:?xt=urn:btih:12345&dn=The+Lord+of+the+Rings+The+Fellowship+of+the+Ring"
        self.assertEqual(extract_torrent_title(mag_plus), "The Lord of the Rings The Fellowship of the Ring")

    def test_complex_release_tags_and_brackets(self):
        """Verifica a remoção de colchetes, chaves e tags de release poluídas."""
        mag_matrix = "magnet:?xt=urn:btih:12345&dn=The.Matrix.1999.1080p.BluRay.x264.[YTS.AM]"
        self.assertEqual(extract_torrent_title(mag_matrix), "The Matrix 1999 1080p BluRay x264 YTS AM")

        mag_brackets = "magnet:?xt=urn:btih:12345&dn={Tracker}_Interstellar.2014.IMAX.1080p.[Dual_Audio]"
        self.assertEqual(extract_torrent_title(mag_brackets), "Tracker Interstellar 2014 IMAX 1080p Dual Audio")

        mag_multiple_dots = "magnet:?xt=urn:btih:12345&dn=Movie...Title...[[2024]]__1080p"
        self.assertEqual(extract_torrent_title(mag_multiple_dots), "Movie Title 2024 1080p")

    def test_fallback_handling(self):
        """Verifica o uso e higienização do título de fallback quando dn= não existe."""
        mag_no_dn = "magnet:?xt=urn:btih:dacf434d28c89c8a9e9a4f4d2f8319e34e567890"
        self.assertEqual(extract_torrent_title(mag_no_dn, fallback="Inception.2010.[1080p]"), "Inception 2010 1080p")
        self.assertEqual(extract_torrent_title(mag_no_dn, fallback="Custom Fallback Title"), "Custom Fallback Title")

        # Sem dn e sem fallback
        self.assertEqual(extract_torrent_title(mag_no_dn, fallback=""), "")
        self.assertEqual(extract_torrent_title("", fallback=""), "")
        self.assertEqual(extract_torrent_title(None, fallback=""), "")


class TestVideoFileSelection(unittest.TestCase):
    """Validação da seleção heurística do arquivo principal de vídeo (find_main_video_file)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_select_main_feature_over_samples_and_extras(self):
        """Verifica se o arquivo principal é selecionado descartando pastas de amostra e extras menores."""
        base_dir = self.temp_dir.name

        # Cria diretórios simulando estrutura real de torrent
        sample_dir = os.path.join(base_dir, "Sample")
        extras_dir = os.path.join(base_dir, "Extras")
        subs_dir = os.path.join(base_dir, "Subs")
        os.makedirs(sample_dir, exist_ok=True)
        os.makedirs(extras_dir, exist_ok=True)
        os.makedirs(subs_dir, exist_ok=True)

        # Arquivo de amostra (pequeno, com "sample" no nome)
        sample_path = os.path.join(sample_dir, "sample.mkv")
        with open(sample_path, "wb") as f:
            f.write(b"0" * 200_000)

        # Outra amostra solta
        sample2_path = os.path.join(base_dir, "feature_sample.mp4")
        with open(sample2_path, "wb") as f:
            f.write(b"0" * 150_000)

        # Extra (menor que o filme)
        extra_path = os.path.join(extras_dir, "behind_the_scenes.mp4")
        with open(extra_path, "wb") as f:
            f.write(b"0" * 1_000_000)

        # Legenda e NFO
        with open(os.path.join(subs_dir, "pt-br.srt"), "wb") as f:
            f.write(b"1\n00:00:01,000 --> 00:00:02,000\nTeste\n")
        with open(os.path.join(base_dir, "release.nfo"), "wb") as f:
            f.write(b"Torrent info")

        # Arquivo principal do filme (o maior de todos)
        main_feature_path = os.path.join(base_dir, "Movie.Title.2024.1080p.mkv")
        with open(main_feature_path, "wb") as f:
            f.write(b"0" * 10_000_000)

        selected = find_main_video_file(base_dir)
        self.assertIsNotNone(selected)
        self.assertEqual(os.path.abspath(selected), os.path.abspath(main_feature_path))

    def test_select_largest_video_when_multiple_features(self):
        """Verifica a escolha do maior arquivo entre múltiplos vídeos válidos (ex: CD1 vs CD2 ou cortes)."""
        base_dir = self.temp_dir.name
        cd1 = os.path.join(base_dir, "Movie.CD1.avi")
        cd2_extended = os.path.join(base_dir, "Movie.CD2.Extended.avi")

        with open(cd1, "wb") as f:
            f.write(b"0" * 700_000)
        with open(cd2_extended, "wb") as f:
            f.write(b"0" * 1_400_000)

        selected = find_main_video_file(base_dir)
        self.assertEqual(os.path.abspath(selected), os.path.abspath(cd2_extended))

    def test_fallback_to_sample_when_only_samples_exist(self):
        """Verifica o fallback para o maior sample quando não há outro vídeo disponível."""
        base_dir = self.temp_dir.name
        sample1 = os.path.join(base_dir, "clip_sample.mp4")
        sample2 = os.path.join(base_dir, "trailer_sample.mkv")

        with open(sample1, "wb") as f:
            f.write(b"0" * 50_000)
        with open(sample2, "wb") as f:
            f.write(b"0" * 250_000)

        selected = find_main_video_file(base_dir)
        self.assertEqual(os.path.abspath(selected), os.path.abspath(sample2))

    def test_no_video_files_returns_none(self):
        """Verifica que diretórios sem nenhum vídeo suportado retornam None."""
        base_dir = self.temp_dir.name
        with open(os.path.join(base_dir, "sub.srt"), "wb") as f:
            f.write(b"sub")
        with open(os.path.join(base_dir, "music.mp3"), "wb") as f:
            f.write(b"audio")

        self.assertIsNone(find_main_video_file(base_dir))

    def test_nonexistent_or_empty_input(self):
        """Verifica chamadas defensivas com diretórios inexistentes ou vazios."""
        self.assertIsNone(find_main_video_file("/caminho/completamente/inexistente/xyz"))
        self.assertIsNone(find_main_video_file(""))
        self.assertIsNone(find_main_video_file(self.temp_dir.name))


class TestSubtitleSearchAndCharsetNormalization(unittest.TestCase):
    """Validação da busca e conversão de legendas (find_and_prepare_subtitle)."""

    def setUp(self):
        self.torrent_temp = tempfile.TemporaryDirectory()
        self.work_temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.torrent_temp.cleanup()
        self.work_temp.cleanup()

    def test_cp1252_charset_normalization_to_utf8(self):
        """Verifica se legendas em Português codificadas em CP1252 são convertidas para UTF-8 limpo."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        subs_dir = os.path.join(torrent_dir, "Subs")
        os.makedirs(subs_dir, exist_ok=True)
        srt_file = os.path.join(subs_dir, "Filme.2024.PT-BR.srt")

        sample_cp1252_content = (
            "1\n00:00:01,000 --> 00:00:04,000\n"
            "Não, você não tem coração! É uma lástima.\n\n"
            "2\n00:00:05,000 --> 00:00:08,000\n"
            "Atenção: bênção, emoção, ação e cafeína!\n"
        )
        with open(srt_file, "wb") as f:
            f.write(sample_cp1252_content.encode("cp1252"))

        prepared = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNotNone(prepared)
        self.assertTrue(os.path.exists(prepared))
        self.assertEqual(os.path.abspath(prepared), os.path.abspath(os.path.join(work_dir, "prepared_sub.srt")))

        # Lê como UTF-8 estrito para validar decodificação perfeita
        with open(prepared, "r", encoding="utf-8") as f:
            result_content = f.read()

        self.assertIn("Não, você não tem coração! É uma lástima.", result_content)
        self.assertIn("bênção, emoção, ação e cafeína!", result_content)

    def test_iso8859_1_charset_normalization_to_utf8(self):
        """Verifica se legendas em ISO-8859-1 são corretamente identificadas e convertidas."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        srt_file = os.path.join(torrent_dir, "Legenda_Portugues.srt")
        content_iso = (
            "1\n00:00:10,000 --> 00:00:13,000\n"
            "Você esqueceu o chapéu e a maçã lá fora.\n"
        )
        with open(srt_file, "wb") as f:
            f.write(content_iso.encode("iso-8859-1"))

        prepared = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNotNone(prepared)

        with open(prepared, "r", encoding="utf-8") as f:
            result_text = f.read()

        self.assertIn("Você esqueceu o chapéu e a maçã lá fora.", result_text)

    def test_utf8_subtitle_preserved(self):
        """Verifica se legendas já em UTF-8 são preservadas fielmente."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        srt_file = os.path.join(torrent_dir, "Movie.Portuguese.srt")
        content_utf8 = (
            "1\n00:00:01,000 --> 00:00:03,000\n"
            "Legenda nativa em UTF-8 com acentuação: Árvore, Ímã, Órfão.\n"
        )
        with open(srt_file, "w", encoding="utf-8") as f:
            f.write(content_utf8)

        prepared = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNotNone(prepared)

        with open(prepared, "r", encoding="utf-8") as f:
            self.assertIn("Árvore, Ímã, Órfão", f.read())

    def test_single_subtitle_in_dir_selected(self):
        """Verifica se uma única legenda no pacote é selecionada mesmo sem 'pt' explícito no nome."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        srt_file = os.path.join(torrent_dir, "movie_subs.srt")
        with open(srt_file, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nSingle sub\n")

        prepared = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNotNone(prepared)

    def test_multiple_subtitles_prioritizes_portuguese(self):
        """Verifica que entre múltiplos idiomas, a legenda em português é priorizada sobre as demais."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        subs_dir = os.path.join(torrent_dir, "Subs")
        os.makedirs(subs_dir, exist_ok=True)

        en_sub = os.path.join(subs_dir, "2_English.srt")
        es_sub = os.path.join(subs_dir, "3_Spanish.srt")
        pt_sub = os.path.join(subs_dir, "4_Brazilian_Portuguese.srt")

        # Inglês com tamanho grande
        with open(en_sub, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:05,000\nEnglish line\n" * 100)
        # Espanhol médio
        with open(es_sub, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:05,000\nLínea en español\n" * 50)
        # Português
        with open(pt_sub, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:05,000\nLinha em português\n" * 20)

        prepared = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNotNone(prepared)

        with open(prepared, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Linha em português", content)
        self.assertNotIn("English line", content)

    def test_embedded_subtitle_extraction(self):
        """Verifica a extração de legenda embutida no MKV quando detectada pelo ffprobe."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name

        dummy_video = os.path.join(torrent_dir, "feature.mkv")
        with open(dummy_video, "wb") as f:
            f.write(b"dummy mkv header")

        ffprobe_mock_stdout = json.dumps({
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "ac3"},
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "por", "title": "Português Brasil"}
                }
            ]
        })

        def mock_subprocess_run(cmd, *args, **kwargs):
            cmd_list = [str(c) for c in cmd]
            if "ffprobe" in cmd_list[0]:
                return mock.Mock(returncode=0, stdout=ffprobe_mock_stdout, stderr="")
            elif "ffmpeg" in cmd_list[0]:
                # Simula extração da legenda escrevendo arquivo temporário em work_dir
                temp_ext = os.path.join(work_dir, "temp_extracted.srt")
                with open(temp_ext, "wb") as f:
                    f.write("1\n00:00:01,000 --> 00:00:02,000\nLegenda embutida extraída.\n".encode("cp1252"))
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=mock_subprocess_run):
            prepared = find_and_prepare_subtitle(video_path=dummy_video, torrent_dir=torrent_dir, work_dir=work_dir)
            self.assertIsNotNone(prepared)
            with open(prepared, "r", encoding="utf-8") as f:
                self.assertIn("Legenda embutida extraída.", f.read())

    def test_no_subtitles_found_returns_none(self):
        """Verifica que None é retornado quando nenhuma legenda está presente."""
        torrent_dir = self.torrent_temp.name
        work_dir = self.work_temp.name
        result = find_and_prepare_subtitle(video_path="", torrent_dir=torrent_dir, work_dir=work_dir)
        self.assertIsNone(result)


class TestAudioPortugueseDetection(unittest.TestCase):
    """Validação da detecção de faixas de áudio em português via ffprobe (is_audio_portuguese)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dummy_video = os.path.join(self.temp_dir.name, "video.mkv")
        with open(self.dummy_video, "wb") as f:
            f.write(b"0" * 1024)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _mock_ffprobe_audio(self, streams):
        return mock.Mock(returncode=0, stdout=json.dumps({"streams": streams}), stderr="")

    def test_audio_language_codes_detected(self):
        """Verifica o reconhecimento dos códigos de idioma ISO para português (por, pob, pt, pt-br)."""
        test_codes = ["por", "pob", "pt", "pt-br", "pt_br", "portuguese"]
        for code in test_codes:
            with self.subTest(lang_code=code):
                streams = [{"index": 1, "codec_type": "audio", "tags": {"language": code}}]
                with mock.patch("subprocess.run", return_value=self._mock_ffprobe_audio(streams)):
                    self.assertTrue(is_audio_portuguese(self.dummy_video))

    def test_audio_title_keywords_detected(self):
        """Verifica a identificação por palavras-chave no título (dublado, nacional, etc.)."""
        keywords = [
            "Áudio Dublado em Português",
            "Filme Nacional Brasileiro",
            "Dublado 5.1",
            "Portuguese Stereo",
            "Brazilian Audio",
        ]
        for kw in keywords:
            with self.subTest(title=kw):
                streams = [{"index": 1, "codec_type": "audio", "tags": {"language": "und", "title": kw}}]
                with mock.patch("subprocess.run", return_value=self._mock_ffprobe_audio(streams)):
                    self.assertTrue(is_audio_portuguese(self.dummy_video))

    def test_non_portuguese_audio_returns_false(self):
        """Verifica que streams puramente em outros idiomas retornam False."""
        streams = [
            {"index": 1, "codec_type": "audio", "tags": {"language": "eng", "title": "English DTS-HD"}},
            {"index": 2, "codec_type": "audio", "tags": {"language": "spa", "title": "Español Latino"}},
            {"index": 3, "codec_type": "audio", "tags": {"language": "fra", "title": "Français"}},
        ]
        with mock.patch("subprocess.run", return_value=self._mock_ffprobe_audio(streams)):
            self.assertFalse(is_audio_portuguese(self.dummy_video))

    def test_multi_audio_tracks_with_portuguese(self):
        """Verifica que faixas múltiplas onde uma é em português retornam True."""
        streams = [
            {"index": 1, "codec_type": "audio", "tags": {"language": "eng", "title": "Original"}},
            {"index": 2, "codec_type": "audio", "tags": {"language": "por", "title": "Dublado PT-BR"}},
        ]
        with mock.patch("subprocess.run", return_value=self._mock_ffprobe_audio(streams)):
            self.assertTrue(is_audio_portuguese(self.dummy_video))

    def test_empty_or_nonexistent_returns_false(self):
        """Verifica comportamento defensivo para arquivo inexistente ou sem streams de áudio."""
        self.assertFalse(is_audio_portuguese("/arquivo/fantasma.mkv"))
        with mock.patch("subprocess.run", return_value=self._mock_ffprobe_audio([])):
            self.assertFalse(is_audio_portuguese(self.dummy_video))


class TestBuildFFmpegTranscodeCommand(unittest.TestCase):
    """Validação da geração de linha de comando FFmpeg para o chip MStar 2013."""

    def test_hardsub_burnin_when_subtitle_present(self):
        """
        Quando srt_path estiver presente, deve aplicar hardsub burn-in via libass
        e transcodificar com os parâmetros rigorosos de compatibilidade Samsung:
        - -vf subtitles='...'
        - -c:v libx264
        - -profile:v high
        - -level 4.1
        - -c:a ac3
        - -b:a 384k
        - -ar 48000
        - -ac 2
        - -movflags +faststart
        - -f mp4
        """
        in_path = "/media/in/movie.mkv"
        out_path = "/media/out/movie.mp4"
        srt_path = "/media/subs/pt_sub.srt"

        cmd = build_ffmpeg_transcode_command(
            video_path=in_path,
            out_mp4_path=out_path,
            srt_path=srt_path,
            can_copy_video=True,  # Mesmo sendo copyable, presença de legenda deve forçar burn-in
        )

        # Validações estruturais básicas
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-y", cmd)
        self.assertIn("-i", cmd)
        self.assertIn(in_path, cmd)
        self.assertEqual(cmd[-1], out_path)

        # Validação do filtro de legenda hardsub
        self.assertIn("-vf", cmd)
        vf_idx = cmd.index("-vf")
        vf_arg = cmd[vf_idx + 1]
        self.assertTrue(vf_arg.startswith("subtitles="))
        self.assertIn("FontSize=22", vf_arg)

        # Validação dos parâmetros de vídeo H.264
        self.assertIn("-c:v", cmd)
        cv_idx = cmd.index("-c:v")
        self.assertEqual(cmd[cv_idx + 1], "libx264")

        self.assertIn("-profile:v", cmd)
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "high")

        self.assertIn("-level", cmd)
        self.assertEqual(cmd[cmd.index("-level") + 1], "4.1")

        self.assertIn("-g", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "30")

        self.assertIn("-r", cmd)
        self.assertEqual(cmd[cmd.index("-r") + 1], "30")

        self.assertIn("-bufsize", cmd)
        self.assertEqual(cmd[cmd.index("-bufsize") + 1], "5000k")

        self.assertIn("-b:v", cmd)
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "7000k")

        self.assertIn("-maxrate", cmd)
        self.assertEqual(cmd[cmd.index("-maxrate") + 1], "8000k")

        # Validação estrita do áudio AC-3 Dolby Digital
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "ac3")

        self.assertIn("-b:a", cmd)
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "384k")

        self.assertIn("-ar", cmd)
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

        self.assertIn("-ac", cmd)
        self.assertEqual(cmd[cmd.index("-ac") + 1], "2")

        # Validação do átomo moov no início (faststart) e contêiner MP4
        self.assertIn("-movflags", cmd)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")
        self.assertIn("-f", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "mp4")

    def test_stream_copy_when_no_subtitle_and_copy_allowed(self):
        """
        Quando srt_path for None e can_copy_video for True, deve realizar cópia rápida:
        - -c:v copy
        - -c:a ac3
        - sem qualquer filtro -vf
        """
        in_path = "/media/in/movie_compatible.mp4"
        out_path = "/media/out/movie_fast.mp4"

        cmd = build_ffmpeg_transcode_command(
            video_path=in_path,
            out_mp4_path=out_path,
            srt_path=None,
            can_copy_video=True,
        )

        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")

        # Áudio sempre recodificado para AC-3 Dolby Digital por segurança do chip MStar
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "ac3")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "384k")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "2")

        # Não deve haver filtro de vídeo no modo copy
        self.assertNotIn("-vf", cmd)
        self.assertNotIn("libx264", cmd)

    def test_transcode_when_no_subtitle_and_copy_disallowed(self):
        """
        Quando srt_path for None mas can_copy_video for False (ex: HEVC/10-bit),
        deve aplicar transcodificação completa para 1080p H.264 High@L4.1.
        """
        in_path = "/media/in/hevc_10bit.mkv"
        out_path = "/media/out/transcoded.mp4"

        cmd = build_ffmpeg_transcode_command(
            video_path=in_path,
            out_mp4_path=out_path,
            srt_path=None,
            can_copy_video=False,
        )

        self.assertIn("-vf", cmd)
        vf_arg = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=1920:1080", vf_arg)

        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "high")
        self.assertEqual(cmd[cmd.index("-level") + 1], "4.1")

        self.assertIn("-g", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "30")

        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "ac3")

    def test_escape_ffmpeg_filter_path(self):
        """Verifica o escape seguro de dois pontos, barras invertidas e aspas simples para libass."""
        path_win = r"C:\media\test:movie\sub's.srt"
        escaped = escape_ffmpeg_filter_path(path_win)
        self.assertNotIn("\\", escaped.replace(r"\:", "").replace(r"\'", ""))
        self.assertIn(r"\:", escaped)
        self.assertIn(r"\'", escaped)


class TestHardwareCompatibilityHelpers(unittest.TestCase):
    """Validação das regras de compatibilidade do chip Samsung MStar 2013."""

    def test_sar_compatibility(self):
        """Verifica quais proporções de aspecto de pixel (SAR) são aceitas sem distorção."""
        self.assertTrue(is_sar_samsung_compatible("1:1"))
        self.assertTrue(is_sar_samsung_compatible("1/1"))
        self.assertTrue(is_sar_samsung_compatible("160:159"))
        self.assertTrue(is_sar_samsung_compatible("64:45"))
        self.assertTrue(is_sar_samsung_compatible(""))
        self.assertTrue(is_sar_samsung_compatible(None))
        # SAR anamórfico extremo incompatível
        self.assertFalse(is_sar_samsung_compatible("2:1"))
        self.assertFalse(is_sar_samsung_compatible("3:1"))

    def test_can_stream_copy_video_compatible(self):
        """Verifica se streams H.264 1080p <= L4.2 com YUV420p são autorizados para stream copy."""
        mock_ffprobe_h264 = json.dumps({
            "streams": [{
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "level": 41,
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1"
            }]
        })
        with tempfile.NamedTemporaryFile() as tmp:
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=mock_ffprobe_h264)):
                self.assertTrue(can_stream_copy_video(tmp.name))

    def test_can_stream_copy_video_incompatible_cases(self):
        """Verifica a rejeição de codecs incompatíveis (HEVC), 4K, 10-bit ou níveis altos."""
        incompatible_streams = [
            # HEVC / H.265
            {"codec_name": "hevc", "pix_fmt": "yuv420p", "level": 40, "width": 1920, "height": 1080},
            # Resolução 4K (Ultra HD)
            {"codec_name": "h264", "pix_fmt": "yuv420p", "level": 41, "width": 3840, "height": 2160},
            # Nível de perfil acima de 4.2 (ex: L5.1)
            {"codec_name": "h264", "pix_fmt": "yuv420p", "level": 51, "width": 1920, "height": 1080},
            # 10-bit (yuv420p10le)
            {"codec_name": "h264", "pix_fmt": "yuv420p10le", "level": 41, "width": 1920, "height": 1080},
        ]
        with tempfile.NamedTemporaryFile() as tmp:
            for st in incompatible_streams:
                with self.subTest(codec=st.get("codec_name"), lvl=st.get("level"), pix=st.get("pix_fmt")):
                    mock_out = json.dumps({"streams": [st]})
                    with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=mock_out)):
                        self.assertFalse(can_stream_copy_video(tmp.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
