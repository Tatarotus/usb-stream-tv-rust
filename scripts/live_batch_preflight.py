#!/usr/bin/env python3
"""Pré-voo de sondagem: reduz o risco de disputa pela conexão de origem.

Limite duro do plano IPTV: **uma conexão simultânea por conta**, contando o
manager/FFmpeg que alimenta a TV, qualquer varredura, ffprobe, túnel ou cliente
de outra máquina.

O que este script faz — e o que ele **não** prova:

  * contagens de socket e de processo são **indício**, não prova. IPs resolvidos
    do `.env` não cobrem redirects/CDN, e nenhuma ferramenta local enxerga outro
    aparelho usando a mesma conta;
  * ele **falha fechado**: qualquer verificação que não possa ser concluída
    (ssh indisponível, comando ausente, resposta estranha) resulta em NÃO LIBERADO;
  * a exclusão real é feita pelo **lock único compartilhado** entre runners
    (`--lock-hold`), e pela confirmação do operador de que nenhum outro aparelho
    está usando a conta.

Saída: 0 = liberado; 3 = ocupado ou não comprovável. Nunca imprime host, IP, URL
ou credencial do provedor.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ENV_FILE = Path(".env")
PROBE_PATTERNS = ("probe_live_favorites_sources", "probe_live_favorites_decode",
                  "probe_live_favorites_output", "--phase winner")
MEDIA_TOOLS = ("ffmpeg", "ffprobe", "stream_fetcher", "run_live_favorites_manager",
               "run_mixed_vod_proxy")
# Túneis/proxies são procurados pelo NOME do executável (``comm``), não por
# substring da linha de comando: procurar "ssh -L" no cmdline casava com o texto
# de qualquer comando alheio e rendia 115 falsos positivos.
TUNNEL_COMM = {"chisel", "socat", "openvpn", "wg-quick", "wireguard-go", "stunnel",
               "frpc", "ngrok", "cloudflared", "redsocks"}
DEFAULT_LOCK = "scratch/live-batch/source.lock"


def provider_host(env_file: Path = ENV_FILE) -> str:
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("XTREAM_UPSTREAM"):
            _, _, value = line.partition("=")
            return re.sub(r"^https?://", "", value.strip()).split("/")[0].split(":")[0]
    return ""


def provider_ips(host: str) -> list[str] | None:
    if not host:
        return None
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except Exception:
        return None


def ps_listing() -> list[str] | None:
    try:
        done = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True,
                              timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    return [line for line in done.stdout.splitlines() if line.strip()]


def count_established(text: str, ips: set[str]) -> int:
    count = 0
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 5 and fields[4].rsplit(":", 1)[0].strip("[]") in ips:
            count += 1
    return count


def host_sockets() -> str | None:
    try:
        done = subprocess.run(["ss", "-tn", "state", "established"],
                              capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


def tunnel_processes() -> list[str] | None:
    """Processos de túnel/proxy pelo nome exato do executável. None = não provado."""
    try:
        done = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True,
                              text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    hits = []
    for line in done.stdout.splitlines()[1:]:
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[1].strip() in TUNNEL_COMM:
            hits.append("pid=%s comm=%s" % (fields[0], fields[1].strip()))
    return hits


def matching(lines: list[str], patterns: tuple[str, ...], ignore: tuple[str, ...]) -> list[str]:
    hits = []
    for line in lines:
        if any(token in line for token in ignore):
            continue
        if any(pattern in line for pattern in patterns):
            hits.append("pid=%s" % line.split()[0])
    return hits


def vps_connections(ips: set[str]) -> int | None:
    """Conexões ao provedor na VPS. None quando não for possível verificar."""
    script = ("sudo ss -tn state established 2>/dev/null | tail -n +2 | "
              "awk '{print $5}' | sed 's/:[0-9]*$//' | sort -u")
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                               "oracle", script], capture_output=True, text=True,
                              timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    peers = {line.strip() for line in done.stdout.splitlines() if line.strip()}
    return len(peers & ips)


def hold_lock(path: Path, wait: float) -> tuple[bool, str]:
    """Lock único compartilhado entre runners (lockfile do próprio projeto)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(handle, ("%d\n" % os.getpid()).encode())
            return True, "lock obtido"
        except OSError:
            if time.monotonic() >= deadline:
                os.close(handle)
                return False, "outro runner segura o lock"
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-vps", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--lock-hold", default="")
    parser.add_argument("--lock-wait", type=float, default=10.0)
    args = parser.parse_args()

    report: dict[str, object] = {"verdict": "unprovable", "reasons": []}
    reasons: list[str] = report["reasons"]  # type: ignore[assignment]

    host = provider_host()
    ips = provider_ips(host)
    if ips is None:
        reasons.append("host do provedor nao resolvido")

    listing = ps_listing()
    if listing is None:
        reasons.append("ps indisponivel: nao da para verificar processos")
        listing = []

    ignore = ("preflight", "ps -eo", "grep")
    probes = matching(listing, PROBE_PATTERNS, ignore)
    tools = matching(listing, MEDIA_TOOLS, ignore)
    tunnels = tunnel_processes()
    if tunnels is None:
        reasons.append("nao foi possivel verificar tuneis/proxies")
    if probes:
        reasons.append("%d sondagem(ns) local(is) ativa(s)" % len(probes))
    if tools:
        reasons.append("%d ferramenta(s) de midia ativa(s) no host" % len(tools))
    # Tunel/proxy ativo NAO e prova de sessao de origem: o chisel do proprio
    # projeto fica de pe carregando o caminho do tablet. Fica registrado como
    # indício; bloqueiam conexao observada ao provedor e verificacao que nao
    # pode ser concluida (tunnel_processes() is None).

    host_conns = None
    if ips is not None:
        text = host_sockets()
        if text is None:
            reasons.append("ss indisponivel: contagem de sockets nao verificada")
        else:
            host_conns = count_established(text, set(ips))
            if host_conns:
                reasons.append("%d conexao(oes) do host ao provedor" % host_conns)

    vps_conns = None
    if args.skip_vps:
        reasons.append("VPS nao verificada (--skip-vps)")
    elif ips is not None:
        vps_conns = vps_connections(set(ips))
        if vps_conns is None:
            reasons.append("nao foi possivel verificar a VPS")
        elif vps_conns:
            reasons.append("%d conexao(oes) da VPS ao provedor" % vps_conns)

    lock_state = "nao solicitado"
    if args.lock_hold:
        got, lock_state = hold_lock(Path(args.lock_hold), args.lock_wait)
        if not got:
            reasons.append(lock_state)

    report.update({
        "provider_resolved_ips": len(ips) if ips else 0,
        "host_connections": host_conns,
        "vps_connections": vps_conns,
        "probe_processes": probes,
        "media_tools": tools,
        "tunnel_processes": tunnels,
        "lock": lock_state,
        "note": ("contagens sao indício, nao prova: nao cobrem redirect/CDN nem "
                 "outro aparelho na mesma conta"),
    })
    report["verdict"] = "free" if not reasons else "blocked"

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("pre-voo: %s | host=%s VPS=%s sondas=%d ferramentas=%d tuneis=%d | lock=%s"
              % ("LIBERADO" if not reasons else "NAO LIBERADO",
                 host_conns if host_conns is not None else "?",
                 vps_conns if vps_conns is not None else "?",
                 len(probes), len(tools),
                 len(tunnels) if tunnels is not None else -1, lock_state))
        for reason in reasons:
            print("   motivo:", reason)
    return 0 if not reasons else 3


if __name__ == "__main__":
    raise SystemExit(main())