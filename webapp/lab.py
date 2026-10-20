"""
Live call laboratory.

Runs a REAL SWARA call -- relay server, two clients, SPARK handshakes, AEAD
media over UDP sockets on the loopback interface -- exactly as demo.py does,
but instrumented frame by frame so the web console can watch it happen:

  * every frame the receiver decides is classified by how it was obtained
    (arrived directly, rebuilt from Reed-Solomon parity, taken from a
    perceptual anchor replica, or concealed);
  * controller state (jitter-buffer depth, FEC budget, codec choice, MOS
    estimate) is sampled four times a second;
  * the audio the callee actually hears is buffered so it can be streamed to
    the browser while the call is in progress and replayed afterwards.

Only one call (or one benchmark) runs at a time: the media threads are paced
against a wall clock, and two concurrent calls would compete for the GIL and
measure each other rather than the network.
"""
from __future__ import annotations

import base64
import io
import struct
import threading
import time
import uuid
import wave

import numpy as np

from swara import emodel
from swara.audio_io import AudioSink, AudioSource, backends_available
from swara.codec import Mode
from swara.net import Impairment
from swara.perceptual import damage_weight
from swara.session import CallSession

import server as server_mod
from client import Client
from demo import audio_quality

SAMPLE_RATE = 16_000
FATE_DIRECT, FATE_FEC, FATE_REPLICA, FATE_CONCEALED = 0, 1, 2, 3

LAB_USERS = {"alice": "hostel-wifi-1", "bob": "hostel-wifi-2"}


def pcm_to_wav(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


class TracedSession(CallSession):
    """A CallSession that records how every received frame was obtained.

    Nothing about the protocol changes; the overrides observe the reassembly
    buffer immediately before and after the base-class logic runs.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fate: dict[int, int] = {}

    def _on_media(self, h, pt, wire_len, full_seq):
        holes = []
        try:
            off = 1
            for _ in range(pt[0]):
                if off + 3 > len(pt):
                    break
                disp, rlen = struct.unpack("!BH", pt[off:off + 3])
                off += 3 + rlen
                holes.append(full_seq - disp)
        except Exception:
            holes = []
        with self._rx_lock:
            fresh = full_seq not in self._rx_payload and full_seq not in self.fate
            filled = [s for s in holes if s not in self._rx_payload and s not in self.fate]
        super()._on_media(h, pt, wire_len, full_seq)
        if fresh:
            self.fate[full_seq] = FATE_DIRECT
        for s in filled:
            self.fate.setdefault(s, FATE_REPLICA)

    def _try_fec(self, gid):
        meta = self._rx_group_r.get(gid)
        before = []
        if meta:
            base, k = meta[0], meta[1]
            before = [s for s in range(base, base + k) if s not in self._rx_payload]
        ok = super()._try_fec(gid)
        if ok:
            for s in before:
                if s in self._rx_payload:
                    self.fate.setdefault(s, FATE_FEC)
        return ok


class RecordingSource:
    """Wraps an AudioSource and keeps every frame handed to the encoder, so the
    'what was said' track exists for live microphone input too."""

    def __init__(self, inner: AudioSource, max_seconds: float):
        self.inner = inner
        self.frames: list[np.ndarray] = []
        self.max_frames = int(max_seconds * 50)

    def read(self, timeout: float = 1.0):
        if len(self.frames) >= self.max_frames:
            return None
        f = self.inner.read(timeout)
        if f is not None:
            self.frames.append(np.asarray(f, dtype=np.int16))
        return f

    def close(self):
        self.inner.close()

    @property
    def collected(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, np.int16)


class LabCall:
    """One instrumented call from alice to bob through a private relay."""

    def __init__(self, params: dict, users_file: str, upload_path: str | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = "call"
        self.p = params
        self.users_file = users_file
        self.upload_path = upload_path
        self.phase = "queued"
        self.error: str | None = None
        self.result: dict | None = None
        self.created = time.time()
        self.events: list[dict] = []
        self.timeline: list[dict] = []
        self.sent_env: list[float] = []
        self.sent_pcm = np.zeros(0, np.int16)
        self.heard_pcm = np.zeros(0, np.int16)
        self.timings: dict[str, float] = {}
        self.done = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._imps: list[Impairment] = []
        self._sa: TracedSession | None = None
        self._sb: TracedSession | None = None
        self._sink_b: AudioSink | None = None
        self._concealed_seen = 0
        self._concealed: set[int] = set()
        self.impairment = {k: float(params.get(k, 0.0)) for k in
                           ("loss", "burst", "jitter", "extra_delay")}

    # -- control from the web layer -----------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self.events.append({"t": round(time.time() - self.created, 2),
                                "msg": msg, "level": level})

    def set_phase(self, phase: str, msg: str | None = None) -> None:
        self.phase = phase
        if msg:
            self.log(msg, "phase")

    def configure_impairment(self, loss=None, burst=None, jitter=None, extra_delay=None):
        for k, v in (("loss", loss), ("burst", burst), ("jitter", jitter),
                     ("extra_delay", extra_delay)):
            if v is not None:
                self.impairment[k] = float(v)
        imp = self.impairment
        on = imp["loss"] > 0 or imp["jitter"] > 0 or imp["extra_delay"] > 0
        for i in self._imps:
            i.configure(loss_rate=imp["loss"], mean_burst=max(1.0, imp["burst"]),
                        extra_delay_ms=imp["extra_delay"], jitter_ms=imp["jitter"],
                        enabled=on)
        if self.phase == "media":
            self.log(f"network changed: loss {imp['loss']*100:.0f}%, burst "
                     f"{imp['burst']:.1f}, jitter {imp['jitter']:.0f} ms, "
                     f"+{imp['extra_delay']:.0f} ms", "warn")

    def stop(self) -> None:
        if not self.done.is_set():
            self.log("hang-up requested by operator", "warn")
        self._stop.set()

    # -- the call itself ------------------------------------------------------
    def run(self) -> None:
        srv = alice = bob = None
        src_a = src_b = sink_a = sink_b = None
        sa = sb = None
        p = self.p
        try:
            self.set_phase("relay", "starting private relay server")
            srv = server_mod.Server("127.0.0.1", 0, self.users_file, verbose=False)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.ep.port
            self.log(f"relay listening on udp://127.0.0.1:{port}")
            time.sleep(0.2)

            imp_a, imp_b = Impairment(), Impairment()
            self._imps = [imp_a, imp_b]
            self.configure_impairment()

            self.set_phase("auth", "SPARK handshake (HELLO > RETRY > AUTH_INIT > AUTH_RESP > CONFIRM)")
            alice = Client("alice", LAB_USERS["alice"], ("127.0.0.1", port), imp_a, verbose=False)
            bob = Client("bob", LAB_USERS["bob"], ("127.0.0.1", port), imp_b, verbose=False)
            for who, cli in (("alice", alice), ("bob", bob)):
                t0 = time.perf_counter()
                if not cli.login():
                    raise RuntimeError(f"{who} failed to authenticate")
                ms = (time.perf_counter() - t0) * 1000
                self.timings[f"auth_{who}_ms"] = round(ms, 1)
                self.log(f"{who} authenticated in {ms:.0f} ms, cid={cli.cid:08x}")

            self.set_phase("setup", "placing call alice -> bob (INVITE-style retransmission)")
            got: dict = {}
            tb = threading.Thread(target=lambda: got.__setitem__(
                "info", bob.wait_for({"incoming"}, 20.0)), daemon=True)
            tb.start()
            time.sleep(0.3)
            t0 = time.perf_counter()
            a_info = alice.place_call("bob", timeout=15.0)
            deadline = time.time() + 15.0
            while got.get("info") is None and time.time() < deadline:
                alice.nudge_call()
                time.sleep(0.8)
            tb.join(timeout=2.0)
            b_info = got.get("info")
            if not a_info or a_info.get("op") != "call_ready" or not b_info:
                raise RuntimeError(f"call setup failed: {a_info}")
            self.timings["setup_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.log(f"call {a_info['call_id']:08x} ready in {self.timings['setup_ms']:.0f} ms; "
                     "relay holds no media keys")

            seconds = float(p["seconds"])
            seed = int(p.get("seed", 7))
            source = p.get("source", "synthetic")
            if source == "upload" and self.upload_path:
                inner = AudioSource("wav", wav_path=self.upload_path)
                seconds = min(seconds, inner.total_frames / 50.0) if inner.total_frames > 0 else seconds
            elif source == "mic":
                inner = AudioSource("live")
            else:
                inner = AudioSource("synthetic", duration_s=seconds, seed=seed)
            src_a = RecordingSource(inner, seconds)
            if inner.backend != "live":
                buf = inner._buf[: int(seconds * SAMPLE_RATE)]
                n = buf.size // 320
                if n:
                    rms = np.sqrt(np.mean(buf[: n * 320].reshape(n, 320).astype(np.float64) ** 2, axis=1))
                    self.sent_env = [round(float(x) / 32768.0, 4) for x in rms]
            src_b = AudioSource("synthetic", duration_s=seconds, seed=seed + 100)
            sink_a, sink_b = AudioSink("null"), AudioSink("null")
            self._sink_b = sink_b

            mode = Mode.NARROWBAND if p.get("mode") == "narrowband" else Mode.WIDEBAND
            protect = p.get("protection", "swara")
            sa = TracedSession(alice.ep, alice.server, bytes.fromhex(a_info["tx"]),
                               bytes.fromhex(a_info["rx"]), a_info["call_id"], src_a, sink_a,
                               mode=mode, protection=protect, name="alice")
            sb = TracedSession(bob.ep, bob.server, bytes.fromhex(b_info["tx"]),
                               bytes.fromhex(b_info["rx"]), b_info["call_id"], src_b, sink_b,
                               mode=mode, protection=protect, name="bob")
            self._sa, self._sb = sa, sb
            # Identical channel realisation for every run with the same seed.
            imp_a.reset(1234 + seed); imp_b.reset(4321 + seed)
            self.set_phase("media", f"media flowing: {Mode.NAME[mode]}, protection={protect}, "
                                    f"{seconds:.0f} s of {source} audio")
            sa.start(); sb.start()
            t_media = time.monotonic()
            last_b, last_t = 0, t_media
            limit = t_media + seconds + 12
            while time.monotonic() < limit and not self._stop.is_set():
                if sa.finished.is_set() and sb.finished.is_set():
                    break
                time.sleep(0.25)
                now = time.monotonic()
                sample = self._sample(alice, now - t_media, last_b, now - last_t)
                last_b, last_t = sample["_tx_bytes"], now
                with self._lock:
                    self.timeline.append(sample)
            if not self._stop.is_set():
                self.set_phase("drain", "sender finished; draining the jitter buffer")
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    time.sleep(0.25)
                    now = time.monotonic()
                    sample = self._sample(alice, now - t_media, last_b, now - last_t)
                    last_b, last_t = sample["_tx_bytes"], now
                    with self._lock:
                        self.timeline.append(sample)
            sa.stop(); sb.stop()
            media_s = max(1e-9, time.monotonic() - t_media)
            self.result = self._summarise(alice, sa, sb, src_a, sink_b, min(seconds, media_s), protect)
            self.set_phase("done", f"call complete: perceptual damage {self.result['perceptual_pct']:.1f}%, "
                                   f"loss after repair {self.result['loss_after_repair']:.1f}%")
        except Exception as e:                       # surfaced to the operator
            self.error = str(e)
            self.set_phase("error", f"error: {e}")
        finally:
            for s in (sa, sb):
                if s is not None:
                    try:
                        s.stop()
                    except Exception:
                        pass
            for x in (src_a, src_b, sink_a, sink_b):
                if x is not None:
                    try:
                        x.close()
                    except Exception:
                        pass
            for cli in (alice, bob):
                if cli is not None:
                    try:
                        cli.ep.close()
                    except Exception:
                        pass
            if srv is not None:
                srv.stop()
            self.done.set()

    def _sample(self, alice, t: float, last_bytes: int, dt: float) -> dict:
        sa, sb = self._sa, self._sb
        ss, rs = sa.stats.snapshot(), sb.stats.snapshot()
        st = sb.est.snapshot()
        dec = sa.navik.history[-1] if sa.navik.history else None
        try:
            mos_now = float(sb._telemetry_values()[3])
        except Exception:
            mos_now = 0.0
        kbps = (ss["tx_bytes"] - last_bytes) * 8 / max(1e-3, dt) / 1000.0
        return {
            "t": round(t, 2), "_tx_bytes": ss["tx_bytes"],
            "tx_frames": ss["tx_frames"], "tx_parity": ss["tx_parity"],
            "tx_replicas": ss["tx_replicas"], "kbps": round(kbps, 1),
            "rx_frames": rs["rx_frames"], "played": rs["played"],
            "fec": rs["recovered_fec"], "replica": rs["recovered_replica"],
            "concealed": rs["concealed"], "auth_fail": rs["rx_auth_fail"],
            "loss_after": round(rs["loss_after_repair_pct"], 2),
            "net_loss": round(st.loss_rate * 100, 2), "burst": round(st.mean_burst, 2),
            "jitter": round(st.jitter_ms, 2), "spread": round(st.delay_spread_ms, 2),
            "depth": round(sb.depth_ms, 1), "rho": round(sa.rho, 3),
            "codec": dec.codec if dec else None,
            "pred_mos": round(dec.predicted_mos, 3) if dec else None,
            "mos": round(mos_now, 3),
            "drops": alice.ep.dropped_by_impairment, "tx_dgrams": alice.ep.tx_packets,
        }

    def _summarise(self, alice, sa, sb, src_a, sink_b, seconds, protect) -> dict:
        lost = sb.concealed_seqs
        dmg, run, prev = 0.0, 0, -10
        for q in sorted(lost):
            run = run + 1 if q == prev + 1 else 0
            dmg += damage_weight(sa.sent_pi.get(q, 128), run)
            prev = q
        played = max(1, sb.stats.played)
        perceptual_pct = 100.0 * dmg / played
        self.sent_pcm = src_a.collected
        self.heard_pcm = sink_b.collected
        q = audio_quality(self.sent_pcm, self.heard_pcm)
        st, sr = sa.stats.snapshot(), sb.stats.snapshot()
        est = sb.est.snapshot()
        qr = emodel.assess("opus-24k", 40.0 + sb.depth_ms, sr["loss_after_repair_pct"],
                           perceptual_pct, max(1.0, est.mean_burst))
        return {
            "protect": protect, "seconds": round(seconds, 2),
            "perceptual_pct": perceptual_pct, "sent_frames": st["tx_frames"],
            "parity": st["tx_parity"], "replicas": st["tx_replicas"],
            "played": sr["played"], "rx_frames": sr["rx_frames"],
            "fec_rec": sr["recovered_fec"], "rep_rec": sr["recovered_replica"],
            "concealed": sr["concealed"], "loss_after_repair": sr["loss_after_repair_pct"],
            "auth_fail": sr["rx_auth_fail"],
            "tx_kbps": st["tx_bytes"] * 8 / max(1e-9, seconds) / 1000,
            "drops": alice.ep.dropped_by_impairment, "tx_dgrams": alice.ep.tx_packets,
            "channel_loss_pct": 100.0 * alice.ep.dropped_by_impairment / max(1, alice.ep.tx_packets),
            "depth_ms": sb.depth_ms, "mos": qr.mos, "r": qr.r,
            "quality": emodel.quality_label(qr.mos),
            "corr": q["corr"], "snr_db": q["snr_db"], "coverage": q["coverage"],
            "lag_ms": q["lag_ms"], **self.timings,
        }

    # -- what the browser polls ------------------------------------------------
    def frames_since(self, cursor: int) -> tuple[list, int]:
        sb, sa = self._sb, self._sa
        if sb is None or sb._play_seq is None:
            return [], cursor
        cl = sb.concealed_seqs
        while self._concealed_seen < len(cl):
            self._concealed.add(cl[self._concealed_seen]); self._concealed_seen += 1
        upto = sb._play_seq
        first = sb._first_seq or 0
        out = []
        start = max(cursor, 0)
        for s in range(start, upto):
            if s < first or s in self._concealed:
                fate = FATE_CONCEALED
            else:
                fate = sb.fate.get(s, FATE_DIRECT)
            out.append([s, int(sa.sent_pi.get(s, 0)), fate])
        return out, max(cursor, upto)

    def audio_since(self, cursor: int) -> tuple[str, int]:
        sink = self._sink_b
        if sink is None:
            return "", cursor
        frames = sink._frames
        n = len(frames)
        if n <= cursor:
            return "", cursor
        chunk = np.concatenate(frames[cursor:n]).astype("<i2")
        return base64.b64encode(chunk.tobytes()).decode(), n

    def poll(self, cur: dict, with_audio: bool = True) -> dict:
        with self._lock:
            tl = self.timeline[cur["t"]:]
            ev = self.events[cur["e"]:]
            cur["t"] += len(tl); cur["e"] += len(ev)
        frames, cur["f"] = self.frames_since(cur["f"])
        audio = ""
        if with_audio:
            audio, cur["a"] = self.audio_since(cur["a"])
        out = {"phase": self.phase, "telemetry": [{k: v for k, v in s.items() if k[0] != "_"} for s in tl],
               "events": ev, "frames": frames, "audio": audio,
               "impairment": self.impairment}
        if self.sent_env and not cur.get("env"):
            out["sent_env"] = self.sent_env
            cur["env"] = True
        if self.done.is_set():
            out["result"] = self.result
            out["error"] = self.error
        return out

    def describe(self) -> dict:
        return {"id": self.id, "kind": self.kind, "phase": self.phase, "params": self.p,
                "error": self.error, "result": self.result, "created": self.created,
                "impairment": self.impairment, "done": self.done.is_set(),
                "has_audio": self.heard_pcm.size > 0}


class Benchmark:
    """The same call run once per protection scheme over an identical channel."""

    ARMS = ("swara", "uniform", "none")

    def __init__(self, params: dict, users_file: str, upload_path: str | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = "bench"
        self.p = params
        self.users_file = users_file
        self.upload_path = upload_path
        self.phase = "queued"
        self.error: str | None = None
        self.created = time.time()
        self.repeat = max(1, min(3, int(params.get("repeat", 1))))
        self.runs: dict[str, list[LabCall]] = {a: [] for a in self.ARMS}
        self.results: dict[str, dict] = {}
        self.current: LabCall | None = None
        self.done = threading.Event()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        if self.current:
            self.current.stop()

    def run(self):
        try:
            for arm in self.ARMS:
                reps = []
                for rep in range(self.repeat):
                    if self._stop.is_set():
                        raise RuntimeError("benchmark cancelled")
                    self.phase = f"{arm}:{rep + 1}"
                    prm = dict(self.p, protection=arm, seed=int(self.p.get("seed", 7)) + rep * 11)
                    call = LabCall(prm, self.users_file, self.upload_path)
                    self.current = call
                    self.runs[arm].append(call)
                    call.run()
                    if call.error:
                        raise RuntimeError(f"arm '{arm}': {call.error}")
                    reps.append(call.result)
                avg = {}
                for k, v in reps[0].items():
                    avg[k] = float(np.mean([r[k] for r in reps])) if isinstance(v, (int, float)) else v
                avg["protect"] = arm
                avg["quality"] = emodel.quality_label(avg["mos"])
                self.results[arm] = avg
            self.phase = "done"
        except Exception as e:
            self.error = str(e)
            self.phase = "error"
        finally:
            self.current = None
            self.done.set()

    def describe(self) -> dict:
        cur = self.current
        live = None
        if cur is not None:
            with cur._lock:
                last = dict(cur.timeline[-1]) if cur.timeline else None
            live = {"phase": cur.phase, "arm": cur.p.get("protection"),
                    "seconds": cur.p.get("seconds"),
                    "sample": {k: v for k, v in last.items() if k[0] != "_"} if last else None}
        arms = {}
        for a, calls in self.runs.items():
            arms[a] = {"runs": len(calls), "call_ids": [c.id for c in calls],
                       "done": a in self.results}
        return {"id": self.id, "kind": self.kind, "phase": self.phase, "params": self.p,
                "repeat": self.repeat, "error": self.error, "results": self.results,
                "arms": arms, "live": live, "done": self.done.is_set(),
                "created": self.created}


class LabManager:
    """Owns the single active lab run and a short history for audio replay."""

    def __init__(self, users_file: str):
        self.users_file = users_file
        self.active = None
        self.history: dict[str, object] = {}
        self._lock = threading.Lock()
        self.ensure_users()

    def ensure_users(self):
        import json, os
        ok = False
        if os.path.exists(self.users_file):
            try:
                with open(self.users_file, encoding="utf-8") as f:
                    ok = set(LAB_USERS) <= set(json.load(f))
            except Exception:
                ok = False
        if not ok:
            for u, pw in LAB_USERS.items():
                server_mod.save_user(u, pw, self.users_file)

    def busy(self) -> bool:
        return self.active is not None and not self.active.done.is_set()

    def _launch(self, run):
        with self._lock:
            if self.busy():
                raise RuntimeError("another lab run is in progress")
            self.ensure_users()
            self.active = run
            self.history[run.id] = run
            if len(self.history) > 24:
                for k in list(self.history)[:-24]:
                    self.history.pop(k, None)
        threading.Thread(target=run.run, name=f"lab-{run.id}", daemon=True).start()
        return run

    def start_call(self, params: dict, upload_path=None) -> LabCall:
        return self._launch(LabCall(params, self.users_file, upload_path))

    def start_bench(self, params: dict, upload_path=None) -> Benchmark:
        return self._launch(Benchmark(params, self.users_file, upload_path))

    def get(self, rid: str):
        run = self.history.get(rid)
        if run is not None:
            return run
        for r in self.history.values():
            if isinstance(r, Benchmark):
                for calls in r.runs.values():
                    for c in calls:
                        if c.id == rid:
                            return c
        return None

    def audio_backends(self) -> dict:
        return backends_available()
