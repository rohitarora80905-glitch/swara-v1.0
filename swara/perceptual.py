"""
Perceptual Importance (PI) analysis -- SWARA's core novel contribution.

Conventional VoIP treats every 20 ms packet as equally valuable.  That is
demonstrably false.  Human speech intelligibility is dominated by a minority of
frames:

  * Voiced vowel nuclei carry the formant structure that identifies the phoneme.
  * Plosive / vowel ONSETS carry the transient cues that mark word boundaries.
  * Unvoiced fricatives (/s/, /f/) are noise-like: the ear tolerates substitution.
  * Silence and background noise frames are perceptually free to lose.

The PI scorer converts each 20 ms frame into a single octet, 0..255, estimating
"how much damage does losing THIS packet do to what the listener understands".
That octet is then carried in the clear (but authenticated) in the SWARA header,
where the FEC scheduler, the DiffServ marker, the relay and the receiver's
concealment engine can all act on it.

Features, all computed in O(N) or O(N log N) on a 320-sample (20 ms @ 16 kHz) frame:

  E     frame energy in dB, normalised against an adaptive noise floor
  V     voicing probability from normalised autocorrelation peak (pitch strength)
  ZCR   zero-crossing rate, the classic voiced/unvoiced discriminator
  F     spectral flux against the previous frame -- an onset detector
  VAD   speech activity gate derived from the adaptive noise floor

    PI_raw = w_E*E' + w_V*V + w_F*F' + w_O*onset_boost
    PI     = 255 * VAD * clip(PI_raw, 0, 1)

Design targets (verified in `analyse_corpus`):
    silence / noise                 ->    0 -  25
    steady unvoiced fricative       ->   50 - 110
    steady voiced vowel             ->  150 - 210
    vowel or plosive ONSET          ->  210 - 255
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000   # 320 samples

# Feature weights.  Tuned so that the four target bands above are hit; the sum
# of the positive weights is 1.0 so PI_raw is naturally in [0, 1].
W_ENERGY = 0.30
W_VOICING = 0.34
W_FLUX = 0.20
W_ONSET = 0.16

# The raw normalised autocorrelation peak of a genuinely voiced frame sits
# around 0.5, not 1.0, because the analysis window contains only a few pitch
# periods.  Rescaling against the measured operating range is what turns the
# feature into a usable voiced/unvoiced discriminator.
VOICING_FLOOR = 0.12
VOICING_SPAN = 0.45

# A frame that begins a talkspurt carries the word-boundary cue.  Losing it
# costs far more than losing a frame in the middle of a steady vowel, so a hard
# onset earns a multiplicative boost on top of the additive features.
HARD_ONSET_BOOST = 1.45
SPEECH_FLOOR = 0.12   # any speech frame is worth at least this much


@dataclass
class FrameFeatures:
    energy_db: float
    energy_norm: float
    voicing: float
    zcr: float
    flux: float
    onset: float
    is_speech: bool
    pi: int

    def label(self) -> str:
        if not self.is_speech:
            return "silence"
        if self.onset > 0.5:
            return "onset"
        if self.voicing > 0.5:
            return "voiced"
        return "unvoiced"


class PerceptualImportanceAnalyzer:
    """Streaming, causal, single-frame-latency PI scorer.

    Causality matters: a real encoder cannot look ahead without adding delay, so
    every feature here uses only the current frame and state accumulated from
    past frames.  The onset detector is therefore a *rising-edge* detector, not
    a lookahead peak-picker.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, frame_len: int = FRAME_LEN):
        self.sr = sample_rate
        self.n = frame_len
        self._prev_spec: np.ndarray | None = None
        self._prev_energy_norm = 0.0
        self._prev_speech = False
        self._noise_floor_db = -60.0   # adaptive, tracks the quietest recent frames
        self._peak_db = -20.0          # adaptive, tracks the loudest recent frames
        self._window = np.hanning(frame_len)
        self._min_lag = max(2, int(sample_rate / 400))   # 400 Hz -> highest pitch
        self._max_lag = min(frame_len - 1, int(sample_rate / 60))  # 60 Hz -> lowest
        self.frames_seen = 0

    # -- individual features ------------------------------------------------

    def _energy_db(self, x: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)
        return 20.0 * np.log10(rms)

    def _voicing(self, x: np.ndarray) -> float:
        """Normalised autocorrelation peak in the plausible pitch range.

        Returns ~1.0 for a strongly periodic (voiced) frame, ~0.0 for noise.
        """
        x = x - x.mean()
        e0 = float(np.dot(x, x))
        if e0 < 1e-9:
            return 0.0
        # FFT-based autocorrelation: O(N log N)
        nfft = 1 << (2 * self.n - 1).bit_length()
        spec = np.fft.rfft(x, nfft)
        ac = np.fft.irfft(spec * np.conj(spec), nfft)[: self.n]
        lo, hi = self._min_lag, self._max_lag
        if hi <= lo:
            return 0.0
        seg = ac[lo:hi]
        if seg.size == 0:
            return 0.0
        return float(np.clip(seg.max() / e0, 0.0, 1.0))

    def _zcr(self, x: np.ndarray) -> float:
        return float(np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8)))))

    def _flux(self, x: np.ndarray) -> float:
        """Half-wave-rectified spectral flux: energy that has just APPEARED."""
        spec = np.abs(np.fft.rfft(x * self._window))
        spec = spec / (spec.sum() + 1e-9)
        if self._prev_spec is None:
            self._prev_spec = spec
            return 0.0
        d = spec - self._prev_spec
        self._prev_spec = spec
        return float(np.clip(np.sum(d[d > 0]) * 2.0, 0.0, 1.0))

    # -- main entry point ---------------------------------------------------

    def analyze(self, frame: np.ndarray) -> FrameFeatures:
        x = np.asarray(frame, dtype=np.float64)
        if x.size < self.n:
            x = np.pad(x, (0, self.n - x.size))
        x = x[: self.n]

        e_db = self._energy_db(x)

        # Adaptive noise floor: fast to rise into silence, slow to fall.
        if e_db < self._noise_floor_db:
            self._noise_floor_db += 0.35 * (e_db - self._noise_floor_db)
        else:
            self._noise_floor_db += 0.004 * (e_db - self._noise_floor_db)
        # Adaptive peak: fast attack, slow release.
        if e_db > self._peak_db:
            self._peak_db += 0.5 * (e_db - self._peak_db)
        else:
            self._peak_db += 0.002 * (e_db - self._peak_db)

        span = max(12.0, self._peak_db - self._noise_floor_db)
        e_norm = float(np.clip((e_db - self._noise_floor_db) / span, 0.0, 1.0))

        zcr = self._zcr(x)
        flux = self._flux(x)

        # A plosive burst is only ~8 ms of a 20 ms frame, so its average energy
        # is low even though it is perceptually a transient.  Admitting frames
        # on high spectral flux as well as on energy catches them -- but the
        # flux route must still clear an absolute energy bar, or the random
        # spectral wander of the noise floor is mistaken for speech.
        above_floor = e_db - self._noise_floor_db
        is_speech = above_floor > 6.0 or (flux > 0.62 and above_floor > 4.0)

        voicing_raw = self._voicing(x) if is_speech else 0.0
        voicing = float(np.clip((voicing_raw - VOICING_FLOOR) / VOICING_SPAN, 0.0, 1.0))

        # Onset = a sharp rise in normalised energy relative to the previous frame.
        rise = e_norm - self._prev_energy_norm
        onset = float(np.clip(rise * 3.2, 0.0, 1.0)) if is_speech else 0.0
        hard_onset = is_speech and not self._prev_speech
        if hard_onset:
            onset = 1.0
        self._prev_energy_norm = e_norm
        self._prev_speech = is_speech

        # High ZCR with low voicing means fricative noise: still needed for
        # intelligibility, but far more concealable than a vowel, so we damp it.
        voicing_eff = voicing * 0.6 if (is_speech and voicing < 0.4 and zcr > 0.20) else voicing

        raw = (W_ENERGY * e_norm + W_VOICING * voicing_eff
               + W_FLUX * flux + W_ONSET * onset)

        if is_speech:
            scaled = SPEECH_FLOOR + (1.0 - SPEECH_FLOOR) * float(np.clip(raw, 0.0, 1.0))
            if hard_onset:
                scaled *= HARD_ONSET_BOOST
            pi = int(round(255.0 * min(scaled, 1.0)))
        else:
            pi = int(round(255.0 * 0.06 * e_norm))

        self.frames_seen += 1
        return FrameFeatures(
            energy_db=e_db, energy_norm=e_norm, voicing=voicing, zcr=zcr,
            flux=flux, onset=onset, is_speech=is_speech, pi=int(np.clip(pi, 0, 255)),
        )


# ---------------------------------------------------------------------------
# Perceptual damage model.
#
# We need a way to say "this frame was lost -- how much did the listener
# suffer?"  We use a damage weight derived from PI, calibrated so that losing a
# silence frame costs ~0 and losing a voiced onset costs ~1.  This is what makes
# unequal protection measurable rather than merely plausible, and it is what
# feeds the effective-loss term of the E-model in emodel.py.
# ---------------------------------------------------------------------------

def damage_weight(pi: int, concealed_neighbours: int = 0) -> float:
    """Perceptual cost of losing a frame with the given PI score.

    The curve is superlinear (exponent 1.6): a frame twice as important is
    roughly three times as costly to lose.  Consecutive losses are penalised
    extra because packet-loss concealment degrades sharply once it has to
    invent more than ~40 ms of speech from nothing.
    """
    base = (pi / 255.0) ** 1.6
    burst_penalty = 1.0 + 0.45 * min(concealed_neighbours, 4)
    return float(base * burst_penalty)
