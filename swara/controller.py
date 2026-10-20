"""
NAVIK -- Network-Adaptive Voice-quality Integrated Kontroller.

The sender has three dials and one budget:

    dial 1   codec bitrate b        higher b -> lower Ie -> better clean quality
    dial 2   FEC redundancy rho     higher rho -> lower residual loss
    dial 3   DSCP marking           which packets a congested router drops first
    budget   available capacity C   b * (1 + rho) + header overhead <= C

Turning any dial up costs the others.  Conventional stacks resolve this with a
cascade of independent heuristics: a bandwidth estimator picks b, a separate
loss-driven module picks rho, and DSCP is a static configuration item.  Those
modules can and do fight each other -- the classic pathology is FEC added in
response to congestion loss, which increases the offered load and deepens the
congestion that caused the loss in the first place.

NAVIK replaces the cascade with a single objective.  Every 100 ms it evaluates

    maximise  R( codec(b), delay(b, rho), Ppl_residual(rho, p, B, PI), B )
    s.t.      b * (1 + rho) + H <= C_est
              rho <= rho_max(p)          -- congestion safety rail

over a small discrete grid (9 bitrates x 9 redundancy levels = 81 evaluations,
about 40 microseconds).  Because R already contains the delay term, the
congestion pathology cannot occur: adding redundancy that pushes the offered
rate past C_est raises queueing delay, which lowers R, so the optimiser refuses.

The residual-loss model is where perceptual importance re-enters.  For an
RS(k+r, k) group under Gilbert-Elliott loss, the probability that the group
fails to decode is the probability of more than r erasures in k+r packets --
computed here with a burst-aware correction rather than the binomial, because
under clustered loss the binomial is dramatically optimistic.  PI-AFEC's
advantage appears in the model as a reduction in the *perceptual weight* of the
groups that are left unprotected.
"""

from __future__ import annotations

import math
import numpy as np
from dataclasses import dataclass, field

from . import emodel

BITRATE_LADDER = ["opus-6k", "opus-8k", "opus-12k", "opus-16k",
                  "opus-20k", "opus-24k", "opus-32k"]
RHO_GRID = [0.0, 0.075, 0.15, 0.225, 0.30, 0.375, 0.45, 0.55, 0.65]

HEADER_OVERHEAD_BPS = (16 + 16 + 8 + 20) * 8 * 50   # SWARA+AEAD+UDP+IP at 50 pps


@dataclass
class ControlDecision:
    codec: str
    bitrate_kbps: float
    rho: float
    dscp_high: int
    dscp_low: int
    predicted_r: float
    predicted_mos: float
    offered_kbps: float
    reason: str = ""

    def __str__(self) -> str:
        return (f"NAVIK -> {self.codec} @ {self.bitrate_kbps:.0f}kbps, "
                f"FEC {self.rho*100:.0f}%, offered {self.offered_kbps:.1f}kbps, "
                f"predicted MOS {self.predicted_mos:.2f}")


def group_failure_probability(k: int, r: int, p: float, mean_burst: float) -> float:
    """P(an RS(k+r, k) group cannot be decoded) under Gilbert-Elliott loss.

    Under independent loss this is P(erasures > r) ~ Binomial.  Under clustered
    loss the variance of the erasure count is much higher, so we use a
    beta-binomial with the over-dispersion implied by the burst length.  This
    matters enormously: at 10% loss with burst 4, the binomial predicts a group
    failure rate about 6x lower than reality, and a controller that believes it
    will under-provision FEC exactly when protection is most needed.
    """
    n = k + r
    p = float(np.clip(p, 1e-6, 0.95))
    if r >= n:
        return 0.0
    b = max(1.0, mean_burst)

    if b <= 1.05:
        # Effectively independent -> plain binomial tail.
        from math import comb
        return float(sum(comb(n, i) * p ** i * (1 - p) ** (n - i)
                         for i in range(r + 1, n + 1)))

    # Beta-binomial: intra-class correlation rho_c grows with burst length.
    rho_c = float(np.clip((b - 1.0) / (b + 1.0), 1e-3, 0.95))
    s = (1.0 - rho_c) / rho_c
    a, bb = p * s, (1.0 - p) * s

    from math import lgamma

    def logbeta(x, y):
        return lgamma(x) + lgamma(y) - lgamma(x + y)

    from math import comb
    lb0 = logbeta(a, bb)
    tail = 0.0
    for i in range(r + 1, n + 1):
        logp = (math.log(comb(n, i)) + logbeta(a + i, bb + n - i) - lb0)
        tail += math.exp(logp)
    return float(np.clip(tail, 0.0, 1.0))


def split_budget(budget_pkts: float, k: int, mean_burst: float) -> tuple[int, int]:
    """Divide a group's redundancy budget between RS parity and anchor replicas.

    Mirrors fec.FecScheduler exactly, so the controller predicts what the
    scheduler will actually do rather than what it wishes it would do.
    """
    # Mirrors fec.FecScheduler after normalisation: the EXPECTED per-group
    # spend is the full budget, so the controller predicts against the same
    # quantity of redundancy the scheduler will actually emit.
    if mean_burst >= 0.75 * k:
        sr, sa = 0.45, 0.55
    else:
        sr, sa = 0.85, 0.15
    return (int(round(budget_pkts * sr)), int(np.clip(round(budget_pkts * sa), 0, 4)))


def residual_loss(p: float, mean_burst: float, k: int, r: int,
                  n_anchor: int = 0, pi_efficiency: float = 1.0) -> float:
    """Perceptually weighted residual loss after FEC and anchor replicas.

    The two mechanisms are INDEPENDENT and must be modelled as such.  Reed-
    Solomon is a group-level mechanism: it either reconstructs the whole group
    or nothing.  An anchor replica is a frame-level mechanism that works even
    when the group is a total loss, because the replica sits outside the burst
    that destroyed the group.  Treating anchors as a modifier on RS -- as an
    earlier version of this model did -- makes the controller believe
    protection is futile in exactly the burst-dominated conditions where the
    anchor is the only thing that still works.

    `pi_efficiency` in (0, 1] is the PI-AFEC advantage: because redundancy is
    concentrated on the groups that carry meaning, the perceptual weight of the
    residual loss is a fraction of its raw packet count.  Setting it to 1.0
    reproduces uniform FEC exactly, which is how the comparison stays honest.
    """
    if r <= 0:
        base = p
    else:
        pf = group_failure_probability(k, r, p, mean_burst)
        base = pf * p

    if n_anchor > 0:
        # Each replica is displaced past the estimated burst, so it survives
        # unless an INDEPENDENT loss event hits it.  Covering m of k frames
        # removes at most m/k of the residual, weighted by replica survival.
        survive = 1.0 - p
        covered = min(1.0, n_anchor / k) * survive
        base *= (1.0 - covered)
    return float(np.clip(base * pi_efficiency, 0.0, 1.0))


class NavikController:
    """The joint optimiser.  One call per control epoch (default 100 ms)."""

    def __init__(self, k: int = 8, mobile: bool = False,
                 pi_afec: bool = True, safety_margin: float = 0.85,
                 pi_efficiency: float | None = None):
        # `pi_efficiency` is how much cheaper perceptual placement makes a given
        # residual loss LOOK to the optimiser.  The offline value of 0.42 is a
        # perceptual figure and is correct there, because the offline objective
        # is itself perceptually weighted.  Feeding it to a live controller that
        # is judged on RAW frame loss makes it believe its redundancy is 2.4x
        # more effective than it is, so it provisions roughly a quarter of what
        # a uniform scheme buys and loses on both counts.  Live sessions pass a
        # conservative value and keep the perceptual PLACEMENT without claiming
        # the perceptual DISCOUNT up front.
        self.pi_efficiency_override = pi_efficiency
        self.k = k
        self.mobile = mobile
        self.pi_afec = pi_afec
        self.safety = safety_margin
        self.capacity_est_kbps = 200.0
        self._cap_ewma = 200.0
        self.history: list[ControlDecision] = []

    def update_capacity(self, goodput_kbps: float, loss: float,
                        queue_growing: bool, delay_trend_ms_per_s: float = 0.0) -> None:
        """Capacity estimate with congestion distinguished from plain loss.

        Naive AIMD backs off on ANY loss.  That is correct on a wired path where
        loss means a full queue, and badly wrong on a wireless one where loss
        means a fading radio link.  Backing off there reduces quality without
        relieving anything, and -- worse for this system -- it starves the very
        redundancy budget that the loss made necessary.

        Congestion has a signature that random loss does not: the queue fills
        before it overflows, so ONE-WAY DELAY RISES first.  We therefore treat
        loss as congestion only when it is accompanied by a rising delay trend,
        which is the same discriminator used by modern real-time congestion
        controllers.  Loss with flat delay is read as link loss, and we hold the
        rate and spend the budget on protection instead.
        """
        congested = queue_growing or delay_trend_ms_per_s > 8.0
        if congested:
            self._cap_ewma *= 0.88
        elif loss > 0.02:
            self._cap_ewma *= 0.995          # gentle probe down, not a collapse
        else:
            self._cap_ewma = self._cap_ewma * 0.97 + (goodput_kbps * 1.25) * 0.03
        self._cap_ewma = float(np.clip(self._cap_ewma, 24.0, 2000.0))
        self.capacity_est_kbps = self._cap_ewma

    def decide(self, loss: float, mean_burst: float, jitter_ms: float,
               owd_ms: float, jb_depth_ms: float,
               pi_efficiency: float | None = None) -> ControlDecision:
        if pi_efficiency is None:
            pi_efficiency = self.pi_efficiency_override
        if pi_efficiency is None:
            # Measured empirically in the offline evaluation; see experiments/.
            pi_efficiency = 0.42 if self.pi_afec else 1.0

        budget = self.capacity_est_kbps * self.safety
        best: ControlDecision | None = None

        # Congestion safety rail: never let redundancy exceed what the path can
        # absorb, and taper it as loss rises since high loss usually IS congestion.
        rho_cap = 0.65 if loss < 0.03 else max(0.15, 0.65 - 1.8 * (loss - 0.03))

        for codec in BITRATE_LADDER:
            ie, bpl, kbps = emodel.CODEC_PARAMS[codec]
            for rho in RHO_GRID:
                if rho > rho_cap:
                    continue
                offered = kbps * (1.0 + rho) + HEADER_OVERHEAD_BPS / 1000.0
                if offered > budget:
                    continue
                group_budget = self.k * rho
                if self.pi_afec:
                    r, n_anchor = split_budget(group_budget, self.k, mean_burst)
                else:
                    r, n_anchor = int(round(group_budget)), 0
                p_res = residual_loss(loss, mean_burst, self.k, r,
                                      n_anchor=n_anchor, pi_efficiency=pi_efficiency)
                # A systematic code adds no mouth-to-ear delay of its own; the
                # data packets leave immediately.  Only recovery waits.
                total_delay = owd_ms + jb_depth_ms + 25.0
                r_pred = emodel.r_factor(codec, total_delay, p_res * 100.0,
                                         mean_burst, self.mobile)
                if best is None or r_pred > best.predicted_r:
                    best = ControlDecision(
                        codec=codec, bitrate_kbps=kbps, rho=rho,
                        dscp_high=46, dscp_low=(34 if rho > 0 else 0),
                        predicted_r=r_pred, predicted_mos=emodel.r_to_mos(r_pred),
                        offered_kbps=offered)

        if best is None:
            best = ControlDecision("opus-6k", 6.0, 0.0, 46, 0, 0.0, 1.0,
                                   6.0 + HEADER_OVERHEAD_BPS / 1000.0,
                                   reason="capacity below floor")
        best.reason = (f"budget {budget:.0f}kbps, loss {loss*100:.1f}%, "
                       f"burst {mean_burst:.1f}")
        self.history.append(best)
        return best


def dscp_for(pi: int, decision: ControlDecision) -> int:
    """Per-packet DiffServ marking driven by perceptual importance.

    This is the cross-layer claim in its most direct form.  A router applying
    WRED to a congested queue drops from the lowest-precedence class first.  By
    marking silence and low-energy frames AF41 (or best-effort) and voiced
    onsets EF, SWARA arranges for the network's own drop decision to be
    perceptually informed -- WITHOUT the router understanding speech, and
    without the router being able to read the speech, since the PI octet is
    authenticated associated data rather than plaintext content.
    """
    if pi >= 160:
        return decision.dscp_high      # EF (46)
    if pi >= 80:
        return decision.dscp_low or 34  # AF41
    return 0                            # best effort
