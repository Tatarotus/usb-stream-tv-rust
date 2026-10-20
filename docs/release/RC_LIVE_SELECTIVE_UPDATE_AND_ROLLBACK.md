# RC-Live — selective update and rollback procedure

This is a **procedure for an operator**, not a script the RC package runs. It is
written for the ITER#3 candidate (`rc-001-live-iter3`). Every device step
requires explicit operator approval; nothing here is executed automatically and
nothing here has been run yet.

## Why selective

`scripts/deploy_tablet.sh` remounts `/system` read-write and replaces **every**
binary, script and the 128 GiB template. For this candidate that is far more
than needed and increases the blast radius. The only changed component versus
the currently installed state is one binary:

| component | installed / frozen | ITER#3 candidate | changed? |
|-----------|--------------------|------------------|----------|
| `fuse_ntfs` (`/system/xbin`) | `909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898` | `d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55` | **yes** |
| `stream_fetcher` | `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` | same | no |
| `fuse_direct` | `a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46` | same | no |
| `patch_trp` | `3afd258f51ac45fccc3f35d262fbf5141b6090a710a23c9819619704ecf8015d` | same | no |
| `server.py` (server) | `53552fc9a000f6f2a9c9e2477d3429ed4906265a0d8fd62bbf6fb622302be517` | same | no |
| `channels.json`, scripts, template | unchanged | unchanged | no |

So the update is **one file on the tablet and nothing on the server**.

## 0. Pre-flight — capture the real installed state (rollback proof)

Run before changing anything, and keep the output with the release evidence.
This is what turns "we have a rollback package" into "we proved we can restore
what is actually deployed".

```sh
ADB=...            # tablet serial / ip:port
DEST=./rc001-backup-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"

# a. What is installed right now (hashes are the ground truth).
adb -s "$ADB" shell "su -c 'sha256sum /system/xbin/fuse_ntfs /system/xbin/stream_fetcher \
    /system/xbin/fuse_direct /system/xbin/patch_trp'" | tee "$DEST/tablet-installed.sha256"

# b. Keep a byte copy of the installed binary, not just its hash.
adb -s "$ADB" shell "su -c 'cp /system/xbin/fuse_ntfs /data/local/tmp/fuse_ntfs.bak && chmod 644 /data/local/tmp/fuse_ntfs.bak'"
adb -s "$ADB" pull /data/local/tmp/fuse_ntfs.bak "$DEST/fuse_ntfs.installed"
sha256sum "$DEST/fuse_ntfs.installed"      # must equal the hash from (a)

# c. Server side: the running server.py and the container/process identity.
#    (Read-only: do not stop, rebuild or restart anything here.)
adb -s "$ADB" shell "su -c 'pidof python3 || true'"
# on the server host:
sha256sum <app-dir>/server.py
docker ps --format '{{.Names}}\t{{.Image}}'   # capture image+container names
```

Acceptance for step 0: the hash from (a) equals the hash of the pulled copy in
(b), and the server `server.py` hash equals `53552fc9…`. If the installed
`fuse_ntfs` hash is **not** `909e2f06…`, stop — the device is not on the state
this package assumes, and the rollback set must be re-derived.

## 1. Selective update — tablet, one binary

```sh
PKG=/home/sam/Code/rc001-release/rc-001-live-iter3

# 1.1 Stage and verify on the device BEFORE touching /system.
adb -s "$ADB" push "$PKG/armv7/fuse_ntfs" /data/local/tmp/fuse_ntfs.new
adb -s "$ADB" shell "sha256sum /data/local/tmp/fuse_ntfs.new"
# expect d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55

# 1.2 Replace only fuse_ntfs. Do NOT run deploy_tablet.sh.
adb -s "$ADB" shell "su -c '
    mount -o remount,rw /system || exit 1
    cp /data/local/tmp/fuse_ntfs.bak /system/xbin/fuse_ntfs.pre-iter3 2>/dev/null || true
    cp /data/local/tmp/fuse_ntfs.new /system/xbin/fuse_ntfs
    chmod 755 /system/xbin/fuse_ntfs
    mount -o remount,ro /system
    sha256sum /system/xbin/fuse_ntfs'"
# expect d5a973d6…

# 1.3 Restart only the live mode path (no gadget reset, no erase).
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh live'"
```

Server: change nothing (its `server.py` is byte-identical). Do **not** redeploy
the server for this candidate.

## 2. Rollback — restore the installed binary

```sh
# 2.1 Preferred: the copy taken in step 0.
adb -s "$ADB" push "$DEST/fuse_ntfs.installed" /data/local/tmp/fuse_ntfs.restore
# or, equivalently, the pre-RC binary shipped in the package:
adb -s "$ADB" push "$PKG/rollback/armv7-pre-rc/fuse_ntfs" /data/local/tmp/fuse_ntfs.restore

adb -s "$ADB" shell "sha256sum /data/local/tmp/fuse_ntfs.restore"
# expect 909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898

# 2.2 Restore.
adb -s "$ADB" shell "su -c '
    mount -o remount,rw /system || exit 1
    cp /data/local/tmp/fuse_ntfs.restore /system/xbin/fuse_ntfs
    chmod 755 /system/xbin/fuse_ntfs
    mount -o remount,ro /system
    sha256sum /system/xbin/fuse_ntfs'"
# expect 909e2f06…

# 2.3 Restart the live mode path.
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh live'"
```

If step 1.2 stored `/system/xbin/fuse_ntfs.pre-iter3`, that is a second,
on-device rollback source; keep it until the candidate is accepted.

## 3. Escalation (only if the selective path is not enough)

- Server rollback: restore `rollback/server.py.pre-rc`
  (`b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb`) per
  `rollback/ROLLBACK.md`, then restart only the server.
- Full tablet state: the operator's own pre-deployment backup, or
  `deploy_tablet.sh` from the frozen `rc-001-live-16d5991` package — explicitly
  **not** part of this procedure.
- USB/gadget: only through the existing operator-controlled
  `scripts/switch_tv_mode.sh` and the watchdog. Never reset the gadget from a
  source-failure path.

## 4. Acceptance / stop conditions

Accept only if, within the agreed short window:

- `sha256sum /system/xbin/fuse_ntfs` equals `d5a973d6…`;
- live playback (`Canais TV` / `TV AO VIVO.trp`) is steady, with no decoder
  freeze;
- no new SCSI/USB errors, no unsolicited disconnect, no gadget reset;
- no runaway RSS/FD/thread growth in `stream_fetcher`/`fuse_ntfs`.

Roll back immediately on: persistent freeze, repeated SCSI/USB errors, a source
failure that resets the gadget, or any provider/credential exposure.