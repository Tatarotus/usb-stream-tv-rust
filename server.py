#!/usr/bin/env python3
"""
USB Stream TV - High-Performance Multi-Channel Hub & Remote
Servidor central de streaming com troca dinâmica de canal em tempo real,
normalização de áudio/vídeo e controle remoto web mobile-first.
"""

import sys
import os
import time
import subprocess
import threading
import queue
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import json
import re
import hashlib
import shutil
import socket
import collections
import base64
import signal

try:
    from pwa_icons import PWA_ICON_192_B64, PWA_ICON_512_B64
except Exception:
    PWA_ICON_192_B64 = ""
    PWA_ICON_512_B64 = ""

home_local_bin = os.path.expanduser("~/.local/bin")
if home_local_bin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = home_local_bin + os.pathsep + os.environ.get("PATH", "")

YT_DLP_BIN = shutil.which("yt-dlp") or os.path.join(home_local_bin, "yt-dlp")

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))
AUTH_PIN = os.environ.get("TV_PIN", "1233")
TABLET_TOKEN = os.environ.get("TABLET_TOKEN", "").strip()
STANDBY_TIMEOUT = float(os.environ.get("STANDBY_TIMEOUT", 90.0))
XTREAM_UPSTREAM = os.environ.get("XTREAM_UPSTREAM", "")
XTREAM_STREAM_RE = re.compile(r'^/(?:(live|movie|series)/)?([^/]+)/([^/]+)/(\d+)(?:\.([a-zA-Z0-9]+))?$')
XTREAM_CACHE = {}
XTREAM_CACHE_LOCK = threading.Lock()

RESIDENTIAL_HTTP_PROXY = os.environ.get("RESIDENTIAL_HTTP_PROXY", "")
RESIDENTIAL_SOCKS_PROXY = os.environ.get("RESIDENTIAL_SOCKS_PROXY", "")

def is_proxy_alive(proxy_url):
    if not proxy_url:
        return False
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (1080 if "socks" in (parsed.scheme or "") else 8118)
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False

def get_effective_socks_proxy():
    # Prefer smart failover router on port 1082
    router_socks = "socks5h://172.20.0.1:1082"
    if is_proxy_alive(router_socks):
        return router_socks
    if RESIDENTIAL_SOCKS_PROXY and is_proxy_alive(RESIDENTIAL_SOCKS_PROXY):
        return RESIDENTIAL_SOCKS_PROXY
    return ""
STREAM_RESOLUTION = os.environ.get("STREAM_RESOLUTION", "1080p").lower()
SLATE_GAP_THRESHOLD = float(os.environ.get("SLATE_GAP_THRESHOLD", 8.0))
SLATE_AUTO_SWITCH_TIMEOUT = float(os.environ.get("SLATE_AUTO_SWITCH_TIMEOUT", 0))


def _load_env_file(env_file):
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k:
                        if k not in os.environ or not os.environ[k]:
                            os.environ[k] = v
        except Exception:
            pass

_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
XTREAM_UPSTREAM = os.environ.get("XTREAM_UPSTREAM", "")

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.environ.get("CHANNELS_FILE", os.path.join(CONFIG_DIR, "channels.json"))
MOVIES_DIR = os.environ.get("MOVIES_DIR", os.path.join(CONFIG_DIR, "filmes"))
EVENT_LOG = os.path.join(CONFIG_DIR, "server_events.log")
VOD_DIR = os.environ.get("VOD_DIR", os.path.join(CONFIG_DIR, "vod_cache"))
os.makedirs(VOD_DIR, exist_ok=True)
CATALOG_CACHE_DIR = os.environ.get("CATALOG_CACHE_DIR", os.path.join(CONFIG_DIR, "catalog_cache"))
os.makedirs(CATALOG_CACHE_DIR, exist_ok=True)
POSTER_CACHE_DIR = os.path.join(CATALOG_CACHE_DIR, "posters")
os.makedirs(POSTER_CACHE_DIR, exist_ok=True)
XTREAM_USER = os.environ.get("XTREAM_USER", "")
XTREAM_PASS = os.environ.get("XTREAM_PASS", "")

def _discover_xtream_credentials():
    global XTREAM_USER, XTREAM_PASS, XTREAM_UPSTREAM
    if XTREAM_USER and XTREAM_PASS and XTREAM_UPSTREAM:
        return
    candidates = [
        os.path.join(CONFIG_DIR, ".env"),
        os.path.join(os.path.dirname(CONFIG_DIR), ".env"),
        CHANNELS_FILE,
        os.path.join(CONFIG_DIR, "channels_deploy.json"),
        os.path.join(CONFIG_DIR, "channels.json"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            try:
                if p.endswith(".env"):
                    _load_env_file(p)
                    XTREAM_USER = XTREAM_USER or os.environ.get("XTREAM_USER", "")
                    XTREAM_PASS = XTREAM_PASS or os.environ.get("XTREAM_PASS", "")
                    if not XTREAM_UPSTREAM:
                        XTREAM_UPSTREAM = os.environ.get("XTREAM_UPSTREAM", "")
                elif p.endswith(".json"):
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    srv = data.get("servidor", {})
                    if srv.get("usuario") and srv.get("senha"):
                        XTREAM_USER = XTREAM_USER or srv.get("usuario")
                        XTREAM_PASS = XTREAM_PASS or srv.get("senha")
                    if not XTREAM_UPSTREAM and srv.get("url_base"):
                        XTREAM_UPSTREAM = srv.get("url_base")
                    for c in data.get("canais", []):
                        u = c.get("url", "")
                        m = re.search(r'/(?:live|movie|series)/([^/]+)/([^/]+)/\d+', u)
                        if m and m.group(1) not in ("{XTREAM_USER}", ""):
                            XTREAM_USER = XTREAM_USER or m.group(1)
                            XTREAM_PASS = XTREAM_PASS or m.group(2)
                            break
            except Exception:
                pass
        if XTREAM_USER and XTREAM_PASS and XTREAM_UPSTREAM:
            break

    if not XTREAM_UPSTREAM:
        XTREAM_UPSTREAM = os.environ.get("XTREAM_UPSTREAM", "") or "http://studut.shop:80"

_discover_xtream_credentials()
if not XTREAM_UPSTREAM:
    XTREAM_UPSTREAM = "http://studut.shop:80"
VOD_TASKS = {}
VOD_TASKS_LOCK = threading.Lock()
ACTIVE_VOD_TASK = None
VOD_SEMAPHORE = threading.Semaphore(2)
VOD_RUNNING_PROCS = {}
VOD_RUNNING_PROCS_LOCK = threading.Lock()
VOD_RUNNING_THREADS = set()
VOD_RUNNING_THREADS_LOCK = threading.Lock()

def _vod_subproc_setup():
    os.setsid()
    try:
        os.nice(15)
    except Exception:
        pass

VOD_META_FILE = os.path.join(VOD_DIR, "vod_meta.json")

def load_vod_tasks():
    global VOD_TASKS
    if os.path.exists(VOD_META_FILE):
        try:
            with open(VOD_META_FILE, "r") as f:
                VOD_TASKS = json.load(f)
        except Exception:
            VOD_TASKS = {}

def save_vod_tasks():
    try:
        with open(VOD_META_FILE, "w") as f:
            json.dump(VOD_TASKS, f, indent=2)
    except Exception:
        pass

load_vod_tasks()

ACTIVE_VOD_FILE = os.path.join(VOD_DIR, "active_vod.txt")
LAST_VOD_STREAM_TIME = 0.0

def load_active_vod():
    global ACTIVE_VOD_TASK
    try:
        if os.path.exists(ACTIVE_VOD_FILE):
            with open(ACTIVE_VOD_FILE, "r", encoding="utf-8") as f:
                tid = f.read().strip()
                if tid and (tid in VOD_TASKS or os.path.exists(os.path.join(VOD_DIR, f"{tid}.mp4"))):
                    ACTIVE_VOD_TASK = tid
                    return tid
    except Exception:
        pass
    return None

def set_active_vod(task_id):
    global ACTIVE_VOD_TASK
    ACTIVE_VOD_TASK = task_id
    try:
        if task_id:
            with open(ACTIVE_VOD_FILE, "w", encoding="utf-8") as f:
                f.write(task_id)
        else:
            if os.path.exists(ACTIVE_VOD_FILE):
                os.remove(ACTIVE_VOD_FILE)
    except Exception:
        pass

def touch_vod_stream(task_id=None):
    global LAST_VOD_STREAM_TIME, ACTIVE_VOD_TASK
    LAST_VOD_STREAM_TIME = time.time()
    if task_id and ACTIVE_VOD_TASK != task_id:
        set_active_vod(task_id)

load_active_vod()

try:
    import gen_template
except Exception:
    gen_template = None

def smart_cut(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    if " " in truncated:
        last_space = truncated.rfind(" ")
        if last_space >= 6:
            return truncated[:last_space].strip()
    return truncated.strip()

def format_fat_media_name(raw_title, max_len=36):
    """
    Formata o nome do arquivo para exibição no ConnectShare/MediaPlay da TV Samsung.
    Garante que séries incluam o nome e NUNCA cortem a temporada/episódio (ex: 'A Noiva de Istambul S01E10.mp4').
    """
    raw = (raw_title or "").strip().replace("@", "").strip()
    if not raw:
        return "FILME.mp4"
    
    # 1. Detectar Temporada e Episódio
    season = None
    episode = None
    ep_str = None
    
    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,3})\b", raw, re.IGNORECASE)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        ep_str = f"S{season:02d}E{episode:02d}"
    
    if not ep_str:
        m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", raw, re.IGNORECASE)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            ep_str = f"S{season:02d}E{episode:02d}"
            
    if not ep_str:
        m = re.search(r"Temporada\s*(\d{1,2}).*?Epis[oó]dio\s*(\d{1,3})", raw, re.IGNORECASE)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            ep_str = f"S{season:02d}E{episode:02d}"
            
    if not ep_str:
        m = re.search(r"\bT(\d{1,2})\s*E(\d{1,3})\b", raw, re.IGNORECASE)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            ep_str = f"S{season:02d}E{episode:02d}"
            
    if not ep_str:
        m = re.search(r"\b(?:Epis[oó]dio|Ep\.?)\s*(\d{1,3})\b", raw, re.IGNORECASE)
        if m:
            episode = int(m.group(1))
            ep_str = f"EP{episode:02d}"

    if ep_str:
        clean = raw
        clean = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\b\d{1,2}x\d{1,3}\b", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"Temporada\s*\d{1,2}.*?Epis[oó]dio\s*\d{1,3}", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\bT\d{1,2}\s*E\d{1,3}\b", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\b(?:Epis[oó]dio|Ep\.?)\s*\d{1,3}\b", "", clean, flags=re.IGNORECASE)
        
        # Elimina repetições de nomes com hífen (ex: "Largados e Pelados - Largados e Pelados")
        parts = [p.strip() for p in clean.split(" - ") if p.strip()]
        if len(parts) >= 2:
            p0 = re.sub(r"[^a-zA-Z0-9]", "", parts[0].lower())
            p1 = re.sub(r"[^a-zA-Z0-9]", "", parts[1].lower())
            if p0 and p1 and (p0 in p1 or p1 in p0):
                clean = parts[0]
            else:
                clean = parts[0]
        elif len(parts) == 1:
            clean = parts[0]
            
        clean = re.sub(r"[^a-zA-Z0-9 _-]", "", clean).strip()
        series_max = max(10, max_len - len(ep_str) - 5)
        clean_short = smart_cut(clean, series_max)
        clean_short = re.sub(r"\s+(?:com|de|da|do|para|e|em|a|o)\s*$", "", clean_short, flags=re.IGNORECASE).strip()
        
        return f"{clean_short} {ep_str}.mp4"
    else:
        # Filme ou outro vídeo
        clean = re.sub(r"\s*-\s*Filme.*", "", raw, flags=re.IGNORECASE)
        clean = re.sub(r"\|.*", "", clean)
        clean = re.sub(r"[^a-zA-Z0-9 _-]", "", clean).strip()
        clean_short = smart_cut(clean, max_len)
        return (clean_short or "FILME") + ".mp4"

def extract_series_key(title):
    clean = (title or "").strip().replace("@", "").strip()
    if not clean:
        return ""
    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,3})\b", clean, re.IGNORECASE) or \
        re.search(r"\b(\d{1,2})x(\d{1,3})\b", clean, re.IGNORECASE) or \
        re.search(r"Temporada\s*(\d{1,2}).*?Epis[oó]dio\s*(\d{1,3})", clean, re.IGNORECASE) or \
        re.search(r"\bT(\d{1,2})\s*E(\d{1,3})\b", clean, re.IGNORECASE) or \
        re.search(r"\b(?:Epis[oó]dio|Ep\.?)\s*(\d{1,3})\b", clean, re.IGNORECASE)
    if not m:
        return ""
    clean = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b\d{1,2}x\d{1,3}\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"Temporada\s*\d{1,2}.*?Epis[oó]dio\s*\d{1,3}", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\bT\d{1,2}\s*E\d{1,3}\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(?:Epis[oó]dio|Ep\.?)\s*\d{1,3}\b", "", clean, flags=re.IGNORECASE)
    parts = [p.strip() for p in clean.split(" - ") if p.strip()]
    if parts:
        clean = parts[0]
    return re.sub(r"[^a-zA-Z0-9]", "", clean.lower())

def extract_episode_num(title):
    raw = (title or "").strip()
    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,3})\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))
    m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))
    m = re.search(r"Temporada\s*(\d{1,2}).*?Epis[oó]dio\s*(\d{1,3})", raw, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))
    m = re.search(r"\bT(\d{1,2})\s*E(\d{1,3})\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))
    m = re.search(r"\b(?:Epis[oó]dio|Ep\.?)\s*(\d{1,3})\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0

def get_series_playlist(task_id):
    """
    Retorna a lista de tarefas da mesma serie ordenadas por temporada e episodio.
    Se nao for serie ou so tiver 1 episodio baixado, retorna apenas a tarefa atual.
    O tamanho total acumulado dos episodios nao excede 3.8 GB (envelope FAT32).
    """
    with VOD_TASKS_LOCK:
        task = VOD_TASKS.get(task_id)
        if not task:
            f_mp4 = os.path.join(VOD_DIR, f"{task_id}.mp4")
            if os.path.exists(f_mp4):
                return [{"id": task_id, "file_path": f_mp4, "status": "ready"}]
            return []
        
        skey = extract_series_key(task.get("title") or "")
        if not skey:
            return [task]
            
        series_tasks = []
        for tid, t in VOD_TASKS.items():
            if t.get("status") == "ready":
                fp = t.get("file_path") or os.path.join(VOD_DIR, f"{tid}.mp4")
                if os.path.exists(fp):
                    if extract_series_key(t.get("title") or "") == skey:
                        series_tasks.append(t)
                        
        if len(series_tasks) <= 1:
            return [task]
            
        series_tasks.sort(key=lambda x: extract_episode_num(x.get("title") or ""))
        
        MAX_PLAYLIST_BYTES = 3800000000
        total_sz = 0
        filtered = []
        active_idx = 0
        for idx, t in enumerate(series_tasks):
            if t.get("id") == task_id:
                active_idx = idx
                break
                
        filtered.append(series_tasks[active_idx])
        sz_act = series_tasks[active_idx].get("file_size") or (os.path.getsize(series_tasks[active_idx]["file_path"]) if os.path.exists(series_tasks[active_idx].get("file_path", "")) else 0)
        total_sz += sz_act
        
        next_i = active_idx + 1
        while next_i < len(series_tasks):
            sz = series_tasks[next_i].get("file_size") or (os.path.getsize(series_tasks[next_i]["file_path"]) if os.path.exists(series_tasks[next_i].get("file_path", "")) else 0)
            if total_sz + sz > MAX_PLAYLIST_BYTES or len(filtered) >= 8:
                break
            filtered.append(series_tasks[next_i])
            total_sz += sz
            next_i += 1
            
        prev_i = active_idx - 1
        while prev_i >= 0:
            sz = series_tasks[prev_i].get("file_size") or (os.path.getsize(series_tasks[prev_i]["file_path"]) if os.path.exists(series_tasks[prev_i].get("file_path", "")) else 0)
            if total_sz + sz > MAX_PLAYLIST_BYTES or len(filtered) >= 8:
                break
            filtered.insert(0, series_tasks[prev_i])
            total_sz += sz
            prev_i -= 1
            
        return filtered

def refresh_vod_templates():
    """Atualiza nomes amigáveis e templates FAT32 de todas as tarefas prontas em disco."""
    if not gen_template:
        return
    updated = False
    with VOD_TASKS_LOCK:
        for tid, task in list(VOD_TASKS.items()):
            if task.get("status") == "ready":
                out_file = task.get("file_path") or os.path.join(VOD_DIR, f"{tid}.mp4")
                out_tmpl = task.get("template_path") or os.path.join(VOD_DIR, f"{tid}.bin")
                if os.path.exists(out_file):
                    raw_title = task.get("title") or task.get("display_name") or "Vídeo"
                    new_fat_name = format_fat_media_name(raw_title)
                    old_disp = task.get("display_name")
                    cur_fsize = os.path.getsize(out_file)
                    if old_disp != new_fat_name or not os.path.exists(out_tmpl) or task.get("file_size") != cur_fsize:
                        task["display_name"] = new_fat_name
                        task["template_path"] = out_tmpl
                        task["file_size"] = cur_fsize
                        try:
                            fsize = min(cur_fsize, 4294967000)
                            gen_template.build_fat_template(file_name=new_fat_name, file_size=fsize, out_path=out_tmpl)
                            print(f"[✓ TEMPLATE] Atualizado template FAT32 para {new_fat_name} ({tid}, {fsize} bytes)")
                            updated = True
                        except Exception as e:
                            print(f"[!] Erro ao atualizar template {tid}: {e}")
        if updated:
            save_vod_tasks()

refresh_vod_templates()

FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")
SERIES_FAVORITES_FILE = os.path.join(CONFIG_DIR, "series_favorites.json")

class FavoritesManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.lock = threading.RLock()
        self.favorites = set()
        self.load()

    def load(self):
        with self.lock:
            loaded = False
            for p in [self.filepath, self.filepath + ".bak"]:
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if isinstance(data, list):
                                parsed = set(str(x) for x in data)
                                if len(parsed) > 0 or not loaded:
                                    self.favorites = parsed
                                    loaded = True
                                    if len(self.favorites) > 0:
                                        break
                    except Exception as e:
                        print(f"[FAV] Erro ao carregar {p}: {e}")
            if not loaded:
                self.save()

    def save(self):
        with self.lock:
            try:
                tmp = self.filepath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(self.favorites)), f, indent=2)
                try:
                    os.replace(tmp, self.filepath)
                except OSError:
                    # Em Docker bind-mount de arquivo individual, os.replace falha com [Errno 16] EBUSY
                    with open(self.filepath, "w", encoding="utf-8") as f:
                        json.dump(sorted(list(self.favorites)), f, indent=2)
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                # Cria cópia de segurança permanente (.bak)
                bak = self.filepath + ".bak"
                with open(bak, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(self.favorites)), f, indent=2)
            except Exception as e:
                print(f"[FAV] Erro ao salvar {self.filepath}: {e}")

    def get_all(self):
        with self.lock:
            return sorted(list(self.favorites))

    def is_fav(self, item_id):
        with self.lock:
            return str(item_id).strip() in self.favorites

    def toggle(self, item_id):
        with self.lock:
            s_id = str(item_id).strip()
            if not s_id:
                return False, sorted(list(self.favorites))
            if s_id in self.favorites:
                self.favorites.remove(s_id)
                added = False
            else:
                self.favorites.add(s_id)
                added = True
            self.save()
            return added, sorted(list(self.favorites))

    def sync(self, items_list):
        """União bidirecional com persistência automática (nunca perde dados do cliente)."""
        with self.lock:
            added_any = False
            if isinstance(items_list, list):
                for item in items_list:
                    s_id = str(item).strip()
                    if s_id and s_id not in self.favorites:
                        self.favorites.add(s_id)
                        added_any = True
            if added_any or not os.path.exists(self.filepath):
                self.save()
            return sorted(list(self.favorites))

FAV_MGR = FavoritesManager(FAVORITES_FILE)
SERIES_FAV_MGR = FavoritesManager(SERIES_FAVORITES_FILE)


def log_event(msg):
    """Persistent event log: switches, ffmpeg (re)starts, reconnects.
    The background-shell stdout is not captured, so this file is the
    only timeline for correlating TV glitches with server events."""
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# Grupos inteligentes para a interface do controle remoto
SMART_GROUPS = [
    {"id": "fav", "name": "Favoritos", "icon": "⭐", "filter": "favorites"},
    {"id": "all", "name": "Todos", "icon": "📺", "filter": "all"},
    {"id": "globos", "name": "Globos", "icon": "🌐", "categories": ["GLOBOS"]},
    {"id": "abertos", "name": "TV Aberta", "icon": "📡", "categories": ["TV Aberta", "ABERTOS", "SBT", "RECORD", "BAND"]},
    {"id": "noticias", "name": "Notícias", "icon": "📰", "categories": ["Notícias", "NEWS"]},
    {"id": "esportes", "name": "Esportes", "icon": "⚽", "categories": ["Esportes", "SPORTV"]},
    {"id": "hbo", "name": "HBO", "icon": "🎭", "categories": ["HBO"]},
    {"id": "telecine", "name": "Telecine", "icon": "🍿", "categories": ["TELECINE"]},
    {"id": "filmes_locais", "name": "Meus Filmes", "icon": "🎬", "categories": ["Meus Filmes"]}
]

DEFAULT_CHANNELS = {
    "band-rio": {
        "id": "band-rio",
        "name": "Band Rio",
        "quality": "1080p",
        "category": "TV Aberta",
        "url": "{XTREAM_UPSTREAM}/live/{XTREAM_USER}/{XTREAM_PASS}/207177.ts",
        "logo": "📺",
        "is_eco": False
    },
    "globo-rj": {
        "id": "globo-rj",
        "name": "Globo RJ",
        "quality": "1080p",
        "category": "TV Aberta",
        "url": "{XTREAM_UPSTREAM}/live/{XTREAM_USER}/{XTREAM_PASS}/10006.ts",
        "logo": "🌐",
        "is_eco": False
    }
}

NON_ECO_KEYWORDS = (
    "4k", "4 k", "4-k", "[4k]", "(4k)",
    "uhd", "2160p", "2160",
    "hevc", "h.265", "h265", "x265", "x.265",
    "hdr", "10bit", "10-bit", "dv", "dolby vision"
)

CHANNEL_CODEC_CACHE = {}
LAST_RECONNECT_TIME = 0.0

def probe_is_h264(url):
    """
    Verifica se o stream de entrada é estritamente H.264 compatível com Samsung PL51F4000.
    A TV Samsung Plasma PL51F4000 (2013) suporta H.264 até 1080p@30fps SDR 8-bit.
    Canais com B-frames (has_b_frames > 0), >30fps ou SAR anamórfico causam congelamento/distorção
    na TV em modo copy e exigem transcodificação obrigatória (libx264).
    """
    if not url or not (url.startswith("http://") or url.startswith("https://") or os.path.exists(url)):
        return False
    if url in CHANNEL_CODEC_CACHE:
        return CHANNEL_CODEC_CACHE[url]
    try:
        cmd = [
            "ffprobe", "-v", "error", "-rw_timeout", "3000000",
            "-probesize", "500000", "-analyzeduration", "1000000",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,r_frame_rate,has_b_frames,sample_aspect_ratio",
            "-of", "json", url
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=4).decode("utf-8").strip()
        data = json.loads(out)
        streams = data.get("streams", [])
        if not streams:
            CHANNEL_CODEC_CACHE[url] = False
            return False
        video_stream = streams[0]
        sar = (video_stream.get('sample_aspect_ratio') or '').strip()
        if sar and sar not in ('1:1', '1/1'):
            CHANNEL_CODEC_CACHE[url] = False
            return False
        codec = (video_stream.get("codec_name") or "").lower()
        fps_str = str(video_stream.get("r_frame_rate") or "30/1")
        try:
            has_b_frames = int(video_stream.get("has_b_frames") or 0)
        except (ValueError, TypeError):
            has_b_frames = 0
        try:
            num, den = map(float, fps_str.split("/"))
            fps = num / den if den != 0 else 30.0
        except Exception:
            fps = 30.0
        # PL51F4000 MStar SoC in copy mode requires ZERO B-frames and <=31 fps
        is_ok = (codec == "h264" and fps <= 31.0 and has_b_frames == 0)
        CHANNEL_CODEC_CACHE[url] = is_ok
        return is_ok
    except Exception:
        CHANNEL_CODEC_CACHE[url] = False
        return False

def is_channel_eco(name, url=""):
    """
    Determina se um canal pode rodar em Modo Econômico (-c:v copy + -c:a ac3).
    A TV Samsung Plasma PL51F4000 (2013) suporta H.264 até 1080p@30fps SDR 8-bit.
    Canais em 4K, UHD, HEVC, H.265 ou 10-bit NÃO possuem decodificador por hardware na TV
    e exigem transcodificação obrigatória (libx264).
    Para garantir conformidade com as restrições físicas do chip MStar (GOP=30 exato, repeat-headers=1, slices=1),
    streams de IPTV remotos NUNCA devem assumir stream copy (-c:v copy) sem declaração explícita no JSON.
    """
    target = f"{name or ''} {url or ''}".lower()
    if any(term in target for term in NON_ECO_KEYWORDS):
        return False
    return False

def resolve_channel_url(url):
    if not url:
        return ""
    upstream = (XTREAM_UPSTREAM or os.environ.get("XTREAM_UPSTREAM", "") or "http://studut.shop:80").rstrip("/")
    user = XTREAM_USER or os.environ.get("XTREAM_USER", "")
    pwd = XTREAM_PASS or os.environ.get("XTREAM_PASS", "")
    resolved = (str(url).replace("{XTREAM_UPSTREAM}", upstream)
                        .replace("{XTREAM_USER}", user)
                        .replace("{XTREAM_PASS}", pwd))
    if resolved.startswith(("/live/", "/movie/", "/series/")):
        resolved = f"{upstream}{resolved}"
    elif resolved.startswith(("live/", "movie/", "series/")):
        resolved = f"{upstream}/{resolved}"
    return resolved

class ChannelManager:
    """Gerencia o carregamento dinâmico de canais e cache com base no mtime."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_mtime = 0
        self.channels = {}
        self.reload()

    def reload(self):
        with self.lock:
            target_file = CHANNELS_FILE
            if os.path.exists(target_file):
                try:
                    mtime = os.path.getmtime(target_file)
                    if mtime != self.last_mtime:
                        with open(target_file, "r", encoding="utf-8") as f:
                            raw = json.load(f)

                        parsed = dict(DEFAULT_CHANNELS)
                        if isinstance(raw, dict) and "canais" in raw:
                            import re
                            for c in raw.get("canais", []):
                                cid = str(c.get("stream_id") or c.get("nome"))
                                clean_id = re.sub(r"[^a-z0-9_-]", "", c.get("nome", cid).lower().replace(" ", "-"))
                                parsed[clean_id] = {
                                    "id": clean_id,
                                    "name": c.get("nome"),
                                    "quality": "1080p" if c.get("resolucao") in ("FHD", "1080p") else c.get("resolucao", "720p"),
                                    "category": c.get("categoria_nome", "Geral"),
                                    "url": resolve_channel_url(c.get("url_ts") or c.get("url_m3u8") or c.get("url")),
                                    "logo": c.get("logo", "📺"),
                                    "description": c.get("nome_original", ""),
                                    "is_eco": bool(c.get("is_eco", False)) if "is_eco" in c else is_channel_eco(c.get("nome"), c.get("url_ts") or c.get("url_m3u8") or c.get("url"))
                                }
                        elif isinstance(raw, dict):
                            for cid, c in raw.items():
                                item = dict(c)
                                item["url"] = resolve_channel_url(item.get("url"))
                                parsed[cid] = item

                        # Escaneia pasta de filmes locais se existir
                        if os.path.exists(MOVIES_DIR):
                            for fname in sorted(os.listdir(MOVIES_DIR)):
                                if fname.lower().endswith((".mkv", ".mp4", ".avi", ".ts")):
                                    clean_mid = f"movie-{fname.lower().replace(' ', '-')}"
                                    fpath = os.path.join(MOVIES_DIR, fname)
                                    parsed[clean_mid] = {
                                        "id": clean_mid,
                                        "name": os.path.splitext(fname)[0],
                                        "quality": "1080p",
                                        "category": "Meus Filmes",
                                        "url": fpath,
                                        "logo": "🎬",
                                        "description": f"Filme Pessoal ({fname})",
                                        "is_eco": False
                                    }

                        for ch in parsed.values():
                            ch["url"] = resolve_channel_url(ch.get("url"))
                            if "is_eco" not in ch:
                                ch["is_eco"] = is_channel_eco(ch.get("name"), ch.get("url"))

                        self.channels = parsed
                        self.last_mtime = mtime
                        print(f"[✓] Grade recarregada: {len(self.channels)} canais/filmes disponíveis.")
                        return True
                except Exception as e:
                    print(f"[!] Erro ao recarregar {target_file}: {e}")
            if not self.channels:
                self.channels = DEFAULT_CHANNELS.copy()
            return False

    def get_all(self):
        self.reload()
        with self.lock:
            return self.channels

    def get_channel(self, cid):
        self.reload()
        with self.lock:
            return self.channels.get(cid)

CH_MGR = ChannelManager()

class CatalogManager:
    """Gerencia catálogo completo de Canais Ao Vivo, Filmes (VOD) e Séries da API Xtream com cache em disco e memória."""
    def __init__(self):
        self.lock = threading.RLock()
        self.categories = {"live": [], "movie": [], "series": []}
        self.items = {"live": [], "movie": [], "series": []}
        self.series_cover_lookup = {}
        self.movie_cover_lookup = {}
        self.series_info_cache = {}
        self.is_syncing = False
        self.last_sync = 0
        self._needs_initial_sync = False
        self.load_from_disk()

    def _clean_title(self, name):
        if not name:
            return ""
        n = re.sub(r'^@\s*', '', name.strip())
        n = re.sub(r'\s*\([^)]*\)', '', n)
        return n.strip().lower()

    def build_cover_lookups(self):
        with self.lock:
            s_covers = {}
            for s in self.items.get("series", []):
                cov = (s.get("cover") or "").strip()
                if "tmdb.org" in cov or cov.startswith("https://"):
                    cn = self._clean_title(s.get("name"))
                    if cn and cn not in s_covers:
                        s_covers[cn] = cov
            self.series_cover_lookup = s_covers

            m_covers = {}
            for m in self.items.get("movie", []):
                cov = (m.get("stream_icon") or "").strip()
                if "tmdb.org" in cov or cov.startswith("https://"):
                    cn = self._clean_title(m.get("name"))
                    if cn and cn not in m_covers:
                        m_covers[cn] = cov
            self.movie_cover_lookup = m_covers

    def sort_catalog_items(self):
        with self.lock:
            for ctype in ["series", "movie"]:
                items = self.items.get(ctype, [])
                if not items:
                    continue

                def _item_score(it):
                    name = (it.get("name") or it.get("title") or "").strip()
                    cov = (it.get("cover") if ctype == "series" else it.get("stream_icon")) or ""
                    has_at = name.startswith("@")
                    has_tmdb = "tmdb.org" in cov or cov.startswith("https://")
                    has_cov = bool(cov.strip())
                    if has_at and has_tmdb:
                        return 0
                    if has_at and has_cov:
                        return 1
                    if has_tmdb:
                        return 2
                    if has_cov:
                        return 3
                    return 4

                items.sort(key=lambda it: (_item_score(it), (it.get("name") or "").lower()))

    def load_from_disk(self):
        with self.lock:
            for ctype, fname in [("live", "live_categories.json"), ("movie", "vod_categories.json"), ("series", "series_categories.json")]:
                fpath = os.path.join(CATALOG_CACHE_DIR, fname)
                if os.path.exists(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            self.categories[ctype] = json.load(f)
                    except Exception as e:
                        print(f"[CATALOG] Erro ao carregar {fname}: {e}")

            for ctype, fname in [("live", "live_streams.json"), ("movie", "vod_streams.json"), ("series", "series.json")]:
                fpath = os.path.join(CATALOG_CACHE_DIR, fname)
                if os.path.exists(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            self.items[ctype] = json.load(f)
                    except Exception as e:
                        print(f"[CATALOG] Erro ao carregar {fname}: {e}")

            counts = {k: len(v) for k, v in self.items.items()}
            print(f"[✓ CATALOG] Carregado do disco: Live={counts['live']}, Filmes={counts['movie']}, Séries={counts['series']}")

            self.build_cover_lookups()
            self.sort_catalog_items()

            # Import must remain pure: defer the initial background sync until
            # an explicit runtime start. The deferral preserves the original
            # run() behavior without creating a thread or touching the network
            # during import.
            self._needs_initial_sync = not self.items["live"] and not self.items["movie"]

    def start_background_sync(self):
        """Starts the deferred initial catalog sync once runtime begins."""
        with self.lock:
            if not self._needs_initial_sync:
                return
            self._needs_initial_sync = False
        self.sync_all_async()

    def sync_all_async(self):
        with self.lock:
            if self.is_syncing:
                return
            self.is_syncing = True

        def _worker():
            print("[CATALOG] Sincronizando catálogo completo Xtream em segundo plano...")
            actions = [
                ("live", "get_live_categories", "live_categories.json", True),
                ("movie", "get_vod_categories", "vod_categories.json", True),
                ("series", "get_series_categories", "series_categories.json", True),
                ("live", "get_live_streams", "live_streams.json", False),
                ("movie", "get_vod_streams", "vod_streams.json", False),
                ("series", "get_series", "series.json", False),
            ]
            for ctype, action, fname, is_cat in actions:
                try:
                    upstream = (XTREAM_UPSTREAM or os.environ.get("XTREAM_UPSTREAM", "") or "http://studut.shop:80").rstrip("/")
                    url = f"{upstream}/player_api.php?username={XTREAM_USER}&password={XTREAM_PASS}&action={action}"
                    headers = {"User-Agent": "IPTVSmarters/3.1.1", "Accept": "*/*"}
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        raw = resp.read()
                        data = json.loads(raw.decode("utf-8", "ignore"))
                        fpath = os.path.join(CATALOG_CACHE_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(raw)
                        with self.lock:
                            if is_cat:
                                self.categories[ctype] = data
                            else:
                                self.items[ctype] = data
                    print(f"[CATALOG] Sincronizado: {fname} ({len(data)} itens)")
                except Exception as e:
                    print(f"[!] Erro ao sincronizar {fname}: {e}")

            self.build_cover_lookups()
            self.sort_catalog_items()

            with self.lock:
                self.is_syncing = False
                self.last_sync = time.time()
            print("[✓ CATALOG] Sincronização em background concluída com sucesso!")

        threading.Thread(target=_worker, daemon=True).start()

    def get_categories(self, ctype):
        with self.lock:
            cats = self.categories.get(ctype, [])
            res = [{"id": str(c.get("category_id")), "name": c.get("category_name", "Geral")} for c in cats]

        def cat_priority(c):
            name = (c.get("name") or "").strip()
            if name.startswith("@"):
                return 0
            if "@" in name:
                return 1
            return 2

        res.sort(key=cat_priority)
        return res

    def get_items(self, ctype, category_id="", search="", page=1, limit=48):
        with self.lock:
            raw_list = self.items.get(ctype, [])

        filtered = []
        search_lower = (search or "").strip().lower()
        cat_str = str(category_id).strip()

        for it in raw_list:
            stream_id = it.get("stream_id") or it.get("id")
            s_id_str = str(stream_id) if stream_id is not None else ""
            name = it.get("name") or it.get("title") or ""

            if cat_str and cat_str != "all":
                if cat_str == "favorites":
                    mgr = SERIES_FAV_MGR if ctype == "series" else FAV_MGR
                    target_id = it.get("series_id") or it.get("stream_id") or it.get("id")
                    if not (mgr.is_fav(s_id_str) or (target_id and mgr.is_fav(str(target_id)))):
                        continue
                elif cat_str == "eco":
                    ch_url = resolve_channel_url(f"/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts")
                    if not is_channel_eco(name, ch_url):
                        continue
                else:
                    item_cat = str(it.get("category_id", ""))
                    item_cats = [str(x) for x in it.get("category_ids", [])]
                    if item_cat != cat_str and cat_str not in item_cats:
                        continue

            if search_lower and search_lower not in name.lower():
                continue

            if ctype == "live":
                live_url = resolve_channel_url(f"/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts")
                logo = (it.get("stream_icon") or "").strip()
                if logo and logo.startswith("http://"):
                    logo = f"/api/logo?url={urllib.parse.quote(logo, safe='')}"
                filtered.append({
                    "id": stream_id,
                    "name": name,
                    "logo": logo,
                    "cat_id": it.get("category_id"),
                    "stream_type": it.get("stream_type", "live"),
                    "url": live_url,
                    "is_eco": is_channel_eco(name, live_url)
                })
            elif ctype == "movie":
                stream_id = it.get("stream_id") or it.get("id")
                ext = it.get("container_extension") or "mp4"
                poster = (it.get("stream_icon") or "").strip()
                if not poster or "tmdb.org" not in poster:
                    cn = self._clean_title(name)
                    if cn in self.movie_cover_lookup:
                        poster = self.movie_cover_lookup[cn]
                if poster and poster.startswith("http://"):
                    poster = f"/api/logo?url={urllib.parse.quote(poster, safe='')}"
                filtered.append({
                    "id": stream_id,
                    "name": name,
                    "poster": poster,
                    "rating": str(it.get("rating") or ""),
                    "year": str(it.get("year") or ""),
                    "cat_id": it.get("category_id"),
                    "ext": ext,
                    "url": resolve_channel_url(f"/movie/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.{ext}")
                })
            elif ctype == "series":
                series_id = it.get("series_id") or it.get("id")
                poster = (it.get("cover") or "").strip()
                if not poster or "tmdb.org" not in poster:
                    cn = self._clean_title(name)
                    if cn in self.series_cover_lookup:
                        poster = self.series_cover_lookup[cn]
                if poster and poster.startswith("http://"):
                    poster = f"/api/logo?url={urllib.parse.quote(poster, safe='')}"
                year_val = str(it.get("year") or (it.get("release_date") or it.get("releaseDate") or "")[:4] or "").strip()
                if year_val == "None":
                    year_val = ""
                filtered.append({
                    "id": series_id,
                    "name": name,
                    "poster": poster,
                    "rating": str(it.get("rating") or ""),
                    "year": year_val,
                    "plot": it.get("plot") or "",
                    "cat_id": it.get("category_id")
                })

        total = len(filtered)
        start = max(0, (page - 1) * limit)
        end = start + limit
        paginated = filtered[start:end]

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": max(1, (total + limit - 1) // limit),
            "items": paginated
        }

    def get_series_info(self, series_id):
        global XTREAM_USER, XTREAM_PASS, XTREAM_UPSTREAM
        with self.lock:
            if series_id in self.series_info_cache:
                return self.series_info_cache[series_id]

        if not XTREAM_USER or not XTREAM_PASS:
            _discover_xtream_credentials()

        if not XTREAM_USER or not XTREAM_PASS:
            print(f"[!] Erro ao buscar série {series_id}: credenciais XTREAM_USER/XTREAM_PASS não configuradas")
            return {"error": "Credenciais IPTV não configuradas no servidor.", "seasons": []}

        upstream = (XTREAM_UPSTREAM or os.environ.get("XTREAM_UPSTREAM", "") or "http://studut.shop:80").rstrip("/")
        url = f"{upstream}/player_api.php?username={XTREAM_USER}&password={XTREAM_PASS}&action=get_series_info&series_id={series_id}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "IPTVSmarters/3.1.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))

            info = data.get("info", {})
            episodes_by_season = data.get("episodes", {})
            seasons_formatted = []

            for s_num in sorted(episodes_by_season.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                ep_list = episodes_by_season[s_num]
                eps_formatted = []
                for ep in ep_list:
                    ep_id = ep.get("id")
                    ext = ep.get("container_extension") or "mp4"
                    ep_info = ep.get("info", {})
                    eps_formatted.append({
                        "id": ep_id,
                        "episode_num": ep.get("episode_num", 1),
                        "title": ep.get("title") or f"Episódio {ep.get('episode_num')}",
                        "duration": ep_info.get("duration", ""),
                        "plot": ep_info.get("plot", ""),
                        "url": resolve_channel_url(f"/series/{XTREAM_USER}/{XTREAM_PASS}/{ep_id}.{ext}")
                    })
                seasons_formatted.append({
                    "season_number": s_num,
                    "name": f"Temporada {s_num}",
                    "episodes": eps_formatted
                })

            cover = (info.get("cover") or "").strip()
            if not cover or "tmdb.org" not in cover:
                cn = self._clean_title(info.get("name", ""))
                if cn in self.series_cover_lookup:
                    cover = self.series_cover_lookup[cn]
            if cover and cover.startswith("http://"):
                cover = f"/api/logo?url={urllib.parse.quote(cover, safe='')}"

            result = {
                "id": series_id,
                "name": info.get("name", "Série"),
                "cover": cover,
                "plot": info.get("plot", ""),
                "rating": info.get("rating", ""),
                "genre": info.get("genre", ""),
                "seasons": seasons_formatted
            }

            with self.lock:
                if len(self.series_info_cache) > 200:
                    self.series_info_cache.clear()
                self.series_info_cache[series_id] = result
            return result
        except Exception as e:
            safe_url = url.replace(XTREAM_PASS, "***") if XTREAM_PASS else url
            print(f"[!] Erro ao buscar detalhes da série {series_id} ({safe_url}): {e}")
            return {"error": str(e), "seasons": []}

CATALOG_MGR = CatalogManager()

# Cache de logos em memória para evitar requests repetidos
LOGO_CACHE = {}
LOGO_CACHE_LOCK = threading.Lock()

def encode_ts_timestamp(val, header_bits):
    val = int(val) & 0x1FFFFFFFF
    b0 = (header_bits << 4) | (((val >> 30) & 0x07) << 1) | 1
    b1 = (val >> 22) & 0xFF
    b2 = (((val >> 15) & 0x7F) << 1) | 1
    b3 = (val >> 7) & 0xFF
    b4 = ((val & 0x7F) << 1) | 1
    return bytes([b0, b1, b2, b3, b4])

def encode_pcr(pcr_val):
    base = (pcr_val // 300) & 0x1FFFFFFFF
    ext = pcr_val % 300
    b0 = (base >> 25) & 0xFF
    b1 = (base >> 17) & 0xFF
    b2 = (base >> 9) & 0xFF
    b3 = (base >> 1) & 0xFF
    b4 = ((base & 1) << 7) | 0x7E | ((ext >> 8) & 1)
    b5 = ext & 0xFF
    return bytes([b0, b1, b2, b3, b4, b5])

WRAP33_MOD = 1 << 33  # 8589934592 (limite de rollover de 33 bits do PTS/DTS em 90kHz)
HALF_WRAP33 = 1 << 32  # 4294967296 (ponto de corte para signed delta circular)

# O PCR MPEG-TS é composto por PCR_base (33 bits em 90kHz) e PCR_ext (9 bits em 27MHz, 0..299):
# PCR = PCR_base * 300 + PCR_ext.
# O ciclo completo do PCR_base corresponde a 2^33 ticks de 90kHz, ou seja, (2^33 * 300) ticks de 27MHz (~2.576.980.377.600 ticks).
# PCR_MOD representa este período exato de repetição do PCR_base na escala de 27MHz,
# garantindo que o rollover modular do PCR_base e do PTS ocorram em sincronia perfeita no espaço modular.
PCR_MOD = (1 << 33) * 300  # 2576980377600 ticks a 27MHz

# Operational A/V phase threshold (500 ms in 90kHz ticks).
# Not an MPEG-TS spec or Samsung hardware spec, but our pipeline's operational
# rule to distinguish normal GOP interleaving from upstream phase discontinuities.
OPERATIONAL_MAX_SKEW_TICKS = 45000  # 500 ms

def wrap33(v):
    """33-bit timestamp normalization in the ring [0, 2^33 - 1].
    Strict modular ring arithmetic mod 2^33 is enforced without ad-hoc window clamps."""
    return int(v) % WRAP33_MOD

def wrap46(v):
    """
    Normalização modular do PCR a 27MHz no período de repetição do PCR_base: [0, 2^33 * 300 - 1].
    Preserva a relação síncrona com o espaço modular de 33 bits do PTS (onde 1 tick PTS = 300 ticks PCR).
    """
    return int(v) % PCR_MOD

def signed_diff_33(a, b):
    """
    Calculates modular signed difference (a - b) in the 33-bit ring [-2^32, 2^32 - 1].
    Used to safely compare timestamps across 33-bit rollover boundaries without naive integer subtraction.
    """
    diff = (int(a) - int(b)) % WRAP33_MOD
    if diff >= HALF_WRAP33:
        diff -= WRAP33_MOD
    return diff

class SeamlessRestamper:
    """
    Normalizador de fluxo MPEG-TS em tempo real com preservação de fase A/V e relógio mestre.
    Garante que timestamps (PTS, DTS, PCR) e contadores de continuidade (CC)
    avancem estritamente de forma contínua e crescente mesmo durante a troca de canais,
    sem salto temporal para trás, sem desync de áudio AC3 e preservando a ordem dos B-frames.
    O fluxo de vídeo é o relógio mestre (master clock); o áudio preserva a relação temporal original
    se o skew estiver dentro do limiar operacional (|Δ| <= 500ms), ou realinha para uma nova fase
    coerente caso o upstream apresente descontinuidade patológica (|Δ| > 500ms).
    """
    def __init__(self):
        self.rem = bytearray()
        self.video_pts_offset = None
        self.audio_pts_offset = None
        self.pts_offset = None  # Alias para video_pts_offset (compatibilidade)
        self.max_pts_seen = 90000
        self.first_video_in_pts = None
        self.first_audio_in_pts = None
        self.target_base_pts = 270000
        self.cc_map = {}
        self.last_out_video_pts = None
        self.prev_in_video_pts = None
        self.last_out_audio_pts = None

    def reset_epoch(self):
        self.rem.clear()
        self.first_video_in_pts = None
        self.first_audio_in_pts = None
        self.target_base_pts = 270000
        self.max_pts_seen = 90000
        self.last_out_video_pts = None
        self.prev_in_video_pts = None
        self.last_out_audio_pts = None
        self.video_pts_offset = None
        self.audio_pts_offset = None
        self.pts_offset = None
        log_event("PTS_EPOCH_RESET (target_base_pts=270000)")

    def start_new_channel(self):
        self.rem.clear()
        self.first_video_in_pts = None
        self.first_audio_in_pts = None
        self.target_base_pts = wrap33(self.max_pts_seen + 3000)
        self.last_out_video_pts = None
        self.prev_in_video_pts = None
        self.last_out_audio_pts = None
        self.video_pts_offset = None
        self.audio_pts_offset = None
        self.pts_offset = None

    def process_chunk(self, data):
        if not data:
            return b""
        if self.rem:
            data = bytes(self.rem) + data
            self.rem.clear()

        # Sync-hunt: garante alinhamento estrito em múltiplos de 188 bytes iniciando com 0x47
        offset = 0
        l_data = len(data)
        while offset < l_data:
            if data[offset] == 0x47:
                if offset + 188 >= l_data or data[offset + 188] == 0x47:
                    break
            offset += 1

        if offset > 0:
            if offset >= l_data:
                return b""
            data = data[offset:]

        n_pkts = len(data) // 188
        rem_len = len(data) % 188
        if rem_len > 0:
            self.rem = bytearray(data[n_pkts * 188:])
            data = data[:n_pkts * 188]

        if not data:
            return b""

        out = bytearray(data)
        l = len(out)
        for i in range(0, l, 188):
            if out[i] != 0x47:
                continue
            pid = ((out[i+1] & 0x1F) << 8) | out[i+2]
            pusi = (out[i+1] & 0x40) >> 6
            afc = (out[i+3] & 0x30) >> 4

            # Atualiza Continuity Counter para ser contínuo por PID
            if afc in (1, 3):
                if pid not in self.cc_map:
                    self.cc_map[pid] = out[i+3] & 0x0F
                else:
                    self.cc_map[pid] = (self.cc_map[pid] + 1) & 0x0F
                    out[i+3] = (out[i+3] & 0xF0) | self.cc_map[pid]

            offset = 4
            has_af = afc in (2, 3)
            has_payload = afc in (1, 3)

            # Normalização de PCR (Clock de referência do hardware a 27MHz)
            if has_af:
                af_len = out[i+4]
                offset += 1 + af_len
                if af_len >= 7 and (out[i+5] & 0x10):
                    pcr_bytes = out[i+6:i+12]
                    base = (pcr_bytes[0] << 25) | (pcr_bytes[1] << 17) | (pcr_bytes[2] << 9) | (pcr_bytes[3] << 1) | (pcr_bytes[4] >> 7)
                    ext = ((pcr_bytes[4] & 0x01) << 8) | pcr_bytes[5]
                    in_pcr = base * 300 + ext
                    if self.video_pts_offset is None:
                        self.video_pts_offset = wrap33(self.target_base_pts - base)
                        self.pts_offset = self.video_pts_offset
                    out_pcr = wrap46(in_pcr + (self.video_pts_offset * 300))
                    out[i+6:i+12] = encode_pcr(out_pcr)

            # Normalização de PTS e DTS (Áudio MPEG 0xC0-DF, Vídeo 0xE0-EF, Áudio AC3 Dolby 0xBD)
            if has_payload and pusi and offset + 9 <= 188:
                if out[i+offset:i+offset+3] == b'\x00\x00\x01':
                    sid = out[i+offset+3]
                    is_video = (0xE0 <= sid <= 0xEF)
                    is_audio = (0xC0 <= sid <= 0xDF) or (sid == 0xBD)
                    if is_video or is_audio:
                        flags2 = out[i+offset+7]
                        pts_flag = (flags2 & 0x80) >> 7
                        dts_flag = (flags2 & 0x40) >> 6
                        p_pos = i + offset + 9
                        if pts_flag and p_pos + 5 <= i + 188:
                            b = out[p_pos:p_pos+5]
                            in_pts = (((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1))
                            


                            if is_video:
                                if self.first_video_in_pts is None:
                                    self.first_video_in_pts = in_pts
                                    if self.video_pts_offset is None:
                                        self.video_pts_offset = wrap33(self.target_base_pts - in_pts)
                                        self.pts_offset = self.video_pts_offset
                                    if self.first_audio_in_pts is not None:
                                        skew_in = signed_diff_33(self.first_audio_in_pts, in_pts)
                                        if abs(skew_in) <= OPERATIONAL_MAX_SKEW_TICKS:
                                            self.audio_pts_offset = self.video_pts_offset

                                out_pts = wrap33(in_pts + self.video_pts_offset)
                                if self.prev_in_video_pts is not None:
                                    dra = signed_diff_33(in_pts, self.prev_in_video_pts)
                                    if abs(dra) > (1 << 31):
                                        base_ref = self.last_out_video_pts if self.last_out_video_pts is not None else self.target_base_pts
                                        self.video_pts_offset = wrap33(base_ref + 3000 - in_pts)
                                        self.pts_offset = self.video_pts_offset
                                        out_pts = wrap33(in_pts + self.video_pts_offset)
                                        log_event(f"PTS_REBASE dra={dra/90000:+.0f}s")
                                self.prev_in_video_pts = in_pts

                                if self.last_out_video_pts is not None:
                                    jump = signed_diff_33(out_pts, self.last_out_video_pts)
                                    if jump < -45000 or jump > 5400000:
                                        log_event(f"PTS_JUMP {jump/90000:+.1f}s (out_pts={out_pts})")
                                    if jump < -90000 or jump > 450000:
                                        # Re-ancora diretamente em in_pts sem absorver artefatos de wrap 33-bit
                                        self.video_pts_offset = wrap33(self.last_out_video_pts + 3000 - in_pts)
                                        self.pts_offset = self.video_pts_offset
                                        out_pts = wrap33(in_pts + self.video_pts_offset)

                                self.last_out_video_pts = out_pts
                                if signed_diff_33(out_pts, self.max_pts_seen) > 0:
                                    self.max_pts_seen = out_pts

                                out[p_pos:p_pos+5] = encode_ts_timestamp(out_pts, 3 if dts_flag else 2)

                                if dts_flag and p_pos + 10 <= i + 188:
                                    b = out[p_pos+5:p_pos+10]
                                    in_dts = (((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1))
                                    out_dts = wrap33(in_dts + self.video_pts_offset)
                                    out[p_pos+5:p_pos+10] = encode_ts_timestamp(out_dts, 1)

                            elif is_audio:
                                # Relógio mestre é o vídeo
                                if self.audio_pts_offset is None:
                                    self.first_audio_in_pts = in_pts
                                    if self.first_video_in_pts is not None and self.video_pts_offset is not None:
                                        skew_in = signed_diff_33(in_pts, self.first_video_in_pts)
                                        if abs(skew_in) <= OPERATIONAL_MAX_SKEW_TICKS:
                                            # Passo B2: Skew dentro do limiar operacional (<= 500ms)
                                            # Preserva a relação temporal original de upstream
                                            self.audio_pts_offset = self.video_pts_offset
                                        else:
                                            # Passo B3: Descontinuidade de fase do upstream (|skew| > 500ms)
                                            # Inicia nova fase A/V coerente ancorada ao target_base_pts
                                            self.audio_pts_offset = wrap33(self.target_base_pts - in_pts)
                                            log_event(f"A/V_PHASE_REALIGN: in_skew={skew_in/90000:+.3f}s -> realigned audio to target_base_pts")
                                    else:
                                        # Áudio chegou antes do vídeo nesta época: ancora provisoriamente
                                        self.audio_pts_offset = wrap33(self.target_base_pts - in_pts)

                                out_pts = wrap33(in_pts + self.audio_pts_offset)

                                # Proteção contínua de integridade de skew em mid-stream:
                                if self.last_out_video_pts is not None:
                                    curr_skew = signed_diff_33(out_pts, self.last_out_video_pts)
                                    if abs(curr_skew) > OPERATIONAL_MAX_SKEW_TICKS:
                                        self.audio_pts_offset = wrap33(self.last_out_video_pts - in_pts)
                                        out_pts = wrap33(in_pts + self.audio_pts_offset)
                                        log_event(f"A/V_MIDSTREAM_REALIGN: curr_skew={curr_skew/90000:+.3f}s -> realigned audio")

                                # Proteção estrita de monotonicidade com signed_diff_33
                                if self.last_out_audio_pts is not None:
                                    diff_last = signed_diff_33(out_pts, self.last_out_audio_pts)
                                    if diff_last <= 0:
                                        out_pts = wrap33(self.last_out_audio_pts + 2880)

                                if signed_diff_33(out_pts, self.max_pts_seen) > 0:
                                    self.max_pts_seen = out_pts

                                self.last_out_audio_pts = out_pts
                                out[p_pos:p_pos+5] = encode_ts_timestamp(out_pts, 3 if dts_flag else 2)

                                if dts_flag and p_pos + 10 <= i + 188:
                                    b = out[p_pos+5:p_pos+10]
                                    in_dts = (((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1))
                                    out_dts = wrap33(in_dts + self.audio_pts_offset)
                                    diff_dts = signed_diff_33(out_dts, self.last_out_audio_pts)
                                    if diff_dts <= 0:
                                        out_dts = out_pts
                                    out[p_pos+5:p_pos+10] = encode_ts_timestamp(out_dts, 1)

        return bytes(out)

def resolve_youtube(yt_url):
    """
    Usa yt-dlp para extrair título, duração, thumbnail e URLs de stream (vídeo + áudio DASH).
    Tenta primeiro conexão direta para máxima velocidade. Se a VPS for sinalizada como bot
    ou os cookies expirarem, faz fallback transparente através do proxy residencial móvel SOCKS5
    sem cookies corrompidos.
    Retorna dict com metadados estruturados ou levanta ValueError.
    """
    qjs_path = "/usr/bin/qjs"
    cookies_path = os.path.join(VOD_DIR, "youtube_cookies.txt")
    has_cookies = bool(os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 100)

    def _build_cmd(use_cookies=True, proxy=""):
        cmd = [
            YT_DLP_BIN,
            "--no-warnings",
            "--no-playlist",
            # YouTube restringe o cliente 'web' sem PO Token a 360p (640x360) quando o IP
            # nao tem reputacao (datacenter). O cliente web_safari recebe a escada HLS
            # completa (itag 91-96, ate 1920x1080 avc1.640028) sem exigir PO Token.
            # 'web' permanece no fim da cadeia como fallback de metadados.
            "--extractor-args", "youtube:player_client=web_safari,web",
            "--remote-components", "ejs:github",
        ]
        if os.path.exists(qjs_path):
            cmd.extend(["--js-runtimes", f"quickjs:{qjs_path}"])
        elif shutil.which("qjs"):
            cmd.extend(["--js-runtimes", f"quickjs:{shutil.which('qjs')}"])
        if use_cookies and os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 100:
            cmd.extend(["--cookies", cookies_path])
        if proxy:
            cmd.extend(["--proxy", proxy])
        cmd.extend([
            "-f", "bestvideo[height<=1080][vcodec^=avc]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "-J",
            yt_url
        ])
        return cmd

    socks_p = get_effective_socks_proxy()
    proxy_clean = socks_p.replace("socks5h://", "socks5://") if socks_p else ""
    used_proxy = False

    # 1. Tentativa Direta (rápida, com cookies se disponíveis)
    res = None
    try:
        res = subprocess.run(_build_cmd(use_cookies=has_cookies), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    except Exception:
        pass

    need_retry = False
    if not res or res.returncode != 0:
        need_retry = True
    elif res.returncode == 0:
        try:
            test_data = json.loads(res.stdout)
            if not test_data.get("url") and not test_data.get("requested_formats"):
                need_retry = True
        except Exception:
            need_retry = True

    # Se falhou e tínhamos cookies, verificar se o erro foi de sessão/bot/reloaded
    if need_retry and has_cookies:
        err_text = ((res.stderr if res else "") + " " + (res.stdout if res else "")).lower()
        if any(w in err_text for w in ["reloaded", "sign in", "bot", "cookie", "login", "confirm you"]):
            print("[!] Cookies do YouTube expirados/inválidos detectados em resolve_youtube. Desativando cookies...")
            try:
                os.rename(cookies_path, cookies_path + ".expired")
            except Exception:
                pass
            has_cookies = False

    # 2. Contingência via proxy residencial móvel
    if need_retry and proxy_clean:
        print("[*] yt-dlp usando proxy residencial de contingência...")
        used_proxy = True
        try:
            res = subprocess.run(_build_cmd(use_cookies=has_cookies, proxy=proxy_clean), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
        except subprocess.TimeoutExpired:
            res = None

        # Se falhou com cookies no proxy, tenta última vez SEM cookies via proxy
        if (not res or res.returncode != 0) and has_cookies:
            print("[*] Tentativa com cookies no proxy falhou. Tentando via proxy limpo sem cookies...")
            used_proxy = True
            try:
                os.rename(cookies_path, cookies_path + ".expired")
            except Exception:
                pass
            has_cookies = False
            try:
                res = subprocess.run(_build_cmd(use_cookies=False, proxy=proxy_clean), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
            except Exception:
                pass

    if not res or res.returncode != 0:
        err = res.stderr.strip() if res else "Erro desconhecido"
        err_short = err.split("\n")[-1] if err else f"Código de saída {res.returncode if res else 'None'}"
        raise ValueError(f"yt-dlp: {err_short}")

    try:
        data = json.loads(res.stdout)
    except Exception as e:
        raise ValueError(f"Erro ao decodificar metadados do YouTube: {e}")

    title = data.get("title", "Vídeo do YouTube")
    duration = data.get("duration")
    is_live = bool(data.get("is_live", False))
    thumbnail = data.get("thumbnail", "")

    video_url = None
    audio_url = None
    selected_height = None
    selected_fmt = None

    req_formats = data.get("requested_formats") or []
    if req_formats:
        for f in req_formats:
            if f.get("vcodec") != "none" and not video_url:
                video_url = f.get("url")
                selected_height = f.get("height")
                selected_fmt = f.get("format_id")
            elif f.get("acodec") != "none" and not audio_url:
                audio_url = f.get("url")

    if not video_url:
        video_url = data.get("url")
        selected_height = data.get("height")
        selected_fmt = data.get("format_id")

    if not video_url:
        raise ValueError("Não foi possível extrair URL do fluxo de vídeo do YouTube.")

    # Telemetria: sem isso, uma selecao silenciosa de 360p passa despercebida
    # (sintoma classico: cliente 'web' sem PO Token em IP de datacenter).
    if selected_height and int(selected_height) >= 1080:
        print(f"[YouTube] Formato {selected_fmt} selecionado: {selected_height}p (Full HD OK)")
    elif selected_height:
        print(f"[YouTube] ATENCAO: formato {selected_fmt} tem apenas {selected_height}p "
              f"(abaixo de 1080p — Stream pode ser limitado por PO Token/IP)")
    else:
        print("[YouTube] ATENCAO: resolucao do formato selecionado nao detectada.")

    return {
        "title": title,
        "duration": duration,
        "is_live": is_live,
        "thumbnail": thumbnail,
        "video_url": video_url,
        "audio_url": audio_url,
        "height": selected_height,
        "format_id": selected_fmt,
        "original_url": yt_url,
        "use_proxy": used_proxy
    }

def build_ffmpeg_cmd(url, audio_url=None, is_live=False, use_proxy=False, start_sec=0, is_eco=False):
    is_http = url.startswith("http://") or url.startswith("https://")
    upstream_netloc = urllib.parse.urlparse(XTREAM_UPSTREAM).netloc if XTREAM_UPSTREAM else ""
    if is_http and ("/live/" in url or (upstream_netloc and upstream_netloc in url)) and url.lower().endswith(".m3u8"):
        url = url[:-5] + ".ts"
    url_lower = url.lower()
    is_googlevideo = "googlevideo" in url_lower or (audio_url and "googlevideo" in audio_url.lower())
    is_vod = any(x in url_lower for x in [
        "fontedecanais", "/movie/", "/series/", "movies/", "series/",
        ".mp4", ".mkv", "googlevideo.com"
    ])
    if is_live:
        is_vod = False
    elif audio_url or is_googlevideo:
        is_vod = True

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"
    ]

    # Input 0: Vídeo principal
    # A flag -re NÃO deve ser aplicada a streams de rede ao vivo.
    # Streams HTTP ao vivo já são produzidos em tempo real pelo servidor remoto;
    # aplicar -re em transmissões ao vivo impede que o FFmpeg absorva jitter de rede
    # e pode esvaziar o buffer do demuxer. Aplicar apenas em arquivos locais ou VOD estático.
    if not is_http or is_vod:
        cmd.append("-re")

    if is_http:
        ua = "Mozilla/5.0" if ((upstream_netloc and upstream_netloc in url) or "m3u8" in url or is_googlevideo) else "IPTVSmartersPro"
        cmd.extend(["-user_agent", ua])

        # Proxy residencial adicionado se use_proxy for explicitamente True (necessario para googlevideo quando assinado pelo proxy)
        if use_proxy and RESIDENTIAL_HTTP_PROXY:
            cmd.extend(["-http_proxy", RESIDENTIAL_HTTP_PROXY])

        if ".mp4" in url_lower or ".mkv" in url_lower or ".ts" in url_lower or is_googlevideo or is_live or not url_lower.endswith(".m3u8"):
            cmd.extend([
                "-rw_timeout", "10000000",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"
            ])
        else:
            cmd.extend([
                "-rw_timeout", "10000000",
                "-allowed_segment_extensions", "ALL",
                "-extension_picky", "0",
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "3"
            ])
    else:
        # Loop local video files infinitely so tests never exhaust the source
        cmd.extend(["-stream_loop", "-1"])

    cmd.extend([
        "-fflags", "+genpts+discardcorrupt",
        "-probesize", "1000000",
        "-analyzeduration", "1500000"
    ])
    if start_sec and start_sec > 0:
        cmd.extend(["-ss", str(int(start_sec))])
    cmd.extend(["-i", url])

    # Input 1: Áudio DASH separado (YouTube 1080p)
    if audio_url:
        cmd.extend([
            "-fflags", "+genpts+discardcorrupt",
            "-rw_timeout", "10000000",
            "-user_agent", "Mozilla/5.0",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-probesize", "1000000",
            "-analyzeduration", "1500000"
        ])
        if use_proxy and RESIDENTIAL_HTTP_PROXY:
            cmd.extend(["-http_proxy", RESIDENTIAL_HTTP_PROXY])
        if start_sec and start_sec > 0:
            cmd.extend(["-ss", str(int(start_sec))])
        cmd.extend(["-i", audio_url])

    if is_eco:
        # Modo Econômico: Stream Copy direto de vídeo H.264 original da transmissão
        # Reduz uso de CPU na VPS de ~164% para ~2%, mantendo fidelidade total e zero atraso.
        # Áudio é recodificado em AC3 estéreo 48kHz (Dolby Digital) para compatibilidade nativa com Samsung Plasma PL51F4000.
        cmd.extend([
            "-map", "0:v:0",
            "-map", "1:a:0" if audio_url else "0:a:0?",
            "-c:v", "copy",
            "-bsf:v", "dump_extra=freq=keyframe",
            "-af", "aresample=async=1000:first_pts=0:min_hard_comp=0.100000",
            "-c:a", "ac3",
            "-b:a", "384k",
            "-ar", "48000",
            "-ac", "2",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts+discardcorrupt+nobuffer",
            "-flags", "low_delay",
            "-streamid", "0:256",
            "-streamid", "1:257",
            "-mpegts_pmt_start_pid", "4096",
            "-pcr_period", "20",
            "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
            "-muxdelay", "0.7", "-muxpreload", "0.7",
            "-f", "mpegts",
            "pipe:1"
        ])
        return cmd

    if STREAM_RESOLUTION == "1080p":
        vf = "scale=1920:1080:force_original_aspect_ratio=decrease:flags=bicubic,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
        b_v = "3800k"
        maxrate = "4500k"
        bufsize = "7600k"
    else:
        vf = "scale=1280:720:force_original_aspect_ratio=decrease:flags=bicubic,pad=1280:720:(ow-iw)/2:(oh-ih)/2"
        b_v = "2200k"
        maxrate = "2600k"
        bufsize = "4400k"

    cmd.extend([
        "-map", "0:v:0",
        "-map", "1:a:0" if audio_url else "0:a:0?",
        # Normalização visual: 1080p ou 720p 30fps para Samsung Plasma PL51F4000
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-bf", "0",
        "-threads", "0",
        "-b:v", b_v,
        "-maxrate", maxrate,
        "-bufsize", bufsize,
        "-g", "30",
        "-keyint_min", "30",
        "-sc_threshold", "0",
        "-profile:v", "main",
        "-level", "4.1",
        "-x264-params", "repeat-headers=1:slices=1:bframes=0:aq-mode=2:aq-strength=1.0",

        # Normalização sonora: AC3 (Dolby Digital) a 48kHz (padrão nativo de TV Samsung)
        "-af", "aresample=async=1000:first_pts=0:min_hard_comp=0.100000",
        "-c:a", "ac3",
        "-b:a", "384k",
        "-ar", "48000",
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-flags", "low_delay",
        # PIDs fixos e imutáveis por canal (Video 0x100, Audio 0x101, PMT 0x1000)
        "-streamid", "0:256",
        "-streamid", "1:257",
        "-mpegts_pmt_start_pid", "4096",
        "-pcr_period", "20",
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts",
        "pipe:1"
    ])
    return cmd

class CommandItem:
    def __init__(self, cmd_id, cmd, dedupe_key=None, ttl=120.0):
        self.id = cmd_id
        self.cmd = cmd
        self.dedupe_key = dedupe_key
        self.ttl = ttl
        self.created_at = time.time()
        self.delivered_at = 0.0
        self.delivery_count = 0
        self.acked = False
        self.rc = None
        self.output = ""
        self.event = threading.Event()

class CommandBus:
    """Barramento de comandos assíncrono com IDs monotônicos, controle de reentrega e ACKs."""
    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.seq = int(time.time())
        self.queue = collections.deque()
        self.in_flight = {}
        self.history = collections.OrderedDict()

    def submit(self, cmd, dedupe_key=None, ttl=120.0):
        with self.lock:
            if dedupe_key:
                for existing in list(self.queue):
                    if existing.dedupe_key == dedupe_key:
                        existing.cmd = cmd
                        existing.created_at = time.time()
                        return existing
                for existing in list(self.in_flight.values()):
                    if existing.dedupe_key == dedupe_key:
                        if existing.cmd == cmd:
                            return existing
                        self.in_flight.pop(existing.id, None)
                        break

            self.seq += 1
            item = CommandItem(self.seq, cmd, dedupe_key=dedupe_key, ttl=ttl)
            self.queue.append(item)
            self.cv.notify_all()
            return item

    def poll(self, wait_sec=0.0, nocmd=False, is_v2=True):
        if nocmd:
            return None

        deadline = time.time() + max(0.0, wait_sec)
        with self.lock:
            while True:
                now = time.time()

                expired_ids = [cid for cid, it in self.in_flight.items() if now - it.created_at > it.ttl]
                for cid in expired_ids:
                    it = self.in_flight.pop(cid)
                    it.event.set()
                    self._record_history(it)

                for cid, it in list(self.in_flight.items()):
                    if now - it.delivered_at >= 8.0:
                        it.delivered_at = now
                        it.delivery_count += 1
                        return f"{it.id}|{it.cmd}" if is_v2 else it.cmd

                if self.queue:
                    item = self.queue.popleft()
                    item.delivered_at = now
                    item.delivery_count = 1
                    self.in_flight[item.id] = item
                    return f"{item.id}|{item.cmd}" if is_v2 else item.cmd

                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cv.wait(timeout=min(remaining, 1.0))

            return None

    def ack(self, cmd_id, rc=0, output=""):
        with self.lock:
            item = self.in_flight.pop(cmd_id, None)
            if not item:
                item = self.history.get(cmd_id)
            if item:
                item.rc = rc
                item.output = output
                item.acked = True
                item.event.set()
                self._record_history(item)
                return True
            return False

    def ack_legacy(self, output=""):
        with self.lock:
            if self.in_flight:
                cid, item = next(iter(list(self.in_flight.items())))
                self.in_flight.pop(cid)
                item.rc = 0
                item.output = output
                item.acked = True
                item.event.set()
                self._record_history(item)
                return True
            return False

    def _record_history(self, item):
        self.history[item.id] = item
        while len(self.history) > 50:
            self.history.popitem(last=False)

class StreamHub:
    """
    Hub de streaming central com arquitetura Make-Before-Break:
    - Um único processo upstream (FFmpeg) ativo por vez economiza CPU e conexões de rede.
    - O restamper SeamlessRestamper garante continuidade estrita de PTS/DTS entre trocas de canal.
    - A transição do canal antigo para o novo só ocorre após o novo canal enviar os primeiros bytes válidos.
    """
    def __init__(self, start_workers=True):
        self.lock = threading.RLock()
        self.switch_lock = threading.Lock()
        self.restamper = SeamlessRestamper()
        
        initial_ch = (
            CH_MGR.get_channel("cnn-brasil")
            or CH_MGR.get_channel("globo-rj")
            or CH_MGR.get_channel("band-rio")
            or CH_MGR.get_channel("test-timer")
            or list(CH_MGR.get_all().values())[0]
        )
        self.current_channel_id = initial_ch.get("id", "cnn-brasil")
        self.current_channel_name = initial_ch.get("name", "CNN Brasil")
        self.current_url = initial_ch.get("url", "")
        self.current_audio_url = None
        self.current_is_live = False
        self.current_is_temporary = False
        self.current_is_eco = initial_ch.get("is_eco", is_channel_eco(self.current_channel_name, self.current_url))
        self.fallback_channel = None
        self.last_live_channel = self.current_channel_id
        self.temporary_retries = 0
        self.youtube_meta = None
        
        self.subscribers = set()
        self.proc = None
        self.running = True
        self.switching = False
        self.in_standby = False
        self.idle_since = None
        self.total_bytes = 0
        self.start_time = time.time()
        self.last_chunk_time = time.time()
        self.client_telemetry = {}
        self.telemetry_time = 0
        self.pending_command = None
        self.command_output = None
        self.command_done_event = threading.Event()
        self.bus = CommandBus()
        self.tablet_pending_cmd = None
        self.tablet_cmd_res = None
        self.tablet_cmd_event = threading.Event()
        self.tablet_last_seen = 0
        self.tablet_usb_state = "DISCONNECTED"
        self.tablet_usb_pwr = False
        self.tablet_mode = "live"

        # Rolling pre-buffer para clientes novos (~60 MiB = ~100s a 120s de folga na TV)
        self.prebuffer_chunks = collections.deque()
        self.prebuffer_bytes = 0
        self.PREBUFFER_MAX_BYTES = 60 * 1024 * 1024

        # Slate video: carrega .ts pré-encodado na RAM para injeção durante gaps
        self.slate_chunks = []  # list of (chunk_bytes, duration_secs)
        self.slate_mode = False
        self.slate_start_time = 0
        self.stream_health = "ok"  # "ok", "degraded", "slate"
        self.health_window = []  # list of (timestamp, byte_count)
        self.health_window_size = 5.0  # seconds
        self._load_slate()

        # Import de `server` deve ser livre de processo/thread/rede. O start
        # explícito preserva o comportamento de produção via run().
        self._started = False
        self.reader_thread = None
        self.keepalive_thread = None
        if start_workers:
            self.start()

    def start(self):
        """Inicia o produtor e as threads do hub uma única vez."""
        if self._started:
            return
        self._started = True

        # Inicia transmissão do primeiro canal
        self._start_initial()

        # Thread de leitura contínua e distribuição
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        # Thread de keepalive (envia TS NULL packets se o buffer upstream atrasar)
        self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self.keepalive_thread.start()

    def _start_initial(self, start_sec=0):
        is_eco = getattr(self, "current_is_eco", None)
        if is_eco is None:
            is_eco = is_channel_eco(self.current_channel_name, self.current_url)
        if is_eco and not probe_is_h264(self.current_url):
            is_eco = False
        cmd = build_ffmpeg_cmd(
            self.current_url, 
            audio_url=self.current_audio_url, 
            is_live=self.current_is_live,
            use_proxy=getattr(self, "current_use_proxy", False),
            start_sec=start_sec,
            is_eco=is_eco
        )
        mode_tag = " [⚡ ECO: Stream Copy]" if is_eco else " [🔥 TRANSCODE: libx264]"
        tag = f" (offset {start_sec}s)" if start_sec > 0 else ""
        print(f"[*] Hub iniciando canal: {self.current_channel_name} ({self.current_url}){tag}{mode_tag}")
        log_event(f"FFMPEG_START {self.current_channel_id}{tag} eco={is_eco}")
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1048576
            )
        except Exception as e:
            print(f"[!] Erro ao iniciar processo: {e}")

    def _load_slate(self):
        """Carrega o vídeo-placeholder 'Sem Sinal' na RAM, dividido em chunks com pacing exato."""
        slate_file = "slate_1080p.ts" if STREAM_RESOLUTION == "1080p" else "slate.ts"
        slate_path = os.path.join(CONFIG_DIR, slate_file)
        if not os.path.exists(slate_path):
            fallback = "slate.ts" if STREAM_RESOLUTION == "1080p" else "slate_1080p.ts"
            fallback_path = os.path.join(CONFIG_DIR, fallback)
            if os.path.exists(fallback_path):
                slate_path = fallback_path
                slate_file = fallback
            else:
                print(f"[!] Slate não encontrado: {slate_path}. Proteção contra travamento DESABILITADA.")
                return
        try:
            with open(slate_path, "rb") as f:
                raw = f.read()
            total_pkts = len(raw) // 188
            if total_pkts == 0:
                print("[!] Slate vazio ou inválido.")
                return
            pps = total_pkts / 10.0  # slate tem duração de 10s
            chunk_size = 348 * 188  # 65424 bytes
            chunks_raw = [raw[i:i+chunk_size] for i in range(0, len(raw), chunk_size)]
            self.slate_chunks = []
            for c in chunks_raw:
                if len(c) % 188 == 0 and len(c) > 0:
                    dur = (len(c) // 188) / pps
                    self.slate_chunks.append((c, dur))
            total_kb = len(raw) / 1024
            print(f"[✓] Slate carregado: {slate_file} ({total_kb:.0f} KB, {len(self.slate_chunks)} chunks)")
        except Exception as e:
            print(f"[!] Erro ao carregar slate: {e}")

    def get_current_bitrate_kbps(self):
        """Calcula bitrate médio em kbps dos últimos N segundos."""
        now = time.time()
        cutoff = now - self.health_window_size
        with self.lock:
            self.health_window = [(t, b) for t, b in self.health_window if t >= cutoff]
            if not self.health_window:
                return 0.0
            total_bytes = sum(b for _, b in self.health_window)
            span = now - self.health_window[0][0]
            if span <= 0:
                return 0.0
            return (total_bytes * 8) / (span * 1000)

    def reset_pts_epoch(self):
        with self.lock:
            self.prebuffer_chunks.clear()
            self.prebuffer_bytes = 0
            self.restamper.reset_epoch()

    def subscribe(self):
        q = queue.Queue(maxsize=2500)
        with self.lock:
            if self.in_standby:
                print("[*] Despertando do Standby Inteligente: TV conectada!")
                log_event("STANDBY_WAKEUP (tv_connected)")
                self.in_standby = False
                self.prebuffer_chunks.clear()
                self.prebuffer_bytes = 0
                self.restamper.reset_epoch()
                self._start_initial()
            # Pré-carrega o assinante com o buffer recente para iniciar com folga robusta (~100s)
            for chunk in self.prebuffer_chunks:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    break
            self.subscribers.add(q)
            self.idle_since = None
            print(f"[+] Novo cliente conectado ao Hub. Total de ouvintes: {len(self.subscribers)} (buffer inicial: {self.prebuffer_bytes // 1024} KB)")
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)
            if len(self.subscribers) == 0 and not self.in_standby:
                self.idle_since = time.time()
                print(f"[*] Zero ouvintes. Standby agendado para {int(STANDBY_TIMEOUT)}s...")
            print(f"[-] Cliente desconectado do Hub. Total de ouvintes: {len(self.subscribers)}")

    def _mark_live_commit_locked(self):
        """Marca o commit live; chamado sob self.lock pelo switch."""
        # RC-001 iter8: o commit descarta o backlog do canal anterior de cada
        # assinante. Sem isto, um assinante atrasado continua consumindo chunks
        # antigos (até queue.Queue(maxsize=2500) = 156 MiB ~ 284 s a 4.6 Mbps)
        # DEPOIS do SWITCH_OK. Um chunk já retirado pelo writer HTTP segue em
        # voo; a continuidade MPEG-TS do splice é preservada pelo restamper.
        for q in list(self.subscribers):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self.slate_mode = False
        self.slate_start_time = 0
        self.stream_health = "ok"
        # O primeiro chunk pode ser parcial e o restamper pode devolver vazio.
        # O commit, não o processamento, é o novo marco de saúde.
        self.last_chunk_time = time.time()

    def _broadcast(self, data):
        if not data:
            return
        with self.lock:
            self.prebuffer_chunks.append(data)
            self.prebuffer_bytes += len(data)
            while self.prebuffer_bytes > self.PREBUFFER_MAX_BYTES:
                old = self.prebuffer_chunks.popleft()
                self.prebuffer_bytes -= len(old)

            # Enfileira no mesmo critical section do prebuffer. Sem isso, um
            # slate iniciado antes do commit pode chegar à fila depois do
            # primeiro chunk do novo canal.
            for q in list(self.subscribers):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # Buffer cheio por lag transitório: descarta o chunk mais antigo
                    # para que o leitor avance sem travar, preservando a conexão do assinante!
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass

    def _handle_proc_exit(self, p, rc):
        if p != self.proc or self.switching or self.in_standby:
            return

        if self.current_is_temporary:
            meta = self.youtube_meta or {}
            duration = meta.get("duration") or 0
            started_at = meta.get("started_at") or time.time()
            elapsed = time.time() - started_at

            # Checa se o vídeo terminou naturalmente (rc == 0 e tempo decorrido próximo à duração total)
            finished_naturally = (rc == 0) and (duration == 0 or elapsed >= max(0, duration - 15))

            if not finished_naturally and self.temporary_retries < 3:
                self.temporary_retries += 1
                seek_pos = max(0, int(elapsed - 2))
                print(f"[!] Transmissão temporária ({self.current_channel_name}) oscilou (rc={rc}, {int(elapsed)}s/{duration}s). Reconectando em {seek_pos}s (tentativa {self.temporary_retries}/3)...")
                log_event(f"TEMPORARY_STREAM_RETRY {self.current_channel_id} (attempt {self.temporary_retries}/3 at {seek_pos}s)")
                with self.lock:
                    if self.slate_chunks:
                        self.restamper.start_new_channel()
                        self.slate_mode = True
                        self.slate_start_time = time.time()
                        self.stream_health = "slate"
                time.sleep(1)
                if not self.switching and not self.in_standby:
                    with self.lock:
                        self.restamper.start_new_channel()
                    self._start_initial(start_sec=seek_pos)
                return

            # Terminou normalmente ou esgotou tentativas de reconexão
            fallback_ch = self.fallback_channel or getattr(self, "last_live_channel", None) or "band-rio"
            ch = CH_MGR.get_channel(fallback_ch)
            reason = "finalizada" if finished_naturally else f"interrompida após {self.temporary_retries} tentativas"
            print(f"[✓] Transmissão temporária {reason} ({self.current_channel_name}, rc={rc}). Retornando à TV ao vivo ({fallback_ch})...")
            log_event(f"TEMPORARY_STREAM_ENDED {self.current_channel_id} -> fallback to {fallback_ch} (reason: {reason})")
            self.current_is_temporary = False
            self.fallback_channel = None
            self.youtube_meta = None
            self.temporary_retries = 0
            if ch:
                self.current_channel_id = ch["id"]
                self.current_channel_name = ch["name"]
                self.current_url = ch["url"]
                self.current_audio_url = None
                self.current_is_live = False
            self.switch_channel(channel_id=fallback_ch, force=True)
        else:
            print(f"[!] Canal {self.current_channel_name} desconectou (rc={rc}). Reconectando...")
            log_event(f"FFMPEG_DIED rc={rc} ch={self.current_channel_id} -> reconnect")
            with self.lock:
                if self.slate_chunks:
                    self.restamper.start_new_channel()
                    self.slate_mode = True
                    self.slate_start_time = time.time()
                    self.stream_health = "slate"
            time.sleep(1)
            if not self.switching and not self.in_standby:
                with self.lock:
                    self.restamper.start_new_channel()
                self._start_initial()

    def _reader_loop(self):
        while self.running:
            # Standby inteligente: 90s sem ouvintes encerra FFmpeg
            if len(self.subscribers) == 0 and not self.in_standby:
                if self.idle_since is None:
                    self.idle_since = time.time()
                elif time.time() - self.idle_since >= STANDBY_TIMEOUT:
                    with self.lock:
                        print(f"[*] Standby Inteligente: Sem ouvintes por {int(STANDBY_TIMEOUT)}s. Encerrando FFmpeg para poupar IPTV.")
                        log_event("STANDBY_ENTER")
                        self.in_standby = True
                        if self.proc:
                            try:
                                self.proc.kill()
                            except Exception:
                                pass
                            self.proc = None
                        continue

            if self.in_standby:
                time.sleep(0.5)
                continue

            p = self.proc
            if not p:
                time.sleep(0.05)
                continue

            if p.poll() is not None:
                self._handle_proc_exit(p, p.poll())
                time.sleep(0.05)
                continue

            try:
                chunk = os.read(p.stdout.fileno(), 65424)
            except Exception:
                chunk = None

            if chunk:
                with self.lock:
                    # Se o processo ativo mudou enquanto lemos, ignora dados do processo antigo
                    if p != self.proc:
                        continue
                    if self.slate_mode:
                        print("[✓] Upstream recuperou — saindo do modo Slate.")
                        log_event("SLATE_EXIT (upstream recovered)")
                        self.restamper.start_new_channel()
                        self.slate_mode = False
                        self.stream_health = "ok"
                    processed = self.restamper.process_chunk(chunk)
                    if processed:
                        self.total_bytes += len(processed)
                        self.last_chunk_time = time.time()
                        self.health_window.append((time.time(), len(processed)))
                        self._broadcast(processed)
            else:
                # Chunk vazio ou erro de leitura: processo pode ter terminado
                if p.poll() is not None:
                    self._handle_proc_exit(p, p.poll())
                time.sleep(0.01)

    def _keepalive_loop(self):
        """Keepalive inteligente com injeção de Slate Video.
        
        Quando FFmpeg para de produzir dados por > SLATE_GAP_THRESHOLD segundos,
        injeta o vídeo 'Sem Sinal' pré-encodado em loop, mantendo o decodificador
        H.264 do ConnectShare ativo e evitando travamento da TV.
        
        Fallback: se o slate não foi carregado, envia NULL packets (comportamento legado).
        """
        null_pkt = b'\x47\x1f\xff\x10' + b'\xff' * 184
        null_burst = null_pkt * 70  # ~13 KB (~2.1 Mbps compassado)

        slate_idx = 0
        last_slate_broadcast = 0.0
        next_slate_pace = 0.0

        while self.running:
            time.sleep(0.05)
            # O switching não suprime mais o keepalive. Se o canal antigo
            # congelar durante a aquisição do candidato, o slate/NULL continua
            # segurando o decoder da TV até o commit.
            if len(self.subscribers) == 0 or self.in_standby:
                continue

            gap = time.time() - self.last_chunk_time

            if gap <= 0.6:
                # Stream saudável - recuperação de slate é tratada atomicamente no _reader_loop
                continue

            # Se já estamos em modo Slate (por desconexão ou gap longo anterior),
            # continua transmitindo os chunks do slate compassados até que dados reais voltem!
            if self.slate_mode:
                # Watchdog de recuperação: se estamos em slate há mais de 15s e o processo upstream
                # ainda consta como vivo (ex: socket TCP congelado pela operadora sem EOF),
                # força a finalização do FFmpeg para disparar reconexão imediata!
                if self.slate_start_time and (time.time() - self.slate_start_time > 15.0):
                    with self.lock:
                        if not self.slate_mode:
                            continue
                        # Durante a aquisição do candidato o processo antigo é
                        # a garantia de make-before-break. O watchdog não pode
                        # matá-lo até o switch terminar, mas o slate/NULL deve
                        # continuar saindo normalmente.
                        p = None if self.switching else self.proc
                        stuck = bool(p and p.poll() is None)
                        if stuck:
                            # kill() sob o mesmo lock que observa switching:
                            # nenhuma troca pode começar entre a checagem e o
                            # sinal. A chamada é não bloqueante.
                            try:
                                p.kill()
                            except Exception:
                                pass
                    if stuck:
                        print("[!] Upstream travado em Slate por >15s. Forçando reinício do FFmpeg...")
                        log_event("WATCHDOG_FFMPEG_KILL (stuck in slate >15s)")

                if self.slate_chunks:
                    now = time.time()
                    if now - last_slate_broadcast >= next_slate_pace:
                        with self.lock:
                            # Um commit pode ter ocorrido enquanto este tick
                            # esperava o lock; não publique slate obsoleto.
                            if not self.slate_mode or time.time() - self.last_chunk_time <= 0.6:
                                continue
                            # Re-ancora timestamps seamless a cada ciclo completo do slate (10s)
                            if slate_idx > 0 and (slate_idx % len(self.slate_chunks) == 0):
                                self.restamper.start_new_channel()
                            chunk, dur = self.slate_chunks[slate_idx % len(self.slate_chunks)]
                            processed = self.restamper.process_chunk(chunk)
                            if processed:
                                self._broadcast(processed)
                        slate_idx += 1
                        last_slate_broadcast = now
                        next_slate_pace = dur
                else:
                    with self.lock:
                        if not self.slate_mode or time.time() - self.last_chunk_time <= 0.6:
                            continue
                        self._broadcast(null_burst)
                continue

            # Gap detectado: decidir entre NULL packets transitórios e Slate
            if gap < SLATE_GAP_THRESHOLD:
                # Gap curto: NULL packets para absorver jitter transitório
                with self.lock:
                    if time.time() - self.last_chunk_time <= 0.6:
                        continue
                    if self.stream_health == "ok":
                        self.stream_health = "degraded"
                    self._broadcast(null_burst)
                continue

            # Gap longo (>= SLATE_GAP_THRESHOLD): modo Slate!
            if self.slate_chunks:
                with self.lock:
                    gap = time.time() - self.last_chunk_time
                    if gap <= 0.6:
                        continue
                    if gap < SLATE_GAP_THRESHOLD:
                        # O gap encurtou enquanto esperávamos; o próximo tick decide.
                        continue
                    if self.slate_mode:
                        continue
                    print(f"[⚠] Gap de {gap:.1f}s detectado — entrando no modo Slate (Sem Sinal).")
                    log_event(f"SLATE_ENTER gap={gap:.1f}s")
                    self.restamper.start_new_channel()
                    self.slate_mode = True
                    self.slate_start_time = time.time()
                    self.stream_health = "slate"
                    slate_idx = 0
                    now = time.time()
                    chunk, dur = self.slate_chunks[0]
                    processed = self.restamper.process_chunk(chunk)
                    if processed:
                        self._broadcast(processed)
                slate_idx = 1
                last_slate_broadcast = now
                next_slate_pace = dur
            else:
                # Fallback sem slate: NULL packets legados
                with self.lock:
                    if time.time() - self.last_chunk_time <= 0.6:
                        continue
                    if self.stream_health != "slate":
                        self.stream_health = "degraded"
                    self._broadcast(null_burst)

    def switch_channel(self, channel_id=None, custom_url=None, custom_name=None, force=False, is_eco=None, no_reconnect=False):
        if not self.switch_lock.acquire(blocking=True, timeout=4.0):
            print("[~] Troca de canal anterior ainda em andamento. Aguarde...")
            return False

        # Sinaliza a aquisição antes de criar a thread; o watchdog de slate
        # consulta este flag sob o mesmo lock.
        with self.lock:
            self.switching = True

        def _do_switch():
            ch = None
            try:
                t_sw0 = time.time()
                if channel_id:
                    ch = CH_MGR.get_channel(channel_id)

                if custom_url:
                    target_id = f"custom_{custom_url}"
                    target_name = custom_name or (ch["name"] if ch else "Canal Personalizado")
                    target_url = resolve_channel_url(custom_url)
                elif channel_id:
                    if not ch:
                        print(f"[!] Canal id {channel_id} não encontrado na grade.")
                        return
                    target_id = ch["id"]
                    target_name = ch["name"]
                    target_url = resolve_channel_url(ch["url"])
                else:
                    return

                target_url = resolve_channel_url(target_url)

                if not force and target_id == self.current_channel_id:
                    print(f"[~] Já sintonizado em: {target_name}")
                    log_event(f"SWITCH_NOOP already_on {target_id}")
                    return

                if ch and ch.get("is_eco") is False:
                    target_is_eco = False
                elif is_eco is not None:
                    target_is_eco = bool(is_eco)
                elif ch and ch.get("is_eco") is not None:
                    target_is_eco = bool(ch.get("is_eco"))
                else:
                    target_is_eco = is_channel_eco(target_name, target_url)

                eco_desc = " [⚡ MODO ECONÔMICO: STREAM COPY -c:v copy (~2% CPU)]" if target_is_eco else " [🔥 MODO TRANSCODE: libx264]"
                print(f"\n[⚡] INICIANDO SINTONIA MAKE-BEFORE-BREAK: {target_name} ({target_url}){eco_desc}")
                log_event(f"SWITCH_BEGIN {self.current_channel_id} -> {target_id} eco={target_is_eco}")
                def wait_for_stream_chunk(p, timeout_sec):
                    deadline = time.time() + timeout_sec
                    box = []
                    def _reader():
                        try:
                            c = os.read(p.stdout.fileno(), 65424)
                            if c:
                                box.append(c)
                        except Exception:
                            pass
                    rt = threading.Thread(target=_reader, daemon=True)
                    rt.start()
                    while time.time() < deadline:
                        if box:
                            return box[0]
                        if p.poll() is not None:
                            break
                        time.sleep(0.04)
                    return None

                first_chunk_data = None
                new_p = None

                # Tentativa 1: Modo Eco (Stream Copy) se elegível
                if target_is_eco:
                    if not probe_is_h264(target_url):
                        print(f"[!] Canal {target_name} não é H.264/30fps verificado (pode ser HEVC/H.265). Ativando Transcode (libx264)...")
                        log_event(f"SWITCH_ECO_REJECTED_NON_H264 {target_id}")
                        target_is_eco = False
                    else:
                        cmd_eco = build_ffmpeg_cmd(target_url, is_live=True, is_eco=True)
                        try:
                            new_p = subprocess.Popen(
                                cmd_eco,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                bufsize=1048576
                            )
                            first_chunk_data = wait_for_stream_chunk(new_p, 8.0)
                        except Exception as e:
                            print(f"[!] Erro ao iniciar processo Modo Eco para {target_name}: {e}")

                    if not first_chunk_data:
                        rc = new_p.poll() if new_p else "N/A"
                        print(f"[!] Modo Eco falhou ou expirou para {target_name} (rc={rc}). Ativando Fallback Inteligente para Transcode (libx264)...")
                        log_event(f"SWITCH_ECO_FALLBACK_TRANSCODE {target_id} rc={rc}")
                        if new_p:
                            try:
                                new_p.kill()
                            except Exception:
                                pass
                            new_p = None
                        target_is_eco = False

                # Tentativa 2 / Modo Padrão: Transcodificação forçada (libx264)
                if not first_chunk_data:
                    cmd_transcode = build_ffmpeg_cmd(target_url, is_live=True, is_eco=False)
                    try:
                        new_p = subprocess.Popen(
                            cmd_transcode,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=1048576
                        )
                        first_chunk_data = wait_for_stream_chunk(new_p, 20.0)
                    except Exception as e:
                        print(f"[!] Erro ao iniciar processo Transcode para {target_name}: {e}")

                if not first_chunk_data:
                    print(f"[!] Timeout total ao conectar em {target_name}. Mantendo canal {self.current_channel_name} sem queda.")
                    log_event(f"SWITCH_TIMEOUT {target_id} (kept {self.current_channel_id})")
                    if new_p:
                        try:
                            new_p.kill()
                        except Exception:
                            pass
                    return

                # Chaveamento atômico instantâneo!
                with self.lock:
                    old_p = self.proc
                    old_is_eco = self.current_is_eco
                    self.proc = new_p
                    self.current_channel_id = target_id
                    self.current_channel_name = target_name
                    self.current_url = target_url
                    self.current_audio_url = None
                    self.current_is_live = False
                    self.current_is_temporary = False
                    self.current_is_eco = target_is_eco
                    self.current_use_proxy = False
                    self.last_live_channel = target_id
                    self.fallback_channel = None
                    self.youtube_meta = None
                    self.temporary_retries = 0

                    # Avança timestamps e continuity counters de forma estritamente contínua
                    self.restamper.start_new_channel()
                    self.prebuffer_chunks.clear()
                    self.prebuffer_bytes = 0

                    # O commit é live: qualquer slate/health antigo morre aqui,
                    # sob o mesmo lock que publica o primeiro chunk.
                    self._mark_live_commit_locked()

                    # Transmite o primeiro chunk do novo canal com timestamps contínuos
                    processed = self.restamper.process_chunk(first_chunk_data)
                    if processed:
                        self.total_bytes += len(processed)
                        self.last_chunk_time = time.time()
                        self._broadcast(processed)

                    # Encerra o processo do canal anterior
                    if old_p:
                        try:
                            old_p.kill()
                        except Exception:
                            pass

                print(f"[✓] CHAVEAMENTO CONCLUÍDO COM SUCESSO! Novo canal ativo na TV: {target_name}")
                log_event(f"SWITCH_OK {target_id} total={(time.time() - t_sw0):.1f}s")

            finally:
                with self.lock:
                    self.switching = False
                self.switch_lock.release()

        try:
            threading.Thread(target=_do_switch, daemon=True).start()
        except Exception:
            with self.lock:
                self.switching = False
            self.switch_lock.release()
            raise
        return True

    def switch_youtube(self, meta):
        """
        Sintoniza vídeo ou live do YouTube de forma Make-Before-Break.
        Suporta dual-stream DASH (vídeo 1080p + áudio separado).
        Ao final de vídeos normais (VOD), retorna automaticamente ao canal de TV padrão.
        """
        if not self.switch_lock.acquire(blocking=True, timeout=4.0):
            print("[~] Troca de transmissão anterior ainda em andamento. Aguarde...")
            return False

        # Sinaliza a aquisição antes de criar a thread; o watchdog de slate
        # consulta este flag sob o mesmo lock.
        with self.lock:
            self.switching = True

        def _do_switch():
            try:
                yt_id = hashlib.md5(meta['original_url'].encode()).hexdigest()[:8]
                target_id = f"youtube_{yt_id}"
                target_name = f"YouTube: {meta['title']}"
                target_url = meta["video_url"]
                audio_url = meta.get("audio_url")
                is_live = meta.get("is_live", False)
                use_proxy = meta.get("use_proxy", False)
                return_channel = (
                    getattr(self, "last_live_channel", None)
                    or (self.current_channel_id if not self.current_is_temporary else None)
                    or self.fallback_channel
                    or "band-rio"
                )

                print(f"\n[▶️] INICIANDO TRANSMISSÃO DO YOUTUBE: {target_name} (via_proxy={use_proxy})")
                log_event(f"YOUTUBE_START {target_id} ({meta['title']})")

                cmd = build_ffmpeg_cmd(target_url, audio_url=audio_url, is_live=is_live, use_proxy=use_proxy)
                try:
                    new_p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=1048576
                    )
                except Exception as e:
                    print(f"[!] Erro ao iniciar processo para {target_name}: {e}")
                    return

                # Aguarda os primeiros bytes válidos (timeout 22s para YouTube DASH)
                first_chunk = []
                def read_first():
                    try:
                        c = os.read(new_p.stdout.fileno(), 65424)
                        if c:
                            first_chunk.append(c)
                    except Exception:
                        pass

                t = threading.Thread(target=read_first, daemon=True)
                t.start()
                t.join(timeout=22.0)

                if not first_chunk or len(first_chunk[0]) == 0:
                    print(f"[!] Timeout ao conectar no YouTube: {target_name}. Mantendo canal anterior {self.current_channel_name}.")
                    log_event(f"YOUTUBE_TIMEOUT {target_id} (kept {self.current_channel_id})")
                    try:
                        new_p.kill()
                    except Exception:
                        pass
                    return

                # Chaveamento atômico instantâneo na TV!
                with self.lock:
                    old_p = self.proc
                    self.proc = new_p
                    self.current_channel_id = target_id
                    self.current_channel_name = target_name
                    self.current_url = target_url
                    self.current_audio_url = audio_url
                    self.current_is_live = is_live
                    self.current_is_temporary = not is_live
                    self.current_is_eco = False
                    self.current_use_proxy = use_proxy
                    self.fallback_channel = return_channel
                    self.temporary_retries = 0
                    self.youtube_meta = {
                        "title": meta["title"],
                        "duration": meta.get("duration"),
                        "thumbnail": meta.get("thumbnail"),
                        "original_url": meta["original_url"],
                        "started_at": int(time.time()),
                        "is_live": is_live
                    }

                    # Reinicia restamper de forma suave e contínua
                    self.restamper.start_new_channel()
                    self.prebuffer_chunks.clear()
                    self.prebuffer_bytes = 0

                    # O commit é live: qualquer slate/health antigo morre aqui,
                    # sob o mesmo lock que publica o primeiro chunk.
                    self._mark_live_commit_locked()

                    processed = self.restamper.process_chunk(first_chunk[0])
                    if processed:
                        self.total_bytes += len(processed)
                        self.last_chunk_time = time.time()
                        self._broadcast(processed)

                    if old_p:
                        try:
                            old_p.kill()
                        except Exception:
                            pass

                print(f"[✓] YOUTUBE TRANSMITINDO NA TV COM SUCESSO! Novo conteúdo: {target_name}")
                log_event(f"YOUTUBE_OK {target_id}")
            finally:
                with self.lock:
                    self.switching = False
                self.switch_lock.release()

        try:
            threading.Thread(target=_do_switch, daemon=True).start()
        except Exception:
            with self.lock:
                self.switching = False
            self.switch_lock.release()
            raise
        return True

# Unstarted at import: tests and discovery must not spawn ffmpeg/threads.
# run() calls HUB.start() before serving; direct StreamHub() still starts.
HUB = StreamHub(start_workers=False)

def dispatch_device_cmd(cmd, dedupe_key=None):
    """Envia comando para o dispositivo ativo (Tablet via CommandBus ou Xiaomi)."""
    log_event(f"DISPATCH_CMD {cmd}")
    print(f"[*] Disparando comando para aparelho: {cmd}")
    HUB.pending_command = f"su -c '{cmd}'"
    HUB.tablet_pending_cmd = cmd

    if not dedupe_key and cmd:
        if "switch_live" in cmd:
            dedupe_key = "switch_live"
        elif "switch_vod" in cmd:
            dedupe_key = "switch_vod"

    item = HUB.bus.submit(cmd, dedupe_key=dedupe_key)

    def _run_adb():
        for port in [25555, 25556]:
            try:
                subprocess.run(["adb", "-s", f"127.0.0.1:{port}", "shell", f"su -c '{cmd}'"],
                               timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    threading.Thread(target=_run_adb, daemon=True).start()
    return item

def resolve_vod_stream_url(url):
    """
    Resolve redirecionamentos HTTP 302/301 e remove porta :80 explícita
    apenas no netloc para evitar bloqueio 403 da Cloudflare/CDN em filmes e séries.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return url

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    proxies_to_try = [True, False] if (RESIDENTIAL_HTTP_PROXY and is_proxy_alive(RESIDENTIAL_HTTP_PROXY)) else [False]
    for use_proxy in proxies_to_try:
        try:
            handlers = [NoRedirect]
            if use_proxy and RESIDENTIAL_HTTP_PROXY:
                handlers.append(urllib.request.ProxyHandler({
                    'http': RESIDENTIAL_HTTP_PROXY,
                    'https': RESIDENTIAL_HTTP_PROXY
                }))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": "IPTVSmartersPro"})
            with opener.open(req, timeout=5) as resp:
                pass
            return url
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if loc:
                    p = urllib.parse.urlsplit(loc)
                    if p.port == 80 and p.netloc:
                        clean_netloc = p.netloc.replace(":80", "")
                        clean_loc = urllib.parse.urlunsplit((p.scheme, clean_netloc, p.path, p.query, p.fragment))
                    else:
                        clean_loc = loc
                    print(f"[✓] VOD Redirecionamento resolvido: {clean_loc[:70]}...")
                    return clean_loc
            return url
        except Exception as ex:
            if use_proxy:
                continue
            return url
    return url

def is_sar_samsung_compatible(sar):
    """Verifica se o Sample Aspect Ratio (SAR) é compatível com o decodificador Samsung TV."""
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
                # Tolera pequenas variações de digitalização/captura de TV (entre 0.85 e 1.18)
                if 0.85 <= ratio <= 1.18:
                    return True
    except Exception:
        pass
    return False

def probe_vod_stream(url, use_proxy=False):
    """Obtém metadados de vídeo, áudio e duração remota via ffprobe."""
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n",
        "-show_entries", "format=duration:stream=codec_type,codec_name,profile,level,pix_fmt,width,height,channels,sample_rate,sample_aspect_ratio,r_frame_rate",
        "-of", "json"
    ]
    if use_proxy and RESIDENTIAL_HTTP_PROXY and is_proxy_alive(RESIDENTIAL_HTTP_PROXY):
        probe_cmd.extend(["-http_proxy", RESIDENTIAL_HTTP_PROXY])
    probe_cmd.append(url)
    try:
        res = subprocess.check_output(probe_cmd, timeout=12, text=True)
        return json.loads(res)
    except Exception as e:
        print(f"[*] ffprobe probe aviso: {e}")
        return {}

def _prepare_vod_thread_inner(task_id, url, title, poster=""):
    with VOD_TASKS_LOCK:
        task = VOD_TASKS.get(task_id)
        if not task:
            return
        task["status"] = "queued"
        task["status_msg"] = "Na fila (máx 2 simultâneos)..."
        task["poster"] = poster or task.get("poster", "")
        task["title"] = title or task.get("title", "Filme VOD")
        task["url"] = url
        save_vod_tasks()

    with VOD_SEMAPHORE:
        with VOD_TASKS_LOCK:
            task = VOD_TASKS.get(task_id)
            if not task:
                return
            task["status"] = "processing"
            task["progress"] = 5
            task["status_msg"] = "Iniciando análise do stream..."
            task["speed"] = ""
            save_vod_tasks()

        out_file = os.path.join(VOD_DIR, f"{task_id}.mp4")
        out_tmpl = os.path.join(VOD_DIR, f"{task_id}.bin")

        is_torrent = False
        try:
            from torrent_downloader import (
                is_torrent_url, extract_torrent_title, run_aria2_download,
                find_main_video_file, find_and_prepare_subtitle, is_audio_portuguese,
                can_stream_copy_video, build_ffmpeg_transcode_command
            )
            is_torrent = is_torrent_url(url)
        except Exception as e:
            print(f"[*] Importação torrent_downloader: {e}")
            is_torrent = url.strip().lower().startswith("magnet:?") or url.strip().lower().endswith(".torrent")

        is_yt = any(x in url.lower() for x in ["youtube.com", "youtu.be"]) if not is_torrent else False
        clean_title = title or task.get("title") or "Filme VOD"
        duration = 0

        try:
            if is_torrent:
                if (not title or title in ("Filme VOD", "Vídeo VOD", "Vídeo")):
                    clean_title = extract_torrent_title(url, fallback=clean_title)

                fat_name_preview = format_fat_media_name(clean_title)
                with VOD_TASKS_LOCK:
                    task["title"] = clean_title
                    task["display_name"] = fat_name_preview
                    task["poster"] = poster or task.get("poster", "")
                    task["progress"] = 5
                    task["status_msg"] = "Conectando ao enxame BitTorrent (DHT/P2P)..."
                    save_vod_tasks()

                torrent_dir = os.path.join(VOD_DIR, f"torrent_{task_id}")
                work_dir = os.path.join(VOD_DIR, f"work_{task_id}")
                os.makedirs(torrent_dir, exist_ok=True)
                os.makedirs(work_dir, exist_ok=True)

                print(f"[VOD] Iniciando download torrent via aria2c: '{clean_title}'...")
                ok = run_aria2_download(
                    magnet_url=url,
                    download_dir=torrent_dir,
                    task_dict=task,
                    task_lock=VOD_TASKS_LOCK,
                    running_procs_dict=VOD_RUNNING_PROCS,
                    task_id=task_id
                )
                if not ok:
                    with VOD_TASKS_LOCK:
                        if task_id not in VOD_TASKS or task.get("status") == "cancelled":
                            print(f"[VOD] Download torrent cancelado para {task_id}.")
                            return
                    raise RuntimeError("Download do torrent via aria2c falhou ou foi abortado")

                main_video = find_main_video_file(torrent_dir)
                if not main_video:
                    raise RuntimeError("Nenhum arquivo de vídeo suportado encontrado no torrent baixado")

                with VOD_TASKS_LOCK:
                    task["progress"] = 50
                    task["status_msg"] = "Localizando legendas em Português e analisando áudio..."
                    save_vod_tasks()

                sub_file = find_and_prepare_subtitle(main_video, torrent_dir, work_dir)
                is_pt_audio = is_audio_portuguese(main_video)

                try:
                    probe_out = subprocess.check_output([
                        "ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", main_video
                    ], text=True).strip()
                    duration = float(probe_out)
                except Exception:
                    duration = 0

                can_copy = (sub_file is None) and is_pt_audio and can_stream_copy_video(main_video)
                cmd = build_ffmpeg_transcode_command(main_video, out_file, sub_file, can_copy)

                with VOD_TASKS_LOCK:
                    if sub_file:
                        task["status_msg"] = "Gravando legenda PT-BR (hardsub) em 1080p H.264/AC-3..."
                    elif can_copy:
                        task["status_msg"] = "Áudio PT detectado! Cópia de vídeo direta sem perda (-c:v copy)..."
                    else:
                        task["status_msg"] = "Codificando 1080p H.264 High Profile + AC-3 Samsung..."
                    task["progress"] = 52
                    save_vod_tasks()

                print(f"[VOD] Executando FFmpeg Samsung MStar para torrent '{clean_title}' (hardsub={bool(sub_file)}, copy={can_copy})...")
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, preexec_fn=_vod_subproc_setup)
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS[task_id] = proc

                time_pat = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
                spd_pat = re.compile(r"speed=\s*([\d\.]+)x")
                err_lines = []
                for line in proc.stderr:
                    err_lines.append(line.strip())
                    if len(err_lines) > 20:
                        err_lines.pop(0)
                    m = time_pat.search(line)
                    m_spd = spd_pat.search(line)
                    if m:
                        hrs, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
                        cur_secs = hrs * 3600 + mins * 60 + secs
                        if duration > 0:
                            pct = min(98, max(52, int(52 + (cur_secs / duration) * 46)))
                        else:
                            pct = min(95, 52 + int(cur_secs / 30))
                        with VOD_TASKS_LOCK:
                            if task_id not in VOD_TASKS:
                                break
                            task["progress"] = pct
                            if m_spd:
                                task["speed"] = f"{m_spd.group(1)}x"
                proc.wait()
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS.pop(task_id, None)

                try:
                    shutil.rmtree(torrent_dir, ignore_errors=True)
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass

                if proc.returncode != 0:
                    err_snippet = " ".join([l for l in err_lines if "error" in l.lower() or "failed" in l.lower()][-3:])
                    raise RuntimeError(f"FFmpeg falhou ({proc.returncode}): {err_snippet or 'erro na conversão'}")
            elif is_yt:
                if "/live/" in url.lower():
                    with VOD_TASKS_LOCK:
                        if task_id in VOD_TASKS:
                            task["status"] = "error"
                            task["error"] = "Transmissões ao vivo não podem ser salvas no Cinema. Use 'Assistir na TV' para ver ao vivo."
                            task["status_msg"] = "Erro: É uma transmissão ao vivo"
                            save_vod_tasks()
                    return

                raw_file = os.path.join(VOD_DIR, f"{task_id}_raw.mkv")
                if os.path.exists(raw_file) and os.path.getsize(raw_file) > 1000000:
                    print(f"[VOD] Arquivo raw já existente ({os.path.getsize(raw_file) / (1024*1024):.1f} MB), iniciando conversão...")
                    with VOD_TASKS_LOCK:
                        task["progress"] = 50
                        task["status_msg"] = "Processando arquivo baixado..."
                    try:
                        probe_out = subprocess.check_output([
                            "ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", raw_file
                        ], text=True).strip()
                        duration = float(probe_out)
                    except Exception:
                        duration = 600
                else:
                    with VOD_TASKS_LOCK:
                        task["progress"] = 10
                        task["status_msg"] = "Baixando vídeo do YouTube em 1080p..."
                    yt_height = None
                    try:
                        meta = resolve_youtube(url)
                        yt_height = meta.get("height")
                        clean_title = meta.get("title") or clean_title
                        duration = meta.get("duration") or 0
                        if meta.get("thumbnail") and not poster:
                            poster = meta.get("thumbnail")
                        if meta.get("is_live"):
                            with VOD_TASKS_LOCK:
                                if task_id in VOD_TASKS:
                                    task["status"] = "error"
                                    task["error"] = "Transmissões ao vivo não podem ser salvas no Cinema. Use 'Assistir na TV' para ver ao vivo."
                                    task["status_msg"] = "Erro: É uma transmissão ao vivo"
                                    save_vod_tasks()
                            return
                    except Exception as e:
                        print(f"[*] resolve_youtube informativo: {e}, prosseguindo para download direto...")

                    # Reflete a resolucao REAL do formato escolhido (nao promete 1080p as cegas)
                    res_label = f"{yt_height}p" if yt_height else "resolução automática"
                    with VOD_TASKS_LOCK:
                        if task_id in VOD_TASKS:
                            task["status_msg"] = f"Baixando vídeo do YouTube em {res_label}..."
                            save_vod_tasks()

                    fat_name_preview = format_fat_media_name(clean_title)
                    with VOD_TASKS_LOCK:
                        task["title"] = clean_title
                        task["display_name"] = fat_name_preview
                        task["poster"] = poster or task.get("poster", "")
                        save_vod_tasks()

                    cookies_path = os.path.join(VOD_DIR, "youtube_cookies.txt")
                    has_cookies = bool(os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 100)

                    def _build_vod_yt_cmd(use_cookies=True, proxy=""):
                        c = [
                            YT_DLP_BIN,
                            "--no-warnings",
                            "--no-playlist",
                            # Mesma racional do resolve_youtube: web_safari obtem a escada
                            # HLS completa (ate 1920x1080) onde o cliente 'web' ficaria
                            # preso em 360p por falta de PO Token em IP de datacenter.
                            "--extractor-args", "youtube:player_client=web_safari,web",
                            "--match-filter", "!is_live",
                            "--no-live-from-start",
                            # HLS baixa segmento por segmento; sem fragments paralelos
                            # a velocidade cai para ~2 MB/s (download de 1h levaria 20+ min).
                            "--concurrent-fragments", "8",
                            "--remote-components", "ejs:github",
                            "-f", "bestvideo[height<=1080][vcodec^=avc]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                            "--merge-output-format", "mkv",
                            "-o", raw_file
                        ]
                        if os.path.exists("/usr/bin/qjs"):
                            c.extend(["--js-runtimes", "quickjs:/usr/bin/qjs"])
                        elif shutil.which("qjs"):
                            c.extend(["--js-runtimes", f"quickjs:{shutil.which('qjs')}"])
                        if use_cookies and os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 100:
                            c.extend(["--cookies", cookies_path])
                        if proxy:
                            c.extend(["--proxy", proxy])
                        c.append(url)
                        return c

                    socks_p = get_effective_socks_proxy()
                    proxy_clean = socks_p.replace("socks5h://", "socks5://") if socks_p else ""

                    use_proxy_initial = bool(not has_cookies and proxy_clean)
                    initial_proxy = proxy_clean if use_proxy_initial else ""
                    speed_label = "Proxy Móvel" if use_proxy_initial else "Gigabit Direto"

                    print(f"[VOD] Baixando YouTube ({speed_label}) '{clean_title}' em {res_label}...")
                    yt_cmd = _build_vod_yt_cmd(use_cookies=has_cookies, proxy=initial_proxy)
                    proc_yt = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_vod_subproc_setup)
                    with VOD_RUNNING_PROCS_LOCK:
                        VOD_RUNNING_PROCS[task_id] = proc_yt
                    pct_pat = re.compile(r"(\d+\.\d+)%")
                    for line in proc_yt.stdout:
                        m = pct_pat.search(line)
                        if m:
                            pct = min(50, max(10, int(float(m.group(1)) * 0.4 + 10)))
                            with VOD_TASKS_LOCK:
                                if task_id not in VOD_TASKS:
                                    break
                                task["progress"] = pct
                    proc_yt.wait()
                    with VOD_RUNNING_PROCS_LOCK:
                        VOD_RUNNING_PROCS.pop(task_id, None)

                    with VOD_TASKS_LOCK:
                        if task_id not in VOD_TASKS:
                            print(f"[VOD] Tarefa {task_id} cancelada/removida, abortando thread.")
                            return

                    # Se falhou e temos proxy móvel disponível, faz fallback transparente sem cookies corrompidos
                    if proc_yt.returncode != 0 and proxy_clean and (not use_proxy_initial or has_cookies):
                        print(f"[VOD] Tentativa inicial falhou (rc={proc_yt.returncode}). Fazendo fallback transparente via proxy móvel sem cookies...")
                        if has_cookies:
                            try:
                                os.rename(cookies_path, cookies_path + ".expired")
                            except Exception:
                                pass
                            has_cookies = False

                        fallback_cmd = _build_vod_yt_cmd(use_cookies=False, proxy=proxy_clean)
                        proc_yt = subprocess.Popen(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_vod_subproc_setup)
                        with VOD_RUNNING_PROCS_LOCK:
                            VOD_RUNNING_PROCS[task_id] = proc_yt
                        for line in proc_yt.stdout:
                            m = pct_pat.search(line)
                            if m:
                                pct = min(50, max(10, int(float(m.group(1)) * 0.4 + 10)))
                                with VOD_TASKS_LOCK:
                                    if task_id not in VOD_TASKS:
                                        break
                                    task["progress"] = pct
                        proc_yt.wait()
                        with VOD_RUNNING_PROCS_LOCK:
                            VOD_RUNNING_PROCS.pop(task_id, None)

                        with VOD_TASKS_LOCK:
                            if task_id not in VOD_TASKS:
                                print(f"[VOD] Tarefa {task_id} cancelada/removida durante fallback, abortando.")
                                return

                    if proc_yt.returncode != 0:
                        raise RuntimeError(f"yt-dlp falhou com código {proc_yt.returncode}")

                # Transcode raw media with FFmpeg into 100% Samsung-compatible H.264 + AC3 stereo
                can_copy_video = False
                try:
                    probe_cmd = [
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,level,pix_fmt,width,height,sample_aspect_ratio",
                        "-of", "json", raw_file
                    ]
                    v_meta = json.loads(subprocess.check_output(probe_cmd, text=True))
                    st = (v_meta.get("streams") or [{}])[0]
                    codec = st.get("codec_name", "").lower()
                    pix = st.get("pix_fmt", "").lower()
                    lvl = int(st.get("level", 99) or 99)
                    w = int(st.get("width", 0) or 0)
                    h = int(st.get("height", 0) or 0)
                    sar = (st.get("sample_aspect_ratio") or "").strip()
                    is_compatible_sar = is_sar_samsung_compatible(sar)
                    if codec in ("h264", "avc1") and pix in ("yuv420p", "yuvj420p", "") and lvl <= 42 and w <= 1920 and h <= 1080 and is_compatible_sar:
                        can_copy_video = True
                except Exception:
                    can_copy_video = False

                if can_copy_video:
                    cmd = [
                        "ffmpeg", "-y", "-i", raw_file,
                        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
                        "-c:v", "copy",
                        "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
                        "-movflags", "+faststart",
                        "-f", "mp4",
                        out_file
                    ]
                    with VOD_TASKS_LOCK:
                        task["status_msg"] = "Stream copy direto 1080p sem perda (-c:v copy)..."
                    print(f"[VOD] Stream copy de vídeo direto + AC3 Samsung (modo ultra-rápido 90x) '{clean_title}'...")
                else:
                    cmd = [
                        "ffmpeg", "-y", "-i", raw_file,
                        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                        "-r", "30",
                        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.1",
                        "-b:v", "7000k", "-maxrate", "8000k", "-bufsize", "5000k", "-g", "30",
                        "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
                        "-movflags", "+faststart",
                        "-f", "mp4",
                        out_file
                    ]
                    with VOD_TASKS_LOCK:
                        task["status_msg"] = "Transcodificando para 1080p H.264 High Profile (7000k)..."
                    print(f"[VOD] Codificando para Samsung TV 1080p H.264/AC3 '{clean_title}'...")

                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, preexec_fn=_vod_subproc_setup)
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS[task_id] = proc
                time_pat = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
                spd_pat = re.compile(r"speed=\s*([\d\.]+)x")
                err_lines = []
                for line in proc.stderr:
                    err_lines.append(line.strip())
                    if len(err_lines) > 20:
                        err_lines.pop(0)
                    m = time_pat.search(line)
                    m_spd = spd_pat.search(line)
                    if m:
                        hrs, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
                        cur_secs = hrs * 3600 + mins * 60 + secs
                        if duration > 0:
                            pct = min(98, max(50, int(50 + (cur_secs / duration) * 48)))
                        else:
                            pct = min(95, 50 + int(cur_secs / 30))
                        with VOD_TASKS_LOCK:
                            if task_id not in VOD_TASKS:
                                break
                            task["progress"] = pct
                            if m_spd:
                                task["speed"] = f"{m_spd.group(1)}x"
                proc.wait()
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS.pop(task_id, None)
                try:
                    if os.path.exists(raw_file):
                        os.remove(raw_file)
                except Exception:
                    pass
                if proc.returncode != 0:
                    err_snippet = " ".join([l for l in err_lines if "error" in l.lower() or "failed" in l.lower()][-3:])
                    raise RuntimeError(f"FFmpeg falhou ({proc.returncode}): {err_snippet or 'erro na conversão'}")
            else:
                # Xtream / IPTV Movie / Series or Direct HTTP Stream
                resolved_url = resolve_vod_stream_url(url)
                
                with VOD_TASKS_LOCK:
                    task["status_msg"] = "Analisando rota do vídeo (ffprobe)..."
                    task["progress"] = 8

                # Auto-detecção inteligente de rota:
                # Testa se a CDN aceita conexão direta gigabit (ex: cdn33, donivan, workers.dev, etc.)
                # Só aciona o proxy residencial se a CDN bloquear data centers (ex: fontedecanais / 77zzhf54vdll71).
                is_known_blocked = any(kw in resolved_url.lower() for kw in ["fontedecanais", "77zzhf54vdll71"])
                use_proxy = is_known_blocked

                if not is_known_blocked:
                    # Tenta direto primeiro sem proxy (máxima velocidade gigabit 50x-100x)
                    meta_info = probe_vod_stream(resolved_url, use_proxy=False)
                    if not meta_info or not meta_info.get("streams"):
                        # Se direto falhou ou deu 403, ativa fallback pelo proxy residencial
                        print(f"[*] Acesso direto falhou para '{clean_title}', ativando túnel residencial...")
                        use_proxy = True
                        meta_info = probe_vod_stream(resolved_url, use_proxy=True)
                else:
                    meta_info = probe_vod_stream(resolved_url, use_proxy=True)

                fmt_info = meta_info.get("format", {})
                duration = float(fmt_info.get("duration", 0) or 0)

                can_copy_video = False
                streams = meta_info.get("streams", [])
                v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
                w, h = 0, 0
                if v_stream:
                    codec = v_stream.get("codec_name", "").lower()
                    pix = v_stream.get("pix_fmt", "").lower()
                    lvl = int(v_stream.get("level", 99) or 99)
                    w = int(v_stream.get("width", 0) or 0)
                    h = int(v_stream.get("height", 0) or 0)
                    sar = (v_stream.get("sample_aspect_ratio") or "").strip()
                    is_compatible_sar = is_sar_samsung_compatible(sar)
                    if codec in ("h264", "avc1") and pix in ("yuv420p", "yuvj420p", "") and lvl <= 42 and w <= 1920 and h <= 1080 and is_compatible_sar:
                        can_copy_video = True

                cmd = ["ffmpeg", "-y"]
                if use_proxy and RESIDENTIAL_HTTP_PROXY and is_proxy_alive(RESIDENTIAL_HTTP_PROXY):
                    cmd.extend(["-http_proxy", RESIDENTIAL_HTTP_PROXY])
                cmd.extend([
                    "-multiple_requests", "1",
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-recv_buffer_size", "1048576",
                    "-tcp_nodelay", "1",
                    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "-i", resolved_url
                ])

                route_desc = "túnel residencial" if use_proxy else "fibra direta gigabit"
                if can_copy_video:
                    with VOD_TASKS_LOCK:
                        task["status_msg"] = f"Copiando stream 1080p sem perda ({route_desc})..."
                        task["progress"] = 10
                    cmd.extend(["-c:v", "copy"])
                    print(f"[VOD] Stream copy direto 1080p sem perda ({route_desc}) '{clean_title}'...")
                else:
                    with VOD_TASKS_LOCK:
                        task["status_msg"] = "Transcodificando para H.264 Samsung TV..."
                        task["progress"] = 10
                    if (w > 1920 or h > 1080) or (w == 0 or h == 0):
                        scale_vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
                    else:
                        scale_vf = "scale='min(1920,iw)':-2"
                    cmd.extend([
                        "-vf", scale_vf,
                        "-r", "30",
                        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.1",
                        "-crf", "22", "-maxrate", "5000k", "-bufsize", "6000k", "-g", "30"
                    ])
                    print(f"[VOD] Transcodificando para Samsung TV H.264 '{clean_title}'...")

                cmd.extend([
                    "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-map_chapters", "-1",
                    "-c:a", "ac3", "-b:a", "384k", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart",
                    "-f", "mp4",
                    out_file
                ])

                print(f"[VOD] Processando stream '{clean_title}'...")
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, preexec_fn=_vod_subproc_setup)
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS[task_id] = proc

                time_pat = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
                spd_pat = re.compile(r"speed=\s*([\d\.]+)x")
                err_lines = []
                for line in proc.stderr:
                    err_lines.append(line.strip())
                    if len(err_lines) > 20:
                        err_lines.pop(0)
                    m = time_pat.search(line)
                    m_spd = spd_pat.search(line)
                    if m:
                        hrs, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
                        cur_secs = hrs * 3600 + mins * 60 + secs
                        if duration > 0:
                            pct = min(98, max(10, int((cur_secs / duration) * 100)))
                        else:
                            pct = min(95, 10 + int(cur_secs / 30))
                        with VOD_TASKS_LOCK:
                            if task_id not in VOD_TASKS:
                                break
                            task["progress"] = pct
                            if m_spd:
                                task["speed"] = f"{m_spd.group(1)}x"

                proc.wait()
                with VOD_RUNNING_PROCS_LOCK:
                    VOD_RUNNING_PROCS.pop(task_id, None)
                if proc.returncode != 0:
                    err_snippet = " ".join([l for l in err_lines if "error" in l.lower() or "failed" in l.lower()][-3:])
                    raise RuntimeError(f"FFmpeg falhou ({proc.returncode}): {err_snippet or 'erro na conversão'}")

            file_size = os.path.getsize(out_file)
            if file_size < 10000:
                raise RuntimeError("Arquivo gerado vazio ou corrompido")
            if duration > 300 and file_size < 1000000:
                raise RuntimeError("Vídeo rejeitado pela CDN (arquivo muito pequeno ou aviso de método não autorizado)")

            # Verificacao final por ffprobe: o meta passa a refletir o arquivo REAL
            # (nao a resolucao prometida). Sem isso, uma quieta degradacao de
            # qualidade (ex.: 360p silencioso do YouTube) so se descobre na TV.
            final_res = None
            final_codec = None
            try:
                fin = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,profile,width,height",
                    "-of", "json", out_file
                ], text=True, timeout=60))
                fst = (fin.get("streams") or [{}])[0]
                final_codec = fst.get("codec_name")
                fw, fh = int(fst.get("width", 0) or 0), int(fst.get("height", 0) or 0)
                if fw and fh:
                    final_res = f"{fw}x{fh}"
                    print(f"[VOD] Arquivo final verificado: {final_codec} {fst.get('profile')} {final_res} — '{clean_title}'")
                    if fh < 1080:
                        print(f"[VOD] ATENCAO: arquivo final tem apenas {fh}p (a origem limitou a qualidade)")
            except Exception as e:
                print(f"[VOD] ffprobe final falhou (nao-critico): {e}")

            fat_name = format_fat_media_name(clean_title)

            if gen_template:
                fat_size = min(file_size, 4294967000)
                gen_template.build_fat_template(file_name=fat_name, file_size=fat_size, out_path=out_tmpl)

            with VOD_TASKS_LOCK:
                if task_id in VOD_TASKS:
                    task["status"] = "ready"
                    task["progress"] = 100
                    task["status_msg"] = f"Pronto para assistir ({final_res})" if final_res else "Pronto para assistir"
                    task["final_resolution"] = final_res
                    task["final_codec"] = final_codec
                    task["file_path"] = out_file
                    task["template_path"] = out_tmpl
                    task["file_size"] = file_size
                    task["display_name"] = fat_name
                    task["poster"] = poster or task.get("poster", "")
                    task["speed"] = ""
                    task["updated_at"] = time.time()
                    save_vod_tasks()
            print(f"[✓] VOD pronto: {clean_title} ({file_size / (1024*1024):.1f} MB)")
        except Exception as e:
            print(f"[!] Erro no VOD {task_id}: {e}")
            with VOD_TASKS_LOCK:
                if task_id in VOD_TASKS:
                    task["status"] = "error"
                    task["status_msg"] = "Erro no processamento"
                    task["error"] = str(e)
                    task["updated_at"] = time.time()
                    save_vod_tasks()
        finally:
            with VOD_RUNNING_PROCS_LOCK:
                VOD_RUNNING_PROCS.pop(task_id, None)

def _prepare_vod_thread(task_id, url, title, poster=""):
    with VOD_RUNNING_THREADS_LOCK:
        if task_id in VOD_RUNNING_THREADS:
            print(f"[*] Tarefa VOD {task_id} já possui thread em execução, ignorando duplicata.")
            return
        VOD_RUNNING_THREADS.add(task_id)
    try:
        _prepare_vod_thread_inner(task_id, url, title, poster)
    finally:
        with VOD_RUNNING_THREADS_LOCK:
            VOD_RUNNING_THREADS.discard(task_id)

def resume_interrupted_vod_tasks():
    """Retoma automaticamente tarefas VOD que foram interrompidas por reinicialização."""
    with VOD_TASKS_LOCK:
        tasks = list(VOD_TASKS.values())
    for t in tasks:
        if t.get("status") in ("pending", "queued", "processing"):
            tid = t.get("id")
            url = t.get("url")
            title = t.get("title")
            poster = t.get("poster", "")
            if tid and url:
                with VOD_RUNNING_THREADS_LOCK:
                    is_running = tid in VOD_RUNNING_THREADS
                if not is_running:
                    print(f"[*] Auto-retomando tarefa VOD: {tid} ({title})")
                    threading.Thread(target=_prepare_vod_thread, args=(tid, url, title, poster), daemon=True).start()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suprime logs de status e logo polling para manter terminal limpo
        msg = format % args
        if "/api/status" in msg or "/api/logo" in msg:
            return
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {msg}\n")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-PIN, Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/api/tablet_cmd", "/api/tablet_ack", "/api/tablet_cmd_res"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if path == "/":
            self.send_dashboard()
        elif path == "/manifest.json":
            self.send_manifest_json()
        elif path == "/sw.js":
            self.send_service_worker()
        elif path in ("/api/app-icon.svg", "/icon.svg", "/favicon.ico"):
            self.send_app_icon_svg()
        elif path in ("/api/app-icon.png", "/apple-touch-icon.png", "/icon-192.png"):
            self.send_app_icon_png()
        elif path in ("/api/app-icon-512.png", "/icon-512.png"):
            self.send_app_icon_512_png()
        elif path in ("/api/screenshot-desktop.png", "/screenshot-desktop.png"):
            self.send_screenshot_desktop()
        elif path in ("/api/screenshot-mobile.png", "/screenshot-mobile.png"):
            self.send_screenshot_mobile()
        elif path in ("/live.ts", "/stream"):
            self.stream_live_ts()
        elif path in ("/player_api.php", "/xmltv.php"):
            self.proxy_player_api("GET")
        elif XTREAM_STREAM_RE.match(path):
            m = XTREAM_STREAM_RE.match(path)
            kind, user, pwd, stream_id, ext = m.groups()
            self.handle_xtream_stream(kind or "live", user, pwd, stream_id, ext)
        elif path.startswith("/vod/"):
            self.handle_vod_stream(path)
        elif path == "/api/vod/status":
            self.send_vod_status()
        elif path == "/api/status":
            self.send_status_json()
        elif path == "/api/channels":
            self.send_channels_json()
        elif path == "/api/switch_channel":
            self.handle_switch_get(query)
        elif path == "/api/groups":
            self.send_groups_json()
        elif path == "/api/favorites":
            self.send_favorites_json(FAV_MGR)
        elif path == "/api/favorites/series":
            self.send_favorites_json(SERIES_FAV_MGR)
        elif path == "/api/catalog/categories":
            self.send_catalog_categories(query.get("type", ["live"])[0])
        elif path == "/api/catalog/items":
            ctype = query.get("type", ["live"])[0]
            cat_id = query.get("cat", [""])[0]
            search_q = query.get("q", [""])[0]
            try:
                page = int(query.get("page", ["1"])[0] or 1)
            except Exception:
                page = 1
            try:
                limit = int(query.get("limit", ["48"])[0] or 48)
            except Exception:
                limit = 48
            self.send_catalog_items(ctype, cat_id, search_q, page, limit)
        elif path == "/api/catalog/series_info":
            sid = query.get("id", [""])[0]
            self.send_catalog_series_info(sid)
        elif path == "/remote.m3u":
            self.send_remote_m3u()
        elif path.startswith("/remote/"):
            self.handle_remote_play(path)
        elif path == "/api/tablet_cmd":
            token = query.get("k", [""])[0].strip()
            if TABLET_TOKEN and token != TABLET_TOKEN:
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return

            is_v2 = (query.get("v", ["1"])[0] == "2")
            nocmd = (query.get("nocmd", ["0"])[0] == "1")
            usb_st = query.get("usb", [""])[0]
            usb_pwr = query.get("pwr", [""])[0]
            tablet_vod = query.get("vod", [""])[0].strip()
            tablet_mode = query.get("mode", [""])[0].strip()

            HUB.tablet_last_seen = time.time()
            if usb_st:
                HUB.tablet_usb_state = usb_st
            if usb_pwr:
                HUB.tablet_usb_pwr = (usb_pwr == "1")
            if tablet_mode:
                HUB.tablet_mode = tablet_mode

            if tablet_vod and tablet_vod.lower() not in ("none", "-", "0", ""):
                HUB.tablet_vod_mode = tablet_vod
                touch_vod_stream(tablet_vod)
            else:
                HUB.tablet_vod_mode = ""
                if ACTIVE_VOD_TASK and (time.time() - LAST_VOD_STREAM_TIME > 15):
                    set_active_vod(None)

            cmd_str = HUB.bus.poll(nocmd=nocmd, is_v2=is_v2)
            resp_text = cmd_str if cmd_str else "none"
            HUB.tablet_pending_cmd = None

            resp_bytes = resp_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(resp_bytes)
        elif path == "/api/tablet_ack":
            self.handle_tablet_ack()
        elif path == "/api/logo":
            self.proxy_logo(query.get("url", [""])[0])
        elif path == "/api/proxy_stream":
            self.handle_proxy_stream(query)
        elif path == "/fuse_direct_arm_verified":
            self.send_fuse_bin()
        elif path.endswith(".sh") and not "/" in path[1:]:
            fpath = os.path.join(CONFIG_DIR, path.lstrip("/"))
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)
        elif path == "/fat_template.bin":
            fpath = os.path.join(CONFIG_DIR, "fat_template.bin")
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)
        else:
            self.send_error(404, "Not Found")

    def send_manifest_json(self):
        manifest = {
            "name": "Controle da TV — Filmes & TV",
            "short_name": "Controle TV",
            "description": "Controle Remoto de Filmes e TV para Idosos",
            "id": "/?source=pwa",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0b0f19",
            "theme_color": "#0b0f19",
            "lang": "pt-BR",
            "dir": "ltr",
            "categories": ["entertainment", "utilities"],
            "icons": [
                {
                    "src": "/api/app-icon.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any"
                },
                {
                    "src": "/api/app-icon.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "maskable"
                },
                {
                    "src": "/api/app-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any"
                },
                {
                    "src": "/api/app-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable"
                },
                {
                    "src": "/api/app-icon.svg",
                    "sizes": "192x192 512x512",
                    "type": "image/svg+xml",
                    "purpose": "any maskable"
                }
            ],
            "screenshots": [
                {
                    "src": "/api/screenshot-desktop.png",
                    "sizes": "1280x720",
                    "type": "image/png",
                    "form_factor": "wide",
                    "label": "Controle da TV no Computador"
                },
                {
                    "src": "/api/screenshot-mobile.png",
                    "sizes": "540x960",
                    "type": "image/png",
                    "form_factor": "narrow",
                    "label": "Controle da TV no Celular"
                }
            ],
            "shortcuts": [
                {
                    "name": "TV ao Vivo",
                    "short_name": "TV",
                    "description": "Assistir TV ao Vivo",
                    "url": "/",
                    "icons": [{"src": "/api/app-icon.png", "sizes": "192x192", "type": "image/png"}]
                },
                {
                    "name": "Filmes & Séries",
                    "short_name": "Catálogo",
                    "description": "Catálogo de Filmes e Séries",
                    "url": "/?tab=vod",
                    "icons": [{"src": "/api/app-icon.png", "sizes": "192x192", "type": "image/png"}]
                }
            ],
            "display_override": [
                "window-controls-overlay",
                "standalone",
                "minimal-ui"
            ],
            "launch_handler": {
                "client_mode": ["navigate-existing", "auto"]
            },
            "edge_side_panel": {
                "preferred_width": 400
            },
            "file_handlers": [
                {
                    "action": "/",
                    "accept": {
                        "video/mp4": [".mp4"],
                        "video/mp2t": [".ts"],
                        "audio/x-mpegurl": [".m3u", ".m3u8"]
                    }
                }
            ],
            "protocol_handlers": [
                {
                    "protocol": "web+tvstream",
                    "url": "/?play=%s"
                }
            ],
            "prefer_related_applications": False,
            "iarc_rating_id": "e84b072d-71b3-4d3e-86ae-31a8ce4e53b7",
            "share_target": {
                "action": "/",
                "method": "GET",
                "enctype": "application/x-www-form-urlencoded",
                "params": {
                    "title": "title",
                    "text": "text",
                    "url": "url"
                }
            }
        }
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def send_service_worker(self):
        sw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sw.js")
        if not os.path.exists(sw_path):
            sw_path = os.path.join(CONFIG_DIR, "sw.js")
        sw = None
        if os.path.exists(sw_path):
            try:
                with open(sw_path, "r", encoding="utf-8") as f:
                    sw = f.read()
            except Exception:
                pass

        if not sw:
            sw = r"""// Service Worker para PWA do Controle Remoto
const CACHE_NAME = 'controle-tv-v23';
const STATIC_ASSETS = [
    '/',
    '/manifest.json',
    '/api/app-icon.png',
    '/api/app-icon-512.png',
    '/api/screenshot-desktop.png',
    '/api/screenshot-mobile.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(STATIC_ASSETS).catch((err) => {
                console.warn('SW pre-cache error:', err);
            });
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.map((key) => {
                    if (key !== CACHE_NAME) {
                        return caches.delete(key);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    const url = new URL(event.request.url);

    // 1. Intercepta Compartilhamento do YouTube (Web Share Target) diretamente no Service Worker
    if ((url.pathname === '/' || url.pathname === '') && (url.searchParams.has('text') || url.searchParams.has('url') || url.searchParams.has('title'))) {
        const rawText = url.searchParams.get('text') || '';
        const rawUrl = url.searchParams.get('url') || '';
        const rawTitle = url.searchParams.get('title') || '';
        const combined = (rawUrl + ' ' + rawText + ' ' + rawTitle).trim();
        const match = combined.match(/https?:\/\/[^\s"'<>]+/);

        if (match && match[0]) {
            const targetUrl = match[0].replace(/[),;.]+$/, '');
            let videoTitle = rawTitle.trim();
            if (!videoTitle && rawText) {
                const before = rawText.replace(/https?:\/\/[^\s"'<>]+.*/, '').trim();
                if (before) videoTitle = before;
            }
            if (!videoTitle) videoTitle = 'Vídeo do YouTube';

            // Dispara download em segundo plano no servidor
            event.waitUntil(
                fetch('/api/vod/prepare', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': '1233' },
                    body: JSON.stringify({ url: targetUrl, title: videoTitle, pin: '1233' })
                }).then(r => r.json()).then(data => {
                    if (data && data.success) {
                        return self.registration.showNotification('📥 Salvando no Cinema da TV!', {
                            body: `"${videoTitle}" foi adicionado à fila do Cinema. Você será avisado quando terminar.`,
                            icon: '/api/app-icon.png',
                            badge: '/api/app-icon.png',
                            data: { url: '/?tab=vod' }
                        });
                    }
                }).catch(() => {})
            );
        }

        // Responde com o app normalmente
        event.respondWith(
            fetch(event.request).catch(() => caches.match('/'))
        );
        return;
    }

    if (url.pathname.startsWith('/live') || url.pathname.startsWith('/stream') || url.pathname.startsWith('/vod/') || url.pathname.startsWith('/api/status') || url.pathname.startsWith('/api/catalog') || url.pathname.startsWith('/api/vod/status') || url.pathname.startsWith('/api/favorites') || url.pathname.startsWith('/api/logo')) {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then((response) => {
                if (response && response.status === 200 && response.type === 'basic') {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then((c) => c.put(event.request, cloned));
                }
                return response;
            })
            .catch(() => caches.match(event.request).then((res) => res || caches.match('/')))
    );
});

// ==================== PUSH NOTIFICATIONS ====================
self.addEventListener('push', (event) => {
    let data = { title: '🍿 Controle da TV', body: 'Seu filme já está pronto para assistir na TV!', url: '/?tab=vod' };
    if (event.data) {
        try {
            data = Object.assign(data, event.data.json());
        } catch (e) {
            data.body = event.data.text();
        }
    }
    const options = {
        body: data.body,
        icon: '/api/app-icon.png',
        badge: '/api/app-icon.png',
        vibrate: [200, 100, 200],
        tag: data.tag || 'vod-ready-notification',
        renotify: true,
        data: { url: data.url || '/?tab=vod' }
    };
    event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || '/?tab=vod';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
            for (let client of windowClients) {
                if ('focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});

// Mensagens internas para exibição de notificação pelo Service Worker
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
        const payload = event.data.payload || {};
        self.registration.showNotification(payload.title || '🍿 Filme Pronto na TV!', {
            body: payload.body || 'Seu vídeo já está disponível para assistir!',
            icon: '/api/app-icon.png',
            badge: '/api/app-icon.png',
            vibrate: [200, 100, 200],
            tag: payload.tag || 'vod-status-update',
            renotify: true,
            data: { url: payload.url || '/?tab=vod' }
        });
    }
});
"""
        data = sw.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_app_icon_svg(self):
        svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="110" fill="#0b0f19"/>
  <rect x="24" y="24" width="464" height="464" rx="90" fill="#1e293b" stroke="#facc15" stroke-width="12"/>
  <rect x="96" y="80" width="320" height="200" rx="24" fill="#0b0f19" stroke="#38bdf8" stroke-width="8"/>
  <circle cx="256" cy="180" r="40" fill="#facc15"/>
  <polygon points="246,160 276,180 246,200" fill="#0b0f19"/>
  <rect x="110" y="320" width="80" height="50" rx="14" fill="#22c55e"/>
  <text x="150" y="354" font-family="sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">CH+</text>
  <rect x="216" y="320" width="80" height="50" rx="14" fill="#334155"/>
  <text x="256" y="354" font-family="sans-serif" font-size="22" font-weight="bold" fill="#facc15" text-anchor="middle">1 2 3</text>
  <rect x="322" y="320" width="80" height="50" rx="14" fill="#38bdf8"/>
  <text x="362" y="354" font-family="sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">VOL+</text>
  <rect x="110" y="390" width="80" height="50" rx="14" fill="#22c55e"/>
  <text x="150" y="424" font-family="sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">CH-</text>
  <rect x="216" y="390" width="80" height="50" rx="14" fill="#ef4444"/>
  <text x="256" y="424" font-family="sans-serif" font-size="20" font-weight="bold" fill="#ffffff" text-anchor="middle">MUDO</text>
  <rect x="322" y="390" width="80" height="50" rx="14" fill="#38bdf8"/>
  <text x="362" y="424" font-family="sans-serif" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle">VOL-</text>
</svg>"""
        data = svg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_app_icon_png(self):
        png_path = os.path.join(CONFIG_DIR, "app-icon.png")
        data = None
        if os.path.exists(png_path):
            try:
                with open(png_path, "rb") as f:
                    data = f.read()
            except Exception:
                pass
        if not data and PWA_ICON_192_B64:
            try:
                data = base64.b64decode(PWA_ICON_192_B64)
            except Exception:
                pass
        if not data:
            data = b""
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_app_icon_512_png(self):
        png_path = os.path.join(CONFIG_DIR, "app-icon-512.png")
        data = None
        if os.path.exists(png_path):
            try:
                with open(png_path, "rb") as f:
                    data = f.read()
            except Exception:
                pass
        if not data and PWA_ICON_512_B64:
            try:
                data = base64.b64decode(PWA_ICON_512_B64)
            except Exception:
                pass
        if not data:
            data = b""
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_screenshot_desktop(self):
        png_path = os.path.join(CONFIG_DIR, "screenshot-desktop.png")
        data = b""
        if os.path.exists(png_path):
            try:
                with open(png_path, "rb") as f:
                    data = f.read()
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_screenshot_mobile(self):
        png_path = os.path.join(CONFIG_DIR, "screenshot-mobile.png")
        data = b""
        if os.path.exists(png_path):
            try:
                with open(png_path, "rb") as f:
                    data = f.read()
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/switch":
            self.handle_switch()
        elif path == "/api/vod/prepare":
            self.handle_vod_prepare()
        elif path == "/api/vod/delete":
            self.handle_vod_delete()
        elif path == "/api/vod/play":
            self.handle_vod_play()
        elif path == "/api/vod/live":
            self.handle_vod_live()
        elif path == "/api/youtube":
            self.handle_youtube()
        elif path in ("/player_api.php", "/xmltv.php"):
            self.proxy_player_api("POST")
        elif path == "/api/sync":
            self.handle_sync()
        elif path == "/api/catalog/refresh":
            self.handle_catalog_refresh()
        elif path == "/api/favorites/toggle":
            self.handle_favorite_toggle(FAV_MGR)
        elif path == "/api/favorites/sync":
            self.handle_favorite_sync(FAV_MGR)
        elif path == "/api/favorites/series/toggle":
            self.handle_favorite_toggle(SERIES_FAV_MGR)
        elif path == "/api/favorites/series/sync":
            self.handle_favorite_sync(SERIES_FAV_MGR)
        elif path == "/api/telemetry":
            self.handle_telemetry()
        elif path == "/api/telemetry_result":
            self.handle_telemetry_result()
        elif path == "/api/exec":
            self.handle_remote_exec()
        elif path == "/api/tablet_cmd_res":
            self.handle_tablet_cmd_res()
        elif path == "/api/tablet_ack":
            self.handle_tablet_ack()
        elif path == "/api/tablet_exec":
            self.handle_tablet_exec()
        elif path == "/api/reset_epoch":
            self.handle_reset_epoch()
        elif path == "/api/usb/reconnect":
            self.handle_usb_reconnect()
        else:
            self.send_error(404, "Not Found")

    def proxy_logo(self, url):
        """Proxy seguro de imagens/logos/posters para evitar problemas de Mixed Content (HTTP em HTTPS)."""
        if not url:
            self.send_error(404)
            return

        with LOGO_CACHE_LOCK:
            if url in LOGO_CACHE:
                cached_data, ctype = LOGO_CACHE[url]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(cached_data)))
                self.end_headers()
                self.wfile.write(cached_data)
                return

        cache_key = hashlib.md5(url.encode("utf-8")).hexdigest()
        cache_file = os.path.join(POSTER_CACHE_DIR, cache_key)
        meta_file = cache_file + ".meta"

        if os.path.exists(cache_file) and os.path.exists(meta_file):
            try:
                with open(cache_file, "rb") as f:
                    data = f.read()
                with open(meta_file, "r", encoding="utf-8") as f:
                    ctype = f.read().strip() or "image/jpeg"
                with LOGO_CACHE_LOCK:
                    if len(LOGO_CACHE) < 2000:
                        LOGO_CACHE[url] = (data, ctype)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception:
                pass

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                with LOGO_CACHE_LOCK:
                    if len(LOGO_CACHE) < 2000:
                        LOGO_CACHE[url] = (data, ctype)
                try:
                    with open(cache_file, "wb") as f:
                        f.write(data)
                    with open(meta_file, "w", encoding="utf-8") as f:
                        f.write(ctype)
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception:
            self.send_error(404)

    def handle_proxy_stream(self, query):
        """Proxy seguro de fluxos de vídeo (MP4/TS/HLS) para reprodução direta no navegador desktop (Web Player)."""
        raw_url = query.get("url", [""])[0]
        if not raw_url:
            self.send_error(400, "Missing url parameter")
            return

        target_url = self.resolve_stream_url(raw_url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*"
        }
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        req = urllib.request.Request(target_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status_code = resp.status
                self.send_response(status_code)
                for h in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
                    val = resp.headers.get(h)
                    if val:
                        self.send_header(h, val)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
                self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.end_headers()

                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        break
        except Exception as e:
            try:
                self.send_error(502, f"Stream error: {e}")
            except Exception:
                pass

    def stream_live_ts(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = HUB.subscribe()
        try:
            while True:
                try:
                    chunk = q.get(timeout=5.0)
                    self.wfile.write(chunk)
                except queue.Empty:
                    continue
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    def send_status_json(self):
        client_data = None
        if HUB.client_telemetry:
            client_data = dict(HUB.client_telemetry)
            client_data["last_seen_secs"] = round(time.time() - HUB.telemetry_time, 1)

        tablet_online = (time.time() - getattr(HUB, "tablet_last_seen", 0) < 15) if getattr(HUB, "tablet_last_seen", 0) > 0 else False
        tv_on = False
        if getattr(HUB, "tablet_usb_pwr", False) or getattr(HUB, "tablet_usb_state", "") == "CONFIGURED":
            tv_on = True

        is_vod_active = False
        vod_title = None
        current_vod_id = ACTIVE_VOD_TASK or getattr(HUB, "tablet_vod_mode", "")
        if current_vod_id and (current_vod_id in VOD_TASKS or os.path.exists(os.path.join(VOD_DIR, f"{current_vod_id}.mp4"))):
            # VOD está ativo se o tablet reportou ou se houve streaming recente (últimos 60s) ou flag ativa
            if getattr(HUB, "tablet_vod_mode", "") or (time.time() - LAST_VOD_STREAM_TIME < 60) or os.path.exists(ACTIVE_VOD_FILE):
                is_vod_active = True
                v_task = VOD_TASKS.get(current_vod_id, {})
                raw_t = v_task.get("title") or v_task.get("display_name") or "Vídeo"
                vod_title = raw_t.replace("@", "").strip()

        data = {
            "tv_on": tv_on,
            "tv_usb_state": getattr(HUB, "tablet_usb_state", "DISCONNECTED"),
            "tablet_online": tablet_online,
            "active_channel_id": HUB.current_channel_id,
            "active_channel_name": HUB.current_channel_name,
            "active_url": HUB.current_url,
            "listeners": len(HUB.subscribers),
            "in_standby": False if (is_vod_active or HUB.youtube_meta) else HUB.in_standby,
            "total_mb": round(HUB.total_bytes / (1024 * 1024), 2),
            "uptime_secs": int(time.time() - HUB.start_time),
            "stream_health": HUB.stream_health,
            "stream_bitrate_kbps": round(HUB.get_current_bitrate_kbps(), 1),
            "slate_mode": HUB.slate_mode,
            "slate_active_secs": round(time.time() - HUB.slate_start_time, 1) if HUB.slate_mode else 0,
            "youtube": HUB.youtube_meta,
            "active_vod": current_vod_id if is_vod_active else None,
            "vod_display_name": vod_title if is_vod_active else None,
            "is_temporary": HUB.current_is_temporary,
            "is_eco": getattr(HUB, "current_is_eco", is_channel_eco(HUB.current_channel_name, HUB.current_url)),
            "client": client_data
        }
        res = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(res)

    def send_channels_json(self):
        channels = CH_MGR.get_all()
        res = json.dumps(channels, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(res)

    def send_favorites_json(self, mgr=None):
        if mgr is None:
            mgr = FAV_MGR
        favs = mgr.get_all()
        res = json.dumps({"favorites": favs}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_favorite_toggle(self, mgr=None):
        if mgr is None:
            mgr = FAV_MGR
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            params = json.loads(body) if body else {}
        except Exception:
            params = {}

        item_id = str(params.get("id") or params.get("channel_id") or "").strip()
        if not item_id:
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "ID ausente"}).encode("utf-8"))
            return

        added, favs = mgr.toggle(item_id)
        res = json.dumps({"success": True, "added": added, "id": item_id, "favorites": favs}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_favorite_sync(self, mgr=None):
        if mgr is None:
            mgr = FAV_MGR
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            params = json.loads(body) if body else {}
        except Exception:
            params = {}

        fav_list = params.get("favorites") or []
        favs = mgr.sync(fav_list)
        res = json.dumps({"success": True, "favorites": favs}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def send_groups_json(self):
        res = json.dumps(SMART_GROUPS, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(res)

    def send_catalog_categories(self, ctype):
        cats = CATALOG_MGR.get_categories(ctype)
        data = json.dumps(cats, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_catalog_items(self, ctype, cat_id, search_q, page, limit):
        res = CATALOG_MGR.get_items(ctype, category_id=cat_id, search=search_q, page=page, limit=limit)
        data = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_catalog_series_info(self, sid):
        res = CATALOG_MGR.get_series_info(sid)
        data = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        if "error" in res or not res.get("seasons"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        else:
            self.send_header("Cache-Control", "public, max-age=120")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_catalog_refresh(self):
        CATALOG_MGR.sync_all_async()
        data = json.dumps({"success": True, "message": "Sincronização iniciada em segundo plano"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_remote_m3u(self):
        channels = CH_MGR.get_all()
        host = self.headers.get("Host", "127.0.0.1:8080")
        lines = ["#EXTM3U"]
        
        for cid, ch in channels.items():
            name = ch.get("name", cid)
            group = ch.get("category", "General")
            logo = ch.get("logo", "")
            if not logo.startswith("http"):
                logo = ""
            
            line1 = f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}'
            line2 = f'http://{host}/remote/{cid}.ts'
            lines.append(line1)
            lines.append(line2)
            
        res = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(res)

    def handle_remote_play(self, path):
        basename = path.split("/")[-1]
        cid = basename.replace(".ts", "")
        
        channels = CH_MGR.get_all()
        if cid not in channels:
            self.send_error(404, "Channel not found")
            return
            
        HUB.switch_channel(cid)
        print(f"[REMOTE] Triggered switch to {cid} via M3U. Serving live stream.")
            
        # Retorna o fluxo real de vídeo em vez da tela preta!
        self.stream_live_ts()

    def proxy_player_api(self, method="GET"):
        parsed = urllib.parse.urlparse(self.path)
        query_str = parsed.query
        now = time.time()

        body = None
        headers = {
            "User-Agent": self.headers.get("User-Agent", "IPTVSmarters/3.1.1"),
            "Accept": "*/*",
        }

        if method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)
                headers["Content-Type"] = self.headers.get("Content-Type", "application/x-www-form-urlencoded")

        # Determina action da query ou do body
        query = urllib.parse.parse_qs(query_str)
        action = query.get("action", [""])[0]
        if not action and body:
            try:
                post_data = urllib.parse.parse_qs(body.decode("utf-8", "ignore"))
                action = post_data.get("action", [""])[0]
            except Exception:
                pass

        is_list_call = any(k in action for k in ["get_live", "get_vod", "get_series"]) or any(k in query_str for k in ["get_live", "get_vod", "get_series"])
        cache_key = f"{parsed.path}:{query_str}:{body.decode('utf-8', 'ignore') if body else ''}"

        # Cache para chamadas pesadas de lista (live streams / categories / series / vod) por 600s
        if is_list_call:
            with XTREAM_CACHE_LOCK:
                if cache_key in XTREAM_CACHE:
                    c_data, c_ctype, c_exp = XTREAM_CACHE[cache_key]
                    if now < c_exp:
                        self.send_response(200)
                        self.send_header("Content-Type", c_ctype)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(c_data)))
                        self.end_headers()
                        self.wfile.write(c_data)
                        return

        upstream = (XTREAM_UPSTREAM or os.environ.get("XTREAM_UPSTREAM", "") or "http://studut.shop:80").rstrip("/")
        upstream_url = f"{upstream}{parsed.path}"
        if query_str:
            upstream_url += f"?{query_str}"

        data = None
        ctype = "application/json"
        last_err = None

        # Tenta com retry automático e timeout alargado (45s para listas pesadas de 16MB)
        for attempt in range(2):
            try:
                req = urllib.request.Request(upstream_url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type", "application/json")
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"[!] Tentativa {attempt + 1} falhou no proxy Xtream ({e})...")
                time.sleep(1)

        if data is None:
            print(f"[!] Erro definitivo no proxy Xtream: {last_err}")
            self.send_error(502, f"Upstream error: {last_err}")
            return

        try:
            # Se for login/autenticação (sem action), reescreve a URL do server_info para a nossa VPS!
            if not action and (b"server_info" in data):
                try:
                    js = json.loads(data.decode("utf-8", "ignore"))
                    if "server_info" in js:
                        host_header = self.headers.get("Host", "tv.smre.run.place")
                        host_name = host_header.split(":")[0]
                        proto = self.headers.get("X-Forwarded-Proto") or ("https" if "443" in host_header else "http")
                        port = "443" if proto == "https" else "80"

                        js["server_info"]["url"] = host_name
                        js["server_info"]["port"] = port
                        js["server_info"]["server_protocol"] = proto
                        js["server_info"]["https_port"] = "443"
                        data = json.dumps(js).encode("utf-8")
                except Exception as ex:
                    print(f"[!] Erro ao reescrever server_info: {ex}")
            elif is_list_call:
                with XTREAM_CACHE_LOCK:
                    if len(XTREAM_CACHE) > 50:
                        XTREAM_CACHE.clear()
                    XTREAM_CACHE[cache_key] = (data, ctype, now + 600.0)

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"[!] Erro ao responder proxy Xtream: {e}")

    def resolve_stream_url(self, url):
        return resolve_vod_stream_url(url)


    def handle_xtream_stream(self, kind, user, pwd, stream_id, ext):
        if kind == "movie":
            target_url = resolve_channel_url(f"/movie/{user}/{pwd}/{stream_id}.{ext or 'mp4'}")
            cname = f"Filme {stream_id}"
        elif kind == "series":
            target_url = resolve_channel_url(f"/series/{user}/{pwd}/{stream_id}.{ext or 'mp4'}")
            cname = f"Série {stream_id}"
        else:
            # Padrão: canal ao vivo
            target_url = resolve_channel_url(f"/live/{user}/{pwd}/{stream_id}.ts")
            cname = f"Canal {stream_id}"

        target_url = self.resolve_stream_url(target_url)

        print(f"\n[⚡ XTREAM] App selecionou: {cname} -> {target_url}")
        HUB.switch_channel(custom_url=target_url, custom_name=cname)

        # Transmite ao vivo para o player do app simultaneamente
        self.stream_live_ts()

    def handle_switch(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            params = json.loads(body)
        except Exception:
            params = urllib.parse.parse_qs(body)
            params = {k: v[0] for k, v in params.items()}

        # Validação do PIN de Segurança (1233)
        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8"))
            return

        ch_id = params.get("channel_id")
        custom_url = params.get("url")
        custom_name = params.get("name")
        req_is_eco = params.get("is_eco")
        no_reconnect = bool(params.get("no_reconnect") or params.get("no_usb_reconnect"))

        global ACTIVE_VOD_TASK
        if ACTIVE_VOD_TASK or getattr(HUB, "tablet_vod_mode", ""):
            set_active_vod(None)
            HUB.tablet_vod_mode = ""
            dispatch_device_cmd("sh /data/local/tmp/switch_live.sh")

        if custom_url and any(x in custom_url.lower() for x in ["youtube.com", "youtu.be"]):
            try:
                meta = resolve_youtube(custom_url)
                ok = HUB.switch_youtube(meta)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": ok,
                    "channel": f"YouTube: {meta['title']}",
                    "title": meta["title"],
                    "duration": meta.get("duration"),
                    "thumbnail": meta.get("thumbnail")
                }).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": f"Erro YouTube: {str(e)}"}).encode("utf-8"))
                return

        if custom_url and not any(x in custom_url.lower() for x in ["youtube.com", "youtu.be"]):
            custom_url = resolve_channel_url(custom_url)
            if any(x in custom_url.lower() for x in ["/movie/", "/series/"]):
                custom_url = self.resolve_stream_url(custom_url)

        ok = HUB.switch_channel(channel_id=ch_id, custom_url=custom_url, custom_name=custom_name, is_eco=req_is_eco, no_reconnect=no_reconnect)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"success": ok, "channel": HUB.current_channel_name}).encode("utf-8"))

    def handle_switch_get(self, query):
        req_pin = self.headers.get("X-Auth-PIN") or query.get("pin", [""])[0]
        if AUTH_PIN and req_pin != AUTH_PIN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8"))
            return

        ch_id = query.get("id", [""])[0] or query.get("channel_id", [""])[0]
        custom_url = query.get("url", [""])[0]
        custom_name = query.get("name", [""])[0]
        no_reconnect_val = query.get("no_reconnect", ["0"])[0] or query.get("no_usb_reconnect", ["0"])[0]
        no_reconnect = no_reconnect_val in ["1", "true", "True", "yes"]

        if custom_url:
            custom_url = resolve_channel_url(custom_url)
            if any(x in custom_url.lower() for x in ["/movie/", "/series/"]):
                custom_url = self.resolve_stream_url(custom_url)

        ok = HUB.switch_channel(channel_id=ch_id, custom_url=custom_url or None, custom_name=custom_name or None, no_reconnect=no_reconnect)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"success": ok, "channel": HUB.current_channel_name}).encode("utf-8"))

    def handle_youtube(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            params = json.loads(body)
        except Exception:
            params = urllib.parse.parse_qs(body)
            params = {k: v[0] for k, v in params.items()}

        # Validação do PIN de Segurança (1233)
        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8"))
            return

        yt_url = (params.get("url") or "").strip()
        if not yt_url:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "URL não fornecida"}).encode("utf-8"))
            return

        if yt_url.lower().startswith("magnet:?") or yt_url.lower().endswith(".torrent"):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": False,
                "error": "Torrents e Magnet Links precisam ser baixados primeiro para a TV. Use o botão verde 'Salvar no Cinema' para iniciar o download e processamento das legendas."
            }).encode("utf-8"))
            return

        try:
            meta = resolve_youtube(yt_url)
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        if ACTIVE_VOD_TASK or getattr(HUB, "tablet_vod_mode", ""):
            set_active_vod(None)
            HUB.tablet_vod_mode = ""
            dispatch_device_cmd("sh /data/local/tmp/switch_live.sh")

        ok = HUB.switch_youtube(meta)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({
            "success": ok,
            "channel": f"YouTube: {meta['title']}",
            "title": meta["title"],
            "duration": meta.get("duration"),
            "thumbnail": meta.get("thumbnail")
        }).encode("utf-8"))

    def handle_sync(self):
        """Executa sincronização dos canais e recarga de tarefas VOD em segundo plano."""
        def do_sync():
            try:
                load_vod_tasks()
                import sync_iptv
                sync_iptv.sync()
                CH_MGR.reload()
            except Exception as e:
                print(f"[!] Erro no sync: {e}")
        threading.Thread(target=do_sync, daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "Sincronização iniciada."}).encode("utf-8"))

    def handle_telemetry(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(body)
            HUB.client_telemetry = data
            HUB.telemetry_time = time.time()
            cmd = HUB.pending_command
            HUB.pending_command = None
            res = json.dumps({"success": True, "cmd": cmd}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
        except Exception as e:
            res = json.dumps({"success": False, "error": str(e)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

    def handle_telemetry_result(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(body)
            HUB.command_output = data.get("output")
            HUB.command_done_event.set()
            res = json.dumps({"success": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
        except Exception as e:
            res = json.dumps({"success": False, "error": str(e)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

    def handle_remote_exec(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            params = json.loads(body)
        except Exception:
            params = {}
        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        cmd = params.get("cmd")
        if not cmd:
            res = json.dumps({"success": False, "error": "Comando ausente"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        HUB.command_done_event.clear()
        HUB.command_output = None
        HUB.pending_command = cmd
        HUB.command_done_event.wait(timeout=10.0)
        res = json.dumps({"success": True, "output": HUB.command_output or "Timeout aguardando aparelho (está online?)."}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_tablet_ack(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        token = query.get("k", [""])[0].strip()
        if TABLET_TOKEN and token != TABLET_TOKEN:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Forbidden"}).encode("utf-8"))
            return

        cmd_id_str = query.get("id", ["0"])[0]
        try:
            cmd_id = int(cmd_id_str)
        except Exception:
            cmd_id = 0

        rc_str = query.get("rc", ["0"])[0]
        try:
            rc = int(rc_str)
        except Exception:
            rc = 0

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""

        acked = HUB.bus.ack(cmd_id, rc=rc, output=body)
        res = json.dumps({"success": True, "acked": acked, "id": cmd_id}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_tablet_cmd_res(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        HUB.tablet_cmd_res = body
        HUB.tablet_cmd_event.set()
        HUB.bus.ack_legacy(output=body)
        res = json.dumps({"success": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_tablet_exec(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = {}

        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        cmd = params.get("cmd")
        if not cmd:
            res = json.dumps({"success": False, "error": "Comando ausente"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        HUB.tablet_cmd_event.clear()
        HUB.tablet_cmd_res = None
        item = dispatch_device_cmd(cmd)
        success = item.event.wait(timeout=15.0) if hasattr(item, "event") else False
        output = item.output if success else (HUB.tablet_cmd_res or "Timeout aguardando execução no tablet (watchdog ativo?).")
        rc = getattr(item, "rc", 0)
        res = json.dumps({"success": True, "output": output, "rc": rc}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_reset_epoch(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = {}
        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        HUB.reset_pts_epoch()
        res = json.dumps({"success": True, "message": "PTS epoch reset to 3.0s"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_usb_reconnect(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = {}
        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        global LAST_RECONNECT_TIME
        now = time.time()
        if now - LAST_RECONNECT_TIME < 3.5:
            res = json.dumps({
                "success": True,
                "message": "Comando de reconexão já em andamento (debounce)."
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        LAST_RECONNECT_TIME = now

        cmd = "sh /system/xbin/reconnect_usb.sh || sh /data/local/tmp/reconnect_usb.sh"
        dispatch_device_cmd(cmd)

        res = json.dumps({
            "success": True,
            "message": "Comando de reconexão USB enviado para o aparelho."
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def send_fuse_bin(self, fname="fuse_direct_arm_verified"):
        fpath = os.path.join(CONFIG_DIR, fname)
        if not os.path.exists(fpath):
            self.send_error(404, "Binary not found")
            return
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_vod_prepare(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = urllib.parse.parse_qs(body)
            params = {k: v[0] for k, v in params.items()}

        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        url = (params.get("url") or "").strip()
        title = (params.get("title") or "").strip()
        poster = (params.get("poster") or "").strip()
        if not url:
            res = json.dumps({"success": False, "error": "URL não fornecida"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        if "/live/" in url.lower():
            res = json.dumps({
                "success": False,
                "error": "Transmissões ao vivo não podem ser salvas no Cinema. Use 'Assistir na TV' para ver ao vivo."
            }).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        task_id = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        with VOD_TASKS_LOCK:
            existing = VOD_TASKS.get(task_id)
            if existing and existing.get("status") == "ready" and os.path.exists(existing.get("file_path", "")):
                if poster and not existing.get("poster"):
                    existing["poster"] = poster
                    save_vod_tasks()
                res = json.dumps({
                    "success": True,
                    "task_id": task_id,
                    "status": "ready",
                    "display_name": existing.get("display_name", "")
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(res)))
                self.end_headers()
                self.wfile.write(res)
                return

            if existing and existing.get("status") in ("processing", "queued", "pending"):
                res = json.dumps({
                    "success": True,
                    "task_id": task_id,
                    "status": existing.get("status"),
                    "display_name": existing.get("display_name", "")
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(res)))
                self.end_headers()
                self.wfile.write(res)
                return

            if not title and (url.lower().startswith("magnet:?") or url.lower().endswith(".torrent")):
                try:
                    from torrent_downloader import extract_torrent_title
                    title = extract_torrent_title(url)
                except Exception:
                    pass

            VOD_TASKS[task_id] = {
                "id": task_id,
                "url": url,
                "title": title or "Vídeo VOD",
                "display_name": "",
                "poster": poster,
                "series_id": str(params.get("series_id")) if params.get("series_id") else None,
                "status": "queued",
                "progress": 0,
                "status_msg": "Na fila...",
                "speed": "",
                "error": None,
                "created_at": time.time(),
                "updated_at": time.time()
            }
            save_vod_tasks()

        with VOD_RUNNING_THREADS_LOCK:
            already_running = task_id in VOD_RUNNING_THREADS
        if not already_running:
            threading.Thread(target=_prepare_vod_thread, args=(task_id, url, title, poster), daemon=True).start()

        res = json.dumps({"success": True, "task_id": task_id, "status": "queued"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_vod_delete(self):
        global ACTIVE_VOD_TASK
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = urllib.parse.parse_qs(body)
            params = {k: v[0] for k, v in params.items()}

        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        task_id = params.get("task_id")
        if not task_id:
            res = json.dumps({"success": False, "error": "ID da tarefa não informado"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        with VOD_TASKS_LOCK:
            task = VOD_TASKS.get(task_id)
            if not task:
                res = json.dumps({"success": False, "error": "Tarefa não encontrada"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(res)))
                self.end_headers()
                self.wfile.write(res)
                return

            # Aborta processo ffmpeg / yt-dlp ativo e todo o grupo de processos filhos
            with VOD_RUNNING_PROCS_LOCK:
                proc = VOD_RUNNING_PROCS.pop(task_id, None)
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass

            if ACTIVE_VOD_TASK == task_id or getattr(HUB, "tablet_vod_mode", "") == task_id:
                set_active_vod(None)
                HUB.tablet_vod_mode = ""
                dispatch_device_cmd("sh /data/local/tmp/switch_live.sh")

            f_mp4 = os.path.join(VOD_DIR, f"{task_id}.mp4")
            f_bin = os.path.join(VOD_DIR, f"{task_id}.bin")
            f_raw = os.path.join(VOD_DIR, f"{task_id}_raw.mkv")
            for f in [f_mp4, f_bin, f_raw]:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except Exception as ex:
                    print(f"[!] Erro ao remover {f}: {ex}")

            del VOD_TASKS[task_id]
            save_vod_tasks()

        res = json.dumps({"success": True, "task_id": task_id}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def send_vod_status(self):
        global ACTIVE_VOD_TASK
        with VOD_TASKS_LOCK:
            tasks_list = []
            for t in VOD_TASKS.values():
                t_dict = dict(t)
                tid = t.get("id")
                dest = os.path.join(VOD_DIR, f"{tid}.ts")
                if os.path.exists(dest):
                    try:
                        sz = os.path.getsize(dest)
                        t_dict["file_size_bytes"] = sz
                        if sz >= 1024**3:
                            t_dict["file_size_str"] = f"{sz / (1024**3):.1f} GB"
                        elif sz >= 1024**2:
                            t_dict["file_size_str"] = f"{sz / (1024**2):.1f} MB"
                        else:
                            t_dict["file_size_str"] = f"{sz / 1024:.0f} KB"
                    except Exception:
                        pass
                tasks_list.append(t_dict)

        # Coloca os VODs mais recentes no topo da pilha (ordem decrescente de criação/atualização)
        tasks_list.reverse()
        tasks_list.sort(key=lambda t: t.get("created_at") or t.get("updated_at") or 0, reverse=True)

        disk_info = {"total_gb": 0, "free_gb": 0, "used_gb": 0}
        try:
            total, used, free = shutil.disk_usage(VOD_DIR)
            disk_info = {
                "total_gb": round(total / (1024**3), 1),
                "free_gb": round(free / (1024**3), 1),
                "used_gb": round(used / (1024**3), 1)
            }
        except Exception:
            pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({
            "active_vod": ACTIVE_VOD_TASK,
            "tasks": tasks_list,
            "disk": disk_info
        }).encode("utf-8"))

    def handle_vod_play(self):
        global ACTIVE_VOD_TASK
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = urllib.parse.parse_qs(body)
            params = {k: v[0] for k, v in params.items()}

        req_pin = self.headers.get("X-Auth-PIN") or params.get("pin")
        if AUTH_PIN and req_pin != AUTH_PIN:
            res = json.dumps({"success": False, "error": "PIN incorreto"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        task_id = params.get("task_id")
        with VOD_TASKS_LOCK:
            task = VOD_TASKS.get(task_id)
        if not task or task.get("status") != "ready":
            res = json.dumps({"success": False, "error": "VOD não está pronto"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return

        set_active_vod(task_id)
        HUB.tablet_vod_mode = task_id
        dispatch_device_cmd(f"sh /data/local/tmp/switch_vod.sh {task_id}")
        log_event(f"VOD_PLAY {task_id} ({task.get('display_name')})")

        res = json.dumps({"success": True, "task_id": task_id, "display_name": task.get("display_name")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def handle_vod_live(self):
        global ACTIVE_VOD_TASK
        if ACTIVE_VOD_TASK is None and not getattr(HUB, "tablet_vod_mode", ""):
            res = json.dumps({"success": True, "status": "already_live"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        set_active_vod(None)
        HUB.tablet_vod_mode = ""
        dispatch_device_cmd("sh /data/local/tmp/switch_live.sh")
        log_event("VOD_RETURN_LIVE")

        res = json.dumps({"success": True, "status": "live"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def stream_mp4_file(self, file_path):
        if not file_path or not os.path.exists(file_path):
            self.send_error(404, "Movie file not found")
            return
        file_size = os.path.getsize(file_path)
        range_header = self.headers.get("Range")

        if range_header:
            m_suffix = re.match(r"bytes=-(\d+)", range_header.strip())
            m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            start = None
            end = None
            if m_suffix:
                length = int(m_suffix.group(1))
                if file_size == 0 or length <= 0:
                    self.send_response(416, "Range Not Satisfiable")
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = max(0, file_size - length)
                end = file_size - 1
            elif m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                if end >= file_size:
                    end = file_size - 1

            if start is not None and end is not None:
                if start > end or start >= file_size:
                    self.send_response(416, "Range Not Satisfiable")
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content_len = end - start + 1
                self.send_response(206, "Partial Content")
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(content_len))
                self.send_header("Connection", "keep-alive")
                self.send_header("Keep-Alive", "timeout=60, max=10000")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(file_path, "rb") as f:
                    f.seek(start)
                    rem = content_len
                    while rem > 0:
                        chunk = f.read(min(131072, rem))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        rem -= len(chunk)
                return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(131072)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def handle_vod_stream(self, path):
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) < 3:
            self.send_error(404, "Invalid VOD path")
            return
        task_id = parts[1]
        filename = parts[2]

        touch_vod_stream(task_id)

        with VOD_TASKS_LOCK:
            task = VOD_TASKS.get(task_id)

        if not task:
            f_mp4 = os.path.join(VOD_DIR, f"{task_id}.mp4")
            f_bin = os.path.join(VOD_DIR, f"{task_id}.bin")
            if os.path.exists(f_mp4):
                task = {
                    "id": task_id,
                    "file_path": f_mp4,
                    "template_path": f_bin,
                    "status": "ready"
                }
            else:
                self.send_error(404, "VOD task not found")
                return

        if filename == "template.bin":
            playlist = get_series_playlist(task_id)
            if len(playlist) > 1 and gen_template:
                try:
                    files_spec = []
                    for p in playlist:
                        fn = p.get("display_name") or format_fat_media_name(p.get("title") or "")
                        sz = p.get("file_size") or (os.path.getsize(p["file_path"]) if os.path.exists(p.get("file_path", "")) else 1000000)
                        files_spec.append((fn, sz))
                    data = gen_template.build_fat_template_multi(files_spec)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception as e:
                    print(f"[!] Erro ao gerar template multi: {e}")

            tmpl_path = task.get("template_path")
            if not tmpl_path or not os.path.exists(tmpl_path):
                self.send_error(404, "Template not found")
                return
            with open(tmpl_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if filename.startswith("file_") and filename.endswith(".mp4"):
            m_idx = re.match(r"^file_(\d+)\.mp4$", filename)
            if m_idx:
                file_idx = int(m_idx.group(1))
                playlist = get_series_playlist(task_id)
                if file_idx < len(playlist):
                    target_task = playlist[file_idx]
                    target_path = target_task.get("file_path")
                    if target_path and os.path.exists(target_path):
                        self.stream_mp4_file(target_path)
                        return
            self.send_error(404, "Episode not found")
            return

        if filename in ("movie.mp4", "movie.ts"):
            file_path = task.get("file_path")
            if not file_path or not os.path.exists(file_path):
                self.send_error(404, "Movie file not found")
                return
            self.stream_mp4_file(file_path)
            return

        self.send_error(404, "Not Found")

    def send_dashboard(self):
        tunnel_url = "http://localhost:8080"
        tunnel_file = os.path.join(CONFIG_DIR, "tunnel_url.txt")
        if os.path.exists(tunnel_file):
            try:
                with open(tunnel_file, "r") as f:
                    tunnel_url = f.read().strip()
            except Exception:
                pass

        dash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        if not os.path.exists(dash_path):
            dash_path = os.path.join(CONFIG_DIR, "dashboard.html")

        html = None
        if os.path.exists(dash_path):
            try:
                with open(dash_path, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as e:
                print(f"[!] Erro ao ler {dash_path}: {e}")

        if not html:
            html = EMBEDDED_DASHBOARD_HTML

        html = html.replace("{tunnel_url}", tunnel_url)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


EMBEDDED_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Controle da TV — Filmes & TV</title>
    
    <!-- PWA & Mobile Web App Meta Tags -->
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Controle TV">
    <meta name="theme-color" content="#0b0f19">
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" type="image/svg+xml" href="/api/app-icon.svg">
    <link rel="apple-touch-icon" href="/api/app-icon.svg">

    <!-- Players para Reprodução Direta no Navegador Desktop (Web Player) -->
    <script src="https://cdn.jsdelivr.net/npm/mpegts.js@latest/dist/mpegts.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>

    <style>
        :root {
            /* Paleta de Alto Contraste WCAG 2.2 AAA Sênior */
            --bg: #0b0f19;
            --surface: #1e293b;
            --surface-hover: #334155;
            --surface-active: #14532d;
            --border: #334155;
            --border-active: #22c55e;
            
            --text-main: #ffffff;
            --text-muted: #cbd5e1;
            --text-dim: #94a3b8;
            --gold: #facc15;
            --green: #22c55e;
            --red: #fca5a5;
            --red-bg: #450a0a;
            --blue: #38bdf8;

            /* Ergonomia de Alvos de Toque Sênior */
            --btn-radius: 16px;
            --font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        }

        * {
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: var(--font-family);
            background-color: var(--bg);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            padding-bottom: max(110px, calc(85px + env(safe-area-inset-bottom)));
            user-select: none;
            -webkit-user-select: none;
        }

        .app-container {
            width: 100%;
            max-width: 640px;
            padding: max(16px, env(safe-area-inset-top)) 16px 24px 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        /* Banner de Instalação PWA */
        .pwa-banner {
            display: none;
            background: linear-gradient(135deg, #1e3a8a 0%, #1e293b 100%);
            border: 3px solid var(--blue);
            border-radius: var(--btn-radius);
            padding: 16px 18px;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            box-shadow: 0 8px 24px rgba(56, 189, 248, 0.25);
        }
        .pwa-banner-text {
            font-size: 18px;
            font-weight: 700;
            color: #ffffff;
            line-height: 1.3;
        }
        .pwa-install-btn {
            background: var(--gold);
            color: #000000;
            border: none;
            border-radius: 12px;
            padding: 12px 20px;
            font-size: 18px;
            font-weight: 900;
            cursor: pointer;
            white-space: nowrap;
        }

        /* 1. TOPO: Central Universal da TV (Status, O que está no ar & Acesso aos Salvos) */
        header.top-tv-banner {
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 3px solid var(--gold);
            border-radius: var(--btn-radius);
            padding: 16px 18px;
            position: relative;
            box-shadow: 0 10px 28px rgba(250, 204, 21, 0.16);
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }
        header.top-tv-banner:hover {
            border-color: var(--gold);
            box-shadow: 0 8px 32px rgba(250, 204, 21, 0.26);
        }
        header.top-tv-banner:active {
            transform: scale(0.99);
            border-color: var(--gold);
        }
        header.top-tv-banner:focus-visible {
            outline: 3px solid var(--gold);
            outline-offset: 3px;
        }

        .top-tv-status-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            flex-wrap: wrap;
        }

        .top-tv-content {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .tv-status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 16px;
            font-weight: 800;
            padding: 7px 14px;
            border-radius: 30px;
            width: fit-content;
            background: rgba(34, 197, 94, 0.15);
            color: var(--green);
            border: 2px solid rgba(34, 197, 94, 0.4);
            letter-spacing: 0.3px;
        }
        .tv-status-badge.waiting {
            background: rgba(250, 204, 21, 0.15);
            color: var(--gold);
            border-color: rgba(250, 204, 21, 0.4);
        }
        .tv-status-badge.offline {
            background: rgba(239, 68, 68, 0.2);
            color: var(--red);
            border-color: #ef4444;
        }

        /* Badge Dinâmica de Mídia/Downloads no Header */
        .header-media-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 15px;
            font-weight: 800;
            padding: 7px 14px;
            border-radius: 30px;
            background: rgba(56, 189, 248, 0.12);
            color: #38bdf8;
            border: 2px solid rgba(56, 189, 248, 0.35);
            letter-spacing: 0.2px;
            transition: all 0.2s ease;
        }
        .header-media-badge.has-ready {
            background: rgba(250, 204, 21, 0.14);
            color: var(--gold);
            border-color: rgba(250, 204, 21, 0.45);
        }
        .header-media-badge.downloading {
            background: rgba(234, 179, 8, 0.2);
            color: var(--gold);
            border-color: var(--gold);
            box-shadow: 0 0 12px rgba(250, 204, 21, 0.4);
            animation: pulse-badge 1.6s infinite ease-in-out;
        }
        @keyframes pulse-badge {
            0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.4); }
            50% { transform: scale(1.02); box-shadow: 0 0 0 6px rgba(250, 204, 21, 0); }
            100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(250, 204, 21, 0); }
        }

        /* SELETOR PRINCIPAL DE MODO: CINEMA VOD vs TV AO VIVO */
        .main-mode-toggle {
            display: flex;
            gap: 12px;
            width: 100%;
        }

        .btn-mode-tab {
            flex: 1;
            min-height: 56px;
            background: var(--surface);
            border: 3px solid var(--border);
            border-radius: var(--btn-radius);
            color: var(--text-muted);
            font-family: var(--font-family);
            font-size: clamp(16px, 3.8vw, 19px);
            padding: 0 4px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .btn-mode-tab:active {
            transform: scale(0.96);
        }
        .btn-mode-tab.active {
            background: #14532d;
            border-color: var(--gold);
            color: #ffffff;
            box-shadow: 0 0 20px rgba(250, 204, 21, 0.3);
        }
        .btn-mode-tab:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }

        /* Cartão "No Ar Agora na TV" / "Filme em Reprodução" */
        .now-playing-card {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 3px solid var(--gold);
            border-radius: var(--btn-radius);
            padding: 18px 20px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            box-shadow: 0 10px 25px rgba(250, 204, 21, 0.15);
        }

        .now-playing-label {
            font-size: 18px;
            font-weight: 800;
            color: var(--gold);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .pulsing-dot {
            width: 12px;
            height: 12px;
            background-color: var(--green);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 10px var(--green);
            animation: pulse-dot 1.5s infinite;
        }
        @keyframes pulse-dot {
            0% { transform: scale(0.95); opacity: 0.7; }
            50% { transform: scale(1.3); opacity: 1; }
            100% { transform: scale(0.95); opacity: 0.7; }
        }

        .now-playing-title {
            font-size: 26px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
            word-break: break-word;
        }

        .now-playing-subtext {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-muted);
        }

        .btn-return-live {
            min-height: 56px;
            background: #1e3a5f;
            border: 3px solid #2563eb;
            border-radius: 14px;
            color: #ffffff;
            font-size: 19px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            cursor: pointer;
            margin-top: 6px;
        }
        .btn-return-live:active {
            transform: scale(0.96);
            background: #2563eb;
        }

        /* Banner de Notificação de Ação (Aviso Longo) */
        .action-banner {
            display: none;
            background: #1e3a5f;
            border: 3px solid var(--blue);
            border-radius: var(--btn-radius);
            padding: 16px 20px;
            font-size: 18px;
            font-weight: 800;
            color: #ffffff;
            line-height: 1.4;
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.2s ease-in-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-6px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Títulos de Seção */
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 6px;
        }
        .section-title {
            font-size: 20px;
            font-weight: 900;
            color: var(--text-main);
            letter-spacing: 0.3px;
        }

        /* Filtros de Categorias (Chips com Rolagem Suave) */
        .category-chips {
            display: flex;
            gap: 10px;
            overflow-x: auto;
            padding: 4px 2px 10px 2px;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
        }
        .category-chips::-webkit-scrollbar { display: none; }

        .cat-chip {
            background: #1e293b;
            border: 2px solid var(--border);
            color: var(--text-main);
            border-radius: 26px;
            min-height: 52px;
            padding: 10px 22px;
            font-size: 18px;
            font-weight: 800;
            cursor: pointer;
            white-space: nowrap;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            flex-shrink: 0;
            transition: all 0.15s ease;
            user-select: none;
        }
        .cat-chip.active {
            background: var(--gold);
            color: #000000;
            border-color: var(--gold);
            box-shadow: 0 0 16px rgba(250, 204, 21, 0.4);
        }
        .cat-chip:active {
            transform: scale(0.96);
        }
        .cat-chip:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }
        .cat-chip .fav-badge {
            background: #ef4444;
            color: #ffffff;
            font-size: 14px;
            font-weight: 900;
            padding: 2px 8px;
            border-radius: 12px;
            margin-left: 4px;
        }
        .cat-chip.active .fav-badge {
            background: #000000;
            color: var(--gold);
        }

        /* Campo de Busca Acessível com Botão Limpar */
        .search-box-wrapper {
            position: relative;
            width: 100%;
            display: flex;
            align-items: center;
        }
        .senior-search-input {
            width: 100%;
            height: 56px;
            background: #1e293b;
            border: 3px solid #334155;
            border-radius: 14px;
            padding: 0 54px 0 18px;
            font-size: 19px;
            font-weight: 700;
            color: #ffffff;
            font-family: var(--font-family);
            outline: none;
            transition: border-color 0.15s ease, background 0.15s ease;
            box-sizing: border-box;
        }
        .senior-search-input:focus {
            border-color: var(--gold);
            background: #0f172a;
        }
        .btn-clear-search {
            position: absolute;
            right: 8px;
            width: 42px;
            height: 42px;
            background: rgba(255, 255, 255, 0.12);
            border: 2px solid rgba(255, 255, 255, 0.25);
            border-radius: 50%;
            color: #ffffff;
            font-size: 20px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .btn-clear-search:active {
            background: var(--red);
            transform: scale(0.92);
        }

        /* Barra de Informação de Páginas e Itens */
        .catalog-status-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 17px;
            font-weight: 700;
            color: var(--text-muted);
            padding: 4px 6px;
        }

        /* ==================== MODO CINEMA: PRATELEIRA E CATÁLOGO ==================== */
        .vod-shelf-section {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .vod-movie-card {
            background: var(--surface);
            border: 3px solid var(--border);
            border-radius: var(--btn-radius);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
            transition: transform 0.1s ease, border-color 0.15s ease;
        }
        .vod-movie-card.active {
            background: var(--surface-active);
            border-color: var(--gold);
            box-shadow: 0 0 25px rgba(250, 204, 21, 0.35);
        }
        .vod-movie-card:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 4px;
        }

        .vod-card-top {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .vod-poster-thumb {
            width: 68px;
            height: 98px;
            border-radius: 10px;
            object-fit: cover;
            background: #0b0f19;
            border: 2px solid var(--border);
            flex-shrink: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 36px;
        }

        .vod-details {
            flex: 1;
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .vod-title {
            font-size: 22px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
            word-break: break-word;
        }
        .vod-meta {
            font-size: 18px;
            font-weight: 700;
            color: var(--text-muted);
        }

        .btn-play-vod-giant {
            width: 100%;
            min-height: 68px;
            background: #15803d;
            border: 3px solid #22c55e;
            border-radius: 14px;
            color: #ffffff;
            font-size: 20px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(34, 197, 94, 0.3);
            transition: all 0.1s ease;
        }
        .btn-play-vod-giant:active {
            transform: scale(0.97);
            background: #22c55e;
            color: #000000;
        }
        .btn-play-vod-giant:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }

        .vod-card-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 4px;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
        }
        .btn-delete-vod {
            background: transparent;
            border: none;
            color: var(--red);
            font-size: 16px;
            font-weight: 800;
            cursor: pointer;
            padding: 8px 12px;
            border-radius: 8px;
        }
        .btn-delete-vod:active {
            background: rgba(239, 68, 68, 0.2);
        }

        /* Card de Download / Preparação VOD com Informações Completas */
        .vod-downloading-card {
            border-color: #0284c7;
            background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
            box-shadow: 0 4px 20px rgba(2, 132, 199, 0.25);
        }
        .vod-badges-row {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 4px;
        }
        .badge-route {
            font-size: 14px;
            font-weight: 800;
            padding: 4px 10px;
            border-radius: 8px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }
        .badge-direct {
            background: rgba(34, 197, 94, 0.2);
            border: 1.5px solid #22c55e;
            color: #4ade80;
        }
        .badge-proxy {
            background: rgba(234, 179, 8, 0.2);
            border: 1.5px solid #eab308;
            color: #facc15;
        }
        .badge-yt {
            background: rgba(239, 68, 68, 0.2);
            border: 1.5px solid #ef4444;
            color: #f87171;
        }
        .badge-pct {
            font-size: 15px;
            font-weight: 900;
            padding: 3px 10px;
            border-radius: 8px;
            background: #2563eb;
            color: #ffffff;
        }
        .vod-status-detail {
            font-size: 16px;
            font-weight: 700;
            color: #cbd5e1;
            margin-top: 4px;
            line-height: 1.3;
        }
        .vod-speed-detail {
            font-size: 15px;
            font-weight: 700;
            color: #38bdf8;
            margin-top: 2px;
        }
        .vod-progress-bar-wrap {
            width: 100%;
            height: 16px;
            background: #0b0f19;
            border: 2px solid #334155;
            border-radius: 8px;
            overflow: hidden;
            margin-top: 2px;
        }
        .vod-progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #2563eb, #38bdf8);
            transition: width 0.3s ease;
        }
        .btn-cancel-vod {
            background: rgba(239, 68, 68, 0.15);
            border: 2px solid #ef4444;
            color: #f87171;
            font-size: 16px;
            font-weight: 800;
            padding: 8px 16px;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .btn-cancel-vod:active {
            background: #ef4444;
            color: #ffffff;
            transform: scale(0.96);
        }

        /* ==================== CENTRAL DE MEMÓRIA DA TV E YOUTUBE (MODAL / DRAWER) ==================== */
        .downloads-modal-box {
            background: #111827;
            border: 3px solid var(--gold);
            border-radius: 20px;
            padding: 18px;
            max-width: 620px;
            width: 100%;
            height: 92vh;
            max-height: 92vh;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.9);
            position: relative;
            box-sizing: border-box;
            animation: slideUpModal 0.22s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes slideUpModal {
            from { opacity: 0; transform: translateY(20px) scale(0.98); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }

        .downloads-modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 12px;
            border-bottom: 2px solid var(--border);
            flex-shrink: 0;
            padding-right: 48px;
        }

        .downloads-modal-title {
            font-size: 21px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.2;
            letter-spacing: -0.3px;
        }

        .vod-disk-badge {
            font-size: 14px;
            font-weight: 800;
            color: var(--gold);
            margin-top: 3px;
            display: inline-block;
        }

        .downloads-modal-body {
            overflow-y: auto;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 18px;
            padding-right: 4px;
            -webkit-overflow-scrolling: touch;
        }

        /* Fila de Downloads Ativos em Largura Total */
        .active-downloads-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        /* ==================== GRADE DE 2 COLUNAS DE VODS PRONTOS ==================== */
        .vod-grid-2col {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
        }

        .vod-grid-card {
            background: var(--surface);
            border: 2px solid var(--border);
            border-radius: 14px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.1s ease, border-color 0.15s ease, box-shadow 0.15s ease;
            position: relative;
        }
        .vod-grid-card.active {
            border-color: var(--gold);
            box-shadow: 0 0 18px rgba(250, 204, 21, 0.4);
        }
        .vod-grid-card:active {
            transform: scale(0.975);
        }
        .vod-grid-card:focus-visible {
            outline: 3px solid var(--gold);
            outline-offset: 2px;
        }

        .vod-grid-poster-wrap {
            position: relative;
            width: 100%;
            aspect-ratio: 2/3;
            background: #0b0f19;
            overflow: hidden;
        }

        .vod-grid-poster-img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }

        .vod-grid-poster-fallback {
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 38px;
            background: #0f172a;
            color: var(--gold);
        }

        .vod-grid-badge {
            position: absolute;
            top: 6px;
            left: 6px;
            font-size: 11px;
            font-weight: 900;
            padding: 3px 6px;
            border-radius: 6px;
            background: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(4px);
            color: #ffffff;
            border: 1px solid rgba(255, 255, 255, 0.25);
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }
        .vod-grid-badge.live-active {
            background: #15803d;
            border-color: var(--green);
            color: #ffffff;
        }

        .vod-grid-body {
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 1;
            justify-content: space-between;
        }

        .vod-grid-title {
            font-size: 15px;
            font-weight: 800;
            color: #ffffff;
            line-height: 1.25;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            word-break: break-word;
            min-height: 38px;
        }

        .btn-vod-grid-play {
            width: 100%;
            min-height: 44px;
            background: #15803d;
            border: 2px solid #22c55e;
            border-radius: 10px;
            color: #ffffff;
            font-size: 15px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            cursor: pointer;
            transition: all 0.1s ease;
        }
        .btn-vod-grid-play:active {
            transform: scale(0.96);
            background: #22c55e;
            color: #000000;
        }

        .btn-vod-grid-delete {
            background: transparent;
            border: none;
            color: var(--red);
            font-size: 13px;
            font-weight: 700;
            padding: 4px;
            cursor: pointer;
            text-align: center;
            border-radius: 6px;
        }
        .btn-vod-grid-delete:active {
            background: rgba(239, 68, 68, 0.2);
        }

        /* Seção Dedicada ao YouTube */
        .yt-section-card {
            background: #1e293b;
            border: 3px solid #ef4444;
            border-radius: var(--btn-radius);
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: 0 4px 16px rgba(239, 68, 68, 0.2);
        }
        .yt-header-row {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .yt-header-title {
            font-size: 20px;
            font-weight: 900;
            color: #ffffff;
        }
        .yt-actions-row {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        .btn-yt-action {
            flex: 1;
            min-width: 150px;
            min-height: 58px;
            border-radius: 14px;
            font-family: var(--font-family);
            font-size: 18px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .btn-yt-live {
            background: #15803d;
            border: 3px solid #22c55e;
            color: #ffffff;
        }
        .btn-yt-live:active {
            background: #22c55e;
            color: #000000;
            transform: scale(0.96);
        }
        .btn-yt-save {
            background: #991b1b;
            border: 3px solid #ef4444;
            color: #ffffff;
        }
        .btn-yt-save:active {
            background: #ef4444;
            color: #ffffff;
            transform: scale(0.96);
        }

        /* Cartão de YouTube Ativo na TV */
        .yt-active-card {
            background: #450a0a;
            border: 3px solid #ef4444;
            border-radius: var(--btn-radius);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 0 25px rgba(239, 68, 68, 0.4);
        }

        /* Grade do Catálogo de Filmes */
        .catalog-movies-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 16px;
        }

        .catalog-movie-card {
            position: relative;
            background: var(--surface);
            border: 3px solid var(--border);
            border-radius: var(--btn-radius);
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .catalog-movie-card:active {
            transform: scale(0.96);
            border-color: var(--gold);
        }
        .catalog-movie-card:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 4px;
        }

        .catalog-rating-badge {
            position: absolute;
            top: 18px;
            left: 18px;
            background: rgba(11, 15, 25, 0.90);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            border: 1.5px solid rgba(255, 255, 255, 0.24);
            color: #ffffff;
            padding: 4px 8px;
            border-radius: 6px;
            letter-spacing: 0.2px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.65);
            z-index: 4;
            pointer-events: none;
            display: inline-flex;
            align-items: baseline;
            gap: 1px;
            line-height: 1;
        }
        .catalog-rating-badge .rating-num {
            font-size: 13px;
            font-weight: 900;
            color: #ffffff;
        }
        .catalog-rating-badge .rating-denom {
            font-size: 10px;
            font-weight: 700;
            color: #94a3b8;
        }

        .catalog-movie-poster {
            width: 100%;
            aspect-ratio: 2/3;
            border-radius: 10px;
            object-fit: cover;
            background: #0b0f19;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 40px;
        }
        .catalog-movie-title {
            font-size: 19px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .catalog-movie-year {
            font-size: 16px;
            font-weight: 700;
            color: var(--gold);
        }

        /* ==================== MODO TV AO VIVO ==================== */
        .channels-list {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .senior-channel-card {
            background: var(--surface);
            border: 3px solid var(--border);
            border-radius: var(--btn-radius);
            min-height: 76px;
            padding: 14px 18px;
            display: flex;
            align-items: center;
            gap: 16px;
            cursor: pointer;
            position: relative;
            transition: transform 0.1s ease, border-color 0.15s ease;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }
        .senior-channel-card:active {
            transform: scale(0.97);
            background: #334155;
        }
        .senior-channel-card.active {
            background: var(--surface-active);
            border-color: var(--gold);
            box-shadow: 0 0 25px rgba(250, 204, 21, 0.35);
        }
        .senior-channel-card:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 4px;
        }

        .channel-num-badge {
            font-size: 32px;
            font-weight: 900;
            color: var(--gold);
            background: #0b0f19;
            border: 2px solid var(--border);
            min-width: 56px;
            height: 56px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 12px;
            flex-shrink: 0;
            font-variant-numeric: tabular-nums;
        }
        .senior-channel-card.active .channel-num-badge {
            background: var(--gold);
            color: #000000;
            border-color: var(--gold);
        }

        .channel-logo-wrap {
            width: 52px;
            height: 52px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.05);
            overflow: hidden;
        }
        .channel-logo-img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }
        .channel-logo-fallback {
            font-size: 32px;
        }

        .channel-info {
            flex: 1;
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 3px;
        }
        .channel-name {
            font-size: 22px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
            word-break: break-word;
        }
        .channel-category {
            font-size: 18px;
            font-weight: 700;
            color: var(--text-muted);
        }

        .channel-fav-btn {
            width: 48px;
            height: 48px;
            padding: 0;
            margin: 0;
            box-sizing: border-box;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            line-height: 0;
            color: var(--text-muted);
            background: transparent;
            border: none;
            cursor: pointer;
            border-radius: 50%;
            flex-shrink: 0;
            transition: transform 0.15s ease, color 0.15s ease;
        }
        .channel-fav-btn svg {
            width: 26px;
            height: 26px;
            display: block;
            pointer-events: none;
            flex-shrink: 0;
        }
        .channel-fav-btn.is-fav {
            color: var(--gold);
        }
        .channel-fav-btn.is-fav svg {
            filter: drop-shadow(0 0 6px rgba(250, 204, 21, 0.7));
        }
        .channel-fav-btn:focus-visible {
            outline: 4px solid var(--gold);
        }

        .series-fav-star {
            position: absolute;
            top: 14px;
            right: 14px;
            width: 44px;
            height: 44px;
            padding: 0;
            margin: 0;
            box-sizing: border-box;
            line-height: 0;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            border: 2px solid rgba(255, 255, 255, 0.2);
            border-radius: 50%;
            z-index: 5;
            display: flex;
            align-items: center;
            justify-content: center;
            color: rgba(255, 255, 255, 0.4);
            cursor: pointer;
            transition: transform 0.15s ease, border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
        }
        .series-fav-star svg {
            width: 22px;
            height: 22px;
            display: block;
            pointer-events: none;
            flex-shrink: 0;
            transition: transform 0.15s ease;
        }
        .series-fav-star:hover {
            transform: scale(1.12);
            background: rgba(15, 23, 42, 0.95);
            color: var(--gold);
        }
        .series-fav-star.is-fav {
            color: var(--gold);
            border-color: var(--gold);
            background: rgba(15, 23, 42, 0.92);
        }
        .series-fav-star.is-fav svg {
            filter: drop-shadow(0 0 6px rgba(250, 204, 21, 0.8));
        }

        /* Modal de Detalhes do VOD Salvo */
        .vod-details-modal-box {
            height: auto !important;
            max-height: 90vh;
            overflow-y: auto;
            border-color: #3b82f6 !important;
            padding: 18px !important;
            gap: 14px !important;
        }
        .vod-modal-hero {
            display: flex;
            flex-direction: column;
            gap: 12px;
            width: 100%;
        }
        .vod-modal-hero-top {
            display: flex;
            align-items: center;
            gap: 14px;
            padding-right: 44px;
        }
        .vod-modal-poster {
            width: 76px;
            height: 106px;
            border-radius: 10px;
            object-fit: cover;
            background: #0f172a;
            border: 2px solid var(--border);
            flex-shrink: 0;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
        }
        .vod-modal-badges-col {
            display: flex;
            flex-direction: column;
            gap: 8px;
            align-items: flex-start;
            min-width: 0;
        }
        .vod-modal-ep-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%);
            border: 2px solid #a78bfa;
            color: #ffffff;
            font-size: 14px;
            font-weight: 800;
            padding: 5px 12px;
            border-radius: 20px;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.35);
        }
        .vod-modal-title-box {
            background: rgba(15, 23, 42, 0.85);
            border: 2px solid rgba(59, 130, 246, 0.45);
            border-radius: 12px;
            padding: 14px 16px;
            width: 100%;
            box-sizing: border-box;
        }
        .vod-modal-full-title {
            font-size: 19px !important;
            font-weight: 900 !important;
            color: #ffffff !important;
            line-height: 1.4 !important;
            word-break: break-word !important;
            white-space: normal !important;
            overflow: visible !important;
            text-overflow: unset !important;
            display: block !important;
            -webkit-line-clamp: unset !important;
            -webkit-box-orient: unset !important;
        }
        .vod-grid-ep-badge {
            background: rgba(124, 58, 237, 0.9);
            border: 1px solid #a78bfa;
            color: #ffffff;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 6px;
            margin-top: 3px;
            width: fit-content;
            display: inline-block;
        }
        .vod-modal-details-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            background: #0f172a;
            border: 2px solid var(--border);
            border-radius: 12px;
            padding: 12px 14px;
        }
        .vod-modal-detail-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 15px;
            gap: 12px;
        }
        .vod-modal-label {
            color: var(--text-muted);
            font-weight: 600;
        }

        .active-live-badge {
            display: none;
            position: absolute;
            top: -10px;
            right: 14px;
            background: var(--gold);
            color: #000000;
            font-size: 14px;
            font-weight: 900;
            padding: 4px 12px;
            border-radius: 20px;
            letter-spacing: 0.5px;
        }
        .senior-channel-card.active .active-live-badge {
            display: block;
        }

        /* 3. BASE FIXA: Zona do Polegar (Controle Remoto de TV Físico) */
        .fixed-remote-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: rgba(11, 15, 25, 0.97);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border-top: 3px solid var(--border);
            padding: 10px 16px max(10px, env(safe-area-inset-bottom)) 16px;
            display: flex;
            justify-content: center;
            z-index: 1000;
            box-shadow: 0 -8px 30px rgba(0, 0, 0, 0.7);
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.25s ease;
            transform: translateY(0);
        }

        .fixed-remote-bar.hidden-down {
            transform: translateY(100%);
            opacity: 0;
            pointer-events: none;
        }

        .remote-controls-wrapper {
            width: 100%;
            max-width: 640px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        /* Botão Unificado Principal: TV AO VIVO */
        .btn-remote-live-tv {
            width: 100%;
            height: 58px;
            background: linear-gradient(135deg, #1e3a5f 0%, #1d4ed8 100%);
            border: 2px solid #3b82f6;
            border-radius: 14px;
            color: #ffffff;
            font-family: var(--font-family);
            font-size: 19px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            cursor: pointer;
            box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);
            transition: all 0.15s ease;
        }
        .btn-remote-live-tv:active {
            transform: scale(0.97);
            background: #2563eb;
            box-shadow: 0 2px 6px rgba(37, 99, 235, 0.5);
        }
        .btn-remote-live-tv:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }

        .remote-row {
            display: flex;
            gap: 12px;
            width: 100%;
        }

        .btn-remote {
            flex: 1;
            height: 64px;
            background: #1e293b;
            border: 3px solid #475569;
            border-radius: 14px;
            color: #ffffff;
            font-family: var(--font-family);
            font-size: 20px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            cursor: pointer;
            box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3);
            transition: all 0.1s ease;
        }
        .btn-remote:active {
            transform: scale(0.96);
            background: #334155;
            border-color: var(--gold);
        }
        .btn-remote:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }

        .btn-remote-ch {
            background: #1e3a5f;
            border-color: #2563eb;
            color: #ffffff;
        }
        .btn-remote-ch:active {
            background: #2563eb;
        }

        .btn-remote-vol {
            height: 56px;
            font-size: 18px;
            background: #1e293b;
        }

        .btn-remote-mute {
            height: 56px;
            font-size: 18px;
            background: var(--red-bg);
            border-color: #ef4444;
            color: var(--red);
        }
        .btn-remote-mute:active {
            background: #ef4444;
            color: #ffffff;
        }

        /* Botões de Navegação / Paginação do Polegar */
        .btn-remote-nav {
            height: 58px;
            font-size: 18px;
            background: #1e293b;
            border: 3px solid #475569;
            color: #ffffff;
        }
        .btn-remote-nav:active {
            background: #334155;
            border-color: var(--gold);
        }
        .btn-remote-nav[disabled] {
            opacity: 0.35;
            cursor: not-allowed;
            pointer-events: none;
        }

        /* Botão Destaque: Voltar ao Topo */
        .btn-remote-top {
            height: 58px;
            font-size: 17px;
            background: #27272a;
            border: 3px solid #eab308;
            color: #facc15;
            font-weight: 900;
            box-shadow: 0 4px 12px rgba(234, 179, 8, 0.2);
        }
        .btn-remote-top:active {
            background: #facc15;
            color: #000000;
            transform: scale(0.96);
        }

        /* Oculta a Barra Fixa do Controle quando o Teclado Virtual Abre no Celular */
        body.keyboard-open .fixed-remote-bar {
            display: none !important;
        }

        .btn-reconnect {
            height: 56px; /* Ajustado para 56px */
            background: #0f172a;
            border: 2px solid #334155;
            border-radius: 14px;
            color: var(--text-muted);
            font-size: 18px; /* Ajustado para 18px */
            font-weight: 800;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            cursor: pointer;
        }
        .btn-reconnect:active {
            background: #1e293b;
            color: #ffffff;
        }
        .btn-reconnect:focus-visible {
            outline: 4px solid var(--gold);
            outline-offset: 2px;
        }

        /* 4. MODAL SÊNIOR DE DETALHES DO FILME */
        .senior-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.88);
            z-index: 2000;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .senior-modal-box {
            background: #1e293b;
            border: 3px solid var(--gold);
            border-radius: 20px;
            padding: 20px;
            max-width: 500px;
            width: 100%;
            max-height: 90vh;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
        }
        .modal-movie-top {
            display: flex;
            gap: 16px;
        }
        .modal-poster {
            width: 110px;
            height: 160px;
            border-radius: 12px;
            object-fit: cover;
            background: #0b0f19;
            flex-shrink: 0;
        }
        .modal-info {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .modal-title {
            font-size: 24px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
        }
        .modal-meta {
            font-size: 18px;
            font-weight: 700;
            color: var(--gold);
        }
        .modal-plot {
            font-size: 18px;
            font-weight: 500;
            color: var(--text-muted);
            line-height: 1.5;
            background: #111827;
            padding: 12px;
            border-radius: 10px;
        }
        .modal-actions {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .btn-modal-action {
            min-height: 64px;
            border-radius: 14px;
            font-size: 20px;
            font-weight: 900;
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        .btn-modal-save {
            background: #15803d;
            border: 3px solid #22c55e;
            color: #ffffff;
        }
        .btn-modal-save:active {
            background: #166534;
            transform: scale(0.98);
        }
        .btn-modal-play {
            background: #1e3a5f;
            border: 3px solid #38bdf8;
            color: #ffffff;
        }
        .btn-modal-play:active {
            background: #0f2744;
            transform: scale(0.98);
        }
        .btn-modal-close {
            min-height: 52px;
            background: #334155;
            color: #ffffff;
            font-size: 18px;
        }

        /* Estilização Sênior de Séries: Modal Fluido, Temporadas e Episódios Grandes */
        .series-modal-box {
            background: #1e293b;
            border: 3px solid var(--gold);
            border-radius: 20px;
            padding: 16px;
            max-width: 540px;
            width: 100%;
            height: 90vh;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            gap: 10px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.85);
            position: relative;
            box-sizing: border-box;
        }
        .modal-close-icon-btn {
            position: absolute;
            top: 12px;
            right: 12px;
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: #334155;
            color: #ffffff;
            border: 2px solid #475569;
            font-size: 20px;
            font-weight: 900;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            z-index: 10;
            transition: all 0.1s ease;
        }
        .modal-close-icon-btn:active {
            background: #475569;
            transform: scale(0.92);
        }
        .modal-series-header {
            display: flex;
            gap: 12px;
            flex-shrink: 0;
            padding-right: 48px;
        }
        .modal-series-poster {
            width: 80px;
            height: 115px;
            border-radius: 10px;
            object-fit: cover;
            background: #0b0f19;
            flex-shrink: 0;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }
        .modal-series-info {
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: 4px;
            min-width: 0;
        }
        .modal-series-title {
            font-size: 20px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.25;
            overflow: hidden;
            text-overflow: ellipsis;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }
        .modal-series-meta {
            font-size: 15px;
            font-weight: 700;
            color: var(--gold);
        }
        .modal-series-plot {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-muted);
            line-height: 1.35;
            background: #111827;
            padding: 8px 12px;
            border-radius: 10px;
            flex-shrink: 0;
            cursor: pointer;
            max-height: 56px;
            overflow: hidden;
            text-overflow: ellipsis;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            transition: max-height 0.2s ease;
        }
        .modal-series-plot.expanded {
            max-height: 160px;
            -webkit-line-clamp: unset;
            overflow-y: auto;
        }
        .series-seasons-section {
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .series-section-label {
            font-weight: 800;
            font-size: 14px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        .season-chips {
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding: 2px 2px 6px 2px;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
        }
        .season-chips::-webkit-scrollbar {
            display: none;
        }
        .season-chip {
            background: #0f172a;
            border: 2px solid #334155;
            color: #cbd5e1;
            padding: 8px 16px;
            border-radius: 12px;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
            white-space: nowrap;
            flex-shrink: 0;
            transition: all 0.1s ease;
        }
        .season-chip.active {
            background: var(--gold);
            color: #000000;
            border-color: var(--gold);
        }
        .season-chip:active {
            transform: scale(0.95);
        }
        .episodes-list {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
            display: flex;
            flex-direction: column;
            gap: 10px;
            padding-right: 4px;
            padding-bottom: 12px;
        }
        .episode-card {
            background: #0f172a;
            border: 2px solid #334155;
            border-radius: 14px;
            padding: 12px 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            cursor: pointer;
            transition: all 0.15s ease;
            box-shadow: 0 4px 10px rgba(0,0,0,0.3);
        }
        .episode-card:active {
            border-color: var(--gold);
            background: #172554;
            transform: scale(0.98);
        }
        .episode-main-info {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .episode-badge {
            background: rgba(234, 179, 8, 0.2);
            color: var(--gold);
            border: 1.5px solid var(--gold);
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 900;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .episode-title-group {
            flex: 1;
            min-width: 0;
        }
        .episode-title {
            font-size: 17px;
            font-weight: 800;
            color: #ffffff;
            line-height: 1.3;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .episode-duration {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-dim);
            margin-top: 2px;
        }
        .episode-actions-row {
            display: flex;
            gap: 10px;
            margin-top: 4px;
            align-items: stretch;
        }
        .btn-ep-save-senior {
            flex: 1;
            min-height: 48px;
            background: #15803d;
            border: 2px solid #22c55e;
            color: #ffffff;
            font-size: 16px;
            font-weight: 900;
            border-radius: 10px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.1s ease;
        }
        .btn-ep-save-senior:active {
            background: #166534;
            transform: scale(0.97);
        }
        .btn-ep-play-senior {
            flex: 0 0 54px;
            width: 54px;
            min-height: 48px;
            background: #1e3a5f;
            border: 2px solid #38bdf8;
            color: #ffffff;
            font-size: 20px;
            font-weight: 900;
            border-radius: 10px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.1s ease;
        }
        .btn-ep-play-senior:active {
            background: #0f2744;
            transform: scale(0.94);
        }

        /* Modal para instruções iOS */
        .ios-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.88);
            z-index: 2500;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .ios-modal-box {
            background: #1e293b;
            border: 3px solid var(--gold);
            border-radius: 20px;
            padding: 24px;
            max-width: 440px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            text-align: center;
        }
        /* ==================== WEB PLAYER MODAL (DESKTOP & WEB) ==================== */
        .web-player-modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.94);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            z-index: 3000;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .web-player-container {
            background: #0f172a;
            border: 2px solid #38bdf8;
            border-radius: 20px;
            width: 100%;
            max-width: 1100px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.9);
            animation: playerPop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes playerPop {
            from { transform: scale(0.95); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }
        .web-player-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 20px;
            background: #1e293b;
            border-bottom: 2px solid var(--border);
            gap: 12px;
        }
        .web-player-title-box {
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 0;
        }
        .web-player-badge {
            background: #ef4444;
            color: #ffffff;
            font-size: 13px;
            font-weight: 900;
            padding: 3px 8px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            flex-shrink: 0;
        }
        .web-player-badge.vod {
            background: #3b82f6;
        }
        .web-player-title {
            font-size: 19px;
            font-weight: 800;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin: 0;
        }
        .web-player-header-actions {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }
        .btn-player-transfer {
            background: linear-gradient(135deg, #1e3a5f 0%, #1d4ed8 100%);
            border: 2px solid #3b82f6;
            border-radius: 10px;
            color: #ffffff;
            font-size: 15px;
            font-weight: 800;
            padding: 8px 16px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s ease;
        }
        .btn-player-transfer:hover {
            background: #2563eb;
            transform: translateY(-1px);
        }
        .btn-player-close {
            background: #334155;
            border: none;
            color: #ffffff;
            font-size: 22px;
            font-weight: 700;
            width: 40px;
            height: 40px;
            border-radius: 10px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }
        .btn-player-close:hover {
            background: #ef4444;
        }
        .web-player-video-wrapper {
            position: relative;
            width: 100%;
            aspect-ratio: 16/9;
            max-height: 72vh;
            background: #000000;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .web-player-video {
            width: 100%;
            height: 100%;
            object-fit: contain;
            outline: none;
        }
        .web-player-iframe {
            width: 100%;
            height: 100%;
            border: none;
        }
        .web-player-spinner {
            position: absolute;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 12px;
            color: var(--accent);
            font-size: 16px;
            font-weight: 700;
            pointer-events: none;
        }
        .spinner-circle {
            width: 40px;
            height: 40px;
            border: 4px solid rgba(56, 189, 248, 0.2);
            border-top-color: #38bdf8;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .web-player-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            padding: 12px 20px;
            background: #111827;
            border-top: 1px solid var(--border);
            font-size: 14px;
            color: var(--text-muted);
            gap: 10px;
        }
        .web-player-shortcuts-hint strong {
            color: var(--gold);
        }

        /* Botões de Assistir no Computador (Web) - Ocultos por padrão em Mobile e no App */
        .btn-modal-web,
        .btn-ep-web,
        .btn-play-vod-web,
        .btn-channel-web,
        .btn-mirror-web,
        #btn-yt-web {
            display: none !important;
        }

        .btn-modal-web {
            background: linear-gradient(135deg, #065f46 0%, #059669 100%);
            border: 2px solid #10b981;
            color: #ffffff;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.3);
        }
        .btn-modal-web:active, .btn-modal-web:hover {
            background: #10b981;
            color: #000000;
        }
        .btn-ep-web {
            background: #065f46;
            color: #a7f3d0;
            border: 1.5px solid #10b981;
        }
        .btn-ep-web:hover {
            background: #10b981;
            color: #000000;
        }
        .btn-play-vod-web {
            background: #065f46;
            border: 2px solid #10b981;
            border-radius: 14px;
            color: #ffffff;
            font-size: 17px;
            font-weight: 800;
            padding: 12px 20px;
            cursor: pointer;
            transition: all 0.15s ease;
            white-space: nowrap;
        }
        .btn-play-vod-web:hover {
            background: #10b981;
            color: #000000;
        }
        .vod-actions-dual {
            display: flex;
            gap: 10px;
            margin-top: 10px;
        }
        .vod-actions-dual .btn-play-vod-giant {
            margin-top: 0;
            flex: 1;
        }
        .channel-actions-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-left: auto;
        }
        .btn-channel-web {
            background: #065f46;
            border: 1.5px solid #10b981;
            border-radius: 8px;
            color: #a7f3d0;
            font-size: 13px;
            font-weight: 800;
            padding: 6px 12px;
            cursor: pointer;
            transition: all 0.15s ease;
            white-space: nowrap;
        }
        .btn-channel-web:hover {
            background: #10b981;
            color: #000000;
        }
        .btn-mirror-web {
            background: linear-gradient(135deg, #065f46 0%, #059669 100%);
            border: 1.5px solid #10b981;
            border-radius: 10px;
            color: #ffffff;
            font-size: 14px;
            font-weight: 800;
            padding: 8px 16px;
            cursor: pointer;
            transition: all 0.15s ease;
            align-items: center;
            gap: 8px;
            box-shadow: 0 2px 10px rgba(16, 185, 129, 0.25);
        }
        .btn-mirror-web:hover {
            background: #10b981;
            color: #000000;
            transform: translateY(-1px);
        }

        /* ==================== REGRAS ESPECÍFICAS DE DESKTOP WIDESCREEN ==================== */
        @media (min-width: 900px) {
            body {
                padding-bottom: 120px;
            }
            .app-container {
                max-width: 1400px;
                padding: 24px 36px 130px 36px;
                gap: 22px;
            }

            /* Cabeçalho Desktop */
            header.top-tv-banner {
                padding: 18px 24px;
            }

            /* Seletor de Modo Desktop */
            .main-mode-toggle {
                max-width: 800px;
                margin: 0 auto;
                width: 100%;
            }
            .btn-mode-tab:hover {
                background: #334155;
            }

            /* Grade de Filmes e Séries Desktop */
            .catalog-movies-grid {
                grid-template-columns: repeat(auto-fill, minmax(185px, 1fr));
                gap: 20px;
            }
            .catalog-movie-card:hover {
                transform: translateY(-4px);
                border-color: var(--gold);
                box-shadow: 0 12px 30px rgba(0, 0, 0, 0.6);
            }
            .catalog-movie-card:hover .catalog-movie-poster {
                transform: scale(1.02);
            }
            .catalog-movie-poster {
                transition: transform 0.2s ease;
            }

            /* Central de Memória e Downloads Desktop */
            .downloads-modal-box {
                max-width: 680px;
                padding: 24px;
            }
            .vod-grid-2col {
                grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
                gap: 16px;
            }

            /* Grade de Canais Ao Vivo Desktop */
            .channels-list {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
                gap: 16px;
            }
            .senior-channel-card:hover {
                transform: translateY(-2px);
                border-color: #3b82f6;
                box-shadow: 0 8px 24px rgba(37, 99, 235, 0.25);
            }

            /* Barra Fixa Inferior: Dock Flutuante Centralizado */
            .fixed-remote-bar {
                left: 50%;
                transform: translateX(-50%);
                max-width: 760px;
                bottom: 18px;
                border-radius: 20px;
                background: rgba(15, 23, 42, 0.95);
                backdrop-filter: blur(16px);
                border: 2px solid var(--border);
                box-shadow: 0 16px 45px rgba(0, 0, 0, 0.8);
            }
            .fixed-remote-bar.hidden-down {
                transform: translate(-50%, 130%);
            }
        /* Garante que dentro do App Mobile instalado (PWA/APK) os botões de PC nunca apareçam */
        @media (display-mode: standalone) and (max-width: 1024px) {
            .btn-modal-web,
            .btn-ep-web,
            .btn-play-vod-web,
            .btn-channel-web,
            .btn-mirror-web,
            #btn-yt-web {
                display: none !important;
            }
        }
    </style>
</head>
<body>

    <div class="app-container">

        <!-- Banner de Instalação PWA (Celular) -->
        <div id="pwa-banner" class="pwa-banner">
            <div class="pwa-banner-text">
                📲 <strong>Controle na Tela Inicial:</strong><br>Abra direto sem precisar usar o navegador!
            </div>
            <button id="pwa-install-btn" class="pwa-install-btn" onclick="installPWA()">Instalar</button>
            <button style="background:transparent; border:none; color:var(--text-dim); font-size:24px; padding:4px;" onclick="dismissPWABanner()">✕</button>
        </div>

        <!-- TOPO: Central Universal da TV (Status, O que está no ar & Acesso aos Salvos) -->
        <header id="top-tv-banner" class="top-tv-banner" onclick="openDownloadsModal()" role="button" tabindex="0" aria-label="Ver vídeos salvos e downloads da TV">
            <div class="top-tv-status-row">
                <div id="tv-status-badge" class="tv-status-badge" role="status" aria-live="polite">
                    <span>🟢</span>
                    <span id="tv-status-text">Conectando à TV...</span>
                </div>
                <div id="header-media-badge" class="header-media-badge" onclick="event.stopPropagation(); openDownloadsModal()">
                    <span id="header-media-icon">💾</span>
                    <span id="header-media-text">Memória da TV</span>
                    <span style="font-size: 14px; opacity: 0.8;">▾</span>
                </div>
            </div>
            
            <div class="top-tv-content">
                <div id="tv-banner-label" class="now-playing-label">
                    <span id="tv-banner-dot" class="pulsing-dot"></span>
                    <span id="tv-banner-label-text">TRANSMITINDO NA TV AO VIVO</span>
                </div>
                <div id="tv-banner-title" class="now-playing-title">Carregando canal...</div>
                <div id="tv-banner-subtext" class="now-playing-subtext">Modo TV ao Vivo • Toque para ver os salvos na TV</div>
            </div>

            <button id="btn-banner-return-live" class="btn-return-live" style="display: none;" onclick="event.stopPropagation(); returnToLiveTv()">
                📺 VOLTAR PARA A TV AO VIVO
            </button>
        </header>

        <!-- SELETOR PRINCIPAL DE MODO: FILMES vs SÉRIES vs TV AO VIVO -->
        <div class="main-mode-toggle" role="tablist">
            <button id="tab-cinema" class="btn-mode-tab active" role="tab" aria-selected="true" onclick="switchAppMode('cinema')">
                🎬 FILMES
            </button>
            <button id="tab-series" class="btn-mode-tab" role="tab" aria-selected="false" onclick="switchAppMode('series')">
                🍿 SÉRIES
            </button>
            <button id="tab-live" class="btn-mode-tab" role="tab" aria-selected="false" onclick="switchAppMode('live')">
                📺 TV AO VIVO
            </button>
        </div>

        <!-- Banner de Confirmação de Ação (Aviso Longo) -->
        <div id="action-banner" class="action-banner" role="status" aria-live="polite"></div>

        <!-- ==================== ÁREA DO MODO CINEMA & FILMES ==================== -->
        <div id="section-cinema" style="display: flex; flex-direction: column; gap: 18px;">

            <!-- Seção: Catálogo de Filmes -->
            <div class="section-header" style="margin-top: 4px;">
                <div class="section-title">🎬 Escolha o Filme para Assistir:</div>
            </div>

            <!-- Campo de Busca de Filmes com Botão Limpar -->
            <div class="search-box-wrapper">
                <input id="movie-search-input" class="senior-search-input" type="text"
                       placeholder="🔍 Buscar filme pelo nome (ex: Batman, Vingadores)..." 
                       oninput="onMovieSearch(this.value)" autocomplete="off">
                <button id="movie-search-clear" class="btn-clear-search" onclick="clearMovieSearch()" style="display: none;" title="Limpar busca">✕</button>
            </div>

            <!-- Chips de Categorias Dinâmicas de Filmes -->
            <div id="movie-categories" class="category-chips" role="tablist" aria-label="Categorias de Filmes">
                <button class="cat-chip active">● Carregando categorias...</button>
            </div>

            <!-- Status do Catálogo (Contador e Página) -->
            <div id="movie-status-bar" class="catalog-status-bar"></div>

            <!-- Grade de Filmes do Catálogo -->
            <div id="catalog-movies-grid" class="catalog-movies-grid">
                <div style="text-align: center; padding: 30px 10px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">
                    Carregando catálogo de filmes...
                </div>
            </div>

        </div>
 
         <!-- ==================== ÁREA DO MODO SÉRIES ==================== -->
         <div id="section-series" style="display: none; flex-direction: column; gap: 18px;">

             <!-- Seção de Busca de Séries com Botão Limpar -->
             <div class="search-box-wrapper">
                 <input id="series-search-input" class="senior-search-input" type="text"
                        placeholder="🔍 Buscar série pelo nome (ex: Chaves, Sobrenatural, Grey's Anatomy)..." 
                        oninput="onSeriesSearch(this.value)" autocomplete="off">
                 <button id="series-search-clear" class="btn-clear-search" onclick="clearSeriesSearch()" style="display: none;" title="Limpar busca">✕</button>
             </div>

             <!-- Chips de Categorias Dinâmicas de Séries -->
             <div id="series-categories" class="category-chips" role="tablist" aria-label="Categorias de Séries">
                 <button class="cat-chip active">● Carregando categorias...</button>
             </div>

             <!-- Status do Catálogo de Séries (Contador e Página) -->
             <div id="series-status-bar" class="catalog-status-bar"></div>

             <!-- Grade de Séries do Catálogo -->
             <div id="catalog-series-grid" class="catalog-movies-grid">
                 <div style="text-align: center; padding: 30px 10px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">
                     Carregando catálogo de séries...
                 </div>
             </div>

         </div>

         <!-- ==================== ÁREA DO MODO TV AO VIVO ==================== -->
        <div id="section-live" style="display: none; flex-direction: column; gap: 16px;">

            <!-- Cartão "No Ar Agora na TV" -->
            <div class="now-playing-card">
                <div class="now-playing-label">
                    <span class="pulsing-dot"></span>
                    VOCÊ ESTÁ ASSISTINDO NA TV
                </div>
                <div id="now-playing-title" class="now-playing-title">Sintonizando...</div>
                <div id="now-playing-subtext" class="now-playing-subtext">Sinal ao vivo • Transmissão direta</div>
            </div>

            <!-- Grade Tátil de Canais -->
            <div class="section-header">
                <div class="section-title">Escolha o Canal para Assistir:</div>
            </div>

            <!-- Campo de Busca de Canais com Botão Limpar -->
            <div class="search-box-wrapper">
                <input id="channel-search-input" class="senior-search-input" type="text"
                       placeholder="🔍 Buscar canal (ex: Globo, SporTV, SBT, ESPN)..."
                       oninput="onChannelSearch(this.value)" autocomplete="off">
                <button id="channel-search-clear" class="btn-clear-search" onclick="clearChannelSearch()" style="display: none;" title="Limpar busca">✕</button>
            </div>

            <!-- Chips de Categorias Dinâmicas de Canais -->
            <div id="live-categories" class="category-chips" role="tablist" aria-label="Categorias de Canais">
                <button class="cat-chip active">● Carregando categorias...</button>
            </div>

            <!-- Status do Catálogo de Canais (Contador e Página) -->
            <div id="live-status-bar" class="catalog-status-bar"></div>

            <div id="channels-list" class="channels-list">
                <div style="text-align: center; padding: 40px 10px; color: var(--text-muted); font-size: 18px;">
                    Carregando canais da TV...
                </div>
            </div>

        </div>

    </div>

    <!-- BASE FIXA: Zona do Polegar (Controles de Navegação e Topo) -->
    <div class="fixed-remote-bar">
        <div class="remote-controls-wrapper">
            <!-- Paginação e Voltar ao Topo -->
            <div class="remote-row">
                <button id="btn-remote-prev-page" class="btn-remote btn-remote-nav" onclick="prevPage()" aria-label="Página anterior">
                    <span>◄</span> ANTERIOR
                </button>
                <button id="btn-remote-top" class="btn-remote btn-remote-top" onclick="scrollToTop()" aria-label="Voltar ao topo da lista">
                    🔝 VOLTAR AO TOPO
                </button>
                <button id="btn-remote-next-page" class="btn-remote btn-remote-nav" onclick="nextPage()" aria-label="Próxima página">
                    PRÓXIMO <span>►</span>
                </button>
            </div>
        </div>
    </div>

    <!-- MODAL / CENTRAL DE MEMÓRIA DA TV E YOUTUBE -->
    <div id="downloads-modal" class="senior-modal" onclick="closeDownloadsModal()" role="dialog" aria-modal="true" aria-label="Memória da TV e YouTube" style="display: none;">
        <div class="downloads-modal-box" onclick="event.stopPropagation()">
            <!-- Cabeçalho da Gaveta -->
            <div class="downloads-modal-header">
                <div style="display: flex; align-items: center; gap: 12px; min-width: 0;">
                    <span style="font-size: 28px; flex-shrink: 0;">📥</span>
                    <div style="min-width: 0;">
                        <div class="downloads-modal-title">Memória da TV & YouTube</div>
                        <div id="vod-disk-badge" class="vod-disk-badge">
                            💾 Espaço TV: Verificando...
                        </div>
                    </div>
                </div>
                <button class="modal-close-icon-btn" onclick="closeDownloadsModal()" aria-label="Fechar Memória da TV">✕</button>
            </div>

            <!-- Corpo Rolável da Gaveta -->
            <div class="downloads-modal-body">

                <!-- Bloco 1: Assistir ou Baixar do YouTube na TV -->
                <div class="yt-section-card">
                    <div class="yt-header-row">
                        <span style="font-size: 26px;">▶️</span>
                        <div class="yt-header-title">Assistir Vídeo ou Música do YouTube na TV:</div>
                    </div>
                    <div class="search-box-wrapper">
                        <input id="yt-url-input" class="senior-search-input" type="text"
                               placeholder="Cole o link do YouTube aqui (ex: https://youtu.be/...)"
                               oninput="onYtInput(this.value)" autocomplete="off">
                        <button id="yt-clear-btn" class="btn-clear-search" onclick="clearYtInput()" style="display: none;" title="Limpar link">✕</button>
                    </div>
                    <div class="yt-actions-row">
                        <button id="btn-yt-live" class="btn-yt-action btn-yt-live" onclick="playYouTubeLive()">
                            ▶️ ASSISTIR AGORA NA TV
                        </button>
                        <button id="btn-yt-save" class="btn-yt-action btn-yt-save" onclick="saveYouTubeVod()">
                            📥 SALVAR NO CINEMA (COM PAUSA)
                        </button>
                    </div>
                </div>

                <!-- Bloco 2: Downloads em Andamento (Largura Total para Máxima Legibilidade) -->
                <div id="active-downloads-section" style="display: none;">
                    <div class="section-title" style="margin-bottom: 8px; color: var(--gold);">
                        ⏳ Baixando Agora para a TV:
                    </div>
                    <div id="active-downloads-list" class="active-downloads-list"></div>
                </div>

                <!-- Bloco 3: Filmes e Séries Prontos para Assistir (Grid de 2 Colunas) -->
                <div>
                    <div class="section-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
                        <div class="section-title">🎬 Prontos na Memória da TV:</div>
                        <button id="btn-notify-toggle" onclick="requestNotificationPermission()" style="font-size: 13px; font-weight: 800; color: #fff; background: rgba(56,189,248,0.15); padding: 5px 12px; border-radius: 8px; border: 1.5px solid var(--accent); cursor: pointer;" title="Ativar Notificações de Filmes Prontos">
                            🔔 Avisar quando pronto
                        </button>
                    </div>
                    
                    <div id="vod-shelf-list" class="vod-grid-2col">
                        <div style="text-align: center; padding: 24px 10px; color: var(--text-muted); font-size: 17px; grid-column: 1 / -1;">
                            Verificando filmes salvos na TV...
                        </div>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- MODAL SÊNIOR DE DETALHES DO FILME -->
    <div id="movie-modal" class="senior-modal" onclick="closeMovieModal()">
        <div class="senior-modal-box" onclick="event.stopPropagation()">
            <div class="modal-movie-top">
                <img id="modal-poster-img" class="modal-poster" src="" alt="">
                <div class="modal-info">
                    <div id="modal-title-text" class="modal-title">Título</div>
                    <div id="modal-meta-text" class="modal-meta">Ano • Nota</div>
                </div>
            </div>
            <div id="modal-plot-text" class="modal-plot">Sinopse do filme...</div>
            <div class="modal-actions">
                <button class="btn-modal-action btn-modal-save" onclick="saveSelectedMovieVod()">
                    📥 SALVAR NO CINEMA DA TV
                </button>
                <div style="display: flex; gap: 10px;">
                    <button class="btn-modal-action btn-modal-play" style="flex: 1.4; min-height: 52px; font-size: 16px;" onclick="playSelectedMovieNow()">
                        ▶ ASSISTIR NA TV
                    </button>
                    <button class="btn-modal-action btn-modal-close" style="flex: 1; min-height: 52px; font-size: 16px;" onclick="closeMovieModal()">
                        ✕ Fechar
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL SÊNIOR DE DETALHES DA SÉRIE (Temporadas e Episódios) -->
    <div id="series-modal" class="senior-modal" onclick="closeSeriesModal()">
        <div class="series-modal-box" onclick="event.stopPropagation()">
            <button class="modal-close-icon-btn" onclick="closeSeriesModal()" aria-label="Fechar">✕</button>
            
            <div class="modal-series-header">
                <img id="modal-series-poster-img" class="modal-series-poster" src="" alt="">
                <div class="modal-series-info">
                    <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;">
                        <div id="modal-series-title-text" class="modal-series-title">Título da Série</div>
                        <button id="btn-modal-series-fav" class="channel-fav-btn" style="width: 44px; height: 44px; padding: 0;" onclick="toggleCurrentSeriesFavorite(event)" title="Favoritar Série" aria-label="Favoritar série"><svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" aria-hidden="true" style="display:block;pointer-events:none;"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg></button>
                    </div>
                    <div id="modal-series-meta-text" class="modal-series-meta">Ano • Nota</div>
                </div>
            </div>
            
            <div id="modal-series-plot-text" class="modal-series-plot" onclick="toggleSeriesPlot(this)">Sinopse da série...</div>
            
            <div class="series-seasons-section">
                <div class="series-section-label" style="color: var(--gold);">TEMPORADAS:</div>
                <div id="series-seasons-bar" class="season-chips">
                    <div class="season-chip active">Temporadas</div>
                </div>
            </div>

            <div style="display: flex; flex-direction: column; flex: 1; min-height: 0;">
                <div class="series-section-label" style="color: #ffffff; margin-bottom: 6px;">EPISÓDIOS:</div>
                <div id="series-episodes-list" class="episodes-list">
                    <div style="text-align: center; padding: 20px; color: var(--text-muted);">Carregando episódios...</div>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL DE DETALHES DE VOD / VÍDEO SALVO NA TV -->
    <div id="vod-details-modal" class="senior-modal" style="display: none;" onclick="closeVodDetailsModal()">
        <div class="series-modal-box vod-details-modal-box" onclick="event.stopPropagation()">
            <button class="modal-close-icon-btn" onclick="closeVodDetailsModal()" aria-label="Fechar">✕</button>
            
            <div class="vod-modal-hero">
                <div class="vod-modal-hero-top">
                    <img id="vod-modal-poster-img" class="vod-modal-poster" src="" alt="" onerror="this.outerHTML='<div class=\\'vod-modal-poster\\'>🎬</div>'">
                    <div class="vod-modal-badges-col">
                        <div id="vod-modal-badge" class="badge-route badge-direct" style="font-size:12px;">🎬 VÍDEO SALVO</div>
                        <div id="vod-modal-ep-pill" class="vod-modal-ep-pill" style="display: none;">🏷️ S01E05</div>
                    </div>
                </div>
                <div class="vod-modal-title-box">
                    <div id="vod-modal-title-text" class="vod-modal-full-title">Título Completo</div>
                </div>
            </div>

            <div class="vod-modal-details-grid">
                <div class="vod-modal-detail-item">
                    <span class="vod-modal-label">📦 Tamanho no Disco:</span>
                    <strong id="vod-modal-size-val">-- MB</strong>
                </div>
                <div class="vod-modal-detail-item">
                    <span class="vod-modal-label">📅 Gravado em:</span>
                    <strong id="vod-modal-date-val">--</strong>
                </div>
                <div class="vod-modal-detail-item">
                    <span class="vod-modal-label">📺 Status na TV:</span>
                    <strong id="vod-modal-status-val">Pronto no Disco</strong>
                </div>
            </div>
            
            <div class="modal-actions" style="margin-top: 14px; gap: 10px;">
                <button id="btn-vod-modal-play" class="btn-modal-action btn-modal-tv" onclick="playSelectedVodModal()">
                    ▶️ ASSISTIR NA TV
                </button>
                <button id="btn-vod-modal-web" class="btn-modal-action btn-modal-web" onclick="playSelectedVodWeb()">
                    🌐 ASSISTIR NO CELULAR
                </button>
                <button id="btn-vod-modal-delete" class="btn-modal-action" style="background: rgba(239, 68, 68, 0.2); border: 2px solid #ef4444; color: #fca5a5;" onclick="deleteSelectedVodModal()">
                    🗑️ Excluir da Memória da TV
                </button>
            </div>
        </div>
    </div>

    <!-- Modal Auxiliar de Instalação no iOS -->
    <div id="ios-modal" class="ios-modal" onclick="closeIosModal()">
        <div class="ios-modal-box" onclick="event.stopPropagation()">
            <div style="font-size:36px;">📲</div>
            <div style="font-size:22px; font-weight:900; color:#fff;">Como colocar este controle na tela do seu iPhone:</div>
            <div style="font-size:18px; color:var(--text-muted); line-height:1.5; text-align:left;">
                1. Toque no botão <strong>Compartilhar</strong> (o quadrado com uma setinha para cima no rodapé do Safari).<br><br>
                2. Role a lista e toque em <strong>"Adicionar à Tela de Início"</strong>.<br><br>
                3. Toque em <strong>"Adicionar"</strong> no canto superior direito.
            </div>
            <button class="pwa-install-btn" style="width:100%; height:56px; margin-top:8px;" onclick="closeIosModal()">Entendi!</button>
        </div>
    </div>

    <!-- ==================== WEB PLAYER MODAL (DESKTOP & WEB) ==================== -->
    <div id="web-player-modal" class="web-player-modal" role="dialog" aria-modal="true" style="display: none;" onclick="closeWebPlayer()">
        <div class="web-player-container" onclick="event.stopPropagation()">
            <div class="web-player-header">
                <div class="web-player-title-box">
                    <span id="web-player-badge" class="web-player-badge">AO VIVO</span>
                    <h2 id="web-player-title" class="web-player-title">Carregando transmissão...</h2>
                </div>
                <div class="web-player-header-actions">
                    <button id="web-player-transfer-btn" class="btn-player-transfer" onclick="transferCurrentPlayerToTv()" title="Enviar esta transmissão para a TV da sala">
                        📺 Assistir na TV
                    </button>
                    <button class="btn-player-close" onclick="closeWebPlayer()" title="Fechar (Esc)">✕</button>
                </div>
            </div>

            <div class="web-player-video-wrapper">
                <video id="web-player-video" controls autoplay playsinline class="web-player-video"></video>
                <iframe id="web-player-iframe" class="web-player-iframe" style="display:none;" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
                <div id="web-player-spinner" class="web-player-spinner" style="display:none;">
                    <div class="spinner-circle"></div>
                    <span id="web-player-spinner-text">Conectando transmissão...</span>
                </div>
            </div>

            <div class="web-player-footer">
                <div id="web-player-meta" class="web-player-meta">Transmissão em alta resolução direta no navegador</div>
                <div class="web-player-shortcuts-hint">
                    Atalhos: <strong>Espaço</strong> (Play/Pause) • <strong>F</strong> (Tela Cheia) • <strong>M</strong> (Mudo) • <strong>Esc</strong> (Fechar)
                </div>
            </div>
        </div>
    </div>

    <script>
        // Função de Proteção XSS
        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        // Ícones SVG Vetoriais de Alta Precisão (Geometria 100% simétrica sem distorções de fonte do Android)
        const FAV_STAR_SVG = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true" style="display:block;pointer-events:none;"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';
        const FAV_STAR_SVG_LG = '<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor" aria-hidden="true" style="display:block;pointer-events:none;"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';

        // Estado Global com Persistência em LocalStorage (0ms de latência e à prova de reinicialização)
        let currentAppMode = localStorage.getItem('tv_last_mode') || 'cinema'; // Cinema como prioridade do casal!
        let favoritesSet = new Set();
        try {
            const rawFavs = localStorage.getItem('tv_channel_favorites') || localStorage.getItem('tv_favorites');
            if (rawFavs) {
                const parsed = JSON.parse(rawFavs);
                if (Array.isArray(parsed)) favoritesSet = new Set(parsed.map(String));
            }
        } catch (e) {}

        let seriesFavoritesSet = new Set();
        try {
            const rawSeriesFavs = localStorage.getItem('tv_series_favorites');
            if (rawSeriesFavs) {
                const parsed = JSON.parse(rawSeriesFavs);
                if (Array.isArray(parsed)) seriesFavoritesSet = new Set(parsed.map(String));
            }
        } catch (e) {}

        function saveChannelFavorites() {
            try {
                localStorage.setItem('tv_channel_favorites', JSON.stringify(Array.from(favoritesSet)));
            } catch (e) {}
        }

        function saveSeriesFavorites() {
            try {
                localStorage.setItem('tv_series_favorites', JSON.stringify(Array.from(seriesFavoritesSet)));
            } catch (e) {}
        }

        let currentActiveId = null;
        let currentActiveName = '';
        let actionBannerTimer = null;
        let deferredPrompt = null;
        let isMuted = false;
        let isPolling = false;

        // Estado do Cinema VOD
        let vodTasks = [];
        let activeVodTask = null;
        let movieCategory = 'all';
        let movieSearch = '';
        let moviePage = 1;
        let movieTotalPages = 1;
        let movieTotalItems = 0;
        let catalogMovies = [];
        let movieCategoriesList = [];
        let selectedMovie = null;

        // Estado de Séries
        let catalogSeries = [];
        let seriesCategoriesList = [];
        let seriesCategory = 'all';
        let seriesSearch = '';
        let seriesPage = 1;
        let seriesTotalPages = 1;
        let seriesTotalItems = 0;
        let selectedSeries = null;
        let currentSeriesDetails = null;

        // Estado dos Canais Ao Vivo
        let liveCategory = 'favorites';
        let liveSearch = '';
        let livePage = 1;
        let liveTotalPages = 1;
        let liveTotalItems = 0;
        let allChannels = [];
        let liveCategoriesList = [];

        // ==================== ESTADO E CONTROLE DO WEB PLAYER (DESKTOP & WEB) ====================
        let activeMpegtsPlayer = null;
        let activeHlsPlayer = null;
        let currentWebStreamParams = null;

        function closeWebPlayer() {
            const modal = document.getElementById('web-player-modal');
            if (modal) modal.style.display = 'none';

            const video = document.getElementById('web-player-video');
            if (video) {
                video.pause();
                video.removeAttribute('src');
                video.load();
            }

            const iframe = document.getElementById('web-player-iframe');
            if (iframe) {
                iframe.src = '';
                iframe.style.display = 'none';
            }

            if (activeMpegtsPlayer) {
                try {
                    activeMpegtsPlayer.pause();
                    activeMpegtsPlayer.unload();
                    activeMpegtsPlayer.detachMediaElement();
                    activeMpegtsPlayer.destroy();
                } catch (e) {
                    console.warn('Erro ao destruir mpegtsPlayer:', e);
                }
                activeMpegtsPlayer = null;
            }

            if (activeHlsPlayer) {
                try {
                    activeHlsPlayer.destroy();
                } catch (e) {
                    console.warn('Erro ao destruir hlsPlayer:', e);
                }
                activeHlsPlayer = null;
            }

            currentWebStreamParams = null;
        }

        function openWebPlayer({ url, title, badge, type, poster, tvParams }) {
            closeWebPlayer(); // Limpa instâncias anteriores

            const modal = document.getElementById('web-player-modal');
            const titleEl = document.getElementById('web-player-title');
            const badgeEl = document.getElementById('web-player-badge');
            const metaEl = document.getElementById('web-player-meta');
            const video = document.getElementById('web-player-video');
            const iframe = document.getElementById('web-player-iframe');
            const spinner = document.getElementById('web-player-spinner');
            const spinnerText = document.getElementById('web-player-spinner-text');

            if (!modal || !video || !iframe) return;

            currentWebStreamParams = tvParams || { type: 'generic', url: url, name: title };

            if (titleEl) titleEl.innerText = title || 'Reproduzindo';
            if (badgeEl) badgeEl.innerText = badge || 'WEB';
            if (metaEl) {
                metaEl.innerText = (type === 'ts' || type === 'live')
                    ? 'Transmissão MPEG-TS em tempo real direta no navegador'
                    : (type === 'hls' ? 'Transmissão adaptativa HLS direta no navegador' : 'Vídeo HD em alta definição');
            }

            modal.style.display = 'flex';
            if (spinner) {
                spinner.style.display = 'flex';
                if (spinnerText) spinnerText.innerText = 'Conectando transmissão...';
            }

            if (type === 'youtube') {
                video.style.display = 'none';
                iframe.style.display = 'block';
                iframe.src = url;
                if (spinner) spinner.style.display = 'none';
                return;
            }

            iframe.style.display = 'none';
            video.style.display = 'block';
            if (poster) video.poster = poster;

            const onVideoPlaying = () => {
                if (spinner) spinner.style.display = 'none';
            };
            video.onplaying = onVideoPlaying;
            video.onloadeddata = onVideoPlaying;
            video.onerror = () => {
                if (spinner && spinnerText) {
                    spinnerText.innerText = 'Carregando transmissão...';
                }
            };

            if (type === 'ts') {
                if (window.mpegts && mpegts.isSupported()) {
                    try {
                        activeMpegtsPlayer = mpegts.createPlayer({
                            type: 'mpegts',
                            isLive: true,
                            url: url
                        }, {
                            enableWorker: true,
                            lazyLoad: false,
                            liveBufferLatencyChasing: true,
                            enableStashBuffer: false
                        });
                        activeMpegtsPlayer.attachMediaElement(video);
                        activeMpegtsPlayer.load();
                        activeMpegtsPlayer.play().catch(e => {
                            console.warn('Autoplay bloqueado pelo navegador:', e);
                            if (spinner) spinner.style.display = 'none';
                        });
                        activeMpegtsPlayer.on(mpegts.Events.ERROR, (errType, errDetail, errInfo) => {
                            console.warn('mpegts error:', errType, errDetail, errInfo);
                            if (spinner && spinnerText) {
                                spinnerText.innerText = 'Reconectando transmissão ao vivo...';
                            }
                        });
                    } catch (err) {
                        console.error('Falha ao inicializar mpegts.js, fallback para vídeo nativo:', err);
                        video.src = url;
                        video.load();
                        video.play().catch(() => {});
                    }
                } else {
                    video.src = url;
                    video.load();
                    video.play().catch(() => {});
                }
            } else if (type === 'hls') {
                if (window.Hls && Hls.isSupported()) {
                    try {
                        activeHlsPlayer = new Hls({ enableWorker: true });
                        activeHlsPlayer.loadSource(url);
                        activeHlsPlayer.attachMedia(video);
                        activeHlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => {
                            video.play().catch(e => console.warn('Autoplay bloqueado:', e));
                            if (spinner) spinner.style.display = 'none';
                        });
                        activeHlsPlayer.on(Hls.Events.ERROR, (event, data) => {
                            if (data.fatal) {
                                switch (data.type) {
                                    case Hls.ErrorTypes.NETWORK_ERROR:
                                        activeHlsPlayer.startLoad();
                                        break;
                                    case Hls.ErrorTypes.MEDIA_ERROR:
                                        activeHlsPlayer.recoverMediaError();
                                        break;
                                    default:
                                        closeWebPlayer();
                                        break;
                                }
                            }
                        });
                    } catch (err) {
                        video.src = url;
                        video.load();
                        video.play().catch(() => {});
                    }
                } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                    video.src = url;
                    video.load();
                    video.play().catch(() => {});
                } else {
                    video.src = url;
                    video.load();
                    video.play().catch(() => {});
                }
            } else {
                video.src = url;
                video.load();
                video.play().catch(e => {
                    console.warn('Autoplay bloqueado:', e);
                    if (spinner) spinner.style.display = 'none';
                });
            }
        }

        async function transferCurrentPlayerToTv() {
            if (!currentWebStreamParams) {
                showActionBanner(`Nenhuma transmissão ativa para enviar.`, 'warn');
                return;
            }
            triggerHaptic();
            const p = currentWebStreamParams;
            const pin = getAuthPin();

            showActionBanner(`Enviando transmissão para a TV da sala...`);

            try {
                if (p.type === 'movie' || p.type === 'series') {
                    const res = await fetch('/api/switch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                        body: JSON.stringify({ url: p.url, name: p.name, pin: pin })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showActionBanner(`📺 <strong>${escapeHtml(p.name)}</strong> reproduzindo na TV!`, 'success');
                    } else {
                        showActionBanner(`Enviando filme para a TV...`, 'info');
                    }
                } else if (p.type === 'channel' && p.channel) {
                    await playChannel(p.channel);
                } else if (p.type === 'vod' && p.taskId) {
                    await playVodTask(p.taskId, p.title || 'Filme');
                } else if (p.type === 'youtube' && p.url) {
                    const res = await fetch('/api/youtube', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                        body: JSON.stringify({ url: p.url, pin: pin })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showActionBanner(`📺 Vídeo do YouTube reproduzindo na TV!`, 'success');
                    }
                } else if (p.url) {
                    const res = await fetch('/api/switch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                        body: JSON.stringify({ url: p.url, name: p.name || 'Transmissão', pin: pin })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showActionBanner(`📺 Transmissão enviada para a TV!`, 'success');
                    }
                }
            } catch (err) {
                showActionBanner(`Erro ao enviar transmissão para a TV.`, 'warn');
            }
        }

        // Funções de Abertura no Navegador Desktop (Web Player)
        function watchLiveTvOnWeb() {
            triggerHaptic();
            const title = currentActiveName ? `Espelho TV: ${currentActiveName}` : 'Transmissão Ao Vivo da TV';
            openWebPlayer({
                url: '/live.ts',
                title: title,
                badge: 'ESPELHO DA TV',
                type: 'ts',
                tvParams: { type: 'live' }
            });
        }

        function playSelectedMovieOnWeb() {
            if (!selectedMovie) return;
            const movie = selectedMovie;
            closeMovieModal();
            triggerHaptic();

            const streamUrl = movie.url.startsWith('http')
                ? `/api/proxy_stream?url=${encodeURIComponent(movie.url)}`
                : movie.url;

            openWebPlayer({
                url: streamUrl,
                title: movie.name || 'Filme',
                badge: 'FILME',
                type: 'mp4',
                poster: getPosterUrl(movie.poster) || '',
                tvParams: { type: 'movie', url: movie.url, name: movie.name }
            });
        }

        function playEpisodeOnWeb(seasonNum, epIdx) {
            if (!currentSeriesDetails) return;
            const season = (currentSeriesDetails.seasons || []).find(s => String(s.season_number) === String(seasonNum));
            if (!season || !season.episodes || !season.episodes[epIdx]) return;
            const ep = season.episodes[epIdx];

            closeSeriesModal();
            triggerHaptic();

            const fullName = `${currentSeriesDetails.name || 'Série'} - ${ep.title}`;
            const streamUrl = ep.url.startsWith('http')
                ? `/api/proxy_stream?url=${encodeURIComponent(ep.url)}`
                : ep.url;

            openWebPlayer({
                url: streamUrl,
                title: fullName,
                badge: 'SÉRIE',
                type: 'mp4',
                poster: getPosterUrl(currentSeriesDetails.poster) || '',
                tvParams: { type: 'series', url: ep.url, name: fullName }
            });
        }

        function playChannelOnWeb(event, idx) {
            if (event) event.stopPropagation();
            triggerHaptic();

            const ch = filteredChannels[idx];
            if (!ch) return;

            const isHls = ch.url && (ch.url.includes('.m3u8') || ch.url.includes('/hls/'));
            const streamType = isHls ? 'hls' : 'ts';
            const streamUrl = ch.url && ch.url.startsWith('http')
                ? `/api/proxy_stream?url=${encodeURIComponent(ch.url)}`
                : (ch.url || '/live.ts');

            openWebPlayer({
                url: streamUrl,
                title: ch.name || 'Canal Ao Vivo',
                badge: 'CANAL AO VIVO',
                type: streamType,
                poster: ch.logo || '',
                tvParams: { type: 'channel', channel: ch }
            });
        }

        function playVodTaskOnWeb(taskId, title, poster) {
            triggerHaptic();
            const vodUrl = `/vod/${encodeURIComponent(taskId)}/movie.mp4`;
            openWebPlayer({
                url: vodUrl,
                title: title || 'Cinema da TV',
                badge: 'CINEMA HD',
                type: 'mp4',
                poster: poster ? getPosterUrl(poster) : '',
                tvParams: { type: 'vod', taskId: taskId, title: title }
            });
        }

        function playYouTubeOnWeb() {
            triggerHaptic();
            const input = document.getElementById('yt-url-input');
            const raw = input ? input.value.trim() : '';
            if (!raw) {
                showActionBanner(`Cole o link do YouTube no campo acima para assistir.`, 'warn');
                if (input) input.focus();
                return;
            }

            const videoId = extractYouTubeId(raw);
            if (!videoId) {
                showActionBanner(`Link do YouTube inválido. Cole uma URL como https://youtu.be/...`, 'warn');
                return;
            }

            const embedUrl = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1&enablejsapi=1&rel=0`;
            openWebPlayer({
                url: embedUrl,
                title: 'Vídeo do YouTube',
                badge: 'YOUTUBE',
                type: 'youtube',
                tvParams: { type: 'youtube', url: raw }
            });
        }

        function extractYouTubeId(url) {
            if (!url) return null;
            const regExp = /(?:youtu\.be\/|youtube\.com\/(?:embed\/|v\/|watch\?v=|watch\?.+&v=))([\w-]{11})/i;
            const match = url.match(regExp);
            return match ? match[1] : null;
        }

        // Feedback Háptico Suave (35ms)
        function triggerHaptic() {
            if (window.navigator && window.navigator.vibrate) {
                try { window.navigator.vibrate(35); } catch (e) {}
            }
        }

        // Relógio e Data em Português
        function updateClock() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const dateStr = now.toLocaleDateString('pt-BR', { weekday: 'long', day: 'numeric', month: 'long' });
            
            const timeElem = document.getElementById('clock-time');
            const dateElem = document.getElementById('clock-date');
            if (timeElem) timeElem.innerText = timeStr;
            if (dateElem) dateElem.innerText = dateStr;
        }
        setInterval(updateClock, 1000);
        updateClock();

        // Banner Acolhedor de Notificação (Duração de 8 segundos)
        function showActionBanner(msg, type = 'info') {
            const b = document.getElementById('action-banner');
            if (!b) return;
            if (actionBannerTimer) clearTimeout(actionBannerTimer);

            b.innerHTML = msg;
            b.style.display = 'block';
            if (type === 'success') {
                b.style.background = '#14532d';
                b.style.borderColor = '#22c55e';
            } else if (type === 'warn') {
                b.style.background = '#78350f';
                b.style.borderColor = '#facc15';
            } else {
                b.style.background = '#1e3a5f';
                b.style.borderColor = '#38bdf8';
            }

            actionBannerTimer = setTimeout(() => {
                b.style.display = 'none';
            }, 8000);
        }

        // Autenticação Silenciosa (PIN 1233 padrão)
        function getAuthPin() {
            let pin = localStorage.getItem('tv_pin');
            if (!pin) {
                pin = '1233';
                localStorage.setItem('tv_pin', pin);
            }
            return pin;
        }

        // Alternância Principal de Modo: Filmes vs Séries vs TV Ao Vivo
        function switchAppMode(mode) {
            triggerHaptic();
            currentAppMode = mode;
            localStorage.setItem('tv_last_mode', mode);

            const tabCinema = document.getElementById('tab-cinema');
            const tabSeries = document.getElementById('tab-series');
            const tabLive = document.getElementById('tab-live');
            const secCinema = document.getElementById('section-cinema');
            const secSeries = document.getElementById('section-series');
            const secLive = document.getElementById('section-live');

            [tabCinema, tabSeries, tabLive].forEach(t => {
                if (t) { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); }
            });
            [secCinema, secSeries, secLive].forEach(s => {
                if (s) s.style.display = 'none';
            });

            if (mode === 'cinema') {
                if (tabCinema) { tabCinema.classList.add('active'); tabCinema.setAttribute('aria-selected', 'true'); }
                if (secCinema) secCinema.style.display = 'flex';
            } else if (mode === 'series') {
                if (tabSeries) { tabSeries.classList.add('active'); tabSeries.setAttribute('aria-selected', 'true'); }
                if (secSeries) secSeries.style.display = 'flex';
            } else {
                if (tabLive) { tabLive.classList.add('active'); tabLive.setAttribute('aria-selected', 'true'); }
                if (secLive) secLive.style.display = 'flex';
            }
            updatePaginationUI();
        }

        // ==================== NOTIFICAÇÕES PUSH E LÓGICA DO CINEMA VOD ====================

        let knownReadyTasks = null;
        let lastNotifiedYt = null;

        function notifyUser(title, body, url = '/?tab=vod') {
            if (!('Notification' in window)) return;
            if (Notification.permission === 'granted') {
                if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
                    navigator.serviceWorker.controller.postMessage({
                        type: 'SHOW_NOTIFICATION',
                        payload: { title, body, url }
                    });
                } else {
                    try {
                        new Notification(title, {
                            body: body,
                            icon: '/api/app-icon.png',
                            badge: '/api/app-icon.png'
                        });
                    } catch (e) {}
                }
            }
        }

        async function requestNotificationPermission() {
            if ('Notification' in window) {
                try {
                    const perm = await Notification.requestPermission();
                    updateNotificationButtonUI();
                    if (perm === 'granted') {
                        notifyUser('🔔 Notificações Ativadas!', 'Você será avisado quando seus filmes ou vídeos do YouTube estiverem prontos na TV.');
                        showActionBanner('✓ Notificações ativadas com sucesso!', 'success');
                    } else if (perm === 'denied') {
                        showActionBanner('Notificações bloqueadas nas configurações do navegador.', 'warn');
                    }
                } catch (e) {}
            }
        }

        function updateNotificationButtonUI() {
            const btn = document.getElementById('btn-notify-toggle');
            if (!btn || !('Notification' in window)) return;
            if (Notification.permission === 'granted') {
                btn.innerHTML = '✓ Avisos Ativos';
                btn.style.borderColor = 'var(--green)';
                btn.style.color = 'var(--green)';
                btn.style.background = 'rgba(34, 197, 94, 0.15)';
            } else if (Notification.permission === 'denied') {
                btn.innerHTML = '🔕 Avisos Bloqueados';
                btn.style.opacity = '0.6';
            }
        }

        // Estado da Central Universal da TV no Topo
        let activeNowPlayingState = {
            type: 'live', // 'vod', 'yt', 'live'
            title: '',
            subtext: ''
        };

        function renderTvBanner() {
            const bannerLabel = document.getElementById('tv-banner-label-text');
            const bannerDot = document.getElementById('tv-banner-dot');
            const bannerTitle = document.getElementById('tv-banner-title');
            const bannerSubtext = document.getElementById('tv-banner-subtext');
            const btnReturnLive = document.getElementById('btn-banner-return-live');

            if (!bannerTitle) return;

            const readyCount = (vodTasks || []).filter(t => t.status === 'ready').length;
            const savedHint = readyCount > 0 ? `Toque para ver os ${readyCount} vídeos salvos na TV` : 'Toque para abrir a lista de vídeos salvos';

            if (activeNowPlayingState.type === 'vod') {
                if (bannerLabel) bannerLabel.innerText = 'PASSANDO AGORA NO CINEMA DA TV';
                if (bannerDot) {
                    bannerDot.style.background = 'var(--green)';
                    bannerDot.style.boxShadow = '0 0 10px var(--green)';
                }
                bannerTitle.innerText = activeNowPlayingState.title || 'Filme em Reprodução';
                if (bannerSubtext) bannerSubtext.innerText = `Modo Cinema • Controle de Pausa na TV • ${savedHint}`;
                if (btnReturnLive) btnReturnLive.style.display = 'flex';
            } else if (activeNowPlayingState.type === 'yt') {
                if (bannerLabel) bannerLabel.innerText = 'PASSANDO AGORA NO YOUTUBE DA TV';
                if (bannerDot) {
                    bannerDot.style.background = '#ef4444';
                    bannerDot.style.boxShadow = '0 0 10px #ef4444';
                }
                bannerTitle.innerText = activeNowPlayingState.title || 'Vídeo do YouTube';
                if (bannerSubtext) bannerSubtext.innerText = `Vídeo do YouTube • Transmitindo na TV • ${savedHint}`;
                if (btnReturnLive) btnReturnLive.style.display = 'flex';
            } else {
                if (bannerLabel) bannerLabel.innerText = 'TRANSMITINDO NA TV AO VIVO';
                if (bannerDot) {
                    bannerDot.style.background = 'var(--green)';
                    bannerDot.style.boxShadow = '0 0 10px var(--green)';
                }
                bannerTitle.innerText = activeNowPlayingState.title || currentActiveName || 'TV ao Vivo';
                if (bannerSubtext) bannerSubtext.innerText = `Modo TV ao Vivo • ${savedHint}`;
                if (btnReturnLive) btnReturnLive.style.display = 'none';
            }
        }

        // Controle de Abertura e Fechamento da Central de Memória da TV e YouTube
        function openDownloadsModal() {
            triggerHaptic();
            const modal = document.getElementById('downloads-modal');
            if (modal) {
                modal.style.display = 'flex';
                document.body.style.overflow = 'hidden';
            }
        }

        function closeDownloadsModal() {
            const modal = document.getElementById('downloads-modal');
            if (modal) {
                modal.style.display = 'none';
                document.body.style.overflow = '';
            }
        }

        // Carrega Filmes Salvos da TV (/api/vod/status)
        async function pollVodStatus() {
            try {
                const res = await fetch('/api/vod/status', { signal: AbortSignal.timeout(4000) });
                if (!res.ok) return;
                const data = await res.json();

                activeVodTask = data.active_vod;
                vodTasks = data.tasks || [];

                // 1. Atualiza Central Universal da TV no Topo (Modo Cinema VOD)
                if (activeVodTask) {
                    const activeTaskObj = vodTasks.find(t => t.id === activeVodTask);
                    const name = activeTaskObj ? (activeTaskObj.title || activeTaskObj.display_name) : 'Filme em Reprodução';
                    activeNowPlayingState = {
                        type: 'vod',
                        title: name
                    };
                } else if (activeNowPlayingState.type === 'vod') {
                    activeNowPlayingState = {
                        type: 'live',
                        title: currentActiveName || 'TV ao Vivo'
                    };
                }
                renderTvBanner();

                // 2. Atualiza Espaço Livre na TV
                const diskBadge = document.getElementById('vod-disk-badge');
                if (diskBadge && data.disk && data.disk.total_gb > 0) {
                    diskBadge.innerHTML = `💾 Espaço TV: <strong>${data.disk.free_gb} GB livres</strong> de ${data.disk.total_gb} GB`;
                }

                // 3. Atualiza Badge Dinâmica de Mídia no Glance Header
                const headerMediaBadge = document.getElementById('header-media-badge');
                const headerMediaText = document.getElementById('header-media-text');
                const headerMediaIcon = document.getElementById('header-media-icon');

                const inProgressTasks = vodTasks.filter(t => ['processing', 'queued', 'pending', 'downloading', 'converting'].includes(t.status));
                const readyTasks = vodTasks.filter(t => t.status === 'ready');

                if (headerMediaBadge && headerMediaText) {
                    if (inProgressTasks.length > 0) {
                        const topTask = inProgressTasks[0];
                        const prog = topTask.progress ? Math.round(topTask.progress) : 0;
                        headerMediaBadge.className = 'header-media-badge downloading';
                        if (headerMediaIcon) headerMediaIcon.innerText = '📥';
                        headerMediaText.innerHTML = `Baixando (${prog}%)${topTask.speed ? ' • ' + escapeHtml(topTask.speed) : ''}`;
                    } else if (readyTasks.length > 0) {
                        headerMediaBadge.className = 'header-media-badge has-ready';
                        if (headerMediaIcon) headerMediaIcon.innerText = '💾';
                        headerMediaText.innerText = `${readyTasks.length} ${readyTasks.length === 1 ? 'Salvo na TV' : 'Salvos na TV'}`;
                    } else {
                        headerMediaBadge.className = 'header-media-badge';
                        if (headerMediaIcon) headerMediaIcon.innerText = '📥';
                        headerMediaText.innerText = 'Memória da TV & YouTube';
                    }
                }

                // 4. Notificação Automática quando um Filme termina o download e fica pronto
                if (knownReadyTasks === null) {
                    // Inicialização silenciosa para não disparar notificações de downloads antigos
                    knownReadyTasks = new Set(readyTasks.map(t => String(t.id)));
                } else {
                    readyTasks.forEach(task => {
                        const sid = String(task.id);
                        if (!knownReadyTasks.has(sid)) {
                            knownReadyTasks.add(sid);
                            const name = task.title || task.display_name || 'Vídeo';
                            notifyUser('🍿 Conteúdo Pronto na TV!', `"${name}" já terminou de baixar e está pronto para assistir!`, '/?tab=vod');
                            showActionBanner(`🍿 <strong>${escapeHtml(name)}</strong> pronto na TV!`, 'success');
                        }
                    });
                }

                renderVodShelf();
            } catch (err) {}
        }

        // Renderiza a Prateleira de Filmes Prontos e em Download
        function renderVodShelf() {
            const activeSec = document.getElementById('active-downloads-section');
            const activeList = document.getElementById('active-downloads-list');
            const shelf = document.getElementById('vod-shelf-list');

            // Ordena tarefas para que os VODs mais recentes fiquem no topo da pilha
            const sortByRecent = (a, b) => {
                const timeA = a.created_at || a.updated_at || 0;
                const timeB = b.created_at || b.updated_at || 0;
                if (timeA && timeB && timeA !== timeB) return timeB - timeA;
                return 0;
            };

            const readyTasks = vodTasks.filter(t => t.status === 'ready').sort(sortByRecent);
            const inProgressTasks = vodTasks.filter(t => ['processing', 'queued', 'pending', 'downloading', 'converting'].includes(t.status)).sort(sortByRecent);
            const errorTasks = vodTasks.filter(t => t.status === 'error').sort(sortByRecent);

            // 1. Renderiza Fila de Downloads Ativos e Falhas (Largura Total para Máxima Legibilidade)
            if (activeSec && activeList) {
                if (inProgressTasks.length > 0 || errorTasks.length > 0) {
                    activeSec.style.display = 'block';
                    let activeHtml = '';

                    // Downloads em andamento
                    inProgressTasks.forEach(task => {
                        const title = escapeHtml(task.title || task.display_name || 'Vídeo');
                        const safeId = escapeHtml(task.id);
                        const prog = task.progress ? Math.round(task.progress) : 0;
                        const statusMsg = escapeHtml(task.status_msg || 'Baixando e preparando para a TV...');
                        const speed = task.speed ? escapeHtml(task.speed) : '';

                        let routeHtml = '';
                        const urlLower = (task.url || '').toLowerCase();
                        const isYt = urlLower.includes('youtube.com') || urlLower.includes('youtu.be') || title.toLowerCase().includes('youtube');
                        const isProxy = (task.status_msg || '').toLowerCase().includes('residencial') || (task.status_msg || '').toLowerCase().includes('proxy');

                        if (isYt) {
                            routeHtml = `<span class="badge-route badge-yt">▶️ Vídeo do YouTube</span>`;
                        } else if (isProxy) {
                            routeHtml = `<span class="badge-route badge-proxy">📶 Conexão via Proxy Tablet</span>`;
                        } else {
                            routeHtml = `<span class="badge-route badge-direct">⚡ Download Direto Rápido</span>`;
                        }

                        const posterImg = task.poster 
                            ? `<img class="vod-poster-thumb" src="${escapeHtml(task.poster)}" alt="" onerror="this.outerHTML='<div class=\\'vod-poster-thumb\\'>⏳</div>'">`
                            : `<div class="vod-poster-thumb">⏳</div>`;

                        activeHtml += `
                            <div class="vod-movie-card vod-downloading-card" role="region" aria-label="Baixando ${title}">
                                <div class="vod-card-top">
                                    ${posterImg}
                                    <div class="vod-details">
                                        <div class="vod-title">${title}</div>
                                        <div class="vod-badges-row">
                                            ${routeHtml}
                                            <span class="badge-pct">${prog}%</span>
                                        </div>
                                    </div>
                                </div>

                                <div class="vod-status-detail">💬 ${statusMsg}</div>
                                ${speed ? `<div class="vod-speed-detail">🚀 Velocidade: ${speed}</div>` : ''}

                                <div class="vod-progress-bar-wrap" role="progressbar" aria-valuenow="${prog}" aria-valuemin="0" aria-valuemax="100">
                                    <div class="vod-progress-bar-fill" style="width: ${prog}%;"></div>
                                </div>

                                <div style="display: flex; justify-content: flex-end; margin-top: 6px;">
                                    <button class="btn-cancel-vod" onclick="cancelVodTask('${safeId}', '${title}')">
                                        ✕ Cancelar Download
                                    </button>
                                </div>
                            </div>
                        `;
                    });

                    // Falhas
                    errorTasks.forEach(task => {
                        const title = escapeHtml(task.title || task.display_name || 'Vídeo');
                        const safeId = escapeHtml(task.id);
                        const errMsg = escapeHtml(task.error || task.status_msg || 'Erro ao preparar vídeo');
                        activeHtml += `
                            <div class="vod-movie-card" style="border-color: #ef4444; background: #1e1b2e;">
                                <div class="vod-card-top">
                                    <div class="vod-poster-thumb" style="background: rgba(239,68,68,0.2); color: #ef4444;">⚠️</div>
                                    <div class="vod-details">
                                        <div class="vod-title">${title}</div>
                                        <div class="vod-meta" style="color: #f87171;">Não foi possível baixar: ${errMsg}</div>
                                    </div>
                                </div>
                                <div style="display: flex; justify-content: flex-end; margin-top: 10px;">
                                    <button class="btn-delete-vod" onclick="deleteVodTask('${safeId}', '${title}')">
                                        ✕ Remover da Lista
                                    </button>
                                </div>
                            </div>
                        `;
                    });

                    activeList.innerHTML = activeHtml;
                } else {
                    activeSec.style.display = 'none';
                    activeList.innerHTML = '';
                }
            }

            // 2. Renderiza Prateleira de Prontos na Grade de 2 Colunas
            if (!shelf) return;

            if (readyTasks.length === 0) {
                shelf.innerHTML = `
                    <div style="text-align: center; padding: 26px 14px; background: var(--surface); border: 2px dashed var(--border); border-radius: var(--btn-radius); color: var(--text-muted); font-size: 16px; line-height: 1.5; grid-column: 1 / -1;">
                        🍿 Nenhum filme ou série pronto na TV no momento.<br>
                        <strong>Escolha um título no catálogo de Filmes ou Séries, ou cole um link do YouTube acima!</strong>
                    </div>
                `;
                return;
            }

            let html = '';
            readyTasks.forEach(task => {
                const isPlaying = (task.id === activeVodTask);
                let rawTitle = (task.title || task.display_name || 'Vídeo').trim();
                rawTitle = rawTitle.replace(/^@\s*/, '');
                const title = escapeHtml(rawTitle);
                const safeId = escapeHtml(task.id);
                const epInfo = extractSeasonEpisode(rawTitle);
                const epBadge = epInfo ? `<div class="vod-grid-ep-badge">🏷️ ${escapeHtml(epInfo)}</div>` : '';

                const urlLower = (task.url || '').toLowerCase();
                const isYt = urlLower.includes('youtube.com') || urlLower.includes('youtu.be') || title.toLowerCase().includes('youtube');
                const isSeries = Boolean(epInfo) || title.includes(' - ') || title.toLowerCase().includes('temporada') || title.toLowerCase().includes('episodio') || title.toLowerCase().includes('ep ');

                let badgeHtml = '';
                if (isPlaying) {
                    badgeHtml = `<span class="vod-grid-badge live-active">● NO AR NA TV</span>`;
                } else if (isYt) {
                    badgeHtml = `<span class="vod-grid-badge" style="background:#ef4444; border-color:#f87171;">▶️ YOUTUBE</span>`;
                } else if (isSeries) {
                    badgeHtml = `<span class="vod-grid-badge" style="background:#7c3aed; border-color:#a78bfa;">🍿 SÉRIE</span>`;
                } else {
                    badgeHtml = `<span class="vod-grid-badge">🎬 FILME</span>`;
                }

                const posterImg = task.poster 
                    ? `<img class="vod-grid-poster-img" src="${escapeHtml(task.poster)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\\'vod-grid-poster-fallback\\'>🎬</div>'">`
                    : `<div class="vod-grid-poster-fallback">🎬</div>`;

                html += `
                    <div class="vod-grid-card ${isPlaying ? 'active' : ''}" role="region" aria-label="${title}" onclick="openVodDetailsModal('${safeId}')" style="cursor: pointer;">
                        <div class="vod-grid-poster-wrap">
                            ${posterImg}
                            ${badgeHtml}
                        </div>
                        <div class="vod-grid-body">
                            <div class="vod-grid-title" title="${title}">${title}</div>
                            ${epBadge}
                            <button class="btn-vod-grid-play" onclick="event.stopPropagation(); playVodTask('${safeId}', '${title}')" aria-label="Assistir ${title} na TV">
                                ▶️ ASSISTIR
                            </button>
                            <button class="btn-vod-grid-delete" onclick="event.stopPropagation(); deleteVodTask('${safeId}', '${title}')" aria-label="Excluir ${title}">
                                🗑️ Excluir
                            </button>
                        </div>
                    </div>
                `;
            });

            shelf.innerHTML = html;
        }

        // Extrator inteligente de Temporada e Episódio
        function extractSeasonEpisode(title) {
            if (!title) return null;
            let m = title.match(/\bS(\d{1,2})\s*E(\d{1,3})\b/i);
            if (m) return `Temp. ${parseInt(m[1], 10)} • Ep. ${parseInt(m[2], 10)}`;
            m = title.match(/Temporada\s*(\d{1,2}).*?Epis[oó]dio\s*(\d{1,3})/i);
            if (m) return `Temp. ${parseInt(m[1], 10)} • Ep. ${parseInt(m[2], 10)}`;
            m = title.match(/\bT(\d{1,2})\s*E(\d{1,3})\b/i);
            if (m) return `Temp. ${parseInt(m[1], 10)} • Ep. ${parseInt(m[2], 10)}`;
            m = title.match(/\b(?:Epis[oó]dio|Ep\.?)\s*(\d{1,3})\b/i);
            if (m) return `Episódio ${parseInt(m[1], 10)}`;
            return null;
        }

        // ==================== MODAL DE DETALHES DE VOD SALVO ====================
        let selectedVodTaskForModal = null;

        function openVodDetailsModal(taskId) {
            triggerHaptic();
            const task = vodTasks.find(t => String(t.id) === String(taskId));
            if (!task) return;
            selectedVodTaskForModal = task;

            const modal = document.getElementById('vod-details-modal');
            const titleEl = document.getElementById('vod-modal-title-text');
            const badgeEl = document.getElementById('vod-modal-badge');
            const epPillEl = document.getElementById('vod-modal-ep-pill');
            const posterEl = document.getElementById('vod-modal-poster-img');
            const sizeEl = document.getElementById('vod-modal-size-val');
            const dateEl = document.getElementById('vod-modal-date-val');
            const statusEl = document.getElementById('vod-modal-status-val');

            let fullTitle = (task.title || task.display_name || 'Vídeo Salvo').trim();
            fullTitle = fullTitle.replace(/^@\s*/, '');
            if (titleEl) titleEl.innerText = fullTitle;

            const epInfo = extractSeasonEpisode(fullTitle);
            if (epPillEl) {
                if (epInfo) {
                    epPillEl.innerText = `🏷️ ${epInfo}`;
                    epPillEl.style.display = 'inline-flex';
                } else {
                    epPillEl.style.display = 'none';
                }
            }

            const urlLower = (task.url || '').toLowerCase();
            const isYt = urlLower.includes('youtube.com') || urlLower.includes('youtu.be') || fullTitle.toLowerCase().includes('youtube');
            const isSeries = Boolean(epInfo) || fullTitle.includes(' - ') || fullTitle.toLowerCase().includes('temporada') || fullTitle.toLowerCase().includes('episodio') || fullTitle.toLowerCase().includes('ep ');

            if (badgeEl) {
                if (task.id === activeVodTask) {
                    badgeEl.className = 'badge-route badge-direct';
                    badgeEl.style.background = '#22c55e';
                    badgeEl.innerText = '● NO AR NA TV';
                } else if (isYt) {
                    badgeEl.className = 'badge-route badge-yt';
                    badgeEl.style.background = '';
                    badgeEl.innerText = '▶️ VÍDEO DO YOUTUBE';
                } else if (isSeries) {
                    badgeEl.className = 'badge-route';
                    badgeEl.style.background = '#7c3aed';
                    badgeEl.innerText = '🍿 EPISÓDIO DE SÉRIE';
                } else {
                    badgeEl.className = 'badge-route badge-direct';
                    badgeEl.style.background = '';
                    badgeEl.innerText = '🎬 FILME DE CINEMA';
                }
            }

            if (posterEl) {
                if (task.poster) {
                    posterEl.src = task.poster;
                    posterEl.style.display = 'block';
                } else {
                    posterEl.src = '';
                    posterEl.style.display = 'none';
                }
            }

            if (sizeEl) {
                const bytes = task.file_size || task.size || task.file_size_bytes;
                if (task.file_size_str) {
                    sizeEl.innerText = task.file_size_str;
                } else if (bytes && Number(bytes) > 0) {
                    const b = Number(bytes);
                    if (b >= 1024 * 1024 * 1024) {
                        sizeEl.innerText = (b / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
                    } else if (b >= 1024 * 1024) {
                        sizeEl.innerText = (b / (1024 * 1024)).toFixed(1) + ' MB';
                    } else {
                        sizeEl.innerText = (b / 1024).toFixed(0) + ' KB';
                    }
                } else {
                    sizeEl.innerText = 'Armazenado no Flash/USB';
                }
            }

            if (dateEl) {
                const ts = task.created_at || task.updated_at;
                if (ts) {
                    const d = new Date(ts * 1000);
                    dateEl.innerText = d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
                } else {
                    dateEl.innerText = 'Recente';
                }
            }

            if (statusEl) {
                statusEl.innerText = (task.id === activeVodTask) ? 'Transmitindo para a TV' : 'Pronto para reprodução instantânea';
            }

            if (modal) modal.style.display = 'flex';
        }

        function closeVodDetailsModal() {
            triggerHaptic();
            const modal = document.getElementById('vod-details-modal');
            if (modal) modal.style.display = 'none';
            selectedVodTaskForModal = null;
        }

        function playSelectedVodModal() {
            if (!selectedVodTaskForModal) return;
            const task = selectedVodTaskForModal;
            closeVodDetailsModal();
            playVodTask(task.id, task.title || task.display_name);
        }

        function deleteSelectedVodModal() {
            if (!selectedVodTaskForModal) return;
            const task = selectedVodTaskForModal;
            closeVodDetailsModal();
            deleteVodTask(task.id, task.title || task.display_name);
        }

        function playSelectedVodWeb() {
            if (!selectedVodTaskForModal) return;
            const task = selectedVodTaskForModal;
            const title = task.title || task.display_name || 'Vídeo';
            closeVodDetailsModal();
            openWebPlayerVod(task.id, title);
        }

        // Tocar Filme do Cinema VOD na TV
        async function playVodTask(taskId, title) {
            triggerHaptic();
            const pin = getAuthPin();
            showActionBanner(`Iniciando <strong>${escapeHtml(title)}</strong> no Cinema da TV... Aguarde.`);

            try {
                const res = await fetch('/api/vod/play', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ task_id: taskId, pin: pin })
                });
                const data = await res.json();
                if (data.success) {
                    showActionBanner(`✓ <strong>${escapeHtml(title)}</strong> reproduzindo na TV!`, 'success');
                    pollVodStatus();
                } else {
                    showActionBanner(`Não foi possível iniciar o filme.`, 'warn');
                }
            } catch (err) {
                showActionBanner(`Erro ao comunicar com a TV.`, 'warn');
            }
        }

        // Cancelar Download VOD em Andamento
        async function cancelVodTask(taskId, title) {
            triggerHaptic();
            if (!confirm(`Deseja cancelar o download do filme "${title}"?`)) return;
            const pin = getAuthPin();
            showActionBanner(`Cancelando download de <strong>${escapeHtml(title)}</strong>...`);

            try {
                const res = await fetch('/api/vod/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ task_id: taskId, pin: pin })
                });
                showActionBanner(`Download cancelado com sucesso.`, 'info');
                pollVodStatus();
            } catch (err) {
                showActionBanner(`Erro ao cancelar tarefa.`, 'warn');
            }
        }

        // Excluir Filme do VOD
        async function deleteVodTask(taskId, title) {
            triggerHaptic();
            if (!confirm(`Deseja mesmo excluir o filme "${title}" da TV?`)) return;
            const pin = getAuthPin();

            try {
                await fetch('/api/vod/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ task_id: taskId, pin: pin })
                });
                showActionBanner(`Filme removido da TV.`, 'info');
                pollVodStatus();
            } catch (err) {}
        }

        // Voltar do Cinema ou YouTube para a TV Ao Vivo
        async function returnToLiveTv() {
            triggerHaptic();
            activeNowPlayingState = {
                type: 'live',
                title: 'Retornando para a TV Ao Vivo...'
            };
            renderTvBanner();
            showActionBanner(`Retornando para a TV Ao Vivo...`);
            try {
                await fetch('/api/vod/live', { method: 'POST' });
                if (allChannels.length > 0) {
                    const defCh = allChannels.find(c => c.id === 'band-rio') || allChannels[0];
                    playChannel(defCh);
                }
                showActionBanner(`✓ TV Ao Vivo restabelecida!`, 'success');
                switchAppMode('live');
                pollVodStatus();
                pollStatus();
            } catch (err) {}
        }

        // ==================== AÇÕES E CONTROLES DO YOUTUBE ====================

        function onYtInput(val) {
            const clearBtn = document.getElementById('yt-clear-btn');
            if (clearBtn) {
                clearBtn.style.display = (val && val.trim().length > 0) ? 'block' : 'none';
            }
        }

        function clearYtInput() {
            triggerHaptic();
            const input = document.getElementById('yt-url-input');
            if (input) {
                input.value = '';
                input.focus();
            }
            const clearBtn = document.getElementById('yt-clear-btn');
            if (clearBtn) clearBtn.style.display = 'none';
        }

        async function playYouTubeLive() {
            triggerHaptic();
            requestNotificationPermission();
            const input = document.getElementById('yt-url-input');
            const url = input ? input.value.trim() : '';
            if (!url) {
                showActionBanner(`Por favor, cole o link do YouTube no campo acima.`, 'warn');
                if (input) input.focus();
                return;
            }
            const pin = getAuthPin();
            showActionBanner(`Conectando vídeo do YouTube na TV... Aguarde.`);
            try {
                const res = await fetch('/api/youtube', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ url: url, pin: pin })
                });
                const data = await res.json();
                if (data.success) {
                    showActionBanner(`✓ Passando na TV: <strong>${escapeHtml(data.title || 'Vídeo do YouTube')}</strong>!`, 'success');
                    if (input) input.value = '';
                    const clearBtn = document.getElementById('yt-clear-btn');
                    if (clearBtn) clearBtn.style.display = 'none';
                    pollStatus();
                } else {
                    showActionBanner(`Não foi possível reproduzir: ${escapeHtml(data.error || 'Erro no YouTube')}`, 'warn');
                }
            } catch (err) {
                showActionBanner(`Erro de conexão ao enviar vídeo para a TV.`, 'warn');
            }
        }

        async function saveYouTubeVod() {
            triggerHaptic();
            requestNotificationPermission();
            const input = document.getElementById('yt-url-input');
            const url = input ? input.value.trim() : '';
            if (!url) {
                showActionBanner(`Por favor, cole o link do YouTube no campo acima.`, 'warn');
                if (input) input.focus();
                return;
            }
            const pin = getAuthPin();
            showActionBanner(`Preparando vídeo do YouTube para salvar no Cinema da TV...`);
            try {
                const res = await fetch('/api/vod/prepare', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ url: url, title: 'Vídeo do YouTube', pin: pin })
                });
                const data = await res.json();
                if (data.success) {
                    showActionBanner(`✓ Vídeo adicionado à fila do Cinema! Acompanhe o progresso acima.`, 'success');
                    if (input) input.value = '';
                    const clearBtn = document.getElementById('yt-clear-btn');
                    if (clearBtn) clearBtn.style.display = 'none';
                    pollVodStatus();
                } else {
                    showActionBanner(`Não foi possível salvar o vídeo: ${escapeHtml(data.error || 'Erro')}`, 'warn');
                }
            } catch (err) {
                showActionBanner(`Erro de conexão ao salvar vídeo.`, 'warn');
            }
        }

        // ==================== PROCESSAMENTO DE COMPARTILHAMENTO (WEB SHARE TARGET & LAUNCH QUEUE) ====================
        function handleIncomingShareUrl(urlStr) {
            try {
                if (!urlStr) return;
                const parsed = new URL(urlStr, window.location.origin);
                processIncomingShareParams(parsed.searchParams);
            } catch (e) {
                console.error('Erro ao processar URL compartilhada:', e);
            }
        }

        function processIncomingShareParams(params) {
            if (!params) return;
            const rawUrl = params.get('url') || '';
            const rawText = params.get('text') || '';
            const rawTitle = params.get('title') || '';

            let targetUrl = '';
            const combined = (rawUrl + ' ' + rawText + ' ' + rawTitle).trim();
            const urlMatch = combined.match(/https?:\/\/[^\s"'<>]+/);
            if (urlMatch && urlMatch[0]) {
                targetUrl = urlMatch[0].replace(/[),;.]+$/, '');
            }

            if (!targetUrl) return;

            // Limpa query params da barra para evitar reprocessamento ao atualizar a página
            try {
                window.history.replaceState({}, document.title, window.location.pathname);
            } catch (e) {}

            // Abre a Central de Memória da TV & YouTube
            openDownloadsModal();
            triggerHaptic();
            if (typeof requestNotificationPermission === 'function') {
                requestNotificationPermission();
            }

            let videoTitle = rawTitle.trim();
            if (!videoTitle && rawText) {
                const before = rawText.replace(/https?:\/\/[^\s"'<>]+.*/, '').trim();
                if (before) videoTitle = before;
            }
            if (!videoTitle) videoTitle = 'Vídeo do YouTube';
            showActionBanner(`📥 Salvando vídeo no Cinema da TV... Aguarde.`);

            // Preenche input do YouTube como feedback visual
            const input = document.getElementById('yt-url-input');
            if (input) {
                input.value = targetUrl;
                onYtInput(targetUrl);
            }

            // Envia requisição para preparar o VOD em segundo plano sem afetar a TV
            const pin = getAuthPin();
            fetch('/api/vod/prepare', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                body: JSON.stringify({ url: targetUrl, title: videoTitle, pin: pin })
            }).then(r => r.json()).then(data => {
                if (data && data.success) {
                    showActionBanner(`✓ <strong>${escapeHtml(videoTitle)}</strong> adicionado ao Cinema! Você receberá um aviso assim que estiver pronto.`, 'success');
                    pollVodStatus();
                } else {
                    showActionBanner(`Não foi possível salvar: ${escapeHtml((data && data.error) || 'Erro')}`, 'warn');
                }
            }).catch(() => {
                showActionBanner(`Erro ao comunicar com a TV para salvar o vídeo.`, 'warn');
            });
        }

        function checkIncomingShare() {
            try {
                if (window.location.search) {
                    processIncomingShareParams(new URLSearchParams(window.location.search));
                }
            } catch (e) {
                console.error('Erro ao verificar compartilhamento inicial:', e);
            }
        }

        // Suporte à Launch Handling API (quando o app já está em memória no Android)
        if ('launchQueue' in window && typeof window.launchQueue.setConsumer === 'function') {
            window.launchQueue.setConsumer((launchParams) => {
                if (launchParams && launchParams.targetURL) {
                    handleIncomingShareUrl(launchParams.targetURL);
                }
            });
        }

        // Suporte a eventos de navegação popstate/hashchange
        window.addEventListener('popstate', () => {
            if (window.location.search) {
                processIncomingShareParams(new URLSearchParams(window.location.search));
            }
        });


        function sortCategoriesWithAtFirst(cats) {
            if (!Array.isArray(cats)) return [];
            return [...cats].sort((a, b) => {
                const nameA = (a.name || '').trim();
                const nameB = (b.name || '').trim();
                const atA = nameA.startsWith('@') ? 0 : (nameA.includes('@') ? 1 : 2);
                const atB = nameB.startsWith('@') ? 0 : (nameB.includes('@') ? 1 : 2);
                return atA - atB;
            });
        }

        // ==================== CATEGORIAS E CATÁLOGO DE FILMES (CINEMA VOD) ====================

        async function loadMovieCategories() {
            const bar = document.getElementById('movie-categories');
            if (!bar) return;
            try {
                const res = await fetch('/api/catalog/categories?type=movie');
                if (!res.ok) return;
                const cats = await res.json();
                movieCategoriesList = sortCategoriesWithAtFirst(cats || []);
                renderMovieCategories();
            } catch (e) {
                console.error('Erro ao carregar categorias de filmes:', e);
            }
        }

        function renderMovieCategories() {
            const bar = document.getElementById('movie-categories');
            if (!bar) return;
            let html = `
                <button class="cat-chip ${movieCategory === 'all' ? 'active' : ''}" onclick="selectMovieCategory('all')">
                    ✨ Todos os Filmes
                </button>
            `;
            const sortedCats = sortCategoriesWithAtFirst(movieCategoriesList);
            sortedCats.forEach(c => {
                const isActive = (String(movieCategory) === String(c.id));
                html += `
                    <button class="cat-chip ${isActive ? 'active' : ''}" onclick="selectMovieCategory('${escapeHtml(String(c.id))}')">
                        ${escapeHtml(c.name)}
                    </button>
                `;
            });
            bar.innerHTML = html;
        }

        function selectMovieCategory(catId) {
            triggerHaptic();
            movieCategory = catId;
            moviePage = 1;
            renderMovieCategories();
            loadMovieCatalog(true);
        }

        let movieSearchTimeout = null;
        function onMovieSearch(val) {
            movieSearch = (val || '').trim();
            const clearBtn = document.getElementById('movie-search-clear');
            if (clearBtn) clearBtn.style.display = movieSearch ? 'flex' : 'none';

            if (movieSearchTimeout) clearTimeout(movieSearchTimeout);
            movieSearchTimeout = setTimeout(() => {
                moviePage = 1;
                loadMovieCatalog(true);
            }, 300);
        }

        function clearMovieSearch() {
            const inp = document.getElementById('movie-search-input');
            if (inp) inp.value = '';
            onMovieSearch('');
        }

        async function loadMovieCatalog(reset = true) {
            const grid = document.getElementById('catalog-movies-grid');
            const status = document.getElementById('movie-status-bar');
            if (!grid) return;

            if (reset) {
                grid.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">Carregando filmes...</div>';
            }

            try {
                const params = new URLSearchParams({
                    type: 'movie',
                    cat: movieCategory,
                    q: movieSearch,
                    page: moviePage,
                    limit: 48
                });
                const res = await fetch(`/api/catalog/items?${params.toString()}`);
                if (!res.ok) throw new Error('Falha HTTP');
                const data = await res.json();

                movieTotalPages = data.total_pages || 1;
                movieTotalItems = data.total || 0;
                catalogMovies = data.items || [];

                if (status) {
                    status.innerHTML = `<span>🎬 <strong>${movieTotalItems}</strong> filmes</span><span>Página <strong>${moviePage}</strong> de <strong>${movieTotalPages}</strong></span>`;
                }

                renderCatalogMovies();
                updatePaginationUI();
            } catch (e) {
                if (reset) {
                    grid.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--red); font-size: 18px; grid-column: 1 / -1;">Não foi possível carregar os filmes. Toque em Atualizar Conexão.</div>';
                }
            }
        }

        function formatRating(val) {
            if (val === null || val === undefined) return '';
            const s = String(val).trim();
            if (!s) return '';
            const num = parseFloat(s);
            if (isNaN(num) || num <= 0) return '';
            const cleanNum = (num % 1 === 0) ? num.toFixed(0) : num.toFixed(1);
            return `${cleanNum}/10`;
        }

        function getRatingBadgeHtml(val) {
            const r = formatRating(val);
            if (!r) return '';
            const [num, denom] = r.split('/');
            return `<div class="catalog-rating-badge"><span class="rating-num">${escapeHtml(num)}</span><span class="rating-denom">/${escapeHtml(denom)}</span></div>`;
        }

        function getPosterUrl(url) {
            if (!url) return '';
            url = String(url).trim();
            if (url.startsWith('http://')) {
                return `/api/logo?url=${encodeURIComponent(url)}`;
            }
            return url;
        }

        function renderCatalogMovies() {
            const grid = document.getElementById('catalog-movies-grid');
            if (!grid) return;

            if (!catalogMovies || catalogMovies.length === 0) {
                grid.innerHTML = `
                    <div style="text-align: center; padding: 30px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">
                        Nenhum filme encontrado nesta categoria ou busca.
                    </div>
                `;
                return;
            }

            let html = '';
            catalogMovies.forEach((m, idx) => {
                const title = escapeHtml(m.name || 'Filme');
                const year = escapeHtml(m.year || '');
                const ratingBadge = getRatingBadgeHtml(m.rating);
                const posterUrl = getPosterUrl(m.poster);
                const posterImg = posterUrl
                    ? `<img class="catalog-movie-poster" src="${escapeHtml(posterUrl)}" alt="" loading="lazy" onerror="this.onerror=null; this.outerHTML='<div class=\\'catalog-movie-poster\\'>🎬</div>';">`
                    : `<div class="catalog-movie-poster">🎬</div>`;

                html += `
                    <div class="catalog-movie-card" tabindex="0" role="button" aria-label="Ver filme ${title}" onclick="openMovieModal(${idx})">
                        ${posterImg}
                        ${ratingBadge}
                        <div class="catalog-movie-title">${title}</div>
                        <div class="catalog-movie-year">${year}</div>
                    </div>
                `;
            });
            grid.innerHTML = html;
        }

        // Modal de Detalhes do Filme
        function openMovieModal(idx) {
            triggerHaptic();
            selectedMovie = catalogMovies[idx];
            if (!selectedMovie) return;

            document.getElementById('modal-title-text').innerText = selectedMovie.name || 'Filme';
            const movieRatingStr = formatRating(selectedMovie.rating);
            const movieMetaParts = [selectedMovie.year, movieRatingStr].filter(Boolean);
            document.getElementById('modal-meta-text').innerText = movieMetaParts.join(' • ');
            document.getElementById('modal-plot-text').innerText = selectedMovie.plot || 'Filme completo em alta definição disponível para reprodução na TV da sala.';
            
            const pImg = document.getElementById('modal-poster-img');
            if (pImg) pImg.src = getPosterUrl(selectedMovie.poster) || '';

            document.getElementById('movie-modal').style.display = 'flex';
        }

        function closeMovieModal() {
            document.getElementById('movie-modal').style.display = 'none';
            selectedMovie = null;
        }

        // Assistir Filme do Catálogo Agora
        async function playSelectedMovieNow() {
            if (!selectedMovie) return;
            const movie = selectedMovie;
            closeMovieModal();
            triggerHaptic();
            const pin = getAuthPin();

            showActionBanner(`Iniciando <strong>${escapeHtml(movie.name)}</strong> na TV...`);

            try {
                const res = await fetch('/api/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ url: movie.url, name: movie.name, pin: pin })
                });
                const data = await res.json();
                if (data.success) {
                    showActionBanner(`▶️ Reproduzindo <strong>${escapeHtml(movie.name)}</strong> na TV!`, 'success');
                } else {
                    showActionBanner(`Enviando filme para a TV...`, 'info');
                }
            } catch (err) {
                showActionBanner(`Erro ao conectar com a TV.`, 'warn');
            }
        }

        // Salvar Filme no Cinema VOD
        async function saveSelectedMovieVod() {
            if (!selectedMovie) return;
            const movie = selectedMovie;
            closeMovieModal();
            triggerHaptic();
            requestNotificationPermission();
            const pin = getAuthPin();

            showActionBanner(`Baixando <strong>${escapeHtml(movie.name)}</strong> para o Cinema da TV...`);

            try {
                await fetch('/api/vod/prepare', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({
                        url: movie.url,
                        title: movie.name,
                        poster: movie.poster,
                        pin: pin
                    })
                });
                showActionBanner(`✓ Filme adicionado à fila do Cinema!`, 'success');
                pollVodStatus();
            } catch (err) {}
        }

        // ==================== CATEGORIAS E CATÁLOGO DE SÉRIES ====================

        async function loadSeriesCategories() {
            const bar = document.getElementById('series-categories');
            if (!bar) return;
            try {
                const res = await fetch('/api/catalog/categories?type=series');
                if (!res.ok) return;
                const cats = await res.json();
                seriesCategoriesList = sortCategoriesWithAtFirst(cats || []);
                renderSeriesCategories();
            } catch (e) {
                console.error('Erro ao carregar categorias de séries:', e);
            }
        }

        function renderSeriesCategories() {
            const bar = document.getElementById('series-categories');
            if (!bar) return;
            const isFav = (seriesCategory === 'favorites');
            let html = `
                <button class="cat-chip ${isFav ? 'active' : ''}" onclick="selectSeriesCategory('favorites')">
                    ⭐ Favoritas <span class="fav-badge">${seriesFavoritesSet.size}</span>
                </button>
                <button class="cat-chip ${seriesCategory === 'all' ? 'active' : ''}" onclick="selectSeriesCategory('all')">
                    ✨ Todas as Séries
                </button>
            `;
            const sortedCats = sortCategoriesWithAtFirst(seriesCategoriesList);
            sortedCats.forEach(c => {
                const isActive = (String(seriesCategory) === String(c.id));
                html += `
                    <button class="cat-chip ${isActive ? 'active' : ''}" onclick="selectSeriesCategory('${escapeHtml(String(c.id))}')">
                        ${escapeHtml(c.name)}
                    </button>
                `;
            });
            bar.innerHTML = html;
        }

        function selectSeriesCategory(catId) {
            triggerHaptic();
            seriesCategory = catId;
            seriesPage = 1;
            renderSeriesCategories();
            loadSeriesCatalog(true);
        }

        let seriesSearchTimeout = null;
        function onSeriesSearch(val) {
            seriesSearch = (val || '').trim();
            const clearBtn = document.getElementById('series-search-clear');
            if (clearBtn) clearBtn.style.display = seriesSearch ? 'flex' : 'none';

            if (seriesSearchTimeout) clearTimeout(seriesSearchTimeout);
            seriesSearchTimeout = setTimeout(() => {
                seriesPage = 1;
                loadSeriesCatalog(true);
            }, 300);
        }

        function clearSeriesSearch() {
            const inp = document.getElementById('series-search-input');
            if (inp) inp.value = '';
            onSeriesSearch('');
        }

        async function loadSeriesCatalog(reset = true) {
            const grid = document.getElementById('catalog-series-grid');
            const status = document.getElementById('series-status-bar');
            if (!grid) return;

            if (reset) {
                grid.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">Carregando séries...</div>';
            }

            try {
                const params = new URLSearchParams({
                    type: 'series',
                    cat: seriesCategory,
                    q: seriesSearch,
                    page: seriesPage,
                    limit: 48
                });
                const res = await fetch(`/api/catalog/items?${params.toString()}`);
                if (!res.ok) throw new Error('Falha HTTP');
                const data = await res.json();

                seriesTotalPages = data.total_pages || 1;
                seriesTotalItems = data.total || 0;
                catalogSeries = data.items || [];

                if (status) {
                    status.innerHTML = `<span>🍿 <strong>${seriesTotalItems}</strong> séries</span><span>Página <strong>${seriesPage}</strong> de <strong>${seriesTotalPages}</strong></span>`;
                }

                renderCatalogSeries();
                updatePaginationUI();
            } catch (e) {
                if (reset) {
                    grid.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--red); font-size: 18px; grid-column: 1 / -1;">Não foi possível carregar as séries. Toque em Atualizar Conexão.</div>';
                }
            }
        }

        function renderCatalogSeries() {
            const grid = document.getElementById('catalog-series-grid');
            if (!grid) return;

            if (!catalogSeries || catalogSeries.length === 0) {
                if (seriesCategory === 'favorites') {
                    grid.innerHTML = `
                        <div style="text-align: center; padding: 36px 14px; color: var(--text-muted); font-size: 18px; line-height: 1.5; grid-column: 1 / -1; background: var(--surface); border: 2px dashed var(--border); border-radius: var(--btn-radius);">
                            ⭐ Nenhuma série favoritada ainda.<br>
                            <span style="font-size: 15px; color: var(--gold); margin-top: 6px; display: inline-block;">Toque na estrela (★) de qualquer série do catálogo para adicioná-la aos seus favoritos!</span>
                        </div>
                    `;
                } else {
                    grid.innerHTML = `
                        <div style="text-align: center; padding: 30px; color: var(--text-muted); font-size: 18px; grid-column: 1 / -1;">
                            Nenhuma série encontrada nesta categoria ou busca.
                        </div>
                    `;
                }
                return;
            }

            let html = '';
            catalogSeries.forEach((s, idx) => {
                const title = escapeHtml(s.name || 'Série');
                const year = escapeHtml(s.year || '');
                const ratingBadge = getRatingBadgeHtml(s.rating);
                const sIdStr = String(s.id);
                const isFav = seriesFavoritesSet.has(sIdStr);
                const posterUrl = getPosterUrl(s.poster);
                const posterImg = posterUrl
                    ? `<img class="catalog-movie-poster" src="${escapeHtml(posterUrl)}" alt="" loading="lazy" onerror="this.onerror=null; this.outerHTML='<div class=\\'catalog-movie-poster\\'>🍿</div>';">`
                    : `<div class="catalog-movie-poster">🍿</div>`;

                html += `
                    <div class="catalog-movie-card" tabindex="0" role="button" aria-label="Ver série ${title}" onclick="openSeriesModal(${idx})">
                        ${posterImg}
                        ${ratingBadge}
                        <button class="channel-fav-btn series-fav-star ${isFav ? 'is-fav' : ''}" 
                                title="Favoritar Série" 
                                aria-label="${isFav ? 'Remover série dos favoritos' : 'Adicionar série aos favoritos'}"
                                onclick="toggleSeriesFavorite(event, '${escapeHtml(sIdStr)}')">${FAV_STAR_SVG}</button>
                        <div class="catalog-movie-title">${title}</div>
                        <div class="catalog-movie-year">${year}</div>
                    </div>
                `;
            });
            grid.innerHTML = html;
        }

        async function toggleSeriesFavorite(e, id) {
            if (e) e.stopPropagation();
            triggerHaptic();
            const idStr = String(id);

            if (seriesFavoritesSet.has(idStr)) {
                seriesFavoritesSet.delete(idStr);
            } else {
                seriesFavoritesSet.add(idStr);
            }
            saveSeriesFavorites();
            renderSeriesCategories();
            renderCatalogSeries();

            // Atualiza estrela no modal se aberto para esta série
            if (selectedSeries && String(selectedSeries.id) === idStr) {
                const favBtn = document.getElementById('btn-modal-series-fav');
                if (favBtn) {
                    favBtn.classList.toggle('is-fav', seriesFavoritesSet.has(idStr));
                }
            }

            try {
                await fetch('/api/favorites/series/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: idStr })
                });
            } catch (err) {}

            if (seriesCategory === 'favorites') {
                loadSeriesCatalog(true);
            }
        }

        function toggleCurrentSeriesFavorite(e) {
            if (e) e.stopPropagation();
            if (!selectedSeries) return;
            toggleSeriesFavorite(e, selectedSeries.id);
        }

        function toggleSeriesPlot(el) {
            if (el) el.classList.toggle('expanded');
        }

        // Modal de Detalhes da Série (Temporadas e Episódios)
        async function openSeriesModal(idx) {
            triggerHaptic();
            selectedSeries = catalogSeries[idx];
            if (!selectedSeries) return;

            const isFav = seriesFavoritesSet.has(String(selectedSeries.id));
            const favBtn = document.getElementById('btn-modal-series-fav');
            if (favBtn) {
                favBtn.classList.toggle('is-fav', isFav);
            }

            document.getElementById('modal-series-title-text').innerText = selectedSeries.name || 'Série';
            const seriesRatingStr = formatRating(selectedSeries.rating);
            const seriesMetaParts = [selectedSeries.year, seriesRatingStr].filter(Boolean);
            document.getElementById('modal-series-meta-text').innerText = seriesMetaParts.join(' • ');
            const plotEl = document.getElementById('modal-series-plot-text');
            if (plotEl) {
                plotEl.innerText = selectedSeries.plot || 'Carregando sinopse e episódios...';
                plotEl.classList.remove('expanded');
            }
            
            const pImg = document.getElementById('modal-series-poster-img');
            if (pImg) pImg.src = getPosterUrl(selectedSeries.poster) || '';

            const seasonsBar = document.getElementById('series-seasons-bar');
            const epList = document.getElementById('series-episodes-list');
            if (seasonsBar) seasonsBar.innerHTML = '<div class="season-chip active">Carregando temporadas...</div>';
            if (epList) epList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-muted);">Buscando episódios...</div>';

            document.getElementById('series-modal').style.display = 'flex';

            try {
                const res = await fetch(`/api/catalog/series_info?id=${selectedSeries.id}&_t=${Date.now()}`, { cache: 'no-cache' });
                const data = await res.json();
                currentSeriesDetails = data;

                const finalRating = formatRating(data.rating || selectedSeries.rating);
                const finalMeta = [selectedSeries.year, finalRating, data.genre].filter(Boolean);
                document.getElementById('modal-series-meta-text').innerText = finalMeta.join(' • ');

                if (data.cover && pImg) {
                    pImg.src = getPosterUrl(data.cover);
                }

                if (data.plot && plotEl) {
                    plotEl.innerText = data.plot;
                }

                if (!data.seasons || !data.seasons.length) {
                    if (seasonsBar) seasonsBar.innerHTML = '<div class="season-chip active">Temporada 1</div>';
                    const errorMsg = data.error ? `Erro: ${escapeHtml(data.error)}` : 'Nenhum episódio cadastrado nesta série.';
                    if (epList) epList.innerHTML = `<div style="text-align: center; padding: 20px; color: var(--text-muted);">${errorMsg}</div>`;
                    return;
                }

                // Renderiza Temporadas
                let sHtml = '';
                data.seasons.forEach((s, sIdx) => {
                    sHtml += `
                        <div class="season-chip ${sIdx === 0 ? 'active' : ''}" onclick="selectSeason('${s.season_number}', this)">
                            ${escapeHtml(s.name || 'Temporada ' + s.season_number)}
                        </div>
                    `;
                });
                if (seasonsBar) seasonsBar.innerHTML = sHtml;

                // Renderiza episódios da 1ª temporada
                renderEpisodesForSeason(data.seasons[0].season_number);
            } catch (err) {
                if (epList) epList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--red);">Erro ao carregar episódios. Tente novamente.</div>';
            }
        }

        function selectSeason(seasonNum, el) {
            triggerHaptic();
            document.querySelectorAll('#series-seasons-bar .season-chip').forEach(c => c.classList.remove('active'));
            if (el) el.classList.add('active');
            renderEpisodesForSeason(seasonNum);
        }

        function renderEpisodesForSeason(seasonNum) {
            const epList = document.getElementById('series-episodes-list');
            if (!epList || !currentSeriesDetails) return;

            const season = (currentSeriesDetails.seasons || []).find(s => String(s.season_number) === String(seasonNum));
            if (!season || !season.episodes || !season.episodes.length) {
                epList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-muted);">Nenhum episódio disponível nesta temporada.</div>';
                return;
            }

            let html = '';
            season.episodes.forEach((ep, epIdx) => {
                const epTitle = escapeHtml(ep.title || `Episódio ${ep.episode_num || epIdx + 1}`);
                const epNumBadge = ep.episode_num ? `EP ${ep.episode_num}` : `EP ${epIdx + 1}`;
                let epDur = (ep.duration && ep.duration !== '00:00:00' && ep.duration !== '0') ? ep.duration : 'HD';
                if (typeof epDur === 'string' && epDur.startsWith('00:')) epDur = epDur.substring(3);

                html += `
                    <div class="episode-card">
                        <div class="episode-main-info">
                            <div class="episode-badge">${epNumBadge}</div>
                            <div class="episode-title-group">
                                <div class="episode-title">${epTitle}</div>
                                <div class="episode-duration">⏱ ${escapeHtml(epDur)}</div>
                            </div>
                        </div>
                        <div class="episode-actions-row">
                            <button type="button" class="btn-ep-save-senior" onclick="event.stopPropagation(); saveEpisodeVod('${seasonNum}', ${epIdx})">
                                <span>📥 Salvar Episódio</span>
                            </button>
                            <button type="button" class="btn-ep-play-senior" onclick="event.stopPropagation(); playEpisode('${seasonNum}', ${epIdx})" title="Assistir agora na TV" aria-label="Assistir episódio agora na TV">
                                <span>▶</span>
                            </button>
                        </div>
                    </div>
                `;
            });
            epList.innerHTML = html;
        }

        async function playEpisode(seasonNum, epIdx) {
            if (!currentSeriesDetails) return;
            const season = (currentSeriesDetails.seasons || []).find(s => String(s.season_number) === String(seasonNum));
            if (!season || !season.episodes || !season.episodes[epIdx]) return;
            const ep = season.episodes[epIdx];

            const seriesName = currentSeriesDetails.name || 'Série';
            const epTitle = ep.title || `Episódio ${ep.episode_num || epIdx + 1}`;
            const fullName = `${seriesName} - ${epTitle}`;

            closeSeriesModal();
            triggerHaptic();
            const pin = getAuthPin();

            showActionBanner(`Iniciando <strong>${escapeHtml(fullName)}</strong> na TV...`);

            try {
                const res = await fetch('/api/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ url: ep.url, name: fullName, pin: pin })
                });
                const data = await res.json();
                if (data.success) {
                    showActionBanner(`▶️ Reproduzindo <strong>${escapeHtml(fullName)}</strong> na TV!`, 'success');
                } else {
                    showActionBanner(`Enviando episódio para a TV...`, 'info');
                }
            } catch (err) {
                showActionBanner(`Erro ao conectar com a TV.`, 'warn');
            }
        }

        async function saveEpisodeVod(seasonNum, epIdx) {
            if (!currentSeriesDetails) return;
            const season = (currentSeriesDetails.seasons || []).find(s => String(s.season_number) === String(seasonNum));
            if (!season || !season.episodes || !season.episodes[epIdx]) return;
            const ep = season.episodes[epIdx];

            const seriesName = currentSeriesDetails.name || 'Série';
            const seriesCover = currentSeriesDetails.cover || '';
            const epTitle = ep.title || `Episódio ${ep.episode_num || epIdx + 1}`;
            const fullName = `${seriesName} - ${epTitle}`;

            closeSeriesModal();
            triggerHaptic();
            requestNotificationPermission();
            const pin = getAuthPin();

            showActionBanner(`Baixando <strong>${escapeHtml(fullName)}</strong> para o Cinema da TV...`);

            try {
                await fetch('/api/vod/prepare', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({
                        url: ep.url,
                        title: fullName,
                        poster: seriesCover,
                        pin: pin
                    })
                });
                showActionBanner(`✓ Episódio adicionado à fila do Cinema!`, 'success');
                pollVodStatus();
            } catch (err) {}
        }

        function closeSeriesModal() {
            const m = document.getElementById('series-modal');
            if (m) m.style.display = 'none';
            selectedSeries = null;
            currentSeriesDetails = null;
        }

        // ==================== CATEGORIAS E CATÁLOGO DE CANAIS AO VIVO ====================

        async function loadLiveCategories() {
            const bar = document.getElementById('live-categories');
            if (!bar) return;
            try {
                const res = await fetch('/api/catalog/categories?type=live');
                if (!res.ok) return;
                const cats = await res.json();
                liveCategoriesList = cats || [];
                renderLiveCategories();
            } catch (e) {
                console.error('Erro ao carregar categorias de canais:', e);
            }
        }

        function renderLiveCategories() {
            const bar = document.getElementById('live-categories');
            if (!bar) return;
            let html = `
                <button class="cat-chip ${liveCategory === 'favorites' ? 'active' : ''}" onclick="selectLiveCategory('favorites')">
                    ⭐ Favoritos <span class="fav-badge">${favoritesSet.size}</span>
                </button>
                <button class="cat-chip ${liveCategory === 'all' ? 'active' : ''}" onclick="selectLiveCategory('all')">
                    📺 Todos os Canais
                </button>
                <button class="cat-chip ${liveCategory === 'eco' ? 'active' : ''}" onclick="selectLiveCategory('eco')" title="Canais compatíveis com transmissão direta">
                    ⚡ Modo Econômico
                </button>
            `;
            liveCategoriesList.forEach(c => {
                const isActive = (String(liveCategory) === String(c.id));
                html += `
                    <button class="cat-chip ${isActive ? 'active' : ''}" onclick="selectLiveCategory('${escapeHtml(String(c.id))}')">
                        ${escapeHtml(c.name)}
                    </button>
                `;
            });
            bar.innerHTML = html;
        }

        function selectLiveCategory(catId) {
            triggerHaptic();
            liveCategory = catId;
            livePage = 1;
            renderLiveCategories();
            loadLiveCatalog(true);
        }

        let liveSearchTimeout = null;
        function onChannelSearch(val) {
            liveSearch = (val || '').trim();
            const clearBtn = document.getElementById('channel-search-clear');
            if (clearBtn) clearBtn.style.display = liveSearch ? 'flex' : 'none';

            if (liveSearchTimeout) clearTimeout(liveSearchTimeout);
            liveSearchTimeout = setTimeout(() => {
                livePage = 1;
                loadLiveCatalog(true);
            }, 300);
        }

        function clearChannelSearch() {
            const inp = document.getElementById('channel-search-input');
            if (inp) inp.value = '';
            onChannelSearch('');
        }

        async function loadLiveCatalog(reset = true) {
            const container = document.getElementById('channels-list');
            const status = document.getElementById('live-status-bar');
            if (!container) return;

            if (reset) {
                container.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--text-muted); font-size: 18px;">Carregando canais da TV...</div>';
            }

            try {
                const params = new URLSearchParams({
                    type: 'live',
                    cat: liveCategory,
                    q: liveSearch,
                    page: livePage,
                    limit: 48
                });
                const res = await fetch(`/api/catalog/items?${params.toString()}`);
                if (!res.ok) throw new Error('Falha HTTP');
                const data = await res.json();

                liveTotalPages = data.total_pages || 1;
                liveTotalItems = data.total || 0;
                allChannels = data.items || [];

                if (status) {
                    status.innerHTML = `<span>📺 <strong>${liveTotalItems}</strong> canais</span><span>Página <strong>${livePage}</strong> de <strong>${liveTotalPages}</strong></span>`;
                }

                renderChannels();
                updatePaginationUI();
            } catch (e) {
                if (reset) {
                    container.innerHTML = '<div style="text-align: center; padding: 40px 10px; color: var(--red); font-size: 18px;">Erro ao carregar canais. Toque em Atualizar Conexão.</div>';
                }
            }
        }

        // Renderiza Canais com Sanitização XSS e Acessibilidade
        function renderChannels() {
            const container = document.getElementById('channels-list');
            if (!container) return;
            container.innerHTML = '';

            if (!allChannels || allChannels.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 40px 10px; color: var(--text-muted); font-size: 18px;">
                        Nenhum canal encontrado nesta categoria ou busca.
                    </div>
                `;
                return;
            }

            allChannels.forEach((ch, idx) => {
                const card = document.createElement('div');
                const isCurrent = (String(ch.id) === String(currentActiveId));
                card.className = `senior-channel-card ${isCurrent ? 'active' : ''}`;
                card.id = `channel-card-${ch.id}`;
                card.tabIndex = 0;
                card.setAttribute('role', 'button');
                card.setAttribute('aria-label', `Assistir canal ${ch.name}`);

                const channelNumber = (livePage - 1) * 48 + idx + 1;
                const safeName = escapeHtml(ch.name);
                const safeCategory = escapeHtml(ch.category || (ch.is_eco ? '⚡ Modo Econômico' : 'Canal ao Vivo'));
                const isFav = favoritesSet.has(String(ch.id));

                let logoHtml = `<span class="channel-logo-fallback">${escapeHtml(ch.logo && !ch.logo.startsWith('http') ? ch.logo : '📺')}</span>`;
                if (ch.logo && ch.logo.startsWith('http')) {
                    const logoSrc = `/api/logo?url=${encodeURIComponent(ch.logo)}`;
                    logoHtml = `<img class="channel-logo-img" src="${escapeHtml(logoSrc)}" alt="" loading="lazy" onerror="this.outerHTML='<span class=\'channel-logo-fallback\'>📺</span>'">`;
                }

                card.innerHTML = `
                    <div class="active-live-badge">● NO AR NA TV</div>
                    <div class="channel-num-badge">${channelNumber}</div>
                    <div class="channel-logo-wrap">${logoHtml}</div>
                    <div class="channel-info">
                        <div class="channel-name">${safeName}</div>
                        <div class="channel-category">${safeCategory}</div>
                    </div>
                    <div class="channel-actions-row">
                        <button class="channel-fav-btn ${isFav ? 'is-fav' : ''}" 
                                title="Favoritar" 
                                aria-label="${isFav ? 'Remover canal dos favoritos' : 'Adicionar canal aos favoritos'}"
                                onclick="toggleFavorite(event, '${escapeHtml(String(ch.id))}')">${FAV_STAR_SVG_LG}</button>
                    </div>
                `;

                card.onclick = (e) => {
                    if (e.target.closest('.channel-fav-btn')) return;
                    playChannel(ch);
                };

                card.onkeydown = (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        playChannel(ch);
                    }
                };

                container.appendChild(card);
            });
        }

        // Troca de Canal ao Vivo
        async function playChannel(ch) {
            triggerHaptic();
            const pin = getAuthPin();

            showActionBanner(`Sintonizando <strong>${escapeHtml(ch.name)}</strong>... A imagem vai entrar na TV.`);

            currentActiveId = ch.id;
            currentActiveName = ch.name;
            document.querySelectorAll('.senior-channel-card').forEach(c => c.classList.remove('active'));
            const cElem = document.getElementById(`channel-card-${ch.id}`);
            if (cElem) cElem.classList.add('active');

            const nowTitle = document.getElementById('now-playing-title');
            if (nowTitle) nowTitle.innerText = ch.name;

            try {
                const res = await fetch('/api/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({
                        channel_id: ch.id,
                        url: ch.url,
                        name: ch.name,
                        pin: pin,
                        is_eco: true
                    })
                });

                if (res.status === 401) {
                    showActionBanner(`PIN de segurança incorreto.`, 'warn');
                    return;
                }

                const data = await res.json();
                if (data.success) {
                    showActionBanner(`✓ <strong>${escapeHtml(ch.name)}</strong> sintonizado na TV!`, 'success');
                } else {
                    showActionBanner(`Tentando reconectar sinal de <strong>${escapeHtml(ch.name)}</strong>...`, 'warn');
                }
            } catch (err) {
                showActionBanner(`Sinal instável. Tentando reconectar automaticamente...`, 'warn');
            }
        }

        // Alternar Favorito
        async function toggleFavorite(e, id) {
            e.stopPropagation();
            triggerHaptic();
            const idStr = String(id);

            if (favoritesSet.has(idStr)) {
                favoritesSet.delete(idStr);
            } else {
                favoritesSet.add(idStr);
            }
            saveChannelFavorites();
            renderChannels();
            renderLiveCategories();

            try {
                await fetch('/api/favorites/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: idStr })
                });
            } catch (err) {}

            if (liveCategory === 'favorites') {
                loadLiveCatalog(true);
            }
        }

        // Controles Físicos do Polegar: Canal Anterior e Próximo
        function nextChannel() {
            triggerHaptic();
            if (!allChannels.length) return;
            let curIdx = allChannels.findIndex(c => String(c.id) === String(currentActiveId));
            let nextIdx = (curIdx + 1) % allChannels.length;
            playChannel(allChannels[nextIdx]);
        }

        function prevChannel() {
            triggerHaptic();
            if (!allChannels.length) return;
            let curIdx = allChannels.findIndex(c => String(c.id) === String(currentActiveId));
            let prevIdx = (curIdx - 1 + allChannels.length) % allChannels.length;
            playChannel(allChannels[prevIdx]);
        }

        // ==================== NAVEGAÇÃO, PAGINAÇÃO E VOLTAR AO TOPO ====================

        function prevPage() {
            triggerHaptic();
            if (currentAppMode === 'cinema') {
                if (moviePage > 1) {
                    moviePage--;
                    loadMovieCatalog(false);
                    scrollToCatalogTop();
                }
            } else if (currentAppMode === 'series') {
                if (seriesPage > 1) {
                    seriesPage--;
                    loadSeriesCatalog(false);
                    scrollToCatalogTop();
                }
            } else {
                if (livePage > 1) {
                    livePage--;
                    loadLiveCatalog(false);
                    scrollToCatalogTop();
                }
            }
        }

        function nextPage() {
            triggerHaptic();
            if (currentAppMode === 'cinema') {
                if (moviePage < movieTotalPages) {
                    moviePage++;
                    loadMovieCatalog(false);
                    scrollToCatalogTop();
                }
            } else if (currentAppMode === 'series') {
                if (seriesPage < seriesTotalPages) {
                    seriesPage++;
                    loadSeriesCatalog(false);
                    scrollToCatalogTop();
                }
            } else {
                if (livePage < liveTotalPages) {
                    livePage++;
                    loadLiveCatalog(false);
                    scrollToCatalogTop();
                }
            }
        }

        function scrollToTop() {
            triggerHaptic();
            const bar = document.querySelector('.fixed-remote-bar');
            if (bar) bar.classList.remove('hidden-down');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function scrollToCatalogTop() {
            let el = null;
            if (currentAppMode === 'cinema') {
                el = document.getElementById('movie-categories');
            } else if (currentAppMode === 'series') {
                el = document.getElementById('series-categories');
            } else {
                el = document.getElementById('live-categories');
            }
            if (el) {
                const bar = document.querySelector('.fixed-remote-bar');
                if (bar) bar.classList.remove('hidden-down');
                const top = el.getBoundingClientRect().top + window.pageYOffset - 90;
                window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
            }
        }

        function updatePaginationUI() {
            const prevBtn = document.getElementById('btn-remote-prev-page');
            const nextBtn = document.getElementById('btn-remote-next-page');
            if (!prevBtn || !nextBtn) return;

            let curPage = livePage;
            let totalP = liveTotalPages;
            if (currentAppMode === 'cinema') {
                curPage = moviePage;
                totalP = movieTotalPages;
            } else if (currentAppMode === 'series') {
                curPage = seriesPage;
                totalP = seriesTotalPages;
            }

            if (curPage <= 1) {
                prevBtn.disabled = true;
                prevBtn.style.opacity = '0.35';
                prevBtn.style.pointerEvents = 'none';
            } else {
                prevBtn.disabled = false;
                prevBtn.style.opacity = '1';
                prevBtn.style.pointerEvents = 'auto';
            }

            if (curPage >= totalP) {
                nextBtn.disabled = true;
                nextBtn.style.opacity = '0.35';
                nextBtn.style.pointerEvents = 'none';
            } else {
                nextBtn.disabled = false;
                nextBtn.style.opacity = '1';
                nextBtn.style.pointerEvents = 'auto';
            }
        }

        // Funções de Volume e Mudo (Mantidas para compatibilidade de atalhos e testes)
        function adjustVolume(delta) {
            triggerHaptic();
            const actionText = delta > 0 ? "Aumentando o volume da TV..." : "Diminuindo o volume da TV...";
            showActionBanner(actionText);
        }

        function toggleMute() {
            triggerHaptic();
            isMuted = !isMuted;
            showActionBanner(isMuted ? "🔇 Som da TV no MUDO" : "🔊 Volume Ativado", isMuted ? 'warn' : 'info');
        }

        // Ocultar barra fixa do controle quando o teclado estiver aberto
        function setupKeyboardListeners() {
            const addOpen = () => document.body.classList.add('keyboard-open');
            const removeOpen = () => {
                setTimeout(() => {
                    const active = document.activeElement;
                    if (!active || (active.tagName !== 'INPUT' && active.tagName !== 'TEXTAREA')) {
                        document.body.classList.remove('keyboard-open');
                    }
                }, 150);
            };

            document.querySelectorAll('input, textarea').forEach(el => {
                el.addEventListener('focus', addOpen);
                el.addEventListener('blur', removeOpen);
            });

            if (window.visualViewport) {
                window.visualViewport.addEventListener('resize', () => {
                    if (window.visualViewport.height < window.innerHeight * 0.75) {
                        document.body.classList.add('keyboard-open');
                    } else {
                        const active = document.activeElement;
                        if (!active || (active.tagName !== 'INPUT' && active.tagName !== 'TEXTAREA')) {
                            document.body.classList.remove('keyboard-open');
                        }
                    }
                });
            }
        }

        // Barra fixa inferior: Mantida permanentemente visível (sem ocultação no scroll)
        function setupScrollListeners() {
            const bar = document.querySelector('.fixed-remote-bar');
            if (bar) bar.classList.remove('hidden-down');
        }

        // Botão Unificado Principal: Voltar para TV Ao Vivo E Atualizar Conexão da TV
        async function unifiedLiveAndReconnectTv() {
            triggerHaptic();
            const pin = getAuthPin();
            showActionBanner("📺 Sintonizando TV Ao Vivo e atualizando sinal... Aguarde.");
            try {
                // 1. Desliga VOD / YouTube se ativo e volta ao vivo no servidor
                const vodLivePromise = fetch('/api/vod/live', { method: 'POST' }).catch(() => {});
                
                // 2. Dispara reconexão USB / flush limpo na TV
                const reconnectPromise = fetch('/api/usb/reconnect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': pin },
                    body: JSON.stringify({ pin: pin })
                }).catch(() => {});

                // Sintoniza canal padrão na grade ao vivo
                if (allChannels && allChannels.length > 0) {
                    const defCh = allChannels.find(c => c.id === 'band-rio') || allChannels[0];
                    playChannel(defCh);
                }

                await Promise.allSettled([vodLivePromise, reconnectPromise]);

                showActionBanner("✓ TV Ao Vivo e sinal restabelecidos!", "success");
                switchAppMode('live');
                pollVodStatus();
                pollStatus();
            } catch (err) {
                showActionBanner("Comando enviado para a TV.", "info");
            }
        }

        async function reconnectTv() {
            return unifiedLiveAndReconnectTv();
        }

        // Polling de Status Geral da TV (/api/status) com AbortSignal
        async function pollStatus() {
            if (isPolling) return;
            isPolling = true;

            try {
                const res = await fetch('/api/status', { signal: AbortSignal.timeout(3500) });
                if (!res.ok) {
                    markOfflineStatus();
                    return;
                }
                const data = await res.json();

                const statusBadge = document.getElementById('tv-status-badge');
                const statusText = document.getElementById('tv-status-text');

                // Atualiza Estado da Central Universal da TV no Topo
                if (data.youtube) {
                    activeNowPlayingState = {
                        type: 'yt',
                        title: data.youtube.title || 'Vídeo do YouTube'
                    };
                    const ytKey = data.youtube.url || data.youtube.title;
                    if (lastNotifiedYt !== ytKey) {
                        lastNotifiedYt = ytKey;
                        notifyUser('▶️ YouTube Pronto na TV!', `"${data.youtube.title || 'Vídeo'}" começou a transmitir na TV!`, '/');
                    }
                } else if (data.active_vod) {
                    activeNowPlayingState.type = 'vod';
                    if (data.vod_display_name) {
                        activeNowPlayingState.title = data.vod_display_name;
                    }
                } else {
                    if (activeNowPlayingState.type !== 'vod') {
                        activeNowPlayingState = {
                            type: 'live',
                            title: data.active_channel_name || currentActiveName || 'TV ao Vivo'
                        };
                    }
                }
                renderTvBanner();

                if (data.youtube) {
                    statusBadge.className = 'tv-status-badge';
                    statusText.innerText = 'Assistindo YouTube na TV';
                } else if (data.active_vod) {
                    statusBadge.className = 'tv-status-badge';
                    statusText.innerText = 'Assistindo Filme (Cinema)';
                } else if (data.in_standby) {
                    statusBadge.className = 'tv-status-badge waiting';
                    statusText.innerText = 'TV em modo de espera';
                } else if (data.client && data.client.last_seen_secs > 20) {
                    statusBadge.className = 'tv-status-badge offline';
                    statusText.innerText = 'TV sem sinal de imagem';
                } else {
                    statusBadge.className = 'tv-status-badge';
                    statusText.innerText = 'TV Ligada e Transmitindo';
                }

                if (data.active_channel_name && data.active_channel_name !== currentActiveName) {
                    currentActiveName = data.active_channel_name;
                    const nowTitle = document.getElementById('now-playing-title');
                    if (nowTitle) nowTitle.innerText = data.active_channel_name;
                    if (activeNowPlayingState.type === 'live') {
                        activeNowPlayingState.title = data.active_channel_name;
                        renderTvBanner();
                    }
                }

                if (data.active_channel_id && data.active_channel_id !== currentActiveId) {
                    currentActiveId = data.active_channel_id;
                    document.querySelectorAll('.senior-channel-card').forEach(c => c.classList.remove('active'));
                    const curCard = document.getElementById(`channel-card-${currentActiveId}`);
                    if (curCard) curCard.classList.add('active');
                }

            } catch (err) {
                markOfflineStatus();
            } finally {
                isPolling = false;
            }
        }

        function markOfflineStatus() {
            const statusBadge = document.getElementById('tv-status-badge');
            const statusText = document.getElementById('tv-status-text');
            if (statusBadge && statusText) {
                statusBadge.className = 'tv-status-badge offline';
                statusText.innerText = 'Sem conexão com a TV...';
            }
        }

        // Navegação por Teclado e D-Pad de TV Box / Desktop
        window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            if (e.key === 'Escape') {
                closeDownloadsModal();
                closeMovieModal();
                closeSeriesModal();
            }

            // Se o Web Player estiver aberto:
            const webModal = document.getElementById('web-player-modal');
            if (webModal && webModal.style.display !== 'none') {
                const video = document.getElementById('web-player-video');
                if (e.key === 'Escape') {
                    closeWebPlayer();
                    e.preventDefault();
                    return;
                }
                if (e.key === ' ' || e.code === 'Space') {
                    if (video && video.style.display !== 'none') {
                        if (video.paused) video.play();
                        else video.pause();
                        e.preventDefault();
                    }
                    return;
                }
                if (e.key === 'f' || e.key === 'F') {
                    if (document.fullscreenElement) {
                        document.exitFullscreen().catch(() => {});
                    } else if (video && video.requestFullscreen) {
                        video.requestFullscreen().catch(() => {});
                    }
                    e.preventDefault();
                    return;
                }
                if (e.key === 'm' || e.key === 'M') {
                    if (video) {
                        video.muted = !video.muted;
                        e.preventDefault();
                    }
                    return;
                }
                if (e.key === 'ArrowRight') {
                    if (video && !isNaN(video.duration) && video.duration > 0) {
                        video.currentTime = Math.min(video.duration, video.currentTime + 10);
                        e.preventDefault();
                    }
                    return;
                }
                if (e.key === 'ArrowLeft') {
                    if (video && !isNaN(video.duration) && video.duration > 0) {
                        video.currentTime = Math.max(0, video.currentTime - 10);
                        e.preventDefault();
                    }
                    return;
                }
            }

            // Teclas 1 a 9 no Modo TV Ao Vivo
            if (currentAppMode === 'live' && e.key >= '1' && e.key <= '9') {
                const idx = parseInt(e.key, 10) - 1;
                if (filteredChannels[idx]) {
                    playChannel(filteredChannels[idx]);
                }
            } else if (e.key === 'm' || e.key === 'M') {
                toggleMute();
            }
        });

        // Suporte PWA (Android e detecção explícita iOS)
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
            const banner = document.getElementById('pwa-banner');
            if (banner && !localStorage.getItem('pwa_dismissed')) {
                banner.style.display = 'flex';
            }
        });

        function checkIosPwaBanner() {
            const isIos = /iphone|ipad|ipod/.test(window.navigator.userAgent.toLowerCase());
            const isStandalone = window.navigator.standalone || window.matchMedia('(display-mode: standalone)').matches;
            if (isIos && !isStandalone && !localStorage.getItem('pwa_dismissed')) {
                const banner = document.getElementById('pwa-banner');
                if (banner) banner.style.display = 'flex';
            }
        }

        function installPWA() {
            triggerHaptic();
            if (deferredPrompt) {
                deferredPrompt.prompt();
                deferredPrompt.userChoice.then(() => {
                    deferredPrompt = null;
                    dismissPWABanner();
                });
            } else {
                const isIos = /iphone|ipad|ipod/.test(window.navigator.userAgent.toLowerCase());
                if (isIos) {
                    const modal = document.getElementById('ios-modal');
                    if (modal) modal.style.display = 'flex';
                } else {
                    window.location.href = '/controle-tv.apk';
                }
            }
        }

        function dismissPWABanner() {
            const banner = document.getElementById('pwa-banner');
            if (banner) banner.style.display = 'none';
            localStorage.setItem('pwa_dismissed', 'true');
        }

        function closeIosModal() {
            const modal = document.getElementById('ios-modal');
            if (modal) modal.style.display = 'none';
        }

        // Inicialização Completa
        async function init() {
            switchAppMode(currentAppMode);
            checkIncomingShare();
            checkIosPwaBanner();
            setupKeyboardListeners();
            setupScrollListeners();

            // Sincroniza favoritos de canais e séries antes de renderizar (com restauração automática se servidor reiniciar)
            try {
                const favRes = await fetch('/api/favorites', { signal: AbortSignal.timeout(3000) });
                if (favRes.ok) {
                    const favData = await favRes.json();
                    const sFavs = Array.isArray(favData.favorites) ? favData.favorites.map(String) : [];
                    if (sFavs.length === 0 && favoritesSet.size > 0) {
                        // Servidor reiniciado/vazio: restaura favoritos locais do cliente no servidor!
                        fetch('/api/favorites/sync', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ favorites: Array.from(favoritesSet) })
                        }).catch(() => {});
                    } else if (sFavs.length > 0) {
                        sFavs.forEach(id => favoritesSet.add(id));
                        saveChannelFavorites();
                    }
                }
            } catch (e) {}

            try {
                const sFavRes = await fetch('/api/favorites/series', { signal: AbortSignal.timeout(3000) });
                if (sFavRes.ok) {
                    const sFavData = await sFavRes.json();
                    const sFavs = Array.isArray(sFavData.favorites) ? sFavData.favorites.map(String) : [];
                    if (sFavs.length === 0 && seriesFavoritesSet.size > 0) {
                        // Servidor reiniciado/vazio: restaura favoritos locais de séries do cliente no servidor!
                        fetch('/api/favorites/series/sync', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ favorites: Array.from(seriesFavoritesSet) })
                        }).catch(() => {});
                    } else if (sFavs.length > 0) {
                        sFavs.forEach(id => seriesFavoritesSet.add(id));
                        saveSeriesFavorites();
                    }
                }
            } catch (e) {}

            // Registra e Atualiza Service Worker para PWA/APK
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js').then((reg) => {
                    reg.update().catch(() => {});
                }).catch(() => {});
            }
            if ('caches' in window) {
                caches.keys().then((keys) => {
                    keys.forEach((k) => {
                        if (k !== 'controle-tv-v23') caches.delete(k);
                    });
                }).catch(() => {});
            }
            updateNotificationButtonUI();

            await Promise.allSettled([
                pollVodStatus(),
                loadMovieCategories(),
                loadMovieCatalog(true),
                loadSeriesCategories(),
                loadSeriesCatalog(true),
                loadLiveCategories(),
                loadLiveCatalog(true)
            ]);

            pollStatus();
            setInterval(pollStatus, 2500);
            setInterval(pollVodStatus, 3000);
        }

        init();
    </script>
</body>
</html>
"""

def run():
    HUB.start()
    server = ThreadedHTTPServer((HOST, PORT), RequestHandler)
    # A sincronização inicial do catálogo é iniciada apenas no runtime
    # explícito; o import do módulo permanece livre de threads e rede.
    try:
        CATALOG_MGR.start_background_sync()
    except Exception as e:
        print(f"[!] Erro ao iniciar sincronização do catálogo: {e}")
    print("==================================================")
    print(f" [✓] Servidor Multi-Canal Ativo em http://{HOST}:{PORT}")
    print("==================================================")
    try:
        resume_interrupted_vod_tasks()
    except Exception as e:
        print(f"[!] Erro ao retomar VODs: {e}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Encerrando...")
        HUB.running = False
        if HUB.proc:
            HUB.proc.kill()
        server.server_close()

if __name__ == "__main__":
    run()
