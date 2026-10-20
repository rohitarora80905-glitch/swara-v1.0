"""
SWARA wire protocol -- datagram formats.

Every SWARA datagram is a single UDP payload. There is no stream, no
retransmission-by-default, and no head-of-line blocking: a lost packet is a lost
20 ms of audio and nothing more.

Fixed header (16 bytes, network byte order):

   0                   1                   2                   3
   0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
  +-------+-------+---------------+-------------------------------+
  | Ver   | Type  |     Flags     |        Sequence Number        |
  +-------+-------+---------------+-------------------------------+
  |                       Connection ID (CID)                     |
  +---------------------------------------------------------------+
  |                    Timestamp (milliseconds)                   |
  +---------------+---------------+---------------+---------------+
  |   PI Score    |   FEC Group   |   FEC Index   |    Reserved   |
  +---------------+---------------+---------------+---------------+

The PI Score octet is the heart of the design.  It is *authenticated but not
encrypted* (it travels as AEAD associated data), so any element on the path --
the receiver, a relay, or a DiffServ-aware router -- can read how perceptually
important the enclosed 20 ms of speech is WITHOUT being able to read or forge
the speech itself.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

VERSION = 1
HEADER_LEN = 16
AEAD_TAG_LEN = 16
MAX_DATAGRAM = 1200  # stay under the smallest plausible path MTU, no fragmentation


class PType:
    """Datagram type codes (4 bits)."""
    HELLO = 0          # client -> server, stateless probe
    RETRY = 1          # server -> client, anti-amplification cookie
    AUTH_INIT = 2      # client -> server, cookie + ephemeral public key
    AUTH_RESP = 3      # server -> client, salt + envelope + ephemeral public key
    AUTH_CONFIRM = 4   # client -> server, key-confirmation tag
    AUTH_TICKET = 5    # server -> client, encrypted resumption ticket
    MEDIA = 6          # a coded speech frame
    FEC = 7            # a Reed-Solomon parity packet
    ANCHOR = 8         # perceptual anchor replica (duplicate of a high-PI frame)
    TELEMETRY = 9      # standalone telemetry (normally piggybacked instead)
    NACK = 10          # selective negative acknowledgement
    PATH_MIGRATE = 11  # 0-RTT authenticated path migration
    PATH_ACK = 12      # path validation response
    OVERLAY_LSA = 13   # SETU overlay link-state / distance-vector advertisement
    BYE = 14
    CONTROL = 15       # encrypted signalling: call setup, presence, hangup

    NAMES = {
        0: "HELLO", 1: "RETRY", 2: "AUTH_INIT", 3: "AUTH_RESP",
        4: "AUTH_CONFIRM", 5: "AUTH_TICKET", 6: "MEDIA", 7: "FEC",
        8: "ANCHOR", 9: "TELEMETRY", 10: "NACK", 11: "PATH_MIGRATE",
        12: "PATH_ACK", 13: "OVERLAY_LSA", 14: "BYE", 15: "CONTROL",
    }


class Flag:
    """Header flag bits."""
    TELEMETRY_PIGGYBACK = 0x01  # a 16-byte telemetry report follows the header
    MARKER = 0x02               # first frame of a talkspurt
    DSCP_EF = 0x04              # sender requested Expedited Forwarding
    ENCRYPTED = 0x08            # payload is AEAD-protected
    LAST_IN_GROUP = 0x10        # final data frame of an FEC group
    RESUMED = 0x20              # session was resumed 0-RTT


_HDR = struct.Struct("!BBHIIBBBB")


@dataclass
class Header:
    ptype: int
    flags: int = 0
    seq: int = 0
    cid: int = 0
    ts_ms: int = 0
    pi: int = 0            # perceptual importance, 0..255
    fec_group: int = 0
    fec_index: int = 0
    reserved: int = 0
    version: int = VERSION

    def pack(self) -> bytes:
        ver_type = ((self.version & 0x0F) << 4) | (self.ptype & 0x0F)
        return _HDR.pack(
            ver_type, self.flags & 0xFF, self.seq & 0xFFFF, self.cid & 0xFFFFFFFF,
            self.ts_ms & 0xFFFFFFFF, self.pi & 0xFF, self.fec_group & 0xFF,
            self.fec_index & 0xFF, self.reserved & 0xFF,
        )

    @classmethod
    def unpack(cls, buf: bytes) -> "Header":
        if len(buf) < HEADER_LEN:
            raise ValueError("short datagram")
        ver_type, flags, seq, cid, ts, pi, grp, idx, rsv = _HDR.unpack(buf[:HEADER_LEN])
        return cls(
            ptype=ver_type & 0x0F, version=(ver_type >> 4) & 0x0F, flags=flags,
            seq=seq, cid=cid, ts_ms=ts, pi=pi, fec_group=grp, fec_index=idx,
            reserved=rsv,
        )

    def __str__(self) -> str:
        return (f"<{PType.NAMES.get(self.ptype, '?')} seq={self.seq} cid={self.cid:08x} "
                f"pi={self.pi} grp={self.fec_group} idx={self.fec_index}>")


@dataclass
class Datagram:
    header: Header
    payload: bytes = b""
    telemetry: bytes = b""   # optional 16-byte piggybacked report

    def to_bytes(self) -> bytes:
        if self.telemetry:
            self.header.flags |= Flag.TELEMETRY_PIGGYBACK
        return self.header.pack() + self.telemetry + self.payload

    @classmethod
    def from_bytes(cls, buf: bytes) -> "Datagram":
        h = Header.unpack(buf)
        off = HEADER_LEN
        tel = b""
        if h.flags & Flag.TELEMETRY_PIGGYBACK:
            tel = buf[off:off + 16]
            off += 16
        return cls(header=h, payload=buf[off:], telemetry=tel)

    @property
    def size(self) -> int:
        return HEADER_LEN + len(self.telemetry) + len(self.payload)


# --------------------------------------------------------------------------
# Integrity primitives.
#
# These exist partly because we genuinely use them, and partly because the
# course syllabus asks for error detection and correction -- so we implement
# the classic Data Link Layer codes and then show, with measurements, why a
# real-time voice transport uses a detection code (CRC) plus erasure coding
# rather than a bit-level correcting code (Hamming).
# --------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    """CRC-32 (IEEE 802.3 polynomial) -- the same code Ethernet puts in its FCS."""
    return zlib.crc32(data) & 0xFFFFFFFF


def internet_checksum(data: bytes) -> int:
    """RFC 1071 one's-complement checksum -- the code UDP itself carries."""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def parity_2d(block: bytes, width: int = 8) -> bytes:
    """Two-dimensional even parity: one parity bit per row and per column.

    Detects all 1-, 2- and 3-bit errors and corrects any single-bit error.
    """
    rows = [block[i:i + width] for i in range(0, len(block), width)]
    row_par = bytes((sum(bin(b).count("1") for b in r) & 1) for r in rows)
    col_par = bytearray(width)
    for r in rows:
        for j, b in enumerate(r):
            col_par[j] ^= b
    return row_par + bytes(col_par)


_HAMMING_G = [0b1101, 0b1011, 0b1000, 0b0111, 0b0100, 0b0010, 0b0001]


def hamming74_encode(nibble: int) -> int:
    """Hamming(7,4): 4 data bits -> 7 code bits, corrects any single-bit error."""
    d = [(nibble >> i) & 1 for i in (3, 2, 1, 0)]
    p1 = d[0] ^ d[1] ^ d[3]
    p2 = d[0] ^ d[2] ^ d[3]
    p3 = d[1] ^ d[2] ^ d[3]
    return (p1 << 6) | (p2 << 5) | (d[0] << 4) | (p3 << 3) | (d[1] << 2) | (d[2] << 1) | d[3]


def hamming74_decode(code: int) -> tuple[int, bool]:
    """Return (4-bit data, corrected?) after single-error correction."""
    b = [(code >> i) & 1 for i in range(6, -1, -1)]  # b[0]=p1 .. b[6]=d4
    s1 = b[0] ^ b[2] ^ b[4] ^ b[6]
    s2 = b[1] ^ b[2] ^ b[5] ^ b[6]
    s3 = b[3] ^ b[4] ^ b[5] ^ b[6]
    syn = (s3 << 2) | (s2 << 1) | s1
    corrected = False
    if syn:
        b[syn - 1] ^= 1
        corrected = True
    return (b[2] << 3) | (b[4] << 2) | (b[5] << 1) | b[6], corrected
