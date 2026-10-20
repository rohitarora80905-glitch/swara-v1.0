"""
Receiver side: network state estimation, FEC recovery, and the adaptive
playout buffer.

The jitter buffer is where SWARA departs most sharply from convention.

A conventional adaptive jitter buffer targets a LOSS objective: "size the
buffer so that at most 1% of packets arrive too late".  That is the wrong
objective, because it treats the delay it adds as free.  It is not free -- the
E-model's Id term charges 0.024 R-points per millisecond, and above 177 ms it
charges 0.134 per millisecond.

SWARA sizes the buffer by directly maximising the predicted R-factor:

    d* = argmax_d  R( codec, delay_base + d, Ppl(d), burst )

where Ppl(d) is the perceptually weighted residual loss that would result from
a playout deadline of d, computed from the measured arrival-delay histogram.
Deepening the buffer moves packets from "late" to "on time", lowering Ppl; it
simultaneously raises Id.  The optimum is the point where the marginal R gained
from rescued packets equals the marginal R lost to delay -- and because Ppl is
perceptually weighted, the optimum shifts DEEPER when the packets arriving late
happen to be important ones, and shallower when they are silence.
"""

from __future__ import annotations

import math
import numpy as np
from collections import deque
from dataclasses import dataclass, field

from . import emodel
from .perceptual import damage_weight


# ---------------------------------------------------------------------------
# Network state
# ---------------------------------------------------------------------------

@dataclass
class NetworkState:
    """Everything the controller needs, estimated purely from arrivals."""
    loss_rate: float = 0.0          # fraction of expected packets never seen
    mean_burst: float = 1.0         # mean consecutive-loss run length
    jitter_ms: float = 0.0          # RFC 3550 interarrival jitter estimate
    delay_spread_ms: float = 0.0    # p95 - p5 of the relative delay histogram
    rtt_ms: float = 0.0
    reorder_rate: float = 0.0
    goodput_kbps: float = 0.0
    packets_seen: int = 0

    def summary(self) -> str:
        return (f"loss {self.loss_rate*100:5.2f}%  burst {self.mean_burst:4.2f}  "
                f"jitter {self.jitter_ms:5.1f}ms  spread {self.delay_spread_ms:5.1f}ms")


class NetworkStateEstimator:
    """Streaming estimator over a sliding window of arrivals.

    All the statistics come from the media stream itself.  There are no probe
    packets and no separate RTCP-style reports on the wire: the 16-byte
    telemetry block rides in the header of the reverse-direction voice packets,
    which are being sent anyway.  A duplex call therefore pays ZERO extra
    packets for full closed-loop measurement.
    """

    def __init__(self, window: int = 250, frame_ms: int = 20):
        self.window = window
        self.frame_ms = frame_ms
        self._delays: deque[float] = deque(maxlen=window)
        self._arrivals: deque[tuple[int, float]] = deque(maxlen=window)
        self._bytes: deque[tuple[float, int]] = deque(maxlen=window)
        self._jitter = 0.0
        self._last_transit: float | None = None
        self._highest_seq = -1
        self._received: set[int] = set()
        self._loss_runs: list[int] = []
        self._cur_run = 0
        self._reorders = 0
        self._total = 0

    def on_arrival(self, seq: int, send_ts_ms: float, recv_ts_ms: float,
                   size_bytes: int) -> None:
        transit = recv_ts_ms - send_ts_ms
        self._delays.append(transit)
        self._arrivals.append((seq, recv_ts_ms))
        self._bytes.append((recv_ts_ms, size_bytes))
        self._total += 1

        # RFC 3550 interarrival jitter: exponentially smoothed |D(i-1, i)|.
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self._jitter += (d - self._jitter) / 16.0
        self._last_transit = transit

        if seq < self._highest_seq:
            self._reorders += 1
        else:
            gap = seq - self._highest_seq - 1 if self._highest_seq >= 0 else 0
            if gap > 0:
                self._loss_runs.append(gap)
            self._highest_seq = seq
        self._received.add(seq)
        if len(self._received) > self.window * 3:
            cutoff = self._highest_seq - self.window * 2
            self._received = {s for s in self._received if s >= cutoff}

    def snapshot(self, rtt_ms: float = 0.0) -> NetworkState:
        expected = max(1, self._highest_seq + 1)
        seen = min(self._total, expected)
        loss = 1.0 - seen / expected if expected else 0.0
        runs = self._loss_runs[-80:]
        burst = float(np.mean(runs)) if runs else 1.0

        if len(self._delays) >= 8:
            arr = np.fromiter(self._delays, dtype=float)
            spread = float(np.percentile(arr, 95) - np.percentile(arr, 5))
        else:
            spread = 0.0

        if len(self._bytes) >= 2:
            t0, t1 = self._bytes[0][0], self._bytes[-1][0]
            span = max(1e-3, (t1 - t0) / 1000.0)
            total_b = sum(b for _, b in self._bytes)
            goodput = total_b * 8.0 / span / 1000.0
        else:
            goodput = 0.0

        return NetworkState(
            loss_rate=float(np.clip(loss, 0.0, 1.0)),
            mean_burst=max(1.0, burst),
            jitter_ms=self._jitter,
            delay_spread_ms=spread,
            rtt_ms=rtt_ms,
            reorder_rate=self._reorders / max(1, self._total),
            goodput_kbps=goodput,
            packets_seen=self._total,
        )

    def relative_delays(self) -> np.ndarray:
        """Arrival delays relative to the minimum observed -- the histogram the
        jitter buffer optimiser needs."""
        if not self._delays:
            return np.zeros(0)
        arr = np.fromiter(self._delays, dtype=float)
        return arr - arr.min()


# ---------------------------------------------------------------------------
# R-factor-optimal adaptive jitter buffer
# ---------------------------------------------------------------------------

@dataclass
class JitterBufferDecision:
    depth_ms: float
    predicted_r: float
    predicted_mos: float
    late_loss_pct: float
    considered: list[tuple[float, float]] = field(default_factory=list)


class AdaptiveJitterBuffer:
    """Playout buffer whose depth is chosen to maximise predicted R-factor.

    `importance_of_late` lets the optimiser know whether the packets that are
    currently arriving late are perceptually valuable.  When they are, the
    optimiser accepts more delay to rescue them; when they are silence, it
    keeps the buffer shallow and the conversation responsive.
    """

    DEPTH_GRID_MS = (0, 10, 20, 30, 40, 60, 80, 100, 120, 150, 180)

    def __init__(self, base_delay_ms: float = 40.0, frame_ms: int = 20,
                 min_depth_ms: float = 20.0, max_depth_ms: float = 180.0):
        self.base_delay_ms = base_delay_ms
        self.frame_ms = frame_ms
        self.min_depth = min_depth_ms
        self.max_depth = max_depth_ms
        self.depth_ms = 60.0
        self._smoothed = 60.0

    def optimise(self, rel_delays: np.ndarray, codec: str, mean_burst: float,
                 other_delay_ms: float, late_importance: float = 0.5,
                 mobile: bool = False) -> JitterBufferDecision:
        if rel_delays.size < 8:
            return JitterBufferDecision(self.depth_ms, 0.0, 0.0, 0.0)

        considered = []
        best = (-1.0, self.depth_ms, 0.0)
        for d in self.DEPTH_GRID_MS:
            if not (self.min_depth <= d <= self.max_depth):
                continue
            late_frac = float(np.mean(rel_delays > d))
            # Perceptually weight the late packets: a late silence frame costs
            # almost nothing, a late voiced onset costs more than one packet.
            eff_loss_pct = 100.0 * late_frac * (0.25 + 1.5 * late_importance)
            total_delay = other_delay_ms + d
            r = emodel.r_factor(codec, total_delay, eff_loss_pct, mean_burst, mobile)
            considered.append((float(d), r))
            if r > best[0]:
                best = (r, float(d), late_frac)

        r_best, d_best, late_frac = best
        # Hysteresis: buffer depth changes are audible, so move gently.
        self._smoothed += 0.35 * (d_best - self._smoothed)
        self.depth_ms = float(np.clip(self._smoothed, self.min_depth, self.max_depth))
        return JitterBufferDecision(
            depth_ms=self.depth_ms, predicted_r=r_best,
            predicted_mos=emodel.r_to_mos(r_best),
            late_loss_pct=100.0 * late_frac, considered=considered)


# ---------------------------------------------------------------------------
# FEC-aware reassembly
# ---------------------------------------------------------------------------

@dataclass
class GroupBuffer:
    """Collects the packets of one FEC group until it can be decoded."""
    group_id: int
    k: int
    r: int
    shards: dict = field(default_factory=dict)
    pi: dict = field(default_factory=dict)
    first_seen_ms: float = 0.0
    resolved: bool = False

    def complete(self) -> bool:
        return len([i for i in self.shards if i < self.k]) == self.k

    def decodable(self) -> bool:
        return len(self.shards) >= self.k
