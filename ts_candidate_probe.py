"""Pure MPEG-TS candidate-readiness probe for TASK-006 Phase B.

This module deliberately owns no process, thread, socket or network state.
It consumes bytes produced by the existing FFmpeg candidate and returns a
bounded readiness decision. It is not wired into ``server.py`` yet.

Readiness means the returned ``selected`` byte range starts on a PAT packet,
contains a valid PMT for a program with H.264 video, and reaches the end of a
complete H.264 access unit whose first VCL NAL is an IDR and which carries SPS
and PPS before that IDR. No video payload may occur between the selected PAT
and the start of that access unit.
"""

from dataclasses import dataclass
from typing import Optional

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47
PAT_PID = 0x0000
NULL_PID = 0x1FFF
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_AC3 = 0x81
PRIVATE_STREAM = 0x06
AC3_DESCRIPTOR = 0x6A
REGISTRATION_DESCRIPTOR = 0x05
OTHER_AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x87)

NEED_MORE = "need_more"
READY = "ready"
REJECT = "reject"

def _crc_table():
    table = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)

_CRC_TABLE = _crc_table()

def mpeg_crc32(data) -> int:
    """MPEG-2 PSI CRC32 (non-reflected, init 0xffffffff, no final xor)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc

@dataclass(frozen=True)
class ProbeResult:
    status: str
    reason: str
    selected: Optional[bytes] = None
    video_pid: Optional[int] = None
    audio_pid: Optional[int] = None
    audio_kind: str = "unknown"
    packets: int = 0
    buffered: int = 0
    selected_start: int = 0
    selected_end: int = 0
    trailing: bytes = b""

    @property
    def ready(self) -> bool:
        return self.status == READY
class _SectionAssembler:
    """Reassembles PSI sections, including pointer fields and fragmentation."""

    def __init__(self):
        self._buf = bytearray()
        self._need = 0

    def feed(self, payload: bytes, pusi: bool):
        sections = []
        if not payload:
            return sections
        if pusi:
            pointer = payload[0]
            body = payload[1:]
            if pointer > len(body):
                self._buf.clear()
                self._need = 0
                return sections
            if pointer and self._buf:
                self._append(body[:pointer], sections)
            self._buf.clear()
            self._need = 0
            self._append(body[pointer:], sections)
        else:
            self._append(payload, sections)
        return sections

    def _append(self, data: bytes, sections):
        index = 0
        while index < len(data):
            if not self._buf:
                while index < len(data) and data[index] == 0xFF:
                    index += 1
                if index >= len(data):
                    return
            self._buf.append(data[index])
            index += 1
            if len(self._buf) == 3:
                length = ((self._buf[1] & 0x0F) << 8) | self._buf[2]
                if length < 4 or length > 4096:
                    self._buf.clear()
                    self._need = 0
                    continue
                self._need = 3 + length
            if self._need and len(self._buf) == self._need:
                sections.append(bytes(self._buf))
                self._buf.clear()
                self._need = 0
class TsCandidateProbe:
    """Bounded, streaming MPEG-TS readiness probe."""

    def __init__(self, max_bytes: int = 8 * 1024 * 1024):
        if max_bytes < TS_PACKET_SIZE * 4:
            raise ValueError("max_bytes must hold at least four TS packets")
        self._max_bytes = int(max_bytes)
        self._buf = bytearray()
        self._pos = 0
        self._synced = False
        self._packets = 0
        self._result = None

        self._pat_asm = _SectionAssembler()
        self._pat_start = None
        self._pending_pat_start = None
        self._pat_programs = {}
        self._saw_pat = False
        self._saw_pmt = False
        self._pmt_asms = {}
        self._pending_pmt_start = {}
        self._pmt_start = None
        self._pmt_pid = None
        self._selected_program = None

        self._video_pid = None
        self._audio_pids = []
        self._audio_pid = None
        self._audio_kind = "unknown"
        self._audio_ok = False
        self._last_reason = "waiting_for_pat"

        self._video_since_pat = 0
        self._video_since_pmt = 0
        self._saw_video = False
        self._saw_keyframe = False

        self._video_pes = bytearray()
        self._au_active = False
        self._au_start = 0
        self._au_since_pat = 0
        self._au_since_pmt = 0
        self._au_pat_start = None
        self._au_pmt_start = None
        self._au_video_pid = None
        self._au_audio_ok = False

    # -- public API -----------------------------------------------------

    def feed(self, chunk: bytes) -> ProbeResult:
        if self._result is not None:
            return self._result
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("chunk must be bytes-like")
        if len(chunk) > self._max_bytes - len(self._buf):
            return self._reject("buffer_limit")
        self._buf.extend(chunk)

        while True:
            if not self._synced and not self._find_sync():
                break
            if self._pos + TS_PACKET_SIZE > len(self._buf):
                break
            if self._buf[self._pos] != SYNC_BYTE:
                self._synced = False
                continue
            offset = self._pos
            packet = bytes(self._buf[offset:offset + TS_PACKET_SIZE])
            self._pos += TS_PACKET_SIZE
            if self._handle_packet(packet, offset):
                return self._result
        return self._snapshot(NEED_MORE, self._last_reason)

    def finish(self) -> ProbeResult:
        if self._result is not None:
            return self._result
        self.feed(b"")
        if self._result is not None:
            return self._result
        if not self._buf:
            return self._reject("empty")
        if not self._synced:
            return self._reject("no_sync")
        if len(self._buf) - self._pos:
            return self._reject("truncated")
        if not self._saw_pat:
            return self._reject("missing_pat")
        if not self._saw_pmt:
            return self._reject("missing_pmt")
        if self._video_pid is None:
            return self._reject("no_h264")
        if self._audio_kind == "unsupported":
            return self._reject("unsupported_audio")
        if not self._saw_video:
            return self._reject("no_video")
        if not self._saw_keyframe:
            return self._reject("no_keyframe")
        return self._reject("incomplete_access_unit")

    # -- packet / PSI parsing -------------------------------------------

    def _find_sync(self) -> bool:
        limit = len(self._buf) - (TS_PACKET_SIZE * 3)
        if limit < 0:
            return False
        for index in range(self._pos, limit + 1):
            if (self._buf[index] == SYNC_BYTE and
                    self._buf[index + TS_PACKET_SIZE] == SYNC_BYTE and
                    self._buf[index + TS_PACKET_SIZE * 2] == SYNC_BYTE):
                self._pos = index
                self._synced = True
                return True
        self._pos = max(self._pos, len(self._buf) - (TS_PACKET_SIZE * 2))
        return False

    def _handle_packet(self, packet: bytes, offset: int) -> bool:
        if len(packet) != TS_PACKET_SIZE or packet[0] != SYNC_BYTE:
            return False
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        pusi = bool(packet[1] & 0x40)
        adaptation = (packet[3] >> 4) & 0x03
        payload_offset = 4
        if adaptation & 0x02:
            if payload_offset >= TS_PACKET_SIZE:
                return False
            payload_offset += 1 + packet[4]
        if not (adaptation & 0x01) or payload_offset >= TS_PACKET_SIZE:
            return False
        payload = packet[payload_offset:]
        self._packets += 1
        self._last_reason = "waiting_for_pat"

        if pid == PAT_PID:
            if pusi:
                self._pending_pat_start = offset
            for section in self._pat_asm.feed(payload, pusi):
                self._parse_pat(section, self._pending_pat_start)
        elif pid in self._pmt_asms:
            if pusi:
                self._pending_pmt_start[pid] = offset
            for section in self._pmt_asms[pid].feed(payload, pusi):
                start = self._pending_pmt_start.get(pid)
                self._parse_pmt(section, pid, start)
        elif pid == self._video_pid:
            if self._handle_video(payload, pusi, offset):
                return True
        elif pid == NULL_PID:
            return False
        return False

    def _parse_pat(self, section: bytes, start):
        if len(section) < 8 or section[0] != 0x00 or not (section[1] & 0x80):
            return
        length = ((section[1] & 0x0F) << 8) | section[2]
        if length + 3 != len(section) or mpeg_crc32(section) != 0:
            return
        body = section[3:3 + length - 4]
        if len(body) < 5 or body[3] != 0 or not (body[2] & 1):
            return
        programs = {}
        position = 5
        while position + 4 <= len(body):
            program = (body[position] << 8) | body[position + 1]
            pmt_pid = ((body[position + 2] & 0x1F) << 8) | body[position + 3]
            position += 4
            if program:
                programs[program] = pmt_pid
        if not programs:
            return
        self._pat_programs = programs
        self._pat_start = start
        self._saw_pat = True
        for pmt_pid in programs.values():
            self._pmt_asms.setdefault(pmt_pid, _SectionAssembler())
        self._video_since_pat = 0
        self._last_reason = "waiting_for_pmt"

    def _parse_pmt(self, section: bytes, pid: int, start):
        if len(section) < 12 or section[0] != 0x02 or not (section[1] & 0x80):
            return
        length = ((section[1] & 0x0F) << 8) | section[2]
        if length + 3 != len(section) or mpeg_crc32(section) != 0:
            return
        body = section[3:3 + length - 4]
        if len(body) < 9:
            return
        program = (body[0] << 8) | body[1]
        if self._pat_programs.get(program) != pid or not (body[2] & 1):
            return
        program_info_length = ((body[7] & 0x0F) << 8) | body[8]
        position = 9 + program_info_length
        if position > len(body):
            return
        video_pid = None
        audio_pids = []
        audio_ac3 = False
        audio_ac3_pid = None
        audio_seen = False
        while position + 5 <= len(body):
            stream_type = body[position]
            elementary_pid = ((body[position + 1] & 0x1F) << 8) | body[position + 2]
            info_length = ((body[position + 3] & 0x0F) << 8) | body[position + 4]
            info_start = position + 5
            info_end = info_start + info_length
            if info_end > len(body):
                return
            descriptors = body[info_start:info_end]
            if stream_type == STREAM_TYPE_H264 and video_pid is None:
                video_pid = elementary_pid
            if stream_type == STREAM_TYPE_AC3:
                audio_seen = True
                audio_pids.append(elementary_pid)
                audio_ac3 = True
                audio_ac3_pid = audio_ac3_pid or elementary_pid
            elif stream_type == PRIVATE_STREAM and self._has_ac3_descriptor(descriptors):
                audio_seen = True
                audio_pids.append(elementary_pid)
                audio_ac3 = True
                audio_ac3_pid = audio_ac3_pid or elementary_pid
            elif stream_type == PRIVATE_STREAM:
                audio_seen = True
                audio_pids.append(elementary_pid)
            elif stream_type in OTHER_AUDIO_TYPES:
                audio_seen = True
                audio_pids.append(elementary_pid)
            position = info_end

        self._saw_pmt = True
        if video_pid is None:
            if self._video_pid is None:
                self._last_reason = "no_h264"
            return
        if self._video_pid is not None and self._video_pid != video_pid:
            return
        self._video_pid = video_pid
        self._pmt_pid = pid
        self._selected_program = program
        self._pmt_start = start
        self._audio_pids = audio_pids
        self._audio_pid = audio_ac3_pid if audio_ac3 else (audio_pids[0] if audio_pids else None)
        if not audio_seen:
            self._audio_kind = "none"
            self._audio_ok = True
        elif audio_ac3:
            self._audio_kind = "ac3"
            self._audio_ok = True
        else:
            self._audio_kind = "unsupported"
            self._audio_ok = False
        self._video_since_pmt = 0
        self._last_reason = "waiting_for_video"

    @staticmethod
    def _has_ac3_descriptor(descriptors: bytes) -> bool:
        position = 0
        while position + 2 <= len(descriptors):
            tag = descriptors[position]
            length = descriptors[position + 1]
            end = position + 2 + length
            if end > len(descriptors):
                return False
            payload = descriptors[position + 2:end]
            if tag == AC3_DESCRIPTOR:
                return True
            if tag == REGISTRATION_DESCRIPTOR and payload[:4] == b"AC-3":
                return True
            position = end
        return False

    # -- video PES / H.264 access unit ----------------------------------

    def _handle_video(self, payload: bytes, pusi: bool, offset: int) -> bool:
        if pusi:
            if self._video_pes and self._finalize_access_unit(offset):
                return True
            self._video_pes = bytearray()
            self._au_active = True
            self._au_start = offset
            self._au_since_pat = self._video_since_pat
            self._au_since_pmt = self._video_since_pmt
            self._au_pat_start = self._pat_start
            self._au_pmt_start = self._pmt_start
            self._au_video_pid = self._video_pid
            self._au_audio_ok = self._audio_ok
        if not self._au_active:
            return False
        self._saw_video = True
        self._video_pes.extend(payload)
        self._video_since_pat += 1
        self._video_since_pmt += 1
        if len(self._video_pes) > self._max_bytes:
            self._reject("buffer_limit")
            return True
        return False

    def _finalize_access_unit(self, end_offset: int) -> bool:
        pes = bytes(self._video_pes)
        keyframe = self._is_keyframe_pes(pes)
        if keyframe:
            self._saw_keyframe = True
        adjacent = (
            self._au_since_pat == 0 and
            self._au_since_pmt == 0 and
            self._au_pat_start is not None and
            self._au_pmt_start is not None and
            self._au_pat_start < self._au_pmt_start < self._au_start
        )
        if (keyframe and adjacent and self._au_audio_ok and
                self._au_video_pid == self._video_pid):
            selected = bytes(self._buf[self._au_pat_start:end_offset])
            self._result = ProbeResult(
                READY, "ready", selected=selected,
                video_pid=self._video_pid, audio_pid=self._audio_pid,
                audio_kind=self._audio_kind, packets=self._packets,
                buffered=len(self._buf), selected_start=self._au_pat_start,
                selected_end=end_offset,
                trailing=bytes(self._buf[end_offset:]),
            )
            self._buf.clear()
            return True
        if not adjacent:
            self._last_reason = "psi_not_adjacent"
        elif not keyframe:
            self._last_reason = "no_keyframe"
        elif not self._au_audio_ok:
            self._last_reason = "unsupported_audio"
        self._video_pes = bytearray()
        self._au_active = False
        return False

    @staticmethod
    def _pes_elementary_stream(pes: bytes):
        if len(pes) < 9 or pes[0:3] != b"\x00\x00\x01":
            return None
        if not 0xE0 <= pes[3] <= 0xEF or (pes[6] & 0xC0) != 0x80:
            return None
        packet_length = (pes[4] << 8) | pes[5]
        header_length = pes[8]
        payload_offset = 9 + header_length
        end = len(pes) if packet_length == 0 else 6 + packet_length
        if payload_offset > end or end > len(pes):
            return None
        return pes[payload_offset:end]

    @staticmethod
    def _has_decodable_keyframe(es: bytes) -> bool:
        if not es:
            return False
        sps = pps = False
        first_vcl = None
        sps_before = pps_before = False
        position = 0
        while True:
            marker = es.find(b"\x00\x00\x01", position)
            if marker < 0 or marker + 3 >= len(es):
                break
            header = es[marker + 3]
            if header & 0x80:
                return False
            nal_type = header & 0x1F
            if nal_type == 7:
                sps = True
            elif nal_type == 8:
                pps = True
            elif nal_type in (1, 5) and first_vcl is None:
                first_vcl = nal_type
                sps_before = sps
                pps_before = pps
            position = marker + 3
        return first_vcl == 5 and sps_before and pps_before

    def _is_keyframe_pes(self, pes: bytes) -> bool:
        es = self._pes_elementary_stream(pes)
        return bool(es) and self._has_decodable_keyframe(es)

    # -- results --------------------------------------------------------

    def _snapshot(self, status: str, reason: str) -> ProbeResult:
        return ProbeResult(status, reason, video_pid=self._video_pid,
                           audio_pid=self._audio_pid, audio_kind=self._audio_kind,
                           packets=self._packets, buffered=len(self._buf))

    def _reject(self, reason: str) -> ProbeResult:
        self._result = self._snapshot(REJECT, reason)
        self._buf.clear()
        return self._result
