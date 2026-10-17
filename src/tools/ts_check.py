#!/usr/bin/env python3
"""ts_check.py — dependency-free MPEG-TS timeline checker.

Walks a Transport Stream and reports:
  * sync loss (packets where the 0x47 sync byte is missing)
  * PCR jumps  (27 MHz PCR, 33-bit modular, > threshold seconds)
  * PTS jumps  (90 kHz, 33-bit modular, > threshold seconds)
  * continuity-counter errors per PID (payload packets only, NULL excluded)

Exit status: 0 when no PCR/PTS jump above the threshold was found, 1 otherwise.

Usage:
    ts_check.py <file.ts> [threshold_seconds]

Complements tests/test_cont_splice.c by validating the continuity splice against
a real capture produced by the VPS pipeline.
"""

import sys

PKT = 188
PCR_MOD = (1 << 33) * 300
PTS_MOD = 1 << 33
AFC_RESERVED = 0
AFC_PAYLOAD = 1
AFC_AF_ONLY = 2
AFC_AF_PAYLOAD = 3
PID_NULL = 0x1FFF


def decode_pts(b):
    return (((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) |
            (b[3] << 7) | (b[4] >> 1))


def decode_pcr(b):
    base = ((b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7))
    ext = ((b[4] & 1) << 8) | b[5]
    return base * 300 + ext


def signed_diff(a, b, mod):
    d = (a - b) % mod
    if d >= mod // 2:
        d -= mod
    return d


def find_phase(data):
    """First offset where three consecutive 188-byte packets are sync aligned."""
    limit = min(len(data) - 2 * PKT, 1 << 20)
    for i in range(0, limit):
        if data[i] != 0x47:
            continue
        if i + PKT < len(data) and data[i + PKT] != 0x47:
            continue
        if i + 2 * PKT < len(data) and data[i + 2 * PKT] != 0x47:
            continue
        return i
    return -1


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    path = argv[1]
    threshold = float(argv[2]) if len(argv) > 2 else 1.0
    thr_ticks = int(threshold * 90000)

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print("[!] cannot read %s: %s" % (path, exc))
        return 2

    phase = find_phase(data)
    if phase < 0:
        print("[!] %s: no 188-byte packet sync found" % path)
        return 2

    n_pkts = (len(data) - phase) // PKT
    sync_loss = 0
    pcr_jumps = []
    pts_jumps = []
    cc_errors = 0
    cc_first_error = None
    last_pcr = None
    last_pts = None
    cc = {}
    pids_seen = set()

    for n in range(n_pkts):
        off = phase + n * PKT
        p = data[off:off + PKT]
        if p[0] != 0x47:
            sync_loss += 1
            continue

        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = (p[1] & 0x40) >> 6
        afc = (p[3] >> 4) & 3
        if pid != PID_NULL:
            pids_seen.add(pid)

        payload_off = 4
        if afc in (AFC_AF_ONLY, AFC_AF_PAYLOAD):
            af_len = p[4]
            if af_len >= 7 and (p[5] & 0x10) and phase + n * PKT + 12 <= len(data):
                pcr = decode_pcr(p[6:12])
                if last_pcr is not None:
                    d = signed_diff(pcr, last_pcr, PCR_MOD)
                    if abs(d) > thr_ticks * 300:
                        if len(pcr_jumps) < 20:
                            pcr_jumps.append((n, off, d / 300.0 / 90000.0))
                last_pcr = pcr
            payload_off = 5 + af_len

        if afc in (AFC_PAYLOAD, AFC_AF_PAYLOAD):
            if pid != PID_NULL:
                prev = cc.get(pid)
                if prev is not None and ((prev + 1) & 0x0F) != (p[3] & 0x0F):
                    cc_errors += 1
                    if cc_first_error is None:
                        cc_first_error = (n, pid, prev, p[3] & 0x0F)
                cc[pid] = p[3] & 0x0F

            if pusi and payload_off + 9 <= PKT and p[payload_off:payload_off + 3] == b"\x00\x00\x01":
                sid = p[payload_off + 3]
                if 0xE0 <= sid <= 0xEF and (p[payload_off + 7] & 0x80):
                    pts = decode_pts(p[payload_off + 9:payload_off + 14])
                    if last_pts is not None:
                        d = signed_diff(pts, last_pts, PTS_MOD)
                        if abs(d) > thr_ticks:
                            if len(pts_jumps) < 20:
                                pts_jumps.append((n, off, d / 90000.0))
                    last_pts = pts

    print("[*] %s: %d packets (phase=%d), %d PIDs, %.1f MB" %
          (path, n_pkts, phase, len(pids_seen), len(data) / 1048576.0))
    print("    sync_loss      : %d" % sync_loss)
    print("    PCR jumps > %.1fs: %d" % (threshold, len(pcr_jumps)))
    for n, off, secs in pcr_jumps:
        print("      packet #%d @0x%x: %+.3f s" % (n, off, secs))
    print("    video PTS jumps > %.1fs: %d" % (threshold, len(pts_jumps)))
    for n, off, secs in pts_jumps:
        print("      packet #%d @0x%x: %+.3f s" % (n, off, secs))
    print("    CC errors      : %d%s" %
          (cc_errors, "" if cc_first_error is None else
           " (first: packet #%d PID 0x%04X %d -> %d)" % cc_first_error))

    verdict_ok = not pcr_jumps and not pts_jumps
    print("[%s] %s" % ("OK" if verdict_ok else "FAIL",
                       "timeline continuous" if verdict_ok else
                       "timeline discontinuities above %.1f s" % threshold))
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
