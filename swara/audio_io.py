"""
Audio capture and playback.

Three backends, selected automatically so the same application runs on a laptop
with a microphone AND inside a headless test environment with no sound card:

    live       real microphone / speaker via `sounddevice` (if installed)
    wav        read from / write to a WAV file
    synthetic  the source-filter speech generator, for automated testing

Everything downstream sees the identical interface -- 20 ms frames of int16 at
16 kHz -- so no other module knows or cares which backend is in use.
"""

from __future__ import annotations

import queue
import threading
import wave
import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000     # 320 samples

try:
    import sounddevice as _sd
    HAVE_SOUNDDEVICE = True
except Exception:
    _sd = None
    HAVE_SOUNDDEVICE = False


def backends_available() -> dict:
    return {"live": HAVE_SOUNDDEVICE, "wav": True, "synthetic": True}


class AudioSource:
    """Produces successive 20 ms frames of int16 mono at 16 kHz."""

    def __init__(self, backend: str = "auto", wav_path: str | None = None,
                 duration_s: float = 30.0, seed: int = 7, gain: float = 1.0):
        if backend == "auto":
            backend = "live" if HAVE_SOUNDDEVICE else "synthetic"
        self.backend = backend
        self.gain = gain
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._stream = None
        self._buf = np.zeros(0, dtype=np.int16)
        self._pos = 0
        self._closed = False

        if backend == "wav":
            if not wav_path:
                raise ValueError("wav backend needs wav_path")
            self._buf = self._read_wav(wav_path)
        elif backend == "synthetic":
            from .speech import SpeechGenerator
            frames, sig = SpeechGenerator(seed=seed).frames(duration_s, FRAME_MS)
            self._buf = np.clip(sig * 26000.0, -32768, 32767).astype(np.int16)
        elif backend == "live":
            if not HAVE_SOUNDDEVICE:
                raise RuntimeError("sounddevice not installed")
            self._stream = _sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=FRAME_LEN, callback=self._cb)
            self._stream.start()
        else:
            raise ValueError(f"unknown backend {backend!r}")

    @staticmethod
    def _read_wav(path: str) -> np.ndarray:
        with wave.open(path, "rb") as w:
            n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(n)
        if sw != 2:
            raise ValueError("only 16-bit PCM WAV supported")
        x = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if sr != SAMPLE_RATE:                       # crude but adequate resample
            idx = np.linspace(0, x.size - 1, int(x.size * SAMPLE_RATE / sr))
            x = np.interp(idx, np.arange(x.size), x.astype(np.float64)).astype(np.int16)
        return x

    def _cb(self, indata, frames, time_info, status):
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """Next 20 ms frame, or None when the source is exhausted."""
        if self._closed:
            return None
        if self.backend == "live":
            try:
                f = self._q.get(timeout=timeout)
            except queue.Empty:
                return np.zeros(FRAME_LEN, dtype=np.int16)
            return self._apply_gain(f)
        if self._pos + FRAME_LEN > self._buf.size:
            return None
        f = self._buf[self._pos:self._pos + FRAME_LEN]
        self._pos += FRAME_LEN
        return self._apply_gain(f)

    def _apply_gain(self, f: np.ndarray) -> np.ndarray:
        if self.gain == 1.0:
            return f
        return np.clip(f.astype(np.float64) * self.gain, -32768, 32767).astype(np.int16)

    @property
    def total_frames(self) -> int:
        return self._buf.size // FRAME_LEN if self.backend != "live" else -1

    def close(self):
        self._closed = True
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass


class AudioSink:
    """Consumes successive 20 ms frames of int16 mono at 16 kHz."""

    def __init__(self, backend: str = "auto", wav_path: str | None = None):
        if backend == "auto":
            backend = "live" if HAVE_SOUNDDEVICE else "null"
        self.backend = backend
        self._frames: list[np.ndarray] = []
        self._wav_path = wav_path
        self._stream = None
        self._q: queue.Queue = queue.Queue(maxsize=100)

        if backend == "live":
            if not HAVE_SOUNDDEVICE:
                raise RuntimeError("sounddevice not installed")
            self._stream = _sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=FRAME_LEN, callback=self._cb)
            self._stream.start()

    def _cb(self, outdata, frames, time_info, status):
        try:
            f = self._q.get_nowait()
        except queue.Empty:
            f = np.zeros(FRAME_LEN, dtype=np.int16)
        if f.size < frames:
            f = np.pad(f, (0, frames - f.size))
        outdata[:, 0] = f[:frames]

    def write(self, frame: np.ndarray) -> None:
        if self.backend == "live":
            try:
                self._q.put_nowait(np.asarray(frame, dtype=np.int16))
            except queue.Full:
                pass
        else:
            self._frames.append(np.asarray(frame, dtype=np.int16))

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
        if self._wav_path and self._frames:
            data = np.concatenate(self._frames)
            with wave.open(self._wav_path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
                w.writeframes(data.tobytes())

    @property
    def collected(self) -> np.ndarray:
        return np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.int16)
