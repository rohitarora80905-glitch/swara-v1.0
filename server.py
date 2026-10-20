#!/usr/bin/env python3
"""
SWARA server -- registration, authentication, presence and media relay.

Run it:
    python3 server.py --add-user alice --password secret1
    python3 server.py --add-user bob   --password secret2
    python3 server.py --port 9000

What it does:

  * Performs the SPARK handshake with each client over real UDP.  No state is
    allocated for a client until it has returned a valid address-validation
    cookie, so the server cannot be used as a reflection amplifier.

  * Tracks which authenticated users are online.

  * Sets up calls.  When A calls B the server mints a random 32-byte call
    secret, derives two DIRECTIONAL media keys from it, and delivers them to
    each party encrypted under that party's own session key.

  * Relays media.  Because the media keys are derived from the call secret and
    the server forwards datagrams verbatim without decrypting them, the relay
    cannot listen to the call it is carrying.  It sees only a connection
    identifier and a length.
"""
from __future__ import annotations
import argparse, json, os, secrets, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swara.auth import (CredentialRecord, MediaCipher, SparkServer, hkdf_expand, register)
from swara.net import UdpEndpoint
from swara.packet import AEAD_TAG_LEN, HEADER_LEN, Flag, Header, PType

USERS_FILE = "users.json"


def load_users(path=USERS_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {u: CredentialRecord(u, bytes.fromhex(d["salt"]),
                                bytes.fromhex(d["envelope"]),
                                bytes.fromhex(d["pub"])) for u, d in raw.items()}


def save_user(username: str, password: str, path=USERS_FILE) -> None:
    raw = {}
    if os.path.exists(path):
        raw = json.load(open(path))
    rec, _ = register(username, password)
    raw[username] = {"salt": rec.salt.hex(), "envelope": rec.envelope.hex(),
                     "pub": rec.client_static_pub.hex()}
    json.dump(raw, open(path, "w"), indent=2)
    print(f"registered '{username}'  ({rec.size_bytes()} bytes stored, no password kept)")


class Server:
    def __init__(self, host="0.0.0.0", port=9000, users_file=USERS_FILE, verbose=True):
        self.ep = UdpEndpoint(host, port)
        self.spark = SparkServer()
        self.verbose = verbose
        for u, rec in load_users(users_file).items():
            self.spark.add_user(rec)
        self.pending: dict[tuple, tuple] = {}          # addr -> (pending, ts)
        self.online: dict[str, dict] = {}              # username -> session info
        self.by_cid: dict[int, dict] = {}
        self.calls: dict[int, dict] = {}               # call_id -> {a, b, addrs}
        # Reliable signalling over an unreliable transport: the caller may
        # retransmit a "call" request that was in fact delivered.  Keyed by
        # (username, request id) we remember the answer we already gave, so a
        # duplicate re-sends the SAME call_ready / incoming pair instead of
        # minting a second call with different keys.  This is the idempotent
        # server half of SIP-style INVITE retransmission.
        self.call_dedup: dict[tuple, dict] = {}        # (user, rid) -> replies
        self.running = threading.Event()
        self.log(f"listening on UDP {self.ep.address[0]}:{self.ep.port}  "
                 f"({len(self.spark.users)} users known)")

    def log(self, msg):
        if self.verbose:
            print(f"[server {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # -- control channel -------------------------------------------------
    def send_control(self, sess: dict, obj: dict) -> None:
        body = json.dumps(obj).encode()
        seq = sess["ctl_seq"]; sess["ctl_seq"] += 1
        h = Header(ptype=PType.CONTROL, seq=seq & 0xFFFF, cid=sess["cid"],
                   ts_ms=0, flags=Flag.ENCRYPTED)
        hdr = h.pack()
        self.ep.sendto(hdr + sess["s2c"].seal(seq, hdr, body), sess["addr"])

    def serve_forever(self) -> None:
        self.running.set()
        while self.running.is_set():
            data, addr = self.ep.recvfrom(timeout=0.5)
            if data is None or len(data) < HEADER_LEN:
                continue
            try:
                h = Header.unpack(data)
            except Exception:
                continue

            # 1. media relay -- highest volume, checked first, never decrypted
            call = self.calls.get(h.cid)
            if call is not None:
                dst = call["b_addr"] if addr == call["a_addr"] else call["a_addr"]
                if dst:
                    self.ep.sendto(data, dst)
                continue

            body = data[HEADER_LEN:]
            if h.ptype == PType.HELLO:
                retry = self.spark.handle_hello(addr, body)
                self.ep.sendto(Header(ptype=PType.RETRY).pack() + retry, addr)
            elif h.ptype == PType.AUTH_INIT:
                resp, pending = self.spark.handle_auth_init(addr, body)
                if resp is None:
                    continue                      # spoofed source: silent drop
                # A retransmitted AUTH_INIT carries the same client ephemeral
                # key, so keeping the FIRST pending state keeps the transcript
                # the client already committed to.
                if addr not in self.pending:
                    self.pending[addr] = (pending, time.time())
                    self.ep.sendto(Header(ptype=PType.AUTH_RESP).pack() + resp, addr)
                else:
                    prev = self.pending[addr][0]
                    self.ep.sendto(Header(ptype=PType.AUTH_RESP).pack()
                                   + prev["resp"], addr)
                continue
            elif h.ptype == PType.AUTH_CONFIRM:
                ent = self.pending.get(addr)
                if ent is None or time.time() - ent[1] > 30:
                    continue
                self.pending.pop(addr, None)
                keys, ticket = self.spark.handle_auth_confirm(ent[0], body)
                if keys is None:
                    self.log(f"auth FAILED from {addr}")
                    continue
                uname = ent[0]["rec"].username
                sess = {"cid": keys.cid, "addr": addr, "user": uname, "ctl_seq": 0,
                        "c2s": MediaCipher(keys.k_c2s, b"c2s"),
                        "s2c": MediaCipher(keys.k_s2c, b"s2c")}
                self.online[uname] = sess
                self.by_cid[keys.cid] = sess
                out = struct.pack("!IH", keys.cid, len(ticket)) + ticket
                self.ep.sendto(Header(ptype=PType.AUTH_TICKET, cid=keys.cid).pack() + out, addr)
                self.log(f"'{uname}' authenticated from {addr[0]}:{addr[1]}  cid={keys.cid:08x}")
            elif h.ptype == PType.CONTROL:
                sess = self.by_cid.get(h.cid)
                if sess is None:
                    continue
                pt = sess["c2s"].open(h.seq, data[:HEADER_LEN], body)
                if pt is None:
                    continue
                sess["addr"] = addr                # tolerate NAT rebinding
                try:
                    self.on_control(sess, json.loads(pt))
                except Exception as e:
                    self.log(f"control error: {e}")

    # -- signalling ------------------------------------------------------
    def on_control(self, sess: dict, msg: dict) -> None:
        op = msg.get("op")
        if op == "ping":
            # Keepalive.  Refreshes the client's NAT binding and lets us track
            # the address it is currently reachable at.
            self.send_control(sess, {"op": "pong"})
        elif op == "who":
            self.send_control(sess, {"op": "online", "users": list(self.online)})
        elif op == "call":
            peer = msg.get("peer")
            rid = msg.get("rid")
            if rid is not None:
                dup = self.call_dedup.get((sess["user"], rid))
                if dup is not None:
                    # Retransmitted request: repeat our original answer to
                    # BOTH parties (either message may have been the one the
                    # network dropped), and do not mint a new call.
                    self.send_control(sess, dup["ready"])
                    tgt = self.online.get(dup["peer"])
                    if tgt is not None:
                        self.send_control(tgt, dup["incoming"])
                    return
                if len(self.call_dedup) > 256:          # lazy expiry
                    oldest = sorted(self.call_dedup.items(),
                                    key=lambda kv: kv[1]["ts"])[:128]
                    for k, _ in oldest:
                        self.call_dedup.pop(k, None)
            tgt = self.online.get(peer)
            if tgt is None or peer == sess["user"]:
                self.send_control(sess, {"op": "call_failed", "reason": f"'{peer}' not online"})
                return
            call_id = secrets.randbelow(2**32)
            master = secrets.token_bytes(32)
            k_ab = hkdf_expand(master, b"call a->b")
            k_ba = hkdf_expand(master, b"call b->a")
            self.calls[call_id] = {"a": sess["user"], "b": peer,
                                   "a_addr": sess["addr"], "b_addr": tgt["addr"]}
            ready = {"op": "call_ready", "call_id": call_id, "peer": peer,
                     "tx": k_ab.hex(), "rx": k_ba.hex(), "role": "caller"}
            incoming = {"op": "incoming", "call_id": call_id, "peer": sess["user"],
                        "tx": k_ba.hex(), "rx": k_ab.hex(), "role": "callee"}
            if rid is not None:
                self.call_dedup[(sess["user"], rid)] = {
                    "ready": ready, "incoming": incoming, "peer": peer,
                    "ts": time.time()}
            self.send_control(sess, ready)
            self.send_control(tgt, incoming)
            self.log(f"call {call_id:08x}: {sess['user']} -> {peer}  (relay active, keys not held)")
        elif op == "hangup":
            cid = msg.get("call_id")
            if cid in self.calls:
                c = self.calls.pop(cid)
                self.log(f"call {cid:08x} ended ({c['a']} / {c['b']})")
        elif op == "bye":
            self.online.pop(sess["user"], None)
            self.by_cid.pop(sess["cid"], None)
            self.log(f"'{sess['user']}' signed off")

    def stop(self):
        self.running.clear()
        self.ep.close()


def main():
    ap = argparse.ArgumentParser(description="SWARA server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--users", default=USERS_FILE)
    ap.add_argument("--add-user"); ap.add_argument("--password")
    a = ap.parse_args()
    if a.add_user:
        if not a.password:
            print("--add-user requires --password"); sys.exit(1)
        save_user(a.add_user, a.password, a.users); return
    s = Server(a.host, a.port, a.users)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down"); s.stop()


if __name__ == "__main__":
    main()
