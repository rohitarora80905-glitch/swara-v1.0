"""
End-to-end evaluation engine.

Runs a complete SWARA call -- perceptual analysis, FEC scheduling, encryption,
a Gilbert-Elliott path, reassembly, erasure decoding, adaptive playout, and
E-model scoring -- and does the same for two comparators, so every number in
the results is a like-for-like measurement rather than an assertion.

  BASELINE       Plain RTP-style UDP.  No FEC.  Conventional jitter buffer
                 sized at the 95th percentile of observed delay.  This is what
                 a straightforward socket-programming voice chat does.

  UNIFORM-FEC    Loss-driven adaptive FEC given EXACTLY the redundancy budget
                 SWARA spent, spread evenly over all groups.  Conventional
                 jitter buffer.  Essentially RFC 5109 ULP-FEC / FlexFEC
                 behaviour.  Any gain SWARA shows over this is attributable to
                 the perceptual allocation, not to "SWARA has FEC".

  SWARA          PI-AFEC + piggybacked perceptual anchor replicas + R-factor
                 optimal playout + NAVIK joint control.

A note on the anchor replica, because it drove a redesign.  A replica is only
useful if it arrives before its own frame's playout deadline.  Frame i is
played at  i*T + base + depth; a replica riding on frame i+D arrives at
(i+D)*T + transit.  Usefulness therefore requires D*T <= depth, approximately.
The jitter buffer depth is a HARD UPPER BOUND on how far a replica may be
displaced -- so the R-factor-optimal buffer is simultaneously choosing how much
burst decorrelation the anchor mechanism is allowed to buy.  These are not two
independent knobs; they are one coupled decision, and SWARA solves them jointly.

The replica is PIGGYBACKED onto an existing packet rather than sent as its own
datagram, so it costs only its payload bytes -- no second set of SWARA, UDP and
IP headers.  At 24 kbit/s that is 60 bytes instead of 120.
"""

from __future__ import annotations

import numpy as np
from collections import defaultdict
from dataclasses import dataclass, field

from . import emodel
from .channel import ChannelConfig, GilbertElliottChannel
from .controller import NavikController
from .fec import FecScheduler, UniformFecScheduler
from .packet import AEAD_TAG_LEN, HEADER_LEN
from .perceptual import PerceptualImportanceAnalyzer, damage_weight
from .receiver import AdaptiveJitterBuffer, NetworkStateEstimator
from .speech import SpeechGenerator

FRAME_MS = 20
GROUP_K = 8
UDP_IP_OVERHEAD = 28            # 8 UDP + 20 IPv4
ALGORITHMIC_DELAY_MS = 25.0     # encode + decode + resampling


@dataclass
class CallResult:
    system: str
    profile: str
    frames_total: int = 0
    frames_delivered: int = 0
    frames_recovered_fec: int = 0
    frames_recovered_anchor: int = 0
    frames_lost: int = 0
    frames_late: int = 0
    channel_loss_pct: float = 0.0
    residual_loss_pct: float = 0.0
    perceptual_loss_pct: float = 0.0
    mean_burst: float = 1.0
    jitter_ms: float = 0.0
    playout_delay_ms: float = 0.0
    mouth_to_ear_ms: float = 0.0
    wire_kbps: float = 0.0
    redundancy_pct: float = 0.0
    packets_sent: int = 0
    codec: str = "opus-24k"
    r_factor: float = 0.0
    mos: float = 0.0
    lost_pi_mean: float = 0.0
    delivered_pi_mean: float = 0.0
    jb_curve: list = field(default_factory=list)

    def row(self) -> str:
        return (f"{self.system:<12} MOS {self.mos:4.2f}  R {self.r_factor:5.1f}  "
                f"chan {self.channel_loss_pct:5.2f}%  resid {self.residual_loss_pct:5.2f}%  "
                f"perc {self.perceptual_loss_pct:5.2f}%  "
                f"delay {self.mouth_to_ear_ms:5.1f}ms  "
                f"redund {self.redundancy_pct:5.1f}%")


def frame_bytes(codec: str) -> int:
    kbps = emodel.CODEC_PARAMS[codec][2]
    return max(8, int(round(kbps * 1000 * FRAME_MS / 1000 / 8)))


class CallSimulator:
    """Simulates one direction of one call under one system."""

    def __init__(self, system: str, cfg: ChannelConfig, codec: str = "opus-24k",
                 duration_s: float = 30.0, seed: int = 3, k: int = GROUP_K,
                 forced_rho: float | None = None, mobile: bool = False,
                 forced_depth: float | None = None):
        self.system = system
        self.cfg = cfg
        self.codec = codec
        self.duration_s = duration_s
        self.seed = seed
        self.k = k
        self.forced_rho = forced_rho
        self.forced_depth = forced_depth
        self.mobile = mobile
        self._jb_curve: list = []

    # ------------------------------------------------------------------
    def run(self) -> CallResult:
        gen = SpeechGenerator(seed=self.seed)
        frames, _ = gen.frames(self.duration_s, FRAME_MS)
        analyzer = PerceptualImportanceAnalyzer()
        pi = np.array([analyzer.analyze(seg).pi for _, seg, _ in frames], dtype=int)
        n = (len(pi) // self.k) * self.k
        pi = pi[:n]

        navik = NavikController(k=self.k, mobile=self.mobile,
                                pi_afec=(self.system == "swara"))
        navik.capacity_est_kbps = navik._cap_ewma = self.cfg.capacity_kbps

        if self.forced_rho is not None:
            rho = self.forced_rho
        elif self.system == "baseline":
            rho = 0.0
        else:
            d = navik.decide(self.cfg.loss_rate, self.cfg.mean_burst,
                             self.cfg.jitter_ms, self.cfg.base_delay_ms, 60.0)
            rho, self.codec = d.rho, d.codec

        payload = frame_bytes(self.codec)
        pkt_size = HEADER_LEN + AEAD_TAG_LEN + payload + UDP_IP_OVERHEAD

        depth, base_transit, state = self._probe(pkt_size, n, pi)
        # How far a piggybacked replica may be displaced before it misses its
        # own deadline.  The playout decision bounds the protection decision.
        max_disp = max(1, int(depth // FRAME_MS) - 1)

        return self._transmit(pi, n, payload, pkt_size, rho, depth,
                              base_transit, state, max_disp)

    # ------------------------------------------------------------------
    def _probe(self, pkt_size: int, n: int, pi: np.ndarray):
        """An unprotected pilot pass that learns the delay distribution, just
        as a live call learns it during its first second."""
        ch = GilbertElliottChannel(ChannelConfig(**{**self.cfg.__dict__,
                                                   "seed": self.seed * 31 + 1}))
        est = NetworkStateEstimator()
        transit: dict[int, float] = {}
        for i in range(n):
            for at, ok in ch.transmit(i * FRAME_MS, pkt_size):
                if ok:
                    transit[i] = at - i * FRAME_MS
                    est.on_arrival(i, i * FRAME_MS, at, pkt_size)
        state = est.snapshot()
        tr = np.array(list(transit.values())) if transit else np.array([50.0])
        base = float(tr.min())
        rel = tr - base

        if self.forced_depth is not None:
            return float(self.forced_depth), base, state

        if self.system == "swara":
            jb = AdaptiveJitterBuffer(base_delay_ms=base)
            # Perceptual weight of the packets in the delay tail, computed with
            # the SAME damage function that scores the final result.  The
            # optimiser and the evaluator therefore agree by construction --
            # no tuning constant mediates between them.
            thr = float(np.percentile(tr, 88))
            tail = [int(pi[i]) for i, t in transit.items() if t >= thr and i < len(pi)]
            late_dmg = float(np.mean([damage_weight(p) for p in tail])) if tail else 0.35
            dec = None
            for _ in range(15):                       # iterate to steady state
                dec = jb.optimise(rel, self.codec, state.mean_burst,
                                  other_delay_ms=base + ALGORITHMIC_DELAY_MS,
                                  late_importance=late_dmg, mobile=self.mobile)
            self._jb_curve = dec.considered
            depth = dec.depth_ms
        else:
            self._jb_curve = []
            depth = float(np.clip(np.percentile(rel, 95) + 10.0, 20.0, 180.0))
        return depth, base, state

    # ------------------------------------------------------------------
    def _transmit(self, pi, n, payload, pkt_size, rho, depth, base_transit,
                  state, max_disp) -> CallResult:
        ch = GilbertElliottChannel(ChannelConfig(**{**self.cfg.__dict__,
                                                   "seed": self.seed * 977 + 13}))
        if self.system == "swara":
            sched = FecScheduler(k=self.k, rho_net=rho, mean_burst=self.cfg.mean_burst)
        elif self.system == "uniform_fec":
            sched = UniformFecScheduler(k=self.k, rho_net=rho,
                                        mean_burst=self.cfg.mean_burst)
        else:
            sched = None

        # ---- plan every group up front -------------------------------
        plans: dict[int, tuple] = {}
        replicas: dict[int, list[int]] = defaultdict(list)
        for gid, start in enumerate(range(0, n, self.k)):
            idxs = list(range(start, start + self.k))
            plan = sched.plan([int(pi[i]) for i in idxs]) if sched else None
            plans[gid] = (idxs, plan)
            if plan:
                for a_idx, a_delay in plan.anchors:
                    src = idxs[a_idx]
                    disp = min(a_delay, max_disp)
                    carrier = src + disp
                    if disp >= 1 and carrier < n:
                        replicas[carrier].append(src)

        # ---- transmit -------------------------------------------------
        arrivals: dict[int, float] = {}
        parity_arr: dict[int, dict] = defaultdict(dict)
        replica_arr: dict[int, float] = {}
        bytes_sent = packets_sent = 0

        for gid, (idxs, plan) in plans.items():
            r = plan.r if plan else 0
            for fi in idxs:
                size = pkt_size + payload * len(replicas.get(fi, ()))
                bytes_sent += size
                packets_sent += 1
                for at, ok in ch.transmit(fi * FRAME_MS, size):
                    if ok:
                        arrivals[fi] = at
                        for src in replicas.get(fi, ()):
                            replica_arr[src] = min(replica_arr.get(src, 1e18), at)
            for pj in range(r):
                st = idxs[-1] * FRAME_MS + 1 + pj * 0.4
                bytes_sent += pkt_size
                packets_sent += 1
                for at, ok in ch.transmit(st, pkt_size):
                    if ok:
                        parity_arr[gid][pj] = at

        # ---- receive --------------------------------------------------
        deadline_offset = base_transit + depth      # frame i plays at i*T + this
        on_time, late = set(), set()
        for fi, at in arrivals.items():
            (on_time if at - fi * FRAME_MS <= deadline_offset else late).add(fi)

        rec_fec, rec_anchor = set(), set()
        if sched is not None:
            for gid, (idxs, plan) in plans.items():
                r = plan.r if plan else 0
                if r == 0:
                    continue
                have = sum(1 for fi in idxs if fi in on_time)
                par_times = sorted(parity_arr[gid].values())
                if have + len(par_times) < self.k:
                    continue          # group is mathematically undecodable
                # The group can only be decoded once the LAST parity packet it
                # needs has arrived.  A recovered frame is useful only if that
                # moment precedes ITS OWN playout instant -- which is why a
                # systematic code protects the tail of a group far better than
                # its head, and why the anchor mechanism exists at all.
                need = self.k - have
                decode_at = par_times[need - 1] if need > 0 else 0.0
                for fi in idxs:
                    if fi not in on_time and decode_at - fi * FRAME_MS <= deadline_offset:
                        rec_fec.add(fi)
                        on_time.add(fi)

        for src, at in replica_arr.items():
            if src not in on_time and at - src * FRAME_MS <= deadline_offset:
                rec_anchor.add(src)
                on_time.add(src)

        lost = sorted(set(range(n)) - on_time)

        # ---- score ----------------------------------------------------
        damage, run, prev = 0.0, 0, -10
        for fi in lost:
            run = run + 1 if fi == prev + 1 else 0
            damage += damage_weight(int(pi[fi]), run)
            prev = fi
        residual = 100.0 * len(lost) / max(1, n)
        perceptual = emodel.perceptual_ppl(damage, n)
        bursts = self._burst_lengths(lost)
        mean_burst = float(np.mean(bursts)) if bursts else 1.0

        # A SYSTEMATIC code adds no mouth-to-ear delay: the k data packets are
        # transmitted the instant they are encoded and are played out on
        # schedule.  Only the RECOVERY path waits for parity, and that wait is
        # already enforced per frame against each frame's own playout instant.
        # Charging the whole stream for the group span would be simply wrong.
        m2e = base_transit + depth + ALGORITHMIC_DELAY_MS
        rep = emodel.assess(self.codec, m2e, residual, perceptual, mean_burst, self.mobile)

        wire_kbps = bytes_sent * 8 / (n * FRAME_MS / 1000.0) / 1000.0
        media_only = pkt_size * n
        redundancy = 100.0 * (bytes_sent - media_only) / media_only

        return CallResult(
            system=self.system, profile=self.cfg.label(),
            frames_total=n, frames_delivered=len(on_time),
            frames_recovered_fec=len(rec_fec), frames_recovered_anchor=len(rec_anchor),
            frames_lost=len(lost), frames_late=len(late),
            channel_loss_pct=100.0 * ch.observed_loss,
            residual_loss_pct=residual, perceptual_loss_pct=perceptual,
            mean_burst=mean_burst, jitter_ms=state.jitter_ms,
            playout_delay_ms=depth, mouth_to_ear_ms=m2e,
            wire_kbps=wire_kbps, redundancy_pct=redundancy,
            packets_sent=packets_sent, codec=self.codec,
            r_factor=rep.r, mos=rep.mos,
            lost_pi_mean=float(np.mean([pi[i] for i in lost])) if lost else 0.0,
            delivered_pi_mean=float(np.mean([pi[i] for i in sorted(on_time)]))
            if on_time else 0.0,
            jb_curve=self._jb_curve,
        )

    @staticmethod
    def _burst_lengths(sorted_lost: list[int]) -> list[int]:
        if not sorted_lost:
            return []
        out, cur = [], 1
        for a, b in zip(sorted_lost, sorted_lost[1:]):
            if b == a + 1:
                cur += 1
            else:
                out.append(cur); cur = 1
        out.append(cur)
        return out


def compare(cfg: ChannelConfig, duration_s: float = 30.0, seed: int = 3,
            codec: str = "opus-24k", mobile: bool = False) -> dict[str, CallResult]:
    """All three systems, same path, same speech, same redundancy budget."""
    swara = CallSimulator("swara", cfg, codec, duration_s, seed, mobile=mobile).run()
    rho_equal = float(np.clip(swara.redundancy_pct / 100.0, 0.0, 0.65))
    uni = CallSimulator("uniform_fec", cfg, swara.codec, duration_s, seed,
                        forced_rho=rho_equal, mobile=mobile).run()
    base = CallSimulator("baseline", cfg, swara.codec, duration_s, seed,
                         forced_rho=0.0, mobile=mobile).run()
    return {"baseline": base, "uniform_fec": uni, "swara": swara}
