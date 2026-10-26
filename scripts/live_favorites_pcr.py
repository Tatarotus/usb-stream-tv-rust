"""Bound emitted PCR gaps by replacing existing CBR NULL packets in place.

FFmpeg is still configured with -pcr_period 20. Its table scheduling can push
an actual PCR past that boundary. This filter buffers one original interval,
uses a NULL packet near its midpoint, and interpolates PCR at that exact byte
position. Packet count, bitrate, payload order, PTS/DTS and original PCRs stay
unchanged. Missing NULL capacity and oversized intervals fail closed.
"""

TS_SIZE = 188
PCR_HZ = 27_000_000
PCR_MOD = (1 << 33) * 300
PCR_LIMIT = PCR_HZ * 20 // 1000
MAX_INTERVAL_PACKETS = 512
MAX_ORIGINAL_INTERVAL = PCR_HZ // 10  # 100 ms; normal input is ~21 ms.


def pcr_value(packet: bytes) -> int | None:
    afc = (packet[3] >> 4) & 3
    if not afc & 2 or packet[4] < 7 or not packet[5] & 0x10:
        return None
    b = packet[6:12]
    base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
    return base * 300 + ((b[4] & 1) << 8) + b[5]


def packet_pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def pcr_packet(pid: int, cc: int, ticks: int) -> bytes:
    base, ext = divmod(ticks % PCR_MOD, 300)
    packet = bytearray(b"\xff" * TS_SIZE)
    packet[:6] = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20 | cc, 183, 0x10])
    packet[6:12] = bytes([
        (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
        (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E | ((ext >> 8) & 1), ext & 0xFF,
    ])
    return bytes(packet)


class PcrBounder:
    def __init__(self, muxrate: int = 6_000_000):
        if muxrate < 75_200:
            raise ValueError("muxrate too low for a 20 ms TS packet")
        self._muxrate = muxrate
        self._packet_limit = muxrate * 20 // (1000 * TS_SIZE * 8)
        self._lookahead_limit = min(MAX_INTERVAL_PACKETS, 2 * self._packet_limit)
        self._pid = None
        self._anchor = None
        self._anchor_cc = 0
        self._pending = []
        self.replaced_null_packets = 0

    def _repair(self, end: int) -> bytes:
        count = len(self._pending)
        gap = (end - self._anchor) % PCR_MOD
        if not 0 < gap <= MAX_ORIGINAL_INTERVAL:
            raise ValueError("PCR discontinuity or excessive original interval")
        # The source is CBR: PCR time must agree with the distance between
        # anchor packets within one tick of integer rounding.
        if abs(gap * self._muxrate - count * TS_SIZE * 8 * PCR_HZ) > self._muxrate:
            raise ValueError("PCR clock does not match configured muxrate")
        nulls = [index for index, packet in enumerate(self._pending, 1)
                 if packet_pid(packet) == 0x1FFF]
        replacements = set()

        def ticks(index):
            return gap * index // count

        def split(left, right):
            if (ticks(right) - ticks(left) <= PCR_LIMIT
                    and right - left <= self._packet_limit):
                return
            candidates = [index for index in nulls if left < index < right]
            if not candidates:
                raise ValueError("no NULL packet to bound PCR interval")
            middle = min(candidates, key=lambda index: abs(2 * index - left - right))
            replacements.add(middle)
            split(left, middle)
            split(middle, right)

        split(0, count)
        output = bytearray()
        cc = self._anchor_cc
        for index, packet in enumerate(self._pending, 1):
            if index in replacements:
                packet = pcr_packet(self._pid, cc, self._anchor + ticks(index))
                self.replaced_null_packets += 1
            if packet_pid(packet) == self._pid:
                cc = packet[3] & 0x0F
            output.extend(packet)
        return bytes(output)

    def feed(self, data: bytes) -> bytes:
        if len(data) % TS_SIZE:
            raise ValueError("PCR filter requires whole TS packets")
        output = bytearray()
        for offset in range(0, len(data), TS_SIZE):
            packet = data[offset:offset + TS_SIZE]
            afc = (packet[3] >> 4) & 3
            if packet[0] != 0x47 or afc == 0:
                raise ValueError("invalid MPEG-TS framing")
            if afc & 2:
                if packet[4] > 183 or (packet[4] and packet[5] & 0x10 and packet[4] < 7):
                    raise ValueError("invalid MPEG-TS adaptation field")
            value = pcr_value(packet)
            if value is not None:
                if packet[5] & 0x80:
                    raise ValueError("PCR discontinuity indicator")
                pid = packet_pid(packet)
                if self._pid is not None and pid != self._pid:
                    raise ValueError("multiple PCR PIDs are unsupported")
                if self._anchor is not None:
                    self._pending.append(packet)
                    if len(self._pending) > self._lookahead_limit:
                        raise ValueError("PCR lookahead exceeded")
                    output.extend(self._repair(value))
                    self._pending.clear()
                else:
                    output.extend(packet)
                self._pid, self._anchor = pid, value
                self._anchor_cc = packet[3] & 0x0F
            elif self._anchor is None:
                output.extend(packet)
            else:
                self._pending.append(packet)
                if len(self._pending) > self._lookahead_limit:
                    raise ValueError("PCR lookahead exceeded")
        return bytes(output)

    def finish(self) -> bytes:
        # No following PCR exists for a finite tail; preserve bytes exactly.
        if self._anchor is None:
            raise ValueError("source has no PCR")
        if len(self._pending) > self._packet_limit:
            raise ValueError("finite PCR tail exceeds one interval")
        output = b"".join(self._pending)
        self._pending.clear()
        return output
