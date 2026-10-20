"""
Synthetic speech corpus generator.

The evaluation needs speech-like audio with the right STRUCTURE -- alternating
talkspurts and pauses, voiced nuclei with formants, unvoiced fricatives, plosive
onsets -- because that structure is exactly what PI-AFEC exploits.  Shipping a
recorded corpus would make the project depend on licensed audio; generating one
keeps the whole system self-contained and reproducible from a seed.

The generator is a source-filter model, the same abstraction used by every
speech codec since LPC-10:

    source   glottal pulse train at F0 (voiced)  OR  white noise (unvoiced)
    filter   a cascade of resonators at the formant frequencies F1, F2, F3
    envelope per-phone amplitude contour with realistic attack and decay

Statistics are taken from the standard picture of conversational speech:
roughly 40% of a two-way call is silence for a given talker, voiced phones
average ~90 ms, unvoiced ~70 ms, and talkspurts run 1-3 s.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass

SAMPLE_RATE = 16_000

# (F1, F2, F3) in Hz for a set of vowels, plus fricative noise bands.
VOWELS = {
    "a": (730, 1090, 2440), "e": (530, 1840, 2480), "i": (270, 2290, 3010),
    "o": (570, 840, 2410),  "u": (300, 870, 2240),  "ae": (660, 1720, 2410),
}
FRICATIVES = {"s": (4000, 8000), "sh": (2500, 6000), "f": (1500, 7000), "th": (1200, 6500)}


@dataclass
class Phone:
    kind: str          # "voiced" | "unvoiced" | "silence" | "plosive"
    duration_ms: int
    identity: str = ""


def _resonator(x: np.ndarray, f: float, bw: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Two-pole resonator -- one formant."""
    r = np.exp(-np.pi * bw / sr)
    theta = 2 * np.pi * f / sr
    a1, a2 = -2 * r * np.cos(theta), r * r
    b0 = (1 - r) * np.sqrt(1 - 2 * r * np.cos(2 * theta) + r * r)
    y = np.zeros_like(x)
    y1 = y2 = 0.0
    for i in range(x.size):
        v = b0 * x[i] - a1 * y1 - a2 * y2
        y[i] = v
        y2, y1 = y1, v
    return y


class SpeechGenerator:
    def __init__(self, seed: int = 7, sample_rate: int = SAMPLE_RATE):
        self.rng = np.random.default_rng(seed)
        self.sr = sample_rate
        self.f0 = self.rng.uniform(95, 190)     # speaker's mean pitch

    def _glottal(self, n: int, f0: float) -> np.ndarray:
        """Pulse train with jitter and shimmer, low-passed to a glottal shape."""
        t = np.arange(n) / self.sr
        f0_track = f0 * (1.0 + 0.06 * np.sin(2 * np.pi * 1.7 * t)
                         + 0.012 * self.rng.standard_normal(n).cumsum() / max(1, np.sqrt(n)))
        phase = 2 * np.pi * np.cumsum(f0_track) / self.sr
        pulse = np.maximum(0.0, np.sin(phase)) ** 2.4
        pulse -= pulse.mean()
        return pulse

    def _phone_signal(self, ph: Phone) -> np.ndarray:
        n = int(self.sr * ph.duration_ms / 1000)
        if n <= 0:
            return np.zeros(0)
        if ph.kind == "silence":
            return self.rng.normal(0, 0.0016, n)          # room noise floor
        if ph.kind == "plosive":
            burst = np.zeros(n)
            bl = min(n, int(0.008 * self.sr))
            burst[:bl] = self.rng.normal(0, 1.0, bl) * np.exp(-np.linspace(0, 5, bl))
            return burst * 0.55
        if ph.kind == "unvoiced":
            lo, hi = FRICATIVES[ph.identity]
            x = self.rng.normal(0, 1.0, n)
            X = np.fft.rfft(x)
            f = np.fft.rfftfreq(n, 1 / self.sr)
            X *= ((f > lo) & (f < hi)).astype(float)
            y = np.fft.irfft(X, n)
            env = np.minimum(1.0, np.linspace(0, 3, n)) * np.minimum(
                1.0, np.linspace(3, 0, n))
            return 0.22 * y / (np.abs(y).max() + 1e-9) * env
        # voiced
        f1, f2, f3 = VOWELS[ph.identity]
        src = self._glottal(n, self.f0 * self.rng.uniform(0.92, 1.08))
        y = (_resonator(src, f1, 80) + 0.55 * _resonator(src, f2, 110)
             + 0.28 * _resonator(src, f3, 160))
        attack = int(0.018 * self.sr)
        env = np.ones(n)
        if n > 2 * attack and attack > 0:
            env[:attack] = np.linspace(0, 1, attack) ** 0.6
            env[-attack:] = np.linspace(1, 0, attack) ** 0.8
        y = y / (np.abs(y).max() + 1e-9)
        return 0.85 * y * env

    def utterance(self, duration_s: float = 20.0) -> tuple[np.ndarray, list[Phone]]:
        """Generate a conversational-turn signal and its phone-level ground truth."""
        phones: list[Phone] = []
        total = 0.0
        target = duration_s * 1000
        while total < target:
            # A talkspurt: a run of syllables.
            spurt = self.rng.uniform(900, 2800)
            spent = 0.0
            while spent < spurt and total + spent < target:
                roll = self.rng.random()
                if roll < 0.13:
                    p = Phone("plosive", int(self.rng.uniform(25, 55)))
                elif roll < 0.38:
                    p = Phone("unvoiced", int(self.rng.uniform(55, 110)),
                              self.rng.choice(list(FRICATIVES)))
                else:
                    p = Phone("voiced", int(self.rng.uniform(70, 170)),
                              self.rng.choice(list(VOWELS)))
                phones.append(p)
                spent += p.duration_ms
            total += spent
            pause = self.rng.uniform(250, 1100)
            phones.append(Phone("silence", int(pause)))
            total += pause

        sig = np.concatenate([self._phone_signal(p) for p in phones])
        # Gentle level normalisation and a touch of channel noise.
        sig = sig / (np.abs(sig).max() + 1e-9) * 0.7
        sig += self.rng.normal(0, 0.0009, sig.size)
        return sig.astype(np.float64), phones

    def frames(self, duration_s: float = 20.0, frame_ms: int = 20):
        """Yield (frame_index, samples, ground_truth_kind)."""
        sig, phones = self.utterance(duration_s)
        n = self.sr * frame_ms // 1000
        # Build a per-sample ground-truth label track.
        labels = []
        for p in phones:
            labels.extend([p.kind] * int(self.sr * p.duration_ms / 1000))
        labels = labels[: sig.size] + ["silence"] * max(0, sig.size - len(labels))
        out = []
        for i in range(sig.size // n):
            seg = sig[i * n:(i + 1) * n]
            seg_lab = labels[i * n:(i + 1) * n]
            kind = max(set(seg_lab), key=seg_lab.count) if seg_lab else "silence"
            out.append((i, seg, kind))
        return out, sig
