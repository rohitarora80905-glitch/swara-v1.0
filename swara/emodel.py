"""
ITU-T G.107 E-model -- the objective quality metric SWARA optimises directly.

Almost every adaptive VoIP system optimises a PROXY: minimise loss, or minimise
delay, or maximise throughput.  Those proxies conflict.  Adding jitter buffer
depth reduces late-packet loss but adds mouth-to-ear delay; adding FEC reduces
residual loss but consumes bandwidth that would otherwise raise codec quality.
There is no way to trade them off without a common currency.

The E-model provides that currency.  It maps impairments onto a single
transmission rating R in [0, 100], from which a Mean Opinion Score follows:

    R = R0 - Is - Id - Ie_eff + A

    R0      base signal-to-noise rating (93.2 for a modern network)
    Is      simultaneous impairments (loudness, quantisation) -- constant here
    Id      DELAY impairment, from mouth-to-ear delay
    Ie_eff  EQUIPMENT impairment: codec distortion plus packet loss, with the
            crucial burstiness correction from G.107 Annex E
    A       advantage factor (0 for wired, 10 for mobile convenience)

The key term is:

    Ie_eff = Ie + (95 - Ie) * Ppl / (Ppl / BurstR + Bpl)

Ppl is the residual loss AFTER concealment, Bpl is the codec's packet-loss
robustness, and BurstR is the burst ratio -- the observed mean burst length
divided by the mean burst length that random loss would produce.  BurstR > 1
means loss is clustered, and the same average loss then hurts much more.

SWARA's contribution at this layer: we feed Ie_eff a PERCEPTUALLY WEIGHTED
residual loss rather than a raw packet count.  Losing a packet of silence is
not a 1.0 loss event; losing a vowel onset is worse than 1.0.  Section
`perceptual_ppl` implements that mapping, and it is what allows the controller
to see the benefit of unequal protection at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Codec parameters (ITU-T G.113 Appendix I and published Opus measurements).
CODEC_PARAMS = {
    #                Ie   Bpl   nominal kbps
    "G.711":        (0.0, 4.3, 64.0),
    "G.729":        (11.0, 19.0, 8.0),
    "opus-6k":      (23.0, 22.0, 6.0),
    "opus-8k":      (18.0, 22.0, 8.0),
    "opus-12k":     (11.0, 24.0, 12.0),
    "opus-16k":     (7.0, 25.0, 16.0),
    "opus-20k":     (5.0, 26.0, 20.0),
    "opus-24k":     (3.5, 27.0, 24.0),
    "opus-32k":     (2.0, 28.0, 32.0),
}

R0_DEFAULT = 93.2
IS_DEFAULT = 1.4
A_WIRED = 0.0
A_MOBILE = 10.0


def delay_impairment(d_ms: float) -> float:
    """Id: the penalty for mouth-to-ear delay (G.107 simplified form).

    Below ~150 ms conversation feels natural; the term has a knee at 177.3 ms
    after which it rises steeply, which is why an over-deep jitter buffer is a
    quality bug and not merely a latency inconvenience.
    """
    d = max(0.0, d_ms)
    idd = 0.024 * d
    if d > 177.3:
        idd += 0.11 * (d - 177.3)
    return idd


def burst_ratio(mean_burst_len: float, loss_rate: float) -> float:
    """BurstR = observed mean burst length / burst length under random loss.

    Under Bernoulli (random) loss the mean burst length is 1/(1-p), so BurstR
    is 1.0 for random loss and grows as loss becomes clustered.
    """
    p = min(max(loss_rate, 1e-6), 0.95)
    random_burst = 1.0 / (1.0 - p)
    return max(1.0, mean_burst_len / random_burst)


def equipment_impairment(codec: str, ppl_percent: float, burst_r: float = 1.0) -> float:
    """Ie_eff -- codec impairment amplified by residual packet loss."""
    ie, bpl, _ = CODEC_PARAMS[codec]
    ppl = max(0.0, ppl_percent)
    if ppl <= 0:
        return ie
    return ie + (95.0 - ie) * ppl / (ppl / burst_r + bpl)


def r_factor(codec: str, delay_ms: float, ppl_percent: float,
             mean_burst_len: float = 1.0, mobile: bool = False,
             r0: float = R0_DEFAULT) -> float:
    br = burst_ratio(mean_burst_len, ppl_percent / 100.0)
    r = (r0 - IS_DEFAULT - delay_impairment(delay_ms)
         - equipment_impairment(codec, ppl_percent, br)
         + (A_MOBILE if mobile else A_WIRED))
    return float(max(0.0, min(100.0, r)))


def r_to_mos(r: float) -> float:
    """G.107 conversion from transmission rating to Mean Opinion Score."""
    if r <= 0:
        return 1.0
    if r >= 100:
        return 4.5
    mos = 1.0 + 0.035 * r + r * (r - 60.0) * (100.0 - r) * 7.0e-6
    return float(max(1.0, min(4.5, mos)))


def mos(codec: str, delay_ms: float, ppl_percent: float,
        mean_burst_len: float = 1.0, mobile: bool = False) -> float:
    return r_to_mos(r_factor(codec, delay_ms, ppl_percent, mean_burst_len, mobile))


def quality_label(mos_value: float) -> str:
    if mos_value >= 4.3:
        return "Excellent"
    if mos_value >= 4.0:
        return "Good"
    if mos_value >= 3.6:
        return "Fair"
    if mos_value >= 3.1:
        return "Poor"
    return "Bad"


def perceptual_ppl(lost_damage_sum: float, total_frames: int) -> float:
    """Convert accumulated perceptual damage into an equivalent loss percentage.

    A raw loss counter says "3 of 100 packets were lost -> Ppl = 3%".  That is
    only correct if all packets matter equally.  Here each lost frame
    contributes its damage weight (see perceptual.damage_weight), so a run of
    lost silence contributes almost nothing and a lost vowel onset contributes
    more than one packet's worth.  The result is a loss figure the E-model can
    consume that actually reflects what the listener experienced.
    """
    if total_frames <= 0:
        return 0.0
    return 100.0 * lost_damage_sum / total_frames


@dataclass
class QualityReport:
    codec: str
    delay_ms: float
    raw_loss_pct: float
    perceptual_loss_pct: float
    mean_burst: float
    r: float
    mos: float

    def __str__(self) -> str:
        return (f"{self.codec}: MOS {self.mos:.2f} ({quality_label(self.mos)})  "
                f"R={self.r:.1f}  delay={self.delay_ms:.0f}ms  "
                f"loss raw {self.raw_loss_pct:.1f}% / perceptual "
                f"{self.perceptual_loss_pct:.1f}%  burst={self.mean_burst:.2f}")


def assess(codec: str, delay_ms: float, raw_loss_pct: float,
           perceptual_loss_pct: float, mean_burst: float = 1.0,
           mobile: bool = False) -> QualityReport:
    r = r_factor(codec, delay_ms, perceptual_loss_pct, mean_burst, mobile)
    return QualityReport(codec, delay_ms, raw_loss_pct, perceptual_loss_pct,
                         mean_burst, r, r_to_mos(r))
