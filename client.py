#!/usr/bin/env python3
"""
SWARA client -- the actual voice chat application.

    python3 client.py --user alice --password secret1 --server 127.0.0.1:9000 --call bob
    python3 client.py --user bob   --password secret2 --server 127.0.0.1:9000 --wait

Audio:
    --audio live            microphone and speaker (needs `pip install sounddevice`)
    --audio wav --in a.wav --out b.wav
    --audio synthetic       generated speech, for testing without hardware

Protection:
    --protect swara         PI-AFEC + perceptual anchor replicas   (default)
    --protect uniform       conventional evenly-spread FEC
    --protect none          plain UDP, no repair -- the A/B comparison arm

Impairment (for the demonstration -- applied locally at our own socket, and
clearly artificial):
    --loss 0.15 --burst 4 --extra-delay 40 --jitter 15
"""
from __future__ import annotations
import argparse, json, os, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swara.audio_io import AudioSink, AudioSource, backends_available
from swara.auth import MediaCipher, SparkClient
from swara.codec import Mode
from swara.net import Impairment, UdpEndpoint
from swara.packet import HEADER_LEN, Flag, Header, PType
from swara.session import CallSession


class Client:
    def __init__(self, user, password, server_addr, impairment=None, verbose=True):
        self.user = user
        self.server = server_addr
        self.ep = UdpEndpoint("0.0.0.0", 0, impairment or Impairment())
        self.spark = SparkClient(user, password)
        self.cid = None
        self.ticket = None
        self.c2s = self.s2c = None
        self.ctl_seq = 0
        self.verbose = verbose
        self.events: list[dict] = []
        self._ev = threading.Event()

    def log(self, m):
        if self.verbose:
            print(f"[{self.user}] {m}", flush=True)

    # -- handshake -------------------------------------------------------
    def _tx(self, ptype, body, cid=0):
        self.ep.sendto(Header(ptype=ptype, cid=cid).pack() + body, self.server)

    def _rx(self, want, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            data, addr = self.ep.recvfrom(timeout=0.3)
            if data is None or len(data) < HEADER_LEN:
                continue
            h = Header.unpack(data)
            if h.ptype == want:
                return h, data[HEADER_LEN:]
        return None, None

    def _exchange(self, send_type, body, want, attempts=6, timeout=0.6):
        """Send and wait, retransmitting on timeout.

        UDP has no retransmission, so a handshake built on it must supply its
        own or it simply cannot complete on a lossy link.  Each step is retried
        with a linearly growing timeout.  Every message in SPARK is idempotent
        at the server -- HELLO allocates no state and the cookie is valid for a
        whole epoch -- so a retransmission is always safe.
        """
        for i in range(attempts):
            self._tx(send_type, body)
            h, resp = self._rx(want, timeout=timeout * (1 + 0.5 * i))
            if resp is not None:
                return resp
        return None

    def login(self) -> bool:
        t0 = time.perf_counter()
        retry = self._exchange(PType.HELLO, self.spark.make_hello(), PType.RETRY)
        if retry is None:
            self.log("no response from server"); return False
        resp = self._exchange(PType.AUTH_INIT, self.spark.make_auth_init(retry),
                              PType.AUTH_RESP)
        if resp is None:
            self.log("no AUTH_RESP after retries"); return False
        tag = self.spark.make_auth_confirm(resp)
        if tag is None:
            self.log("wrong password (detected locally -- nothing was sent)"); return False
        tick = self._exchange(PType.AUTH_CONFIRM, tag, PType.AUTH_TICKET)
        if tick is None:
            self.log("server rejected the key confirmation"); return False
        cid, tlen = struct.unpack("!IH", tick[:6])
        self.cid = cid
        self.ticket = tick[6:6 + tlen]
        self.spark.ticket = self.ticket
        self.c2s = MediaCipher(self.spark.keys.k_c2s, b"c2s")
        self.s2c = MediaCipher(self.spark.keys.k_s2c, b"s2c")
        ms = (time.perf_counter() - t0) * 1000
        self.log(f"authenticated in {ms:.0f} ms  cid={cid:08x}  (2 round trips)")
        return True

    # -- control ---------------------------------------------------------
    def send_ctl(self, obj):
        body = json.dumps(obj).encode()
        seq = self.ctl_seq; self.ctl_seq += 1
        h = Header(ptype=PType.CONTROL, seq=seq & 0xFFFF, cid=self.cid, flags=Flag.ENCRYPTED)
        hdr = h.pack()
        self.ep.sendto(hdr + self.c2s.seal(seq, hdr, body), self.server)

    def poll_ctl(self, timeout=1.0):
        end = time.time() + timeout
        while time.time() < end:
            data, addr = self.ep.recvfrom(timeout=0.25)
            if data is None or len(data) < HEADER_LEN:
                continue
            h = Header.unpack(data)
            if h.ptype != PType.CONTROL or h.cid != self.cid:
                continue
            pt = self.s2c.open(h.seq, data[:HEADER_LEN], data[HEADER_LEN:])
            if pt is None:
                continue
            try:
                return json.loads(pt)
            except Exception:
                continue
        return None

    def place_call(self, peer: str, timeout: float = 12.0, rtx_s: float = 1.2):
        """Place a call reliably over the unreliable transport.

        A single 'call' datagram on a 15% loss link fails one time in seven,
        and the failure looks exactly like the callee ignoring you.  So we do
        what SIP does with INVITE: attach a request id, retransmit on a timer,
        and rely on the server treating duplicates of the same id as the same
        request (see server.py call_dedup).  Either direction being lossy is
        healed, because every retransmission re-sends BOTH answers.
        """
        import secrets as _secrets
        rid = _secrets.token_hex(8)
        self._last_call = (peer, rid)
        end = time.time() + timeout
        while time.time() < end:
            self.send_ctl({"op": "call", "peer": peer, "rid": rid})
            m = self.wait_for({"call_ready", "call_failed"},
                              min(rtx_s, max(0.1, end - time.time())))
            if m is not None:
                return m
        return None

    def nudge_call(self):
        """Re-poke the server with the same rid so it re-sends 'incoming'
        to the callee -- used when the caller is ready but the callee's copy
        was the datagram the network ate."""
        peer, rid = getattr(self, "_last_call", (None, None))
        if rid:
            self.send_ctl({"op": "call", "peer": peer, "rid": rid})

    def wait_for(self, ops, timeout=30.0, keepalive_s=15.0):
        """Wait for a signalling message, keeping the NAT binding alive.

        A client sitting idle behind NAT loses its address translation entry
        after roughly 30 seconds of silence, at which point the server can no
        longer reach it and an incoming call is simply never delivered.  A
        small periodic packet keeps the pinhole open.  This is why every real
        VoIP client sends keepalives, and why one that does not appears to work
        perfectly in testing and then fails for real users.
        """
        end = time.time() + timeout
        last_ka = time.time()
        while time.time() < end:
            m = self.poll_ctl(1.0)
            if m and m.get("op") in ops:
                return m
            if time.time() - last_ka >= keepalive_s:
                try:
                    self.send_ctl({"op": "ping"})
                except Exception:
                    pass
                last_ka = time.time()
        return None


def run_call(cli, info, args) -> CallSession:
    src_backend = args.audio
    if src_backend == "wav" and not args.infile:
        print("--audio wav needs --in FILE"); sys.exit(1)
    source = AudioSource(backend=("wav" if src_backend == "wav" else src_backend),
                         wav_path=args.infile, duration_s=args.seconds, seed=args.seed)
    sink = AudioSink(backend=("wav" if args.outfile else
                              ("live" if src_backend == "live" else "null")),
                     wav_path=args.outfile)
    sess = CallSession(
        endpoint=cli.ep, peer_addr=cli.server,
        tx_key=bytes.fromhex(info["tx"]), rx_key=bytes.fromhex(info["rx"]),
        call_id=info["call_id"], source=source, sink=sink,
        mode=(Mode.NARROWBAND if args.narrowband else Mode.WIDEBAND),
        protection=args.protect, name=cli.user)
    cli.log(f"call {info['call_id']:08x} with '{info['peer']}' -- "
            f"protection={args.protect}, impairment={cli.ep.impairment.describe()}")
    sess.start()
    return sess, source, sink


def main():
    ap = argparse.ArgumentParser(description="SWARA voice chat client")
    ap.add_argument("--user", required=True); ap.add_argument("--password", required=True)
    ap.add_argument("--server", default="127.0.0.1:9000")
    ap.add_argument("--call"); ap.add_argument("--wait", action="store_true")
    ap.add_argument("--audio", default="auto",
                    choices=["auto", "live", "wav", "synthetic"])
    ap.add_argument("--in", dest="infile"); ap.add_argument("--out", dest="outfile")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--protect", default="swara", choices=["swara", "uniform", "none"])
    ap.add_argument("--narrowband", action="store_true")
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--burst", type=float, default=1.0)
    ap.add_argument("--extra-delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    a = ap.parse_args()

    if a.audio == "auto":
        a.audio = "live" if backends_available()["live"] else "synthetic"

    host, _, port = a.server.partition(":")
    imp = Impairment()
    if a.loss > 0 or a.extra_delay > 0 or a.jitter > 0:
        imp.configure(loss_rate=a.loss, mean_burst=a.burst,
                      extra_delay_ms=a.extra_delay, jitter_ms=a.jitter, enabled=True)

    cli = Client(a.user, a.password, (host, int(port or 9000)), imp)
    if not cli.login():
        sys.exit(1)

    if a.call:
        info = cli.place_call(a.call, timeout=12.0)
        if info is None or info["op"] == "call_failed":
            cli.log(f"call failed: {info.get('reason') if info else 'timeout'}"); sys.exit(1)
    elif a.wait:
        cli.log("waiting for an incoming call...")
        info = cli.wait_for({"incoming"}, 120.0)
        if info is None:
            cli.log("no call arrived"); sys.exit(1)
    else:
        cli.log("nothing to do -- pass --call USER or --wait"); sys.exit(0)

    sess, source, sink = run_call(cli, info, a)
    try:
        while not sess.finished.is_set():
            time.sleep(0.5)
        time.sleep(1.5)                      # let the tail drain out of the buffer
    except KeyboardInterrupt:
        pass
    sess.stop(); source.close(); sink.close()
    print(sess.report())
    cli.send_ctl({"op": "hangup", "call_id": info["call_id"]})


if __name__ == "__main__":
    main()
