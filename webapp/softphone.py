"""
Softphone agents: a real SWARA client run on behalf of a browser tab.

The browser signs in over a WebSocket.  The agent performs the SPARK handshake
with the relay, keeps the user present, places or answers calls, and runs the
live media session -- codec, perceptual analysis, FEC, AEAD, UDP -- exactly as
client.py does.  Audio reaches the session either from the browser's
microphone (16 kHz int16 PCM over the same WebSocket) or from this computer's
own audio device through sounddevice.

One thread per agent owns the UDP socket.  While idle it polls the control
channel; during a call the CallSession's own threads own the socket and the
agent thread only samples statistics.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import Counter, deque

import numpy as np

from swara.audio_io import AudioSink, AudioSource, backends_available
from swara.codec import Mode
from swara.net import Impairment
from swara.packet import HEADER_LEN, Flag, Header, PType
from client import Client
from webapp.lab import TracedSession

FRAME = 320                      # 20 ms at 16 kHz
AGENTS: set["PhoneAgent"] = set()
_LOCK = threading.Lock()


class _ByeAwareCipher:
    """Wraps the session's receive cipher so an authenticated BYE datagram from
    the peer is noticed.  The media loop itself only routes MEDIA and FEC."""

    def __init__(self, inner, on_bye) -> None:
        self.inner, self.on_bye = inner, on_bye

    def open(self, seq, header, ciphertext):
        pt = self.inner.open(seq, header, ciphertext)
        if pt is not None and len(header) >= HEADER_LEN:
            try:
                if Header.unpack(header[:HEADER_LEN]).ptype == PType.BYE:
                    self.on_bye()
            except Exception:
                pass
        return pt

    def __getattr__(self, name):
        return getattr(self.inner, name)


class PhoneSession(TracedSession):
    """TracedSession that also learns when the peer says goodbye."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.peer_bye = False
        self.rx = _ByeAwareCipher(self.rx, self._on_bye)

    def _on_bye(self) -> None:
        self.peer_bye = True

    def send_bye(self, peer_addr, copies: int = 3) -> None:
        """Tell the peer the call is over, through the relay, before the relay
        forgets the call.  Sequence numbers beyond the sender's last frame keep
        the AEAD nonces unique; the sender must already be stopped."""
        for i in range(1, copies + 1):
            seq = self._seq + i
            h = Header(ptype=PType.BYE, seq=seq & 0xFFFF, cid=self.call_id, ts_ms=int(self._now_ms()),
                       flags=Flag.ENCRYPTED)
            hdr = h.pack()
            try:
                self.ep.sendto(hdr + self.tx.seal(seq, hdr, b"bye"), peer_addr)
            except Exception:
                return
            time.sleep(0.03)


class QueueSource:
    """Microphone audio arriving from the browser, re-cut into 20 ms frames.

    The browser's audio clock and the sender's monotonic clock drift apart, so
    the queue is bounded: when it runs dry the sender gets silence until a
    small cushion has rebuilt, and when it overfills the oldest frames are
    dropped so the added latency stays bounded.
    """
    CUSHION, CAP = 6, 40             # 120 ms cushion, 800 ms cap

    def __init__(self) -> None:
        self.q: deque = deque()
        self.lock = threading.Lock()
        self.rem = np.zeros(0, np.int16)
        self.primed = False
        self.underruns = self.overruns = self.received = 0
        self.gain = 1.0

    def push(self, data: bytes) -> None:
        arr = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.int16)
        with self.lock:
            buf = np.concatenate([self.rem, arr]) if self.rem.size else arr
            n = buf.size // FRAME
            for i in range(n):
                self.q.append(buf[i * FRAME:(i + 1) * FRAME])
            self.rem = buf[n * FRAME:]
            self.received += n
            while len(self.q) > self.CAP:
                self.q.popleft()
                self.overruns += 1

    def read(self, timeout: float = 0.5):
        with self.lock:
            if not self.primed:
                if len(self.q) < self.CUSHION:
                    return np.zeros(FRAME, np.int16)
                self.primed = True
            if self.q:
                return self.q.popleft()
            self.primed = False
            self.underruns += 1
            return np.zeros(FRAME, np.int16)

    def close(self) -> None:
        pass


class QueueSink:
    """Decoded frames on their way to the browser's speaker."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self.frames = 0

    def write(self, frame) -> None:
        self.frames += 1
        self.emit(np.asarray(frame, dtype="<i2").tobytes())

    def close(self) -> None:
        pass


class PhoneAgent:
    STALL_S = 4.0        # no packets from the peer for this long -> the call is over
    GRACE_S = 8.0        # ...but give a new call time to start first

    def __init__(self, params: dict, loop: asyncio.AbstractEventLoop, outq: asyncio.Queue) -> None:
        user = str(params.get("user", "")).strip()
        password = str(params.get("password", ""))
        if not user or not password:
            raise ValueError("username and password are required")
        if len(user) > 40 or not all(c.isalnum() or c in "-_." for c in user):
            raise ValueError("username may contain letters, digits, '-', '_' and '.'")
        host, _, port = str(params.get("server") or "127.0.0.1:9000").strip().partition(":")
        try:
            self.server = (host.strip() or "127.0.0.1", int(port or 9000))
        except ValueError:
            raise ValueError("relay address must look like host:port")
        self.user, self.password = user, password
        self.audio = "device" if params.get("audio") == "device" else "browser"
        if self.audio == "device" and not backends_available()["live"]:
            raise ValueError("this computer has no audio-device backend (sounddevice); use the browser microphone")
        self.protection = params.get("protection") if params.get("protection") in ("swara", "uniform", "none") else "swara"
        self.mode = Mode.NARROWBAND if params.get("mode") == "narrowband" else Mode.WIDEBAND
        self.loop, self.outq = loop, outq

        self.imp = Impairment()
        self.impairment = {"loss": 0.0, "burst": 1.0, "jitter": 0.0, "extra_delay": 0.0}
        self.configure_impairment(params.get("loss"), params.get("burst"), params.get("jitter"), params.get("extra_delay"))

        self.client: Client | None = None
        self.session: TracedSession | None = None
        self.source = self.sink = None
        self.state = "connecting"
        self.peer = self.call_id = self.role = None
        self.muted = False
        self.cmds: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.t0 = time.time()
        self.t_call = self._last_rx_t = self._last_t = 0.0
        self._last_rx = self._last_bytes = self._ticks = 0
        self.thread = threading.Thread(target=self.run, name=f"phone-{user}", daemon=True)

    # -- plumbing to the browser -------------------------------------------
    def _put(self, item) -> None:
        try:
            self.loop.call_soon_threadsafe(self.outq.put_nowait, item)
        except RuntimeError:                       # event loop already closed
            pass

    def emit(self, obj: dict) -> None:
        self._put(json.dumps(obj))

    def emit_audio(self, data: bytes) -> None:
        if self.outq.qsize() < 300:               # a stalled browser must not pile up audio
            self._put(data)

    def log(self, msg: str, level: str = "info") -> None:
        self.emit({"op": "log", "t": round(time.time() - self.t0, 1), "msg": msg, "level": level})

    def set_state(self, state: str, **extra) -> None:
        self.state = state
        self.emit({"op": "state", "state": state, **extra})

    # -- called from the WebSocket handler -----------------------------------
    def start(self) -> None:
        with _LOCK:
            AGENTS.add(self)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> bool:
        return self.thread.is_alive()

    def command(self, cmd: dict) -> None:
        self.cmds.put(cmd)

    def push_audio(self, data: bytes) -> None:
        src = self.source
        if isinstance(src, QueueSource):
            src.push(data)

    def configure_impairment(self, loss=None, burst=None, jitter=None, extra_delay=None) -> None:
        for k, v, lo, hi in (("loss", loss, 0.0, 0.6), ("burst", burst, 1.0, 20.0),
                             ("jitter", jitter, 0.0, 200.0), ("extra_delay", extra_delay, 0.0, 500.0)):
            if v is not None:
                try:
                    self.impairment[k] = min(hi, max(lo, float(v)))
                except (TypeError, ValueError):
                    pass
        i = self.impairment
        on = i["loss"] > 0 or i["jitter"] > 0 or i["extra_delay"] > 0
        self.imp.configure(loss_rate=i["loss"], mean_burst=i["burst"], extra_delay_ms=i["extra_delay"],
                           jitter_ms=i["jitter"], enabled=on)

    def describe(self) -> dict:
        return {"user": self.user, "state": self.state, "peer": self.peer, "audio": self.audio,
                "server": f"{self.server[0]}:{self.server[1]}", "protection": self.protection,
                "impairment": self.impairment}

    # -- the agent thread ----------------------------------------------------
    def run(self) -> None:
        try:
            self.client = Client(self.user, self.password, self.server, self.imp, verbose=False)
            self.log(f"SPARK handshake with relay {self.server[0]}:{self.server[1]} ...")
            t = time.perf_counter()
            if not self.client.login():
                self.emit({"op": "error", "msg": "sign-in failed: wrong password, unknown user, or the relay is unreachable"})
                return
            ms = (time.perf_counter() - t) * 1000
            self.emit({"op": "logged_in", "cid": f"{self.client.cid:08x}", "ms": round(ms)})
            self.log(f"authenticated as '{self.user}' in {ms:.0f} ms (2 round trips), cid={self.client.cid:08x}", "ok")
            self.set_state("online")
            self.client.send_ctl({"op": "who"})
            last_who = last_ping = time.time()
            while not self._stop.is_set():
                self._commands()
                if self.session is None:
                    m = self.client.poll_ctl(0.2)
                    if m:
                        self._on_ctl(m)
                    now = time.time()
                    if now - last_who >= 3.0:
                        self.client.send_ctl({"op": "who"})
                        last_who = now
                    if now - last_ping >= 15.0:       # keeps the NAT binding alive
                        self.client.send_ctl({"op": "ping"})
                        last_ping = now
                else:
                    time.sleep(0.25)
                    self._tick()
        except Exception as e:                      # surfaced to the browser, never silent
            self.emit({"op": "error", "msg": f"{type(e).__name__}: {e}"})
        finally:
            if self.session is not None:
                self._end_call("signed out")
            if self.client is not None and self.client.cid is not None:
                self._send_ctl_repeated({"op": "bye"})
            try:
                if self.client is not None:
                    self.client.ep.close()
            except Exception:
                pass
            with _LOCK:
                AGENTS.discard(self)
            self.set_state("idle")

    def _send_ctl_repeated(self, obj: dict, copies: int = 3) -> None:
        """Control messages that must survive the (possibly impaired) link.
        The relay treats repeats of 'hangup' and 'bye' as no-ops."""
        for _ in range(copies):
            try:
                self.client.send_ctl(obj)
            except Exception:
                return
            time.sleep(0.03)

    def _commands(self) -> None:
        while True:
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                return
            op = cmd.get("op")
            if op == "call":
                self._place(str(cmd.get("peer", "")).strip())
            elif op == "hangup":
                if self.session is not None:
                    self._end_call("you hung up")
            elif op == "impairment":
                self.configure_impairment(cmd.get("loss"), cmd.get("burst"), cmd.get("jitter"), cmd.get("extra_delay"))
                if self.session is not None:
                    self.log(f"network changed: {self.imp.describe()}", "warn")
            elif op == "mute":
                self.muted = bool(cmd.get("on"))
                if self.source is not None and hasattr(self.source, "gain"):
                    self.source.gain = 0.0 if self.muted else 1.0
            elif op == "logout":
                self._stop.set()

    def _on_ctl(self, m: dict) -> None:
        op = m.get("op")
        if op == "online":
            self.emit({"op": "online", "users": [u for u in m.get("users", []) if isinstance(u, str)]})
        elif op == "incoming":
            self.log(f"incoming call from '{m.get('peer')}' -- answering", "ok")
            self.emit({"op": "incoming", "peer": m.get("peer")})
            self._start_session(m, "callee")
        elif op == "call_failed":
            self.emit({"op": "error", "msg": m.get("reason", "call failed")})

    def _place(self, peer: str) -> None:
        if self.session is not None:
            self.emit({"op": "error", "msg": "already in a call"}); return
        if not peer or peer == self.user:
            self.emit({"op": "error", "msg": "choose someone else to call"}); return
        self.set_state("calling", peer=peer)
        self.log(f"calling '{peer}' (request retransmitted until the relay answers) ...")
        info = self.client.place_call(peer, timeout=10.0)
        if info is None:
            self.emit({"op": "error", "msg": "no answer from the relay; is it still running?"})
            self.set_state("online"); return
        if info.get("op") != "call_ready":
            self.emit({"op": "error", "msg": info.get("reason", "call failed")})
            self.log(f"call failed: {info.get('reason')}", "error")
            self.set_state("online"); return
        self._start_session(info, "caller")

    def _start_session(self, info: dict, role: str) -> None:
        try:
            if self.audio == "device":
                source, sink = AudioSource("live"), AudioSink("live")
                source.gain = 0.0 if self.muted else 1.0
            else:
                source, sink = QueueSource(), QueueSink(self.emit_audio)
        except Exception as e:
            self.emit({"op": "error", "msg": f"audio device unavailable: {e}"})
            try:
                self.client.send_ctl({"op": "hangup", "call_id": info["call_id"]})
            except Exception:
                pass
            self.set_state("online"); return
        self.source, self.sink = source, sink
        self.session = PhoneSession(self.client.ep, self.client.server, bytes.fromhex(info["tx"]),
                                    bytes.fromhex(info["rx"]), info["call_id"], source, sink,
                                    mode=self.mode, protection=self.protection, name=self.user)
        self.peer, self.call_id, self.role = info.get("peer"), info["call_id"], role
        self.t_call = self._last_rx_t = self._last_t = time.monotonic()
        self._last_rx = self._last_bytes = self._ticks = 0
        self.session.start()
        self.emit({"op": "call_started", "peer": self.peer, "call_id": f"{self.call_id:08x}", "role": role,
                   "protection": self.protection, "mode": Mode.NAME[self.mode], "audio": self.audio})
        self.set_state("in_call", peer=self.peer)
        self.log(f"call {self.call_id:08x} with '{self.peer}': media flowing, {Mode.NAME[self.mode]}, "
                 f"protection={self.protection}, impairment={self.imp.describe()}", "ok")

    def _tick(self) -> None:
        s = self.session
        now = time.monotonic()
        self._ticks += 1
        if s.peer_bye:
            self._end_call(f"{self.peer} hung up")
            return
        rx = s.stats.rx_frames
        if rx != self._last_rx:
            self._last_rx, self._last_rx_t = rx, now
        elif now - self.t_call > self.GRACE_S and now - self._last_rx_t > self.STALL_S:
            self._end_call("the other side disconnected (no packets for 4 s)")
            return
        if self._ticks % 2 == 0:
            self.emit(self._stats(now))

    def _stats(self, now: float, s=None) -> dict:
        s = s or self.session
        st, est = s.stats.snapshot(), s.est.snapshot()
        dt = max(1e-3, now - self._last_t)
        kbps = (st["tx_bytes"] - self._last_bytes) * 8 / dt / 1000.0
        self._last_bytes, self._last_t = st["tx_bytes"], now
        try:
            mos = float(s._telemetry_values()[3])
        except Exception:
            mos = 0.0
        fates = Counter(s.fate.values())
        dec = s.navik.history[-1] if s.navik.history else None
        src = self.source
        return {"op": "stats", "t": round(now - self.t_call, 1),
                "tx_frames": st["tx_frames"], "tx_parity": st["tx_parity"], "tx_replicas": st["tx_replicas"],
                "kbps": round(kbps, 1), "rx_frames": st["rx_frames"], "played": st["played"],
                "fec": st["recovered_fec"], "replica": st["recovered_replica"], "concealed": st["concealed"],
                "loss_after": round(st["loss_after_repair_pct"], 2), "net_loss": round(est.loss_rate * 100, 2),
                "burst": round(est.mean_burst, 2), "jitter": round(est.jitter_ms, 2),
                "depth": round(s.depth_ms, 1), "rho": round(s.rho, 3), "codec": dec.codec if dec else None,
                "mos": round(mos, 3), "peer_mos": round(st["peer_mos"], 3), "auth_fail": st["rx_auth_fail"],
                "fates": [fates.get(0, 0), fates.get(1, 0), fates.get(2, 0), st["concealed"]],
                "drops": self.client.ep.dropped_by_impairment,
                "mic": ({"underruns": src.underruns, "overruns": src.overruns, "frames": src.received}
                        if isinstance(src, QueueSource) else None)}

    def _end_call(self, reason: str) -> None:
        s = self.session
        now = time.monotonic()
        report: dict = {}
        try:
            report = self._stats(now, s)
            report.pop("op", None)
            report.update({"reason": reason, "peer": self.peer, "role": self.role,
                           "duration": round(now - self.t_call, 1), "protection": self.protection,
                           "avg_kbps": round(s.stats.tx_bytes * 8 / max(1e-3, now - self.t_call) / 1000.0, 1)})
        except Exception as e:
            report = {"error": f"{type(e).__name__}: {e}"}
        self.session = None
        try:
            s.stop()                                  # sender stopped: BYE nonces below are unused
        except Exception:
            pass
        if not s.peer_bye:                            # we are the one leaving: say so to the peer first
            s.send_bye(self.client.server)
        for x in (self.source, self.sink):
            try:
                x.close()
            except Exception:
                pass
        self.source = self.sink = None
        self._send_ctl_repeated({"op": "hangup", "call_id": self.call_id})
        self.log(f"call ended: {reason}. played {report.get('played', 0)} frames, "
                 f"concealed {report.get('concealed', 0)} ({report.get('loss_after', 0)}% after repair)", "warn")
        self.emit({"op": "call_ended", "reason": reason, "report": report})
        self.peer = self.call_id = self.role = None
        if not self._stop.is_set():
            self.set_state("online")
            try:
                self.client.send_ctl({"op": "who"})
            except Exception:
                pass
