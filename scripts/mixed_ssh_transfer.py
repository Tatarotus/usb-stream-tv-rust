"""Copy a local staging directory to ADB on an SSH host via a private spool."""
from __future__ import annotations

from pathlib import Path
import re
import shlex

_HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}\Z")
_SPOOL = re.compile(r"/tmp/mixed-vod-[A-Za-z0-9]{8}\Z")


def push_via_ssh(transport, local_dir: Path, device_dir: str, *, check: bool = True):
    """Transfer via a private random remote directory and remove it on exit.

    Only a server-generated, strictly validated path is passed to remote rm.
    The tablet's destination is never removed here.
    """
    host = transport.ssh_host
    if not isinstance(host, str) or not _HOST.fullmatch(host):
        raise ValueError("invalid SSH host")
    local = Path(local_dir).resolve(strict=True)
    if not local.is_dir():
        raise ValueError("staging path is not a directory")
    if not isinstance(device_dir, str) or not device_dir.startswith('/data/local/tmp/'):
        raise ValueError("invalid tablet destination")
    mkdir = transport._run(['ssh', '-o', 'BatchMode=yes', host,
                            'umask 077; mktemp -d /tmp/mixed-vod-XXXXXXXX'], check=True,
                           timeout=None)
    spool = (mkdir.stdout or '').strip()
    if not _SPOOL.fullmatch(spool):
        raise ValueError("unsafe remote temporary directory")
    try:
        transport._run(['scp', '-r', '-o', 'BatchMode=yes', str(local) + '/.',
                        host + ':' + spool + '/'], check=True, timeout=None)
        return transport.adb('push', '--sync', spool + '/.', device_dir.rstrip('/') + '/',
                             check=check)
    finally:
        transport._run(['ssh', '-o', 'BatchMode=yes', host,
                        'rm -rf -- ' + shlex.quote(spool)], check=False, timeout=None)
