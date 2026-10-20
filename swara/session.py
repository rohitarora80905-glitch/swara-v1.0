"""
The live media session.

This is where the whole system actually runs.  Three threads per call:

  SENDER    capture -> encode -> perceptual analysis -> FEC scheduling ->
            replica piggyback -> AEAD seal -> UDP sendto
  RECEIVER  UDP recvfrom -> AEAD open -> replica extraction -> insert into the
            reassembly buffer -> telemetry accounting
  PLAYOUT   every 20 ms: take the next frame in sequence, recovering it from
            parity or from a replica if the original never arrived, conceal it
            if nothing can be recovered, decode, write to the speaker

The sender and playout threads are paced independently against a monotonic
clock rather than by sleeping a fixed interval, so accumulated scheduling error
does not cause the call to drift.

Media plaintext layout (before AEAD sealing):

    +--------+----------------------------------------+----------------+
    | nrep 1B| nrep x { seq 2B, len 2B, payload ... }  | primary frame  |
    +--------+----------------------------------------+----------------+

The 16-byte SWARA header is supplied as associated data, so the perceptual
importance octet is readable on the wire but cannot be altered.
"""

from __future__ import annotations

import struct
import threading
import time
import numpy as np
from collections import defaultdict, deque

from . import emodel
from .auth import MediaCipher, ReplayWindow, SequenceReconstructor
from .codec import AdpcmCodec, Mode, conceal, frame_samples
from .controller import NavikController, dscp_for
from .fec import FecScheduler, ReedSolomon
from .packet import AEAD_TAG_LEN, HEADER_LEN, Datagram, Flag, Header, PType
from .perceptual import PerceptualImportanceAnalyzer, damage_weight
from .receiver import AdaptiveJitterBuffer, NetworkStateEstimator

FRAME_MS = 20
GROUP_K = 4     # see _min_depth_for_fec: parity must beat its own deadline
OUT_LEN = 320                      # 20 ms @ 16 kHz


def pack_telemetry(loss: float, burst: float, jitter_ms: float, mos: float) -> bytes:
    """16-byte report, piggybacked on reverse-direction media -- costs no packet."""
    return struct.pack("!HHHHQ",
                       int(min(65535, loss * 10000)),
                       int(min(65535, burst * 100)),
                       int(min(65535, jitter_ms * 100)),
                       int(min(65535, mos * 1000)), 0)


def unpack_telemetry(b: bytes):
    if len(b) < 16:
        return None
    loss, burst, jit, mos, _ = struct.unpack("!HHHHQ", b[:16])
    return {"loss": loss / 10000.0, "burst": burst / 100.0,
            "jitter_ms": jit / 100.0, "mos": mos / 1000.0}


class CallStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tx_frames = 0
        self.tx_parity = 0
        self.tx_replicas = 0
        self.tx_bytes = 0
        self.rx_frames = 0
        self.rx_late = 0
        self.rx_auth_fail = 0
        self.played = 0
        self.recovered_fec = 0
        self.recovered_replica = 0
        self.concealed = 0
        self.pi_sum = 0
        self.lost_pi_sum = 0
        self.peer_mos = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            d = dict(self.__dict__)
        d.pop("lock", None)
        played = max(1, self.played)
        d["loss_after_repair_pct"] = 100.0 * self.concealed / played
        d["recovery_pct"] = 100.0 * (self.recovered_fec + self.recovered_replica) / played
        return d


class CallSession:
    """One end of one call.  Owns the codec, the protection state and the keys."""

    def __init__(self, endpoint, peer_addr, tx_key: bytes, rx_key: bytes,
                 call_id: int, source, sink, k: int = GROUP_K,
                 mode: int = Mode.WIDEBAND, adaptive: bool = True,
                 protection: str = "swara", name: str = "peer"):
        self.ep = endpoint
        self.peer = peer_addr
        self.call_id = call_id
        self.name = name
        self.source = source
        self.sink = sink
        self.k = k
        self.mode = mode
        self.adaptive = adaptive
        self.protection = protection          # "swara" | "uniform" | "none"

        self.tx = MediaCipher(tx_key, b"tx")
        self.rx = MediaCipher(rx_key, b"tx")   # peer's tx direction

        self.codec = AdpcmCodec(mode)
        self.analyzer = PerceptualImportanceAnalyzer()
        self.sched = FecScheduler(k=k, rho_net=0.0, mean_burst=1.0)
        # Provisioning is deliberately IDENTICAL for every protection scheme.
        #
        # Letting the controller anticipate the perceptual benefit made it buy
        # less redundancy than the uniform comparator -- 83 parity packets
        # against 373 -- so SWARA was being compared at a fifth of the budget
        # and lost on raw frame loss as a direct result.  That is not the
        # experiment this project claims to run.  The claim is "the same budget,
        # spent where it is heard", so the budget is sized the same way for all
        # arms and only the PLACEMENT differs.
        self.navik = NavikController(k=k, pi_afec=False, pi_efficiency=1.0)
        self.navik.capacity_est_kbps = self.navik._cap_ewma = 400.0
        self.jb = AdaptiveJitterBuffer(base_delay_ms=0.0)
        self.est = NetworkStateEstimator()
        self.stats = CallStats()

        # Start at a depth the protection scheme can actually use.  Starting
        # shallow and deepening later costs a stall the listener can hear.
        self.depth_ms = 60.0 if protection == "none" else float((k + 3) * FRAME_MS)
        self.rho = 0.0

        self._seq = 0
        self._group_id = 0
        self._pending: list[tuple[int, bytes, int]] = []   # (seq, payload, pi)
        self._replica_queue: dict[int, list[tuple[int, bytes]]] = defaultdict(list)

        self._rx_payload: dict[int, bytes] = {}
        self._rx_parity: dict[int, dict[int, bytes]] = defaultdict(dict)
        self._rx_group_r: dict[int, int] = {}
        self._rx_lock = threading.Lock()
        self._roc = SequenceReconstructor()
        self._replay = ReplayWindow()
        self._first_seq: int | None = None
        self._play_seq: int | None = None
        self._last_pcm: np.ndarray | None = None
        self._conceal_run = 0
        self._epoch = 0.0
        self._epoch_seq = 0
        self._spread_ms = 0.0        # p95 - min of observed one-way delay
        self._delays: deque = deque(maxlen=300)
        # Perceptual accounting.  A raw count of concealed frames is the wrong
        # scoreboard for this system: PI-AFEC deliberately declines to protect
        # silence, so it will always concede raw frames to a scheme that
        # protects everything equally.  What it claims to reduce is the
        # perceptual DAMAGE, so that is what has to be measured.
        self.sent_pi: dict[int, int] = {}
        self.concealed_seqs: list[int] = []

        self._running = threading.Event()
        self._threads: list[threading.Thread] = []
        self.t0 = time.monotonic()
        self.on_frame_sent = None            # optional UI callback
        self.finished = threading.Event()

    # ------------------------------------------------------------------
    def _now_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0

    def start(self) -> None:
        self._running.set()
        for fn, nm in ((self._sender_loop, "tx"), (self._receiver_loop, "rx"),
                       (self._playout_loop, "play"), (self._control_loop, "ctl")):
            t = threading.Thread(target=fn, name=f"{self.name}-{nm}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._running.clear()
        for t in self._threads:
            t.join(timeout=2.0)

    # ------------------------------------------------------------------
    # SENDER
    # ------------------------------------------------------------------
    def _sender_loop(self) -> None:
        next_tick = time.monotonic()
        while self._running.is_set():
            pcm = self.source.read(timeout=0.5)
            if pcm is None:
                break
            payload = self.codec.encode(pcm)
            pi = self.analyzer.analyze(pcm.astype(np.float64) / 32768.0).pi
            self._pending.append((self._seq, payload, pi))
            self.sent_pi[self._seq] = pi
            with self.stats.lock:
                self.stats.pi_sum += pi

            self._emit_media(self._seq, payload, pi)
            self._seq += 1

            if len(self._pending) >= self.k:
                self._close_group()

            next_tick += FRAME_MS / 1000.0
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()      # we fell behind; resynchronise
        self._running.clear()
        self.finished.set()

    def _emit_media(self, seq: int, payload: bytes, pi: int) -> None:
        reps = self._replica_queue.pop(seq, [])
        body = bytearray([len(reps)])
        for rseq, rpay in reps:
            # A displacement survives sequence wrap; an absolute 16-bit value
            # does not.  The receiver reconstructs the source from its own
            # full counter for the carrier packet.
            body += struct.pack("!BH", min(255, seq - rseq), len(rpay)) + rpay
        body += payload

        h = Header(ptype=PType.MEDIA, seq=seq & 0xFFFF, cid=self.call_id,
                   ts_ms=int(self._now_ms()), pi=pi,
                   fec_group=self._group_id & 0xFF, fec_index=seq % self.k,
                   flags=Flag.ENCRYPTED)
        tel = pack_telemetry(*self._telemetry_values())
        h.flags |= Flag.TELEMETRY_PIGGYBACK
        hdr = h.pack()
        ct = self.tx.seal(seq, hdr + tel, bytes(body))
        self.ep.set_dscp(dscp_for(pi, self.navik.history[-1]) if self.navik.history else 0)
        self.ep.sendto(hdr + tel + ct, self.peer)
        with self.stats.lock:
            self.stats.tx_frames += 1
            self.stats.tx_bytes += len(hdr) + len(tel) + len(ct)
            self.stats.tx_replicas += len(reps)

    def _close_group(self) -> None:
        group = self._pending[: self.k]
        self._pending = self._pending[self.k:]
        gid = self._group_id
        self._group_id = (self._group_id + 1) & 0xFF

        if self.protection == "none":
            return
        pis = [p for _, _, p in group]
        plan = self.sched.plan(pis) if self.protection == "swara" else None
        if self.protection == "uniform":
            r = int(round(self.k * self.rho))
            anchors = []
        else:
            r, anchors = plan.r, plan.anchors

        # Replica displacement.  The copy must clear the loss burst AND still
        # beat its own frame's playout instant.  The usable budget is not the
        # whole buffer depth: the copy is itself subject to the same delay
        # variation as everything else, so the p95 delay spread has to be
        # subtracted first.  Ignoring that term places the replica about one
        # frame too late and it is discarded on arrival -- which is precisely
        # what happened before this was corrected, with only 7% of transmitted
        # replicas ever being used.
        usable_ms = self.depth_ms - self._spread_ms - FRAME_MS
        max_disp = max(1, int(usable_ms // FRAME_MS))
        for a_idx, a_delay in anchors:
            src_seq, src_pay, _ = group[a_idx]
            disp = min(a_delay, max_disp)
            if disp >= 1:
                self._replica_queue[src_seq + disp].append((src_seq, src_pay))

        if r <= 0:
            return
        shard_len = max(len(p) for _, p, _ in group)
        shards = [p.ljust(shard_len, b"\x00") for _, p, _ in group]
        try:
            parity = ReedSolomon(self.k, r).encode(shards)
        except Exception:
            return
        base_seq = group[0][0]
        for j, par in enumerate(parity):
            h = Header(ptype=PType.FEC, seq=(base_seq + self.k + j) & 0xFFFF,
                       cid=self.call_id, ts_ms=int(self._now_ms()), pi=0,
                       fec_group=gid, fec_index=0x80 | j, flags=Flag.ENCRYPTED)
            hdr = h.pack()
            body = struct.pack("!HBBH", base_seq & 0xFFFF, self.k, r, shard_len) + par
            ct = self.tx.seal(h.seq, hdr, body)
            self.ep.sendto(hdr + ct, self.peer)
            with self.stats.lock:
                self.stats.tx_parity += 1
                self.stats.tx_bytes += len(hdr) + len(ct)

    # ------------------------------------------------------------------
    # RECEIVER
    # ------------------------------------------------------------------
    def _receiver_loop(self) -> None:
        while self._running.is_set():
            data, addr = self.ep.recvfrom(timeout=0.4)
            if data is None or len(data) < HEADER_LEN:
                continue
            try:
                h = Header.unpack(data)
            except Exception:
                continue
            if h.cid != self.call_id:
                continue
            off = HEADER_LEN
            tel = b""
            if h.flags & Flag.TELEMETRY_PIGGYBACK:
                tel = data[off:off + 16]; off += 16
            aad = data[:off]
            ct = data[off:]

            full_seq = self._roc.unwrap(h.seq)
            pt = self.rx.open(full_seq, aad, ct)
            if pt is None:
                with self.stats.lock:
                    self.stats.rx_auth_fail += 1
                continue
            if h.ptype == PType.MEDIA and not self._replay.check_and_set(full_seq):
                continue          # duplicate or replayed packet

            if tel:
                t = unpack_telemetry(tel)
                if t:
                    with self.stats.lock:
                        self.stats.peer_mos = t["mos"]

            if h.ptype == PType.MEDIA:
                self._on_media(h, pt, len(data), full_seq)
            elif h.ptype == PType.FEC:
                self._on_parity(h, pt)

    def _on_media(self, h: Header, pt: bytes, wire_len: int, full_seq: int) -> None:
        nrep = pt[0]
        off = 1
        reps = []
        for _ in range(nrep):
            if off + 3 > len(pt):
                break
            disp, rlen = struct.unpack("!BH", pt[off:off + 3]); off += 3
            reps.append((full_seq - disp, pt[off:off + rlen])); off += rlen
        primary = pt[off:]

        recv_ms = self._now_ms()
        self._delays.append(recv_ms - h.ts_ms)
        self.est.on_arrival(full_seq, h.ts_ms, recv_ms, wire_len)

        with self._rx_lock:
            if self._first_seq is None:
                self._first_seq = full_seq
                self._play_seq = full_seq
            if full_seq not in self._rx_payload:
                self._rx_payload[full_seq] = primary
                with self.stats.lock:
                    self.stats.rx_frames += 1
            for rseq, rpay in reps:
                if rseq not in self._rx_payload:
                    self._rx_payload[rseq] = rpay
                    with self.stats.lock:
                        self.stats.recovered_replica += 1

    def _on_parity(self, h: Header, pt: bytes) -> None:
        if len(pt) < 6:
            return
        base_seq, k, r, shard_len = struct.unpack("!HBBH", pt[:6])
        base_full = self._roc.nearest(base_seq)
        par = pt[6:]
        j = h.fec_index & 0x7F
        with self._rx_lock:
            self._rx_parity[h.fec_group][j] = par
            self._rx_group_r[h.fec_group] = (base_full, k, r, shard_len)

    def _try_fec(self, gid: int) -> bool:
        """Attempt to reconstruct a group.  Returns True if anything recovered."""
        meta = self._rx_group_r.get(gid)
        if not meta:
            return False
        base_seq, k, r, shard_len = meta
        have = {}
        missing = []
        for i in range(k):
            s = base_seq + i
            if s in self._rx_payload:
                have[i] = self._rx_payload[s].ljust(shard_len, b"\x00")
            else:
                missing.append((i, s))
        if not missing:
            return False
        for j, par in self._rx_parity.get(gid, {}).items():
            have[k + j] = par
        if len(have) < k:
            return False
        try:
            out = ReedSolomon(k, r).decode(have, shard_len)
        except Exception:
            return False
        if out is None:
            return False
        rescued = False
        for i, s in missing:
            if i in out:
                self._rx_payload[s] = out[i].rstrip(b"\x00") or out[i]
                rescued = True
                with self.stats.lock:
                    self.stats.recovered_fec += 1
        return rescued

    # ------------------------------------------------------------------
    # PLAYOUT
    # ------------------------------------------------------------------
    def _playout_loop(self) -> None:
        # Fill to the target depth before starting.  Playout thereafter runs
        # against an ABSOLUTE schedule anchored at `_epoch`, not a free-running
        # 20 ms tick.  This matters: with a free-running tick the buffer depth
        # is whatever happened to accumulate at startup and can never change,
        # so raising `depth_ms` later has no effect at all and any parity that
        # needs the extra slack arrives too late to be used.  Moving `_epoch`
        # forward genuinely deepens the buffer while the call is running.
        deadline = time.monotonic() + 5.0
        while self._running.is_set() and time.monotonic() < deadline:
            with self._rx_lock:
                ready = self._play_seq is not None and \
                    len(self._rx_payload) >= max(2, int(self.depth_ms // FRAME_MS))
            if ready:
                break
            time.sleep(0.005)

        with self._rx_lock:
            self._epoch = time.monotonic()
            self._epoch_seq = self._play_seq if self._play_seq is not None else 0
        idle = 0
        while self._running.is_set():
            # Wait until this slot is actually due.
            with self._rx_lock:
                cur = self._play_seq
                ep, eps = self._epoch, self._epoch_seq
            if cur is not None:
                due = ep + (cur - eps) * (FRAME_MS / 1000.0)
                sl = due - time.monotonic()
                if sl > 0:
                    time.sleep(min(sl, 0.25))
                    continue

            with self._rx_lock:
                if self._play_seq is None:
                    seq = None
                else:
                    seq = self._play_seq
                    pay = self._rx_payload.pop(seq, None)
                    if pay is None:
                        gid = (seq // self.k) & 0xFF
                        for g in (gid, (gid - 1) & 0xFF):
                            if self._try_fec(g):
                                break
                        pay = self._rx_payload.pop(seq, None)
                    if pay is not None:
                        self._play_seq = seq + 1

            if seq is None:
                time.sleep(0.01)
                continue

            if pay is not None:
                idle = 0
                try:
                    pcm = self.codec.decode(pay)
                except Exception:
                    pcm = conceal(self._last_pcm, OUT_LEN, self._conceal_run)
                self._last_pcm = pcm
                self._conceal_run = 0
                with self.stats.lock:
                    self.stats.played += 1
            else:
                # Nothing recoverable for this slot.
                still_coming = any(s > seq for s in self._rx_payload)
                if not still_coming:
                    idle += 1
                    if idle > 60 and self.finished.is_set():
                        break
                    time.sleep(0.005)
                    continue
                pcm = conceal(self._last_pcm, OUT_LEN, self._conceal_run)
                self._conceal_run += 1
                with self._rx_lock:
                    self._play_seq = seq + 1
                self.concealed_seqs.append(seq)
                with self.stats.lock:
                    self.stats.concealed += 1
                    self.stats.played += 1

            self.sink.write(pcm)

    # ------------------------------------------------------------------
    # CONTROL
    # ------------------------------------------------------------------
    def _telemetry_values(self):
        st = self.est.snapshot()
        s = self.stats
        played = max(1, s.played)
        ppl = 100.0 * s.concealed / played
        m = emodel.mos("opus-24k", 40.0 + self.depth_ms, ppl, max(1.0, st.mean_burst))
        return st.loss_rate, st.mean_burst, st.jitter_ms, m

    def _delay_trend(self) -> float:
        """Slope of recent one-way delay, in ms per second.

        A rising slope means a queue is filling somewhere, which is congestion.
        A flat slope with loss present means the link itself is dropping.
        """
        if len(self._delays) < 40:
            return 0.0
        arr = np.fromiter(self._delays, dtype=float)[-100:]
        t = np.arange(arr.size) * (FRAME_MS / 1000.0)
        try:
            slope = float(np.polyfit(t, arr, 1)[0])
        except Exception:
            slope = 0.0
        return slope

    def _control_loop(self) -> None:
        while self._running.is_set():
            time.sleep(0.1)
            if not self.adaptive:
                continue
            st = self.est.snapshot()
            if st.packets_seen < 25:
                continue
            self.navik.update_capacity(st.goodput_kbps, st.loss_rate, False,
                                       self._delay_trend())
            d = self.navik.decide(st.loss_rate, st.mean_burst, st.jitter_ms,
                                  40.0, self.depth_ms)
            self.rho = d.rho
            self.sched.rho_net = d.rho
            self.sched.mean_burst = st.mean_burst

            if len(self._delays) >= 20:
                arr = np.fromiter(self._delays, dtype=float)
                rel = arr - arr.min()
                dec = self.jb.optimise(rel, "opus-24k", st.mean_burst,
                                       other_delay_ms=float(arr.min()) + 25.0,
                                       late_importance=0.45)
                depth = dec.depth_ms
                # A systematic code emits its parity only once the group is
                # complete.  A frame early in the group is therefore rescuable
                # ONLY if the buffer still holds it when that parity lands.
                # Running FEC with a buffer shallower than the group span means
                # transmitting parity that can never be used, so we either
                # deepen the buffer or we stop paying for parity.
                self._spread_ms = float(np.percentile(rel, 95)) if rel.size else 0.0
                if self.rho > 0:
                    depth = max(depth, self._min_depth_for_protection(st.mean_burst))
                self._retarget_depth(min(depth, 220.0))

    def _retarget_depth(self, new_depth: float) -> None:
        """Change the playout depth on a running call by shifting the epoch.

        Deepening is applied immediately (the listener hears one slightly longer
        gap).  Shallowing is applied gradually, because pulling playout earlier
        means discarding audio, and doing that abruptly is audible.
        """
        delta = new_depth - self.depth_ms
        if abs(delta) < 5.0 or self._epoch == 0.0:
            self.depth_ms = new_depth
            return
        if delta > 0:
            shift = delta
        else:
            shift = max(delta, -10.0)          # at most 10 ms shallower per epoch
        with self._rx_lock:
            self._epoch += shift / 1000.0
        self.depth_ms += shift

    def _min_depth_for_protection(self, mean_burst: float) -> float:
        """Shallowest buffer at which the protection we are paying for can work.

        Two separate requirements, whichever is larger:

          parity    a systematic code emits parity only once the group closes,
                    so rescuing the FIRST frame of a group needs the group span
                    plus a frame of slack.

          replicas  a copy must be displaced past the burst that killed the
                    original, so it needs (burst + 2) frame times, plus the
                    delay spread it will itself experience.

        Buying protection without the depth to use it is pure waste, so if the
        controller will not accept this depth it should not be paying for the
        redundancy either.
        """
        parity_need = (self.k + 1) * FRAME_MS
        replica_need = (max(1.0, mean_burst) + 2.0) * FRAME_MS + self._spread_ms
        return max(parity_need, replica_need)

    # ------------------------------------------------------------------
    def report(self) -> str:
        s = self.stats.snapshot()
        return (f"[{self.name}] sent {s['tx_frames']} frames "
                f"(+{s['tx_parity']} parity, +{s['tx_replicas']} replicas)  "
                f"played {s['played']}  recovered {s['recovered_fec']} by FEC  "
                f"concealed {s['concealed']} ({s['loss_after_repair_pct']:.1f}%)  "
                f"auth-fail {s['rx_auth_fail']}")
