#!/usr/bin/env python3
"""
Migração em lote retomável dos episódios VOD de A Noiva de Istambul para Google Drive.
Chama exclusivamente a API POST /api/vod/migrate do servidor usb-stream-tv.
Nunca edita o catálogo JSON diretamente.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def normalizar(texto):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', texto or '')
        if not unicodedata.combining(c)
    ).lower()


def is_istanbullu_star(title):
    nome = normalizar(title)
    return bool(
        ('noiva' in nome and ('istambul' in nome or 'istanbul' in nome))
        and re.search(r'star\s*(?:526|528)\s*p', nome)
    )


def extract_ep_num(title):
    m = re.search(r'\b(?:episodio|ep\.?|)\s*(\d{1,3})\b', normalizar(title))
    if m:
        return int(m.group(1))
    return 0


def get_df_free(path):
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_gb": round(usage.free / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2)
        }
    except Exception as e:
        return {"error": str(e)}


def query_vod_status(api_url):
    req = urllib.request.Request(f"{api_url.rstrip('/')}/api/vod/status", headers={"Connection": "close"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode())


def trigger_migrate(api_url, task_id, pin):
    url = f"{api_url.rstrip('/')}/api/vod/migrate"
    headers = {"Content-Type": "application/json", "Connection": "close"}
    if pin:
        headers["X-Auth-PIN"] = pin
    data = json.dumps({"task_id": task_id}).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.status, json.loads(res.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Migrate Istanbullu VOD episodes to Google Drive")
    parser.add_argument("--api-url", default="http://172.20.0.5:8080", help="VOD Server API base URL")
    parser.add_argument("--pin", default=None, help="TV_PIN (or read from environment)")
    parser.add_argument("--dry-run", action="store_true", help="List selected tasks without migrating")
    parser.add_argument("--report", default="scratch/migration_report.json", help="Path to save report")
    parser.add_argument("--watch-dir", default="/opt/containers/apps/usb-stream-tv/vod_cache", help="Path to monitor df")
    parser.add_argument("--item-timeout", type=int, default=600, help="Timeout in seconds per file upload")
    parser.add_argument("--all-local", action="store_true", help="Migrate all local VOD files (movies, series, etc.)")
    args = parser.parse_args()

    pin = args.pin or os.environ.get("TV_PIN")
    if not pin and not args.dry_run:
        env_file = Path("/opt/containers/apps/usb-stream-tv/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TV_PIN="):
                    pin = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    df_before = get_df_free(args.watch_dir)
    print(f"[*] Inspecionando inventário em {args.api_url}...")
    try:
        status_data = query_vod_status(args.api_url)
    except Exception as e:
        print(f"[!] Falha ao consultar /api/vod/status: {e}", file=sys.stderr)
        sys.exit(1)

    active_vod = status_data.get("active_vod")
    all_tasks = status_data.get("tasks", [])
    print(f"[*] Total de tarefas no VOD: {len(all_tasks)} (active_vod: {active_vod})")

    selected = []
    if args.all_local:
        for t in all_tasks:
            sz = t.get("file_size", 0) or 0
            if t.get("storage") != "gdrive" and t.get("status") in ("ready", "downloaded", "queued") and sz > 0:
                selected.append(t)
        selected.sort(key=lambda t: t.get("file_size", 0), reverse=True)
        print(f"[*] Tarefas locais selecionadas para migração geral: {len(selected)}")
    else:
        for t in all_tasks:
            if is_istanbullu_star(t.get("title", "")):
                selected.append(t)
        selected.sort(key=lambda t: extract_ep_num(t.get("title", "")))
        print(f"[*] Tarefas selecionadas (A Noiva de Istambul Star 528p): {len(selected)}")

    already_gdrive = [t for t in selected if t.get("storage") == "gdrive" and t.get("status") == "ready"]
    pending = [t for t in selected if not (t.get("storage") == "gdrive" and t.get("status") == "ready")]

    print(f"[*] Já migradas (Drive): {len(already_gdrive)}")
    print(f"[*] Pendentes de migração: {len(pending)}")
    total_bytes_pending = sum(t.get("file_size", 0) for t in pending)
    print(f"[*] Volume total pendente: {total_bytes_pending / (1024**3):.2f} GiB")

    if args.dry_run:
        print("\n--- MODO DRY-RUN: Lista de tarefas selecionadas ---")
        for t in selected:
            storage = t.get("storage", "local")
            sz_mb = t.get("file_size", 0) / (1024**2)
            print(f"  ID: {t.get('id')} | Ep: {extract_ep_num(t.get('title'))} | {t.get('title')} | Status: {t.get('status')} | Storage: {storage} | {sz_mb:.1f} MB")
        print("--- Fim do modo dry-run ---")
        return

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "df_before": df_before,
        "total_selected": len(selected),
        "already_migrated_initial": len(already_gdrive),
        "total_pending_initial": len(pending),
        "migrated": [],
        "errors": [],
        "skipped_active": [],
    }

    def save_report():
        report["df_current"] = get_df_free(args.watch_dir)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    save_report()

    migrated_bytes = 0
    active_postponed = []

    for idx, t in enumerate(pending, start=1):
        task_id = t.get("id")
        title = t.get("title", "")
        ep = extract_ep_num(title)
        size = t.get("file_size", 0)

        current_status = query_vod_status(args.api_url)
        if current_status.get("active_vod") == task_id:
            print(f"[!] Tarefa {task_id} (Ep {ep}) está ativa na TV. Postergando para o final.")
            active_postponed.append(t)
            report["skipped_active"].append(task_id)
            save_report()
            continue

        for other in current_status.get("tasks", []):
            if other.get("id") != task_id and other.get("status") in ("processing", "queued"):
                print(f"[*] Nova publicação concorrente em andamento ({other.get('id')} - {other.get('title')}). Aguardando...")
                while True:
                    time.sleep(5)
                    st = query_vod_status(args.api_url)
                    in_prog = [x for x in st.get("tasks", []) if x.get("id") != task_id and x.get("status") in ("processing", "queued")]
                    if not in_prog:
                        break

        print(f"\n[{idx}/{len(pending)}] Iniciando migração {task_id} (Ep {ep}: {title}) - {size / 1024**2:.1f} MB...")
        try:
            http_code, resp = trigger_migrate(args.api_url, task_id, pin)
            print(f"  API respondeu HTTP {http_code}: {resp.get('message', resp.get('status'))}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"  [!] HTTP {e.code}: {err_body}")
            report["errors"].append({"task_id": task_id, "error": err_body, "code": e.code})
            save_report()
            continue
        except Exception as e:
            print(f"  [!] Erro de conexão: {e}")
            report["errors"].append({"task_id": task_id, "error": str(e)})
            save_report()
            continue

        t0 = time.time()
        success = False
        while time.time() - t0 < args.item_timeout:
            time.sleep(2)
            try:
                st = query_vod_status(args.api_url)
            except Exception:
                continue
            cur = next((x for x in st.get("tasks", []) if x.get("id") == task_id), None)
            if not cur:
                print(f"  [!] Tarefa {task_id} desapareceu do status!")
                break
            status = cur.get("status")
            storage = cur.get("storage")
            if status == "ready" and storage == "gdrive":
                print(f"  [✓] Concluído em {time.time() - t0:.1f}s: remote_path={cur.get('remote_path')} md5={cur.get('remote_md5')}")
                migrated_bytes += size
                report["migrated"].append({
                    "task_id": task_id,
                    "title": title,
                    "bytes": size,
                    "remote_path": cur.get("remote_path"),
                    "remote_md5": cur.get("remote_md5"),
                    "duration_sec": round(time.time() - t0, 2)
                })
                success = True
                save_report()
                break
            elif status == "error":
                print(f"  [!] Falha no upload: {cur.get('error')}")
                report["errors"].append({"task_id": task_id, "error": cur.get("error")})
                save_report()
                break

        if not success:
            print(f"  [!] Timeout ou falha aguardando tarefa {task_id}")

    for t in active_postponed:
        task_id = t.get("id")
        current_status = query_vod_status(args.api_url)
        if current_status.get("active_vod") == task_id:
            print(f"[!] Tarefa {task_id} continua ativa na TV. Não interrompendo reprodução. Permanece pendente.")
        else:
            print(f"[*] Retentando tarefa postergada {task_id}...")
            try:
                http_code, resp = trigger_migrate(args.api_url, task_id, pin)
                t0 = time.time()
                while time.time() - t0 < args.item_timeout:
                    time.sleep(2)
                    st = query_vod_status(args.api_url)
                    cur = next((x for x in st.get("tasks", []) if x.get("id") == task_id), None)
                    if cur and cur.get("status") == "ready" and cur.get("storage") == "gdrive":
                        migrated_bytes += t.get("file_size", 0)
                        report["migrated"].append({
                            "task_id": task_id,
                            "title": t.get("title"),
                            "bytes": t.get("file_size", 0),
                            "remote_path": cur.get("remote_path"),
                            "remote_md5": cur.get("remote_md5"),
                            "duration_sec": round(time.time() - t0, 2)
                        })
                        save_report()
                        break
            except Exception as e:
                print(f"[!] Erro ao retentar tarefa ativa {task_id}: {e}")

    df_after = get_df_free(args.watch_dir)
    report["df_after"] = df_after
    report["timestamp_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["total_migrated"] = len(report["migrated"])
    report["total_errors"] = len(report["errors"])
    report["migrated_bytes"] = migrated_bytes
    if "free_bytes" in df_after and "free_bytes" in df_before:
        recovered = df_after["free_bytes"] - df_before["free_bytes"]
        report["recovered_bytes"] = recovered
        report["recovered_gb"] = round(recovered / (1024**3), 2)
    save_report()

    print("\n==========================================")
    print("Migração finalizada!")
    print(f"Episódios migrados nesta execução: {len(report['migrated'])} / {len(pending)}")
    print(f"Erros: {len(report['errors'])}")
    print(f"Bytes enviados: {migrated_bytes} ({migrated_bytes / (1024**3):.2f} GiB)")
    if "recovered_gb" in report:
        print(f"Espaço em disco recuperado na VPS: {report['recovered_gb']} GiB")
    print(f"Relatório salvo em: {args.report}")
    print("==========================================")


if __name__ == "__main__":
    main()
