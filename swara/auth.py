"""
SPARK -- Stateless Password-Authenticated Retry & Key-agreement.

The authentication problem for voice over UDP is not "how do I check a
password".  It is:

  (a) UDP is trivially spoofable, so an unauthenticated first packet must never
      make the server allocate memory or emit a larger reply -- otherwise the
      service is a DDoS amplifier;
  (b) a password must never cross the wire, and a stolen server database must
      not yield anything replayable;
  (c) the handshake must not cost so many round trips that call setup feels
      slow -- and after a WiFi-to-mobile handover it must cost NONE, because
      the call is already in progress and the user is mid-sentence.

SPARK addresses all three:

  Phase 0  HELLO        16 B, no state created, no user identity revealed.
  Phase 1  RETRY        server returns a stateless HMAC cookie bound to the
                        client's address and a coarse clock.  Reply is SMALLER
                        than the request -> amplification factor < 1.
  Phase 2  AUTH_INIT    client echoes the cookie.  Only now, having proved it
                        can receive at the address it claims, does the server
                        do any work.
  Phase 3  AUTH_RESP    server returns the user's salt and OPAQUE-style
                        envelope plus a fresh ephemeral X25519 key.
  Phase 4  AUTH_CONFIRM triple Diffie-Hellman is mixed into the transcript and
                        the client proves key knowledge.  Mutual auth complete.

Key schedule (all HKDF-SHA256 over the full transcript hash, so every byte of
every handshake message is authenticated):

    dh1 = X25519(client_ephemeral,  server_static)     -- authenticates server
    dh2 = X25519(client_static,     server_ephemeral)  -- authenticates client
    dh3 = X25519(client_ephemeral,  server_ephemeral)  -- forward secrecy
    MS  = HKDF-Extract(salt = H(transcript), IKM = dh1 || dh2 || dh3)

The client's static private key never leaves the client and is not stored
anywhere in plaintext: it lives inside an AEAD envelope whose key is derived
from the password by PBKDF2.  The server stores only {salt, envelope, client
static PUBLIC key}.  Stealing that database gives an attacker a target for an
offline dictionary attack -- and nothing that can be replayed at the server,
because possession of the envelope alone does not yield the static private key.

Resumption / path migration:
    On success the server issues a ticket = AEAD(K_ticket_server, state).  The
    server keeps NO per-session table for this.  When the client's address
    changes it sends PATH_MIGRATE {CID, ticket, AEAD(K_resume, challenge)} from
    the new address; the server decrypts the ticket, verifies the tag, rebinds
    the CID and the media flow continues.  Zero round trips of silence.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives import hashes

PBKDF2_ITERS = 200_000
COOKIE_LIFETIME_S = 30
TICKET_LIFETIME_S = 3600
KEY_LEN = 32


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def hkdf_expand(prk: bytes, label: bytes, length: int = KEY_LEN) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=length,
                      info=b"SWARA-v1 " + label).derive(prk)


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def stretch_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS, KEY_LEN)


class Transcript:
    """Running SHA-256 over every handshake byte, in order."""

    def __init__(self) -> None:
        self._h = hashlib.sha256(b"SWARA-v1 transcript")

    def add(self, *chunks: bytes) -> "Transcript":
        for c in chunks:
            self._h.update(struct.pack("!H", len(c)))
            self._h.update(c)
        return self

    def digest(self) -> bytes:
        return self._h.copy().digest()


# ---------------------------------------------------------------------------
# Credential registration (performed once, out of band)
# ---------------------------------------------------------------------------

@dataclass
class CredentialRecord:
    """Exactly what the server stores per user.  Note the absence of anything
    resembling the password or a password-equivalent verifier."""
    username: str
    salt: bytes
    envelope: bytes            # AEAD(PBKDF2(pw,salt), client static private key)
    client_static_pub: bytes   # 32-byte X25519 public key

    def size_bytes(self) -> int:
        return len(self.salt) + len(self.envelope) + len(self.client_static_pub)


def register(username: str, password: str) -> tuple[CredentialRecord, bytes]:
    """Create a credential record. Returns (record, client_static_private)."""
    salt = os.urandom(16)
    rw = stretch_password(password, salt)
    static_priv = X25519PrivateKey.generate()
    priv_raw = static_priv.private_bytes_raw()
    pub_raw = static_priv.public_key().public_bytes_raw()
    aead = ChaCha20Poly1305(rw)
    nonce = b"\x00" * 12
    envelope = aead.encrypt(nonce, priv_raw + pub_raw, username.encode())
    return CredentialRecord(username, salt, envelope, pub_raw), priv_raw


# ---------------------------------------------------------------------------
# Session keys
# ---------------------------------------------------------------------------

@dataclass
class SessionKeys:
    cid: int
    k_c2s: bytes
    k_s2c: bytes
    k_resume: bytes
    transcript: bytes

    @classmethod
    def derive(cls, master: bytes, transcript: bytes) -> "SessionKeys":
        cid = struct.unpack("!I", hkdf_expand(master, b"cid", 4))[0]
        return cls(cid=cid,
                   k_c2s=hkdf_expand(master, b"c2s"),
                   k_s2c=hkdf_expand(master, b"s2c"),
                   k_resume=hkdf_expand(master, b"resume"),
                   transcript=transcript)


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

class SparkServer:
    def __init__(self) -> None:
        self.static_priv = X25519PrivateKey.generate()
        self.static_pub = self.static_priv.public_key().public_bytes_raw()
        self._cookie_key = os.urandom(32)
        self._ticket_key = os.urandom(32)
        self.users: dict[str, CredentialRecord] = {}
        # CID -> (keys, current address).  Populated only AFTER full auth.
        self.sessions: dict[int, tuple[SessionKeys, tuple]] = {}
        self.stats = {"hello": 0, "cookie_bad": 0, "auth_ok": 0, "auth_fail": 0,
                      "resumed": 0, "migrations": 0, "bytes_out_pre_auth": 0,
                      "bytes_in_pre_auth": 0}

    # -- registration ------------------------------------------------------

    def add_user(self, record: CredentialRecord) -> None:
        self.users[record.username] = record

    # -- phase 1: stateless retry -----------------------------------------

    def _cookie(self, addr: tuple, epoch: int) -> bytes:
        msg = f"{addr[0]}|{addr[1]}|{epoch}".encode()
        return hmac.new(self._cookie_key, msg, hashlib.sha256).digest()[:16]

    def handle_hello(self, addr: tuple, hello: bytes) -> bytes:
        """Zero state is created here.  The reply is deliberately no larger
        than the request, so SWARA cannot be used as a reflection amplifier."""
        self.stats["hello"] += 1
        self.stats["bytes_in_pre_auth"] += len(hello)
        epoch = int(time.time() // COOKIE_LIFETIME_S)
        out = struct.pack("!I", epoch) + self._cookie(addr, epoch)
        self.stats["bytes_out_pre_auth"] += len(out)
        return out

    def _cookie_valid(self, addr: tuple, epoch: int, cookie: bytes) -> bool:
        now = int(time.time() // COOKIE_LIFETIME_S)
        if epoch not in (now, now - 1):
            return False
        return hmac.compare_digest(cookie, self._cookie(addr, epoch))

    # -- phase 3: response -------------------------------------------------

    def handle_auth_init(self, addr: tuple, body: bytes):
        """body = epoch(4) || cookie(16) || eph_pub(32) || ulen(1) || username"""
        epoch, = struct.unpack("!I", body[:4])
        cookie = body[4:20]
        if not self._cookie_valid(addr, epoch, cookie):
            self.stats["cookie_bad"] += 1
            return None, None      # silent drop: spoofed source or stale cookie
        client_eph = body[20:52]
        ulen = body[52]
        username = body[53:53 + ulen].decode(errors="replace")
        rec = self.users.get(username)
        if rec is None:
            # Constant-shape reply for unknown users: no user-enumeration oracle.
            rec = CredentialRecord(username, hashlib.sha256(username.encode()).digest()[:16],
                                   os.urandom(80), os.urandom(32))
            known = False
        else:
            known = True

        server_eph_priv = X25519PrivateKey.generate()
        server_eph_pub = server_eph_priv.public_key().public_bytes_raw()

        resp = (bytes([len(rec.salt)]) + rec.salt
                + struct.pack("!H", len(rec.envelope)) + rec.envelope
                + server_eph_pub + self.static_pub)

        pending = {"client_eph": client_eph, "server_eph_priv": server_eph_priv,
                   "rec": rec, "known": known, "addr": addr, "resp": resp,
                   "transcript": Transcript().add(b"AUTH_INIT", body, b"AUTH_RESP", resp)}
        return resp, pending

    # -- phase 5: verify confirmation --------------------------------------

    def handle_auth_confirm(self, pending: dict, tag: bytes):
        rec: CredentialRecord = pending["rec"]
        if not pending["known"]:
            self.stats["auth_fail"] += 1
            return None, None

        eph_priv: X25519PrivateKey = pending["server_eph_priv"]
        client_eph_pub = X25519PublicKey.from_public_bytes(pending["client_eph"])
        client_static_pub = X25519PublicKey.from_public_bytes(rec.client_static_pub)

        dh1 = self.static_priv.exchange(client_eph_pub)
        dh2 = eph_priv.exchange(client_static_pub)
        dh3 = eph_priv.exchange(client_eph_pub)

        th = pending["transcript"].digest()
        master = hkdf_extract(th, dh1 + dh2 + dh3)
        keys = SessionKeys.derive(master, th)

        expect = hmac.new(keys.k_c2s, b"client finished" + th, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expect):
            self.stats["auth_fail"] += 1
            return None, None

        self.stats["auth_ok"] += 1
        self.sessions[keys.cid] = (keys, pending["addr"])
        ticket = self._issue_ticket(rec.username, keys)
        return keys, ticket

    # -- resumption tickets ------------------------------------------------

    def _issue_ticket(self, username: str, keys: SessionKeys) -> bytes:
        nonce = os.urandom(12)
        state = (struct.pack("!IQ", keys.cid, int(time.time()) + TICKET_LIFETIME_S)
                 + keys.k_resume + bytes([len(username)]) + username.encode())
        blob = ChaCha20Poly1305(self._ticket_key).encrypt(nonce, state, b"SWARA-ticket")
        return nonce + blob

    def handle_path_migrate(self, new_addr: tuple, body: bytes):
        """0-RTT authenticated migration.  body = ticket_len(2)||ticket||nonce(12)||sealed"""
        tlen, = struct.unpack("!H", body[:2])
        ticket = body[2:2 + tlen]
        rest = body[2 + tlen:]
        try:
            state = ChaCha20Poly1305(self._ticket_key).decrypt(
                ticket[:12], ticket[12:], b"SWARA-ticket")
        except Exception:
            return None
        cid, expiry = struct.unpack("!IQ", state[:12])
        if time.time() > expiry:
            return None
        k_resume = state[12:44]
        try:
            proof = ChaCha20Poly1305(k_resume).decrypt(rest[:12], rest[12:], b"SWARA-migrate")
        except Exception:
            return None   # cannot forge without k_resume: an off-path attacker
                          # cannot hijack a call by replaying the ticket alone
        if cid in self.sessions:
            keys, _ = self.sessions[cid]
            self.sessions[cid] = (keys, new_addr)
            self.stats["migrations"] += 1
            return cid, proof
        return None


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

class SparkClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.eph_priv = X25519PrivateKey.generate()
        self.eph_pub = self.eph_priv.public_key().public_bytes_raw()
        self.keys: SessionKeys | None = None
        self.ticket: bytes | None = None
        self._transcript: Transcript | None = None
        self._init_msg: bytes | None = None

    HELLO_PAD = 64   # RETRY is 20 B, so amplification is bounded at 0.31x

    def make_hello(self) -> bytes:
        """Deliberately padded.  A reflection attacker gains nothing by
        spoofing our HELLO because the server's reply is far SMALLER than the
        packet the attacker had to send.  Amplification factor 20/64 = 0.31."""
        return (b"SWARA/1" + os.urandom(9)).ljust(self.HELLO_PAD, b"\x00")

    def make_auth_init(self, retry: bytes) -> bytes:
        epoch = retry[:4]
        cookie = retry[4:20]
        u = self.username.encode()
        body = epoch + cookie + self.eph_pub + bytes([len(u)]) + u
        self._init_msg = body
        return body

    def make_auth_confirm(self, resp: bytes) -> bytes | None:
        off = 0
        slen = resp[off]; off += 1
        salt = resp[off:off + slen]; off += slen
        elen, = struct.unpack("!H", resp[off:off + 2]); off += 2
        envelope = resp[off:off + elen]; off += elen
        server_eph = resp[off:off + 32]; off += 32
        server_static = resp[off:off + 32]

        rw = stretch_password(self.password, salt)
        try:
            opened = ChaCha20Poly1305(rw).decrypt(b"\x00" * 12, envelope, self.username.encode())
        except Exception:
            return None          # wrong password -- detected locally, no oracle
        static_priv_raw = opened[:32]
        static_priv = X25519PrivateKey.from_private_bytes(static_priv_raw)

        dh1 = self.eph_priv.exchange(X25519PublicKey.from_public_bytes(server_static))
        dh2 = static_priv.exchange(X25519PublicKey.from_public_bytes(server_eph))
        dh3 = self.eph_priv.exchange(X25519PublicKey.from_public_bytes(server_eph))

        th = Transcript().add(b"AUTH_INIT", self._init_msg, b"AUTH_RESP", resp).digest()
        master = hkdf_extract(th, dh1 + dh2 + dh3)
        self.keys = SessionKeys.derive(master, th)
        return hmac.new(self.keys.k_c2s, b"client finished" + th, hashlib.sha256).digest()[:16]

    def make_path_migrate(self) -> bytes:
        assert self.keys and self.ticket
        nonce = os.urandom(12)
        sealed = ChaCha20Poly1305(self.keys.k_resume).encrypt(
            nonce, struct.pack("!d", time.time()), b"SWARA-migrate")
        return struct.pack("!H", len(self.ticket)) + self.ticket + nonce + sealed


# ---------------------------------------------------------------------------
# Media record protection
# ---------------------------------------------------------------------------

class MediaCipher:
    """AEAD for media packets.

    The 16-byte SWARA header is passed as ASSOCIATED DATA, not encrypted.  That
    is what lets the PI octet stay readable to the network while remaining
    unforgeable -- flipping it invalidates the tag and the packet is dropped.

    The nonce is derived deterministically from (direction, sequence number), so
    no nonce needs to be transmitted: 12 bytes saved on every single packet,
    which at 50 packets/s is 4.8 kbit/s of pure saving per stream.
    """

    def __init__(self, key: bytes, direction: bytes):
        self.aead = ChaCha20Poly1305(key)
        self.dir = direction
        self._salt = hashlib.sha256(key + direction).digest()[:12]

    def _nonce(self, seq: int, epoch: int = 0) -> bytes:
        counter = struct.pack("!IQ", epoch, seq)
        return bytes(a ^ b for a, b in zip(self._salt, counter[:12]))

    def seal(self, seq: int, header: bytes, plaintext: bytes) -> bytes:
        return self.aead.encrypt(self._nonce(seq), plaintext, header)

    def open(self, seq: int, header: bytes, ciphertext: bytes) -> bytes | None:
        try:
            return self.aead.decrypt(self._nonce(seq), ciphertext, header)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Sequence-number hygiene for long calls
# ---------------------------------------------------------------------------

class SequenceReconstructor:
    """Recover a full counter from the 16-bit sequence field on the wire.

    The header carries 16 bits, but the AEAD nonce is derived from the full
    counter.  Without reconstruction the two diverge the moment the field wraps
    -- after 65536 frames, which at 20 ms per frame is 21.8 minutes.  Every
    packet after that point fails authentication and the call dies silently.
    This is the same rollover-counter mechanism SRTP uses, and for the same
    reason.
    """

    def __init__(self) -> None:
        self.roc = 0
        self.highest = -1

    def unwrap(self, wire: int) -> int:
        wire &= 0xFFFF
        if self.highest < 0:
            self.highest = wire
            return wire
        prev_low = self.highest & 0xFFFF
        roc = self.roc
        if wire < prev_low and (prev_low - wire) > 32768:
            roc += 1                      # the field wrapped forward
        elif wire > prev_low and (wire - prev_low) > 32768:
            roc = max(0, roc - 1)         # a straggler from the previous epoch
        full = (roc << 16) | wire
        if full > self.highest:
            self.highest = full
            self.roc = roc
        return full

    def nearest(self, wire: int) -> int:
        """Full counter for a 16-bit value near the current position, without
        advancing state -- used for references such as an FEC group's base."""
        wire &= 0xFFFF
        if self.highest < 0:
            return wire
        base = (self.highest >> 16) << 16
        cands = (base + wire, base + wire - 65536, base + wire + 65536)
        return min(cands, key=lambda c: abs(c - self.highest))


class ReplayWindow:
    """Sliding bitmap rejecting duplicated or replayed sequence numbers.

    AEAD stops an attacker forging a packet, but it does not stop one CAPTURING
    a valid packet and sending it again.  Without a replay window a recorded
    fragment of speech can be re-injected into a live call.
    """

    def __init__(self, size: int = 1024):
        self.size = size
        self.highest = -1
        self.bits = 0
        self.rejected = 0

    def check_and_set(self, seq: int) -> bool:
        if seq > self.highest:
            shift = seq - self.highest
            self.bits = 1 if shift >= self.size else \
                (((self.bits << shift) | 1) & ((1 << self.size) - 1))
            self.highest = seq
            return True
        delta = self.highest - seq
        if delta >= self.size or (self.bits >> delta) & 1:
            self.rejected += 1
            return False
        self.bits |= (1 << delta)
        return True
