"""
Módulo standalone para download e processamento de torrents/magnets no USB-Stream-TV.

Compatibilidade de Hardware Obrigatória:
- Decodificador Samsung MStar 2013 (TV Samsung Plasma PL51F4000).
- Áudio estritamente AC-3 Dolby Digital (384k, 48000 Hz, stereo).
- Vídeo H.264 High Profile Level 4.1 (1080p @ 30fps max, VBV bufsize 5000k, GOP 30).
- Legendas obrigatórias gravadas diretamente no vídeo (burn-in / hardsub) via libass (-vf subtitles=...).
"""

import os
import re
import json
import subprocess
import urllib.parse
from contextlib import nullcontext
from typing import Optional, List, Dict, Any, Callable

# Extensões de vídeo suportadas para detecção do arquivo principal
VALID_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov"}

# Idiomas e palavras-chave de identificação para dublagem/legendas em Português
PT_LANG_CODES = {"por", "pob", "pt", "pt-br", "pt_br", "portuguese"}
PT_TITLE_KEYWORDS = (
    "português",
    "portugues",
    "pt-br",
    "pt_br",
    "dublado",
    "nacional",
    "brazilian",
    "portuguese",
)


def is_torrent_url(url: str) -> bool:
    """
    Verifica se a URL fornecida é um magnet link ou aponta para um arquivo .torrent.

    Args:
        url: URL ou string do link.

    Returns:
        True se for magnet link ou .torrent, False caso contrário.
    """
    if not url or not isinstance(url, str):
        return False
    u = url.strip().lower()
    return u.startswith("magnet:?") or u.endswith(".torrent") or ".torrent?" in u


def extract_torrent_title(magnet_url: str, fallback: str = "") -> str:
    """
    Extrai e decodifica via URL-decode o parâmetro 'dn=' (Display Name) do magnet link.
    Limpa pontos, underscores, colchetes e chaves para produzir um título limpo e legível.

    Args:
        magnet_url: Magnet URL completa.
        fallback: Título alternativo de fallback caso 'dn=' não exista.

    Returns:
        Título limpo formatado para exibição e gravação.
    """
    dn = ""
    if magnet_url and isinstance(magnet_url, str):
        try:
            parsed = urllib.parse.urlparse(magnet_url)
            query = parsed.query or (magnet_url.split("?", 1)[1] if "?" in magnet_url else "")
            qs = urllib.parse.parse_qs(query)
            if "dn" in qs and qs["dn"]:
                dn = qs["dn"][0].strip()
        except Exception:
            pass

        if not dn:
            m = re.search(r'[?&]dn=([^&]+)', magnet_url)
            if m:
                dn = urllib.parse.unquote_plus(m.group(1)).strip()

    raw_title = dn if dn else (fallback.strip() if fallback else "")
    if not raw_title:
        return ""

    # Substitui pontos, underscores e colchetes/chaves por espaços
    cleaned = re.sub(r'[._\[\]{}]+', ' ', raw_title)
    # Colapsa múltiplos espaços em branco e remove espaços nas bordas
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else raw_title


def run_aria2_download(
    magnet_url: str,
    download_dir: str,
    task_dict: Optional[dict] = None,
    task_lock: Any = None,
    is_cancelled_fn: Optional[Callable[[], bool]] = None,
    running_procs_dict: Optional[dict] = None,
    task_id: Optional[str] = None,
) -> bool:
    """
    Executa o download de um magnet/torrent via aria2c com telemetria em tempo real
    e suporte a cancelamento cooperativo.

    Args:
        magnet_url: URL magnet a ser baixada.
        download_dir: Diretório de destino para os arquivos do torrent.
        task_dict: Dicionário da tarefa para atualização de progresso/status/velocidade.
        task_lock: Mutex/Lock para sincronização de acesso a task_dict.
        is_cancelled_fn: Função opcional que retorna True caso a tarefa deva ser abortada.
        running_procs_dict: Dicionário opcional para registrar o processo sob task_id.
        task_id: Identificador da tarefa.

    Returns:
        True se o download concluiu com returncode == 0, False caso contrário ou cancelado.
    """
    os.makedirs(download_dir, exist_ok=True)

    cmd = [
        "aria2c",
        "--enable-dht=true",
        "--bt-enable-lpd=true",
        "--enable-peer-exchange=true",
        "--bt-stop-timeout=600",
        "--seed-time=0",
        "--summary-interval=1",
        "--file-allocation=none",
        "--max-connection-per-server=16",
        "--split=16",
        "--follow-torrent=mem",
        f"--dir={download_dir}",
        magnet_url,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )

    if running_procs_dict is not None and task_id is not None:
        running_procs_dict[task_id] = proc

    pct_pat = re.compile(r"\((\d+(?:\.\d+)?)%\)")
    dl_pat = re.compile(r"DL:([0-9.]+[A-Za-z]+)")
    cn_pat = re.compile(r"CN:(\d+)")
    sd_pat = re.compile(r"SD:(\d+)")

    try:
        for line in iter(proc.stdout.readline, ""):
            # Verificação contínua de cancelamento
            cancelled = False
            if is_cancelled_fn and is_cancelled_fn():
                cancelled = True
            elif task_dict is not None:
                with task_lock if task_lock else nullcontext():
                    if task_dict.get("cancelled") or task_dict.get("status") == "cancelled":
                        cancelled = True

            if cancelled:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait()
                    except Exception:
                        pass
                return False

            line_clean = line.strip()
            if not line_clean:
                continue

            # Verifica evento de download completado
            if "(OK):download completed" in line_clean or "Download complete:" in line_clean:
                if task_dict is not None:
                    with task_lock if task_lock else nullcontext():
                        task_dict["progress"] = 50
                        task_dict["status_msg"] = "Download concluído (100%). Analisando arquivos..."

            # Raspagem de progresso, velocidade e peers
            m_pct = pct_pat.search(line_clean)
            m_dl = dl_pat.search(line_clean)
            m_cn = cn_pat.search(line_clean)
            m_sd = sd_pat.search(line_clean)

            if m_pct or m_dl or m_cn:
                raw_pct = float(m_pct.group(1)) if m_pct else 0.0
                # Mapeia progresso de 0..100% para a fatia de VOD de 5% a 50%
                vod_progress = min(50, max(5, int(5 + (raw_pct / 100.0) * 45)))

                speed_str = "0 B/s"
                if m_dl:
                    raw_spd = m_dl.group(1)
                    s_clean = re.sub(r'([0-9.]+)\s*([A-Za-z]+)', r'\1 \2', raw_spd)
                    s_clean = s_clean.replace("iB", "B")
                    if not s_clean.endswith("/s"):
                        s_clean += "/s"
                    speed_str = s_clean

                peers_val = m_cn.group(1) if m_cn else "0"
                status_msg = f"Baixando torrent: {int(raw_pct)}% ({speed_str}, {peers_val} peers)..."

                if task_dict is not None:
                    with task_lock if task_lock else nullcontext():
                        task_dict["progress"] = vod_progress
                        if m_dl:
                            task_dict["speed"] = speed_str
                        task_dict["status_msg"] = status_msg

        proc.wait()
        if proc.returncode == 0:
            if task_dict is not None:
                with task_lock if task_lock else nullcontext():
                    task_dict["progress"] = max(task_dict.get("progress", 0), 50)
                    task_dict["status_msg"] = "Download do torrent concluído com sucesso."
            return True
        return False
    finally:
        if running_procs_dict is not None and task_id is not None:
            running_procs_dict.pop(task_id, None)


def find_main_video_file(download_dir: str) -> Optional[str]:
    """
    Varre recursivamente o diretório procurando arquivos de vídeo (.mkv, .mp4, .avi, .ts, .m4v, .mov).
    Ignora arquivos que contenham 'sample' no nome caso existam arquivos normais (não-sample).
    Seleciona o maior arquivo de vídeo encontrado.

    Args:
        download_dir: Diretório onde o torrent foi descompactado/baixado.

    Returns:
        Caminho absoluto do arquivo principal de vídeo, ou None se nenhum for encontrado.
    """
    if not download_dir or not os.path.exists(download_dir):
        return None

    non_samples = []
    samples = []

    for root, _, files in os.walk(download_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in VALID_VIDEO_EXTENSIONS:
                full_path = os.path.join(root, fname)
                try:
                    fsize = os.path.getsize(full_path)
                except OSError:
                    continue
                if "sample" in fname.lower():
                    samples.append((fsize, full_path))
                else:
                    non_samples.append((fsize, full_path))

    if non_samples:
        non_samples.sort(key=lambda item: item[0], reverse=True)
        return os.path.abspath(non_samples[0][1])
    elif samples:
        samples.sort(key=lambda item: item[0], reverse=True)
        return os.path.abspath(samples[0][1])

    return None


def _read_and_normalize_srt(src_srt_path: str, work_dir: str) -> Optional[str]:
    """
    Lê o arquivo de legendas detectando automaticamente a codificação
    (UTF-8, CP1252, Latin1) e regrava como UTF-8 limpo no work_dir.

    Args:
        src_srt_path: Caminho do arquivo .srt de origem.
        work_dir: Diretório de trabalho para gravação da legenda normalizada.

    Returns:
        Caminho absoluto do arquivo .srt normalizado, ou None em caso de falha.
    """
    try:
        with open(src_srt_path, "rb") as f:
            raw_bytes = f.read()

        content = None
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                content = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if content is None:
            content = raw_bytes.decode("utf-8", errors="replace")

        os.makedirs(work_dir, exist_ok=True)
        out_srt = os.path.join(work_dir, "prepared_sub.srt")
        with open(out_srt, "w", encoding="utf-8") as f:
            f.write(content)

        return os.path.abspath(out_srt)
    except Exception:
        return None


def find_and_prepare_subtitle(video_path: str, torrent_dir: str, work_dir: str) -> Optional[str]:
    """
    Localiza legendas em Português em 2 níveis:
    - Nível 1: Legendas embutidas no vídeo (via ffprobe). Verifica stream com codec_type == 'subtitle'
      e language in ('por', 'pob', 'pt', 'pt-br') ou títulos contendo palavras-chave PT.
      Extrai a legenda via ffmpeg.
    - Nível 2: Arquivos .srt externos em torrent_dir. Prioriza nomes/pastas correspondentes a PT.
      Se houver exatamente um único arquivo .srt no diretório inteiro, seleciona-o.

    Normaliza a legenda encontrada (UTF-8, CP1252, Latin1) para um arquivo .srt UTF-8 limpo em work_dir.

    Args:
        video_path: Caminho do arquivo de vídeo.
        torrent_dir: Diretório completo do download do torrent.
        work_dir: Diretório temporário de trabalho para saída da legenda.

    Returns:
        Caminho absoluto da legenda preparada, ou None se nenhuma legenda PT for encontrada.
    """
    os.makedirs(work_dir, exist_ok=True)

    # Tier 1: Legendas embutidas no contêiner de vídeo
    if video_path and os.path.exists(video_path):
        try:
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", video_path,
            ]
            res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            meta = json.loads(res.stdout)
            streams = meta.get("streams", [])

            for st in streams:
                if st.get("codec_type") == "subtitle":
                    idx = st.get("index")
                    tags = st.get("tags") or {}
                    lang = ""
                    title = ""
                    for k, v in tags.items():
                        k_low = k.lower()
                        if k_low == "language":
                            lang = str(v).lower().strip()
                        elif k_low == "title":
                            title = str(v).lower().strip()

                    is_pt = (lang in PT_LANG_CODES) or any(kw in title for kw in PT_TITLE_KEYWORDS)
                    if is_pt and idx is not None:
                        temp_extracted = os.path.join(work_dir, "temp_extracted.srt")
                        extract_cmd = [
                            "ffmpeg", "-y", "-i", video_path,
                            "-map", f"0:{idx}",
                            temp_extracted,
                        ]
                        ext_res = subprocess.run(extract_cmd, capture_output=True, text=True)
                        if (
                            ext_res.returncode == 0
                            and os.path.exists(temp_extracted)
                            and os.path.getsize(temp_extracted) > 0
                        ):
                            normalized = _read_and_normalize_srt(temp_extracted, work_dir)
                            try:
                                os.remove(temp_extracted)
                            except Exception:
                                pass
                            if normalized:
                                return normalized
        except Exception:
            pass

    # Tier 2: Legendas externas (.srt) no diretório do torrent
    if torrent_dir and os.path.exists(torrent_dir):
        all_srts = []
        for root, _, files in os.walk(torrent_dir):
            for f in files:
                if f.lower().endswith(".srt"):
                    all_srts.append(os.path.join(root, f))

        if all_srts:
            pt_candidates = []
            for srt_path in all_srts:
                base = os.path.basename(srt_path).lower()
                rel_dir = os.path.relpath(os.path.dirname(srt_path), torrent_dir).lower()

                # Verifica se nome ou pasta contém palavras-chave de Português
                if any(kw in base for kw in PT_TITLE_KEYWORDS) or any(kw in rel_dir for kw in PT_TITLE_KEYWORDS):
                    pt_candidates.append(srt_path)
                else:
                    # Verifica códigos de idioma isolados por pontuação (ex: movie.pt.srt)
                    tokens = set(re.split(r'[^a-zA-Z0-9]+', f"{base} {rel_dir}"))
                    if tokens.intersection(PT_LANG_CODES):
                        pt_candidates.append(srt_path)

            if pt_candidates:
                # Escolhe o maior arquivo entre os candidatos PT (evita amostras vazias)
                pt_candidates.sort(
                    key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
                    reverse=True,
                )
                return _read_and_normalize_srt(pt_candidates[0], work_dir)
            elif len(all_srts) == 1:
                # Única legenda disponível no pacote
                return _read_and_normalize_srt(all_srts[0], work_dir)

    return None


def is_audio_portuguese(video_path: str) -> bool:
    """
    Inspeciona os streams de áudio do arquivo via ffprobe.
    Retorna True se algum stream possuir idioma em ('por', 'pob', 'pt', 'pt-br')
    ou título contendo 'português', 'dublado', 'nacional' ou 'brazilian'.

    Args:
        video_path: Caminho do arquivo de vídeo.

    Returns:
        True se houver faixa de áudio em português/dublada, False caso contrário.
    """
    if not video_path or not os.path.exists(video_path):
        return False

    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "a",
            video_path,
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        meta = json.loads(res.stdout)
        streams = meta.get("streams", [])

        for st in streams:
            tags = st.get("tags") or {}
            lang = ""
            title = ""
            for k, v in tags.items():
                k_low = k.lower()
                if k_low == "language":
                    lang = str(v).lower().strip()
                elif k_low == "title":
                    title = str(v).lower().strip()

            if lang in PT_LANG_CODES:
                return True
            if any(kw in title for kw in ("português", "portugues", "dublado", "nacional", "brazilian")):
                return True
    except Exception:
        return False

    return False


def is_sar_samsung_compatible(sar: str) -> bool:
    """
    Verifica se o Sample Aspect Ratio (SAR) é compatível com o decodificador Samsung TV.
    """
    if not sar:
        return True
    s = str(sar).strip()
    if s in ("1:1", "1/1", "0:1", "", "160:159", "64:45", "40:33", "12:11", "10:11"):
        return True
    try:
        sep = ":" if ":" in s else ("/" if "/" in s else None)
        if sep:
            num, den = map(float, s.split(sep, 1))
            if den > 0:
                ratio = num / den
                if 0.85 <= ratio <= 1.18:
                    return True
    except Exception:
        pass
    return False


def can_stream_copy_video(video_path: str) -> bool:
    """
    Verifica se o vídeo é estritamente compatível para cópia direta (-c:v copy)
    pelo decodificador MStar 2013 da Samsung TV PL51F4000:
    - Codec H.264 (avc1/h264)
    - Pixel format YUV420p (ou yuvj420p)
    - Level <= 4.2
    - Resolução <= 1920x1080
    - SAR compatível
    """
    if not video_path or not os.path.exists(video_path):
        return False
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,level,pix_fmt,width,height,sample_aspect_ratio",
            "-of", "json", video_path,
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        v_meta = json.loads(res.stdout)
        st = (v_meta.get("streams") or [{}])[0]
        codec = st.get("codec_name", "").lower()
        pix = st.get("pix_fmt", "").lower()
        lvl = int(st.get("level", 99) or 99)
        w = int(st.get("width", 0) or 0)
        h = int(st.get("height", 0) or 0)
        sar = (st.get("sample_aspect_ratio") or "").strip()
        is_compatible_sar = is_sar_samsung_compatible(sar)
        if (
            codec in ("h264", "avc1")
            and pix in ("yuv420p", "yuvj420p", "")
            and lvl <= 42
            and w <= 1920
            and h <= 1080
            and is_compatible_sar
        ):
            return True
    except Exception:
        pass
    return False


def escape_ffmpeg_filter_path(path: str) -> str:
    """
    Escapa caminhos para uso seguro em filtros de legenda do FFmpeg.
    Dois pontos e barras invertidas são escapados.
    """
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_ffmpeg_transcode_command(
    video_path: str,
    out_mp4_path: str,
    srt_path: Optional[str],
    can_copy_video: bool,
) -> List[str]:
    """
    Constrói o comando FFmpeg rigorosamente ajustado para o chipset Samsung MStar 2013:

    - Se srt_path estiver presente:
      Gravação obrigatória de legendas (burn-in / hardsub) via libass:
      -vf subtitles='<escaped_srt_path>':force_style='FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,Outline=2,Shadow=1'
      Áudio AC-3 estéreo: -c:a ac3 -b:a 384k -ar 48000 -ac 2
      Vídeo H.264: -c:v libx264 -preset veryfast -profile:v high -level 4.1 -b:v 7000k -maxrate 8000k -bufsize 5000k -g 30 -r 30
      Contêiner: -movflags +faststart -f mp4

    - Se srt_path for None e can_copy_video for True:
      Cópia rápida de fluxo: -c:v copy -c:a ac3 -b:a 384k -ar 48000 -ac 2 -movflags +faststart -f mp4

    - Se srt_path for None e can_copy_video for False:
      Transcodificação completa:
      -vf scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2 -r 30
      -c:v libx264 -preset veryfast -profile:v high -level 4.1 -b:v 7000k -maxrate 8000k -bufsize 5000k -g 30
      -c:a ac3 -b:a 384k -ar 48000 -ac 2 -movflags +faststart -f mp4

    Args:
        video_path: Caminho do arquivo original de vídeo.
        out_mp4_path: Caminho de destino do arquivo MP4 final.
        srt_path: Caminho do arquivo .srt para burn-in, ou None se não houver legendas.
        can_copy_video: Se o vídeo original pode ser repassado sem re-codificação (-c:v copy).

    Returns:
        Lista de argumentos para execução do FFmpeg.
    """
    if srt_path:
        escaped_srt = escape_ffmpeg_filter_path(srt_path)
        vf_style = (
            "FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BackColour=&H80000000,Outline=2,Shadow=1"
        )
        vf_filter = f"subtitles='{escaped_srt}':force_style='{vf_style}'"

        return [
            "ffmpeg", "-y", "-i", video_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
            "-vf", vf_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-b:v", "7000k", "-maxrate", "8000k", "-bufsize", "5000k", "-g", "30", "-r", "30",
            "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            out_mp4_path,
        ]
    elif can_copy_video:
        return [
            "ffmpeg", "-y", "-i", video_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
            "-c:v", "copy",
            "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            out_mp4_path,
        ]
    else:
        return [
            "ffmpeg", "-y", "-i", video_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-b:v", "7000k", "-maxrate", "8000k", "-bufsize", "5000k", "-g", "30",
            "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            out_mp4_path,
        ]
