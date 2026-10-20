"""
Network impairment model -- the Gilbert-Elliott channel.

Real packet loss on the Internet is not a coin flip.  It is CLUSTERED: a queue
overflows, a radio link fades, a handover stalls, and eight consecutive packets
die together.  Any evaluation of a voice system against uniform Bernoulli loss
overstates how well it works, because Bernoulli loss is the easiest loss there
is -- concealment can interpolate across a single missing 20 ms frame almost
inaudibly, but not across 160 ms.

The Gilbert-Elliott model is the standard two-state Markov chain for this:

        p_GB
    GOOD  ---->  BAD          loss prob in GOOD = p_g  (near 0)
      ^            |          loss prob in BAD  = p_b  (near 1)
      +----<-------+
        p_BG

    steady-state P(BAD) = p_GB / (p_GB + p_BG)
    mean burst length   = 1 / p_BG

We parameterise it the way an operator would: "I want 10% average loss with a
mean burst of 4 packets", and solve for p_GB and p_BG.  Delay is modelled as a
base propagation term plus a heavy-tailed jitter component (log-normal), which
matches measured wide-area delay distributions far better than a Gaussian and
is what makes jitter buffer sizing genuinely hard.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field


@dataclass
class ChannelConfig:
    loss_rate: float = 0.0        # long-run average packet loss, 0..1
    mean_burst: float = 1.0       # mean consecutive losses when loss occurs
    base_delay_ms: float = 25.0   # one-way propagation + transmission
    jitter_ms: float = 8.0        # scale of the delay variation
    jitter_tail: float = 0.55     # log-normal sigma: higher = heavier tail
    reorder_prob: float = 0.0     # probability a packet is displaced in time
    duplicate_prob: float = 0.0
    capacity_kbps: float = 1000.0 # bottleneck rate; exceeding it causes queueing
    seed: int | None = None

    def label(self) -> str:
        return (f"loss={self.loss_rate*100:.0f}% burst={self.mean_burst:.1f} "
                f"delay={self.base_delay_ms:.0f}±{self.jitter_ms:.0f}ms")


class GilbertElliottChannel:
    """A stateful, replayable UDP path model."""

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.p_g = 0.0     # loss probability in GOOD state
        self.p_b = 1.0     # loss probability in BAD state
        self._solve_transitions()
        self.state_bad = False
        self.sent = 0
        self.lost = 0
        self.bursts: list[int] = []
        self._cur_burst = 0
        # token-bucket queue state for the bottleneck
        self._queue_bytes = 0.0
        self._last_t = 0.0

    def _solve_transitions(self) -> None:
        p = min(max(self.cfg.loss_rate, 0.0), 0.95)
        b = max(1.0, self.cfg.mean_burst)
        if p <= 0.0:
            self.p_gb, self.p_bg = 0.0, 1.0
            return
        # Loss only happens in BAD (p_b = 1, p_g = 0), so P(BAD) = p.
        self.p_bg = 1.0 / b                      # mean burst length = 1/p_bg
        self.p_gb = self.p_bg * p / (1.0 - p)    # steady state gives P(BAD)=p
        self.p_gb = min(1.0, self.p_gb)

    # -- per-packet decision ------------------------------------------------

    def _step_state(self) -> None:
        if self.state_bad:
            if self.rng.random() < self.p_bg:
                self.state_bad = False
                if self._cur_burst:
                    self.bursts.append(self._cur_burst)
                    self._cur_burst = 0
        else:
            if self.rng.random() < self.p_gb:
                self.state_bad = True

    def _delay(self) -> float:
        """Base delay plus a heavy-tailed positive jitter component."""
        if self.cfg.jitter_ms <= 0:
            return self.cfg.base_delay_ms
        mu = math.log(max(self.cfg.jitter_ms, 1e-3)) - 0.5 * self.cfg.jitter_tail ** 2
        j = self.rng.lognormvariate(mu, self.cfg.jitter_tail)
        return self.cfg.base_delay_ms + j

    def transmit(self, send_time_ms: float, size_bytes: int):
        """Return a list of (arrival_time_ms, delivered: bool) outcomes.

        A packet may be dropped (delivered=False), delivered once, or -- if
        duplication is enabled -- delivered twice.
        """
        self.sent += 1
        self._step_state()

        # Bottleneck queueing: drain the virtual queue since the last packet,
        # then add this packet.  An over-full queue drops (tail drop).
        dt = max(0.0, send_time_ms - self._last_t)
        self._last_t = send_time_ms
        drained = self.cfg.capacity_kbps * 1000.0 / 8.0 * (dt / 1000.0)
        self._queue_bytes = max(0.0, self._queue_bytes - drained)
        self._queue_bytes += size_bytes
        queue_delay_ms = (self._queue_bytes * 8.0 / (self.cfg.capacity_kbps * 1000.0)) * 1000.0
        if queue_delay_ms > 250.0:
            self.lost += 1
            self._cur_burst += 1
            self._queue_bytes -= size_bytes
            return [(send_time_ms, False)]

        loss_p = self.p_b if self.state_bad else self.p_g
        if self.rng.random() < loss_p:
            self.lost += 1
            self._cur_burst += 1
            return [(send_time_ms, False)]

        arrive = send_time_ms + self._delay() + queue_delay_ms
        if self.cfg.reorder_prob and self.rng.random() < self.cfg.reorder_prob:
            arrive += self.rng.uniform(20.0, 90.0)     # displaced behind others
        out = [(arrive, True)]
        if self.cfg.duplicate_prob and self.rng.random() < self.cfg.duplicate_prob:
            out.append((arrive + self.rng.uniform(1.0, 15.0), True))
        return out

    # -- observed statistics ------------------------------------------------

    @property
    def observed_loss(self) -> float:
        return self.lost / self.sent if self.sent else 0.0

    @property
    def observed_mean_burst(self) -> float:
        b = self.bursts + ([self._cur_burst] if self._cur_burst else [])
        return sum(b) / len(b) if b else 0.0


class DeliveryQueue:
    """Reorders packets by arrival time so the receiver sees them as the
    network would actually present them -- out of order when the path
    reorders, and never before their arrival timestamp."""

    def __init__(self) -> None:
        self._h: list = []
        self._c = 0

    def push(self, arrive_ms: float, item) -> None:
        heapq.heappush(self._h, (arrive_ms, self._c, item))
        self._c += 1

    def pop_until(self, now_ms: float):
        out = []
        while self._h and self._h[0][0] <= now_ms:
            t, _, item = heapq.heappop(self._h)
            out.append((t, item))
        return out

    def drain(self):
        out = []
        while self._h:
            t, _, item = heapq.heappop(self._h)
            out.append((t, item))
        return out

    def __len__(self) -> int:
        return len(self._h)


# Preset path profiles used throughout the evaluation.
PROFILES = {
    "ideal_lan":     ChannelConfig(0.00, 1.0, 5.0, 1.0, 0.30, 0.000, capacity_kbps=10_000),
    "good_wifi":     ChannelConfig(0.01, 1.5, 20.0, 6.0, 0.50, 0.005, capacity_kbps=2_000),
    "busy_wifi":     ChannelConfig(0.05, 3.0, 35.0, 14.0, 0.60, 0.020, capacity_kbps=800),
    "congested_4g":  ChannelConfig(0.10, 4.0, 60.0, 25.0, 0.70, 0.030, capacity_kbps=400),
    "poor_3g":       ChannelConfig(0.20, 6.0, 110.0, 45.0, 0.80, 0.050, capacity_kbps=200),
    "hostel_wifi":   ChannelConfig(0.15, 8.0, 45.0, 30.0, 0.85, 0.040, capacity_kbps=300),
    "satellite":     ChannelConfig(0.03, 2.0, 280.0, 20.0, 0.55, 0.010, capacity_kbps=600),
}
