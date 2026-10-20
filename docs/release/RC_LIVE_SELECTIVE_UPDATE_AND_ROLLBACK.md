# RC-Live — selective update and rollback procedure

This is a **procedure for an operator**, not a script the RC package runs. It is
written for the ITER#5 candidate (`rc-001-live-iter5`, ARM binaries identical to
ITER#4/ITER#3). Every device step requires explicit operator approval; nothing
here is executed automatically and nothing here has been run yet.

## Why selective

`scripts/deploy_tablet.sh` remounts `/system` read-write and replaces **every**
binary, script and the 128 GiB template. For this candidate that is far more
than needed and increases the blast radius. The only changed component versus
the currently installed state is one binary:

| component | installed / frozen | candidate | changed? |
|-----------|--------------------|-----------|----------|
| `fuse_ntfs` (`/system/xbin`) | `909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898` | `d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55` | **yes** |
| `stream_fetcher` | `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` | same | no |
| `fuse_direct` | `a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46` | same | no |
| `patch_trp` | `3afd258f51ac45fccc3f35d262fbf5141b6090a710a23c9819619704ecf8015d` | same | no |
| `server.py` (server) | `53552fc9a000f6f2a9c9e2477d3429ed4906265a0d8fd62bbf6fb622302be517` | same | no |
| `channels.json`, scripts, template | unchanged | unchanged | no |

So the update is **one file on the tablet and nothing on the server**.

## What `switch_tv_mode.sh live` actually does (do not misread it)

The `live|tv` branch is **not** a reset-free restart. Reading
`scripts/switch_tv_mode.sh`, it calls `cleanup_stack()`, which:

- writes `0` to `/sys/class/android_usb/android0/enable` (disables the gadget),
- detaches `.../f_mass_storage/lun0/file`,
- lazily unmounts `/data/local/tmp/ntfs_lab/mnt` and `/data/local/tmp/vfat_mnt`,
- `killall -9 stream_fetcher fuse_ntfs fuse_direct fuse_direct_arm32`.

It then recreates the FIFO, may re-run `/data/local/tmp/patch_trp` on the live
template, starts `fuse_ntfs` and exactly one `stream_fetcher`, waits for a
≥ 8 MB pre-buffer, flips the USB inquiry string between `LIVETV1`/`LIVETV2`,
detaches and re-attaches `lun0/file`, and re-enables the gadget.

That is a **planned, controlled re-enumeration of the USB mass-storage path**
(the TV deliberately sees the device disappear and come back), not a
maintenance-free operation. Keep it distinct from a **spontaneous** gadget reset
that appears with no mode switch in progress — that one is a defect signal.

## 0. Preflight — read-only, no interruption

Everything in this section reads the device. It does **not** stop processes,
remount `/system`, detach the USB or touch the TV. Do it first and decide from
its result: if the environment is not the expected one, never reach section 1.

```sh
ADB=...            # tablet serial / ip:port
MNT=/data/local/tmp/ntfs_lab/mnt
DEST=./rc001-preflight-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"

# 0.1 Identify the device and its current state (read-only).
adb devices -l | tee "$DEST/adb-devices.txt"
adb -s "$ADB" shell 'getprop ro.product.model; getprop ro.build.version.release; \
    getprop ro.product.cpu.abi; uname -m' | tee "$DEST/device.txt"
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh status'" \
    | tee "$DEST/status.before.txt"          # reports ATIVO/PARADO, USB enable, backing file

# 0.2 Installed hashes: the tablet binaries and the server identity.
adb -s "$ADB" shell "su -c 'sha256sum /system/xbin/fuse_ntfs /system/xbin/stream_fetcher \
    /system/xbin/fuse_direct /system/xbin/patch_trp'" | tee "$DEST/tablet-installed.sha256"
# server.py is on the server host, not the tablet:
sha256sum server.py | tee "$DEST/server.py.sha256"

# 0.3 Keep a byte copy of the installed binary as the rollback source.
adb -s "$ADB" shell "su -c 'cat /system/xbin/fuse_ntfs'" > "$DEST/fuse_ntfs.installed"
sha256sum "$DEST/fuse_ntfs.installed" | tee -a "$DEST/tablet-installed.sha256"
```

### GATE — decide before interrupting anything

Compare the values just captured. Do **not** continue to section 1 unless all of
them hold:

- `$DEST/fuse_ntfs.installed` hashes to
  `909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898`;
- the other three tablet binaries match the table above;
- `server.py` hashes to
  `53552fc9a000f6f2a9c9e2477d3429ed4906265a0d8fd62bbf6fb622302be517`;
- `status.before.txt` shows the mode and processes you expect.

If any check fails, **stop here**. The device is not in the state this package
assumes, the rollback set must be re-derived, and the live stream has not been
interrupted at all.

## 1. Stop the service — only after the gate passes

Never copy over `/system/xbin/fuse_ntfs` while an instance may be running. There
is no `stop` mode, so stop explicitly with the same primitives `cleanup_stack()`
uses, then prove it stopped. Process detection is **name-based** (`pidof`, the
same idiom `switch_tv_mode.sh status` uses); a `pgrep -f "a|b"` pattern would
also match the shell that runs the command, and could never report STOPPED.

```sh
adb -s "$ADB" shell "su -c '
    echo 0 > /sys/class/android_usb/android0/enable
    sleep 1
    echo \"\" > /sys/class/android_usb/android0/f_mass_storage/lun0/file
    umount -l $MNT 2>/dev/null
    killall -9 stream_fetcher fuse_ntfs fuse_direct 2>/dev/null
    sleep 1
    if pidof fuse_ntfs stream_fetcher >/dev/null 2>&1; then
        echo RUNNING; pidof fuse_ntfs stream_fetcher
    else
        echo STOPPED
    fi'"
```

Require the literal `STOPPED` before continuing. If it prints `RUNNING`, stop and
re-investigate; do not replace a binary that is still mapped.

## 2. Selective update — tablet, one binary

```sh
PKG=/home/sam/Code/rc001-release/rc-001-live-iter5

# 2.1 Stage and verify on the device BEFORE touching /system.
adb -s "$ADB" push "$PKG/armv7/fuse_ntfs" /data/local/tmp/fuse_ntfs.new
adb -s "$ADB" shell 'sha256sum /data/local/tmp/fuse_ntfs.new'
# expect d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55

# 2.2 Replace only fuse_ntfs, with the service already stopped (section 1).
adb -s "$ADB" shell "su -c '
    mount -o remount,rw /system || exit 1
    cp /data/local/tmp/fuse_ntfs.new /system/xbin/fuse_ntfs
    chmod 755 /system/xbin/fuse_ntfs
    mount -o remount,ro /system
    sha256sum /system/xbin/fuse_ntfs'"
# expect d5a973d6...

# 2.3 Start the live path: this performs the planned re-enumeration described
#     above (disconnect, >=8MB pre-buffer, reconnect). The TV will blink once.
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh live'"
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh status'"
```

Server: change nothing (its `server.py` is byte-identical). Do **not** redeploy
the server for this candidate.

## 3. Rollback — restore the installed binary

Rollback uses the same stop-before-replace rule.

```sh
# 3.1 Stop the live path (section 1 again; require STOPPED).
# 3.2 Restore. Preferred source: the preflight copy from 0.3.
adb -s "$ADB" push "$DEST/fuse_ntfs.installed" /data/local/tmp/fuse_ntfs.restore
# equivalent, from the package:
# adb -s "$ADB" push "$PKG/rollback/armv7-pre-rc/fuse_ntfs" /data/local/tmp/fuse_ntfs.restore
adb -s "$ADB" shell 'sha256sum /data/local/tmp/fuse_ntfs.restore'
# expect 909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898

adb -s "$ADB" shell "su -c '
    mount -o remount,rw /system || exit 1
    cp /data/local/tmp/fuse_ntfs.restore /system/xbin/fuse_ntfs
    chmod 755 /system/xbin/fuse_ntfs
    mount -o remount,ro /system
    sha256sum /system/xbin/fuse_ntfs'"
# expect 909e2f06...

# 3.3 Start the live path again.
adb -s "$ADB" shell "su -c '/system/xbin/switch_tv_mode.sh live'"
```

Keep the preflight copy and `/data/local/tmp/fuse_ntfs.new` on the device until
the candidate is accepted, so either direction is one command away.

## 4. Escalation (only if the selective path is not enough)

- Server rollback: restore `rollback/server.py.pre-rc`
  (`b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb`) per
  `rollback/ROLLBACK.md`, then restart only the server.
- Full tablet state: the operator's own pre-deployment backup, or
  `deploy_tablet.sh` from the frozen `rc-001-live-16d5991` package — explicitly
  **not** part of this procedure.
- USB/gadget: only through `scripts/switch_tv_mode.sh` and the watchdog. If a
  mode switch fails, `fail_rollback()` re-attaches the backing image and
  re-enables the gadget; never reset the gadget from a source-failure path.

## 5. Acceptance / stop conditions

Accept only if, within the agreed short window:

- `sha256sum /system/xbin/fuse_ntfs` equals `d5a973d6...` and
  `switch_tv_mode.sh status` reports the Live/FUSE/fetcher processes `ATIVO`;
- live playback (`Canais TV` / `TV AO VIVO.trp`) is steady, with no decoder
  freeze;
- the single USB re-enumeration from step 2.3 is the only disconnect observed —
  no further spontaneous disconnect or gadget reset;
- no runaway RSS/FD/thread growth in `stream_fetcher`/`fuse_ntfs`.

Roll back immediately on: persistent freeze, a **spontaneous** disconnect or
gadget reset, repeated SCSI/USB errors, or any provider/credential exposure.