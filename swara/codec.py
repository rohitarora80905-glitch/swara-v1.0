"""
SWARA speech codec -- IMA ADPCM, with one deliberate modification.

This is a REAL codec: it takes 16-bit PCM samples in and produces compressed
bytes out, and the bytes decode back to audible speech.  It is not a bitrate
model.

IMA ADPCM encodes the DIFFERENCE between the current sample and a prediction,
using a quantiser whose step size adapts to the signal.  Four bits per sample
gives 4:1 compression against 16-bit PCM.

The modification, and it matters: standard ADPCM carries its predictor and step
index continuously from one block to the next.  Over a lossy transport that is
fatal -- lose one packet and the decoder's predictor diverges from the
encoder's, so every subsequent frame decodes to garbage until something
resynchronises.  A single lost packet would corrupt the rest of the call.

So SWARA RESETS the codec state at every frame boundary and transmits the
starting predictor and step index in a 3-byte preamble.  Every frame is then
independently decodable.  The cost is a small loss of coding efficiency at
frame boundaries; the benefit is that packet loss damages exactly the frames
that were lost and nothing else.

This is the same reasoning that makes real codecs limit inter-frame prediction
on lossy channels, and it is worth stating in the viva: a codec designed for a
file is the wrong codec for a network.

Two modes are provided so the NAVIK controller has something real to switch
between:

    wideband   16 kHz, 4 bits/sample   ->  320 samples -> 163 B/frame ~ 65 kbit/s
    narrowband  8 kHz, 4 bits/sample   ->  160 samples ->  83 B/frame ~ 33 kbit/s
"""

from __future__ import annotations

import numpy as np
import struct

# IMA ADPCM standard tables.
_STEP_TABLE = ([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767])

_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]

SAMPLE_RATE_WB = 16_000
SAMPLE_RATE_NB = 8_000
FRAME_MS = 20


class Mode:
    WIDEBAND = 0     # 16 kHz
    NARROWBAND = 1   # 8 kHz

    RATE = {WIDEBAND: SAMPLE_RATE_WB, NARROWBAND: SAMPLE_RATE_NB}
    NAME = {WIDEBAND: "wideband-16k", NARROWBAND: "narrowband-8k"}


def frame_samples(mode: int) -> int:
    return Mode.RATE[mode] * FRAME_MS // 1000


def nominal_kbps(mode: int) -> float:
    n = frame_samples(mode)
    payload = 3 + (n + 1) // 2
    return payload * 8 * (1000 / FRAME_MS) / 1000.0


def _decimate2(x: np.ndarray) -> np.ndarray:
    """16 kHz -> 8 kHz with a short anti-alias FIR (no scipy dependency)."""
    h = np.array([0.0155, 0.0605, 0.1664, 0.2470, 0.2470, 0.1664, 0.0605, 0.0155])
    y = np.convolve(x, h, mode="same")
    return y[::2]


def _interp2(x: np.ndarray) -> np.ndarray:
    """8 kHz -> 16 kHz by zero-stuffing plus the same smoothing filter."""
    y = np.zeros(x.size * 2, dtype=np.float64)
    y[::2] = x
    h = np.array([0.0310, 0.1210, 0.3328, 0.4940, 0.4940, 0.3328, 0.1210, 0.0310])
    return np.convolve(y, h, mode="same")


class AdpcmCodec:
    """Stateless across frames by design -- see the module docstring."""

    def __init__(self, mode: int = Mode.WIDEBAND):
        self.mode = mode

    # -- encode ---------------------------------------------------------
    def encode(self, pcm16: np.ndarray) -> bytes:
        """pcm16: int16 samples at 16 kHz.  Returns the coded frame."""
        x = np.asarray(pcm16, dtype=np.float64)
        if self.mode == Mode.NARROWBAND:
            x = _decimate2(x)
        x = np.clip(np.round(x), -32768, 32767).astype(np.int32)

        pred = int(x[0]) if x.size else 0
        index = 24                       # a mid-range starting step
        out = bytearray()
        out += struct.pack("<hB", int(np.clip(pred, -32768, 32767)), index)

        nibbles = []
        step_tab, idx_tab = _STEP_TABLE, _INDEX_TABLE
        for s in x.tolist():
            step = step_tab[index]
            diff = s - pred
            code = 0
            if diff < 0:
                code = 8
                diff = -diff
            delta = step >> 3
            if diff >= step:
                code |= 4; diff -= step; delta += step
            if diff >= (step >> 1):
                code |= 2; diff -= step >> 1; delta += step >> 1
            if diff >= (step >> 2):
                code |= 1; delta += step >> 2
            pred = pred - delta if (code & 8) else pred + delta
            if pred > 32767: pred = 32767
            elif pred < -32768: pred = -32768
            index += idx_tab[code]
            if index > 88: index = 88
            elif index < 0: index = 0
            nibbles.append(code)

        for i in range(0, len(nibbles), 2):
            lo = nibbles[i]
            hi = nibbles[i + 1] if i + 1 < len(nibbles) else 0
            out.append((hi << 4) | lo)
        return bytes(out)

    # -- decode ---------------------------------------------------------
    def decode(self, data: bytes, out_rate: int = SAMPLE_RATE_WB) -> np.ndarray:
        """Returns int16 samples at `out_rate`."""
        if len(data) < 3:
            return np.zeros(frame_samples(Mode.WIDEBAND), dtype=np.int16)
        pred, index = struct.unpack("<hB", data[:3])
        pred = int(pred)
        index = int(np.clip(index, 0, 88))
        body = data[3:]

        n = frame_samples(self.mode)
        step_tab, idx_tab = _STEP_TABLE, _INDEX_TABLE
        out_s = []
        append = out_s.append
        for byte in body:
            for code in (byte & 0x0F, (byte >> 4) & 0x0F):
                if len(out_s) >= n:
                    break
                step = step_tab[index]
                delta = step >> 3
                if code & 4: delta += step
                if code & 2: delta += step >> 1
                if code & 1: delta += step >> 2
                pred = pred - delta if (code & 8) else pred + delta
                if pred > 32767: pred = 32767
                elif pred < -32768: pred = -32768
                index += idx_tab[code]
                if index > 88: index = 88
                elif index < 0: index = 0
                append(pred)
        while len(out_s) < n:
            append(0)
        y = np.asarray(out_s, dtype=np.float64)
        if self.mode == Mode.NARROWBAND and out_rate == SAMPLE_RATE_WB:
            y = _interp2(y)
        return np.clip(y, -32768, 32767).astype(np.int16)

    @property
    def payload_bytes(self) -> int:
        n = frame_samples(self.mode)
        return 3 + (n + 1) // 2


def conceal(previous: np.ndarray | None, n: int, run: int) -> np.ndarray:
    """Packet loss concealment.

    Repeats the previous decoded frame with progressive attenuation.  This is
    the simplest concealment that is honest about what it is doing: it keeps
    the sound continuous for a frame or two, then fades to silence rather than
    producing a buzzing artefact by repeating indefinitely.
    """
    if previous is None or previous.size == 0:
        return np.zeros(n, dtype=np.int16)
    gain = max(0.0, 1.0 - 0.35 * run)
    src = previous[-n:] if previous.size >= n else np.pad(previous, (0, n - previous.size))
    # Taper the join to avoid a click at the splice point.
    ramp = np.ones(n)
    edge = max(1, n // 8)
    ramp[:edge] = np.linspace(0.6, 1.0, edge)
    ramp[-edge:] = np.linspace(1.0, 0.7, edge)
    return np.clip(src.astype(np.float64) * gain * ramp, -32768, 32767).astype(np.int16)
