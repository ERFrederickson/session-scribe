"""Write mono 16-bit audio as a series of fixed-length WAV chunk files.

Chunk N always starts at exactly N * chunk_seconds of session time, and each
chunk is closed (and therefore safe on disk) as soon as it is full. If the app
or computer crashes mid-session, everything up to the current chunk survives.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


class ChunkedWavWriter:
    def __init__(self, directory: Path, prefix: str, sample_rate: int,
                 chunk_seconds: int = 600):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.sample_rate = int(sample_rate)
        self.chunk_samples = int(chunk_seconds * sample_rate)
        self.samples_written = 0
        self._wav: wave.Wave_write | None = None
        self._index = -1

    def chunk_path(self, index: int) -> Path:
        return self.directory / f"{self.prefix}_{index:03d}.wav"

    def _open(self, index: int) -> None:
        self._close()
        self._index = index
        w = wave.open(str(self.chunk_path(index)), "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(self.sample_rate)
        self._wav = w

    def _close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def write(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.int16)
        pos = 0
        while pos < len(samples):
            index = self.samples_written // self.chunk_samples
            if index != self._index or self._wav is None:
                self._open(index)
            room = self.chunk_samples - (self.samples_written % self.chunk_samples)
            part = samples[pos:pos + room]
            self._wav.writeframes(part.tobytes())
            pos += len(part)
            self.samples_written += len(part)
            if self.samples_written % self.chunk_samples == 0:
                self._close()

    def write_silence(self, n: int) -> None:
        block = self.sample_rate  # one second at a time keeps memory flat
        while n > 0:
            k = min(n, block)
            self.write(np.zeros(k, dtype=np.int16))
            n -= k

    def pad_to(self, seconds: float) -> None:
        target = int(seconds * self.sample_rate)
        if target > self.samples_written:
            self.write_silence(target - self.samples_written)

    def close(self) -> None:
        self._close()


def to_mono_int16(raw: bytes, channels: int) -> np.ndarray:
    data = np.frombuffer(raw, dtype=np.int16)
    if channels <= 1:
        return data.copy()
    usable = len(data) - (len(data) % channels)
    return data[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)
