"""Record what Discord plays through your speakers/headphones, plus your mic.

Uses WASAPI loopback (via PyAudioWPatch) so no bot is needed. Two tracks are
written into the session folder:

  audio/discord_000.wav, discord_001.wav, ...   everything you hear
  audio/mic_000.wav, ...                        your own microphone

Windows loopback delivers nothing while the output is silent, so the writer
pads with silence to keep each track lined up with the session clock.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

import numpy as np

from .clock import SessionClock
from .wavchunks import ChunkedWavWriter, to_mono_int16

log = logging.getLogger(__name__)

CHUNK_SECONDS = 600
FRAMES_PER_BUFFER = 1024
# How far behind the clock a track may fall before we fill the gap with silence.
GAP_TOLERANCE = 0.5


def _pyaudio():
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
    except ImportError as e:  # pragma: no cover - Windows only
        raise RuntimeError(
            "PyAudioWPatch is not installed. Run setup again (Windows only)."
        ) from e
    return pyaudio


def find_devices(pa) -> dict:
    """Return {'loopback': info, 'mic': info} for the default Windows devices."""
    pyaudio = _pyaudio()
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    loopback = pa.get_default_wasapi_loopback()
    mic_index = wasapi.get("defaultInputDevice", -1)
    mic = pa.get_device_info_by_index(mic_index) if mic_index is not None and mic_index >= 0 else None
    return {"loopback": loopback, "mic": mic}


class Track:
    def __init__(self, name: str, device: dict, clock: SessionClock, out_dir: Path):
        self.name = name
        self.device = device
        self.clock = clock
        self.channels = max(1, int(device["maxInputChannels"]))
        self.rate = int(device["defaultSampleRate"])
        self.writer = ChunkedWavWriter(out_dir, name, self.rate, CHUNK_SECONDS)
        self.queue: queue.Queue = queue.Queue()
        self.level = 0.0
        self.stream = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"track-{name}", daemon=True)
        self.error: str | None = None

    # Runs on the PortAudio thread: keep it tiny.
    def callback(self, in_data, frame_count, time_info, status):
        if self.clock.running:
            self.queue.put((in_data, self.clock.elapsed()))
        return (None, _pyaudio().paContinue)

    def open(self, pa) -> None:
        pyaudio = _pyaudio()
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self.callback,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    raw, at = self.queue.get(timeout=1.0)
                except queue.Empty:
                    # Nothing arriving (silence, or paused): keep the file in step.
                    target = self.clock.elapsed()
                    if self.clock.running:
                        target -= GAP_TOLERANCE
                    self.writer.pad_to(target)
                    self.level = 0.0
                    continue
                samples = to_mono_int16(raw, self.channels)
                start_expected = at - len(samples) / self.rate
                if start_expected - self.writer.samples_written / self.rate > GAP_TOLERANCE:
                    self.writer.pad_to(start_expected)
                self.writer.write(samples)
                if len(samples):
                    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                    self.level = min(1.0, rms / 8000.0)
        except Exception as e:  # pragma: no cover - surfaced in the UI
            log.exception("Audio writer for %s failed", self.name)
            self.error = str(e)

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=30)
        self.writer.pad_to(self.clock.elapsed())
        self.writer.close()

    def describe(self) -> dict:
        return {
            "name": self.name,
            "device": self.device.get("name"),
            "sample_rate": self.rate,
            "chunk_seconds": CHUNK_SECONDS,
        }


class Recorder:
    def __init__(self, clock: SessionClock):
        self.clock = clock
        self.tracks: list[Track] = []
        self._pa = None

    def start(self, audio_dir: Path, record_mic: bool = True) -> list[dict]:
        pyaudio = _pyaudio()
        self._pa = pyaudio.PyAudio()
        devices = find_devices(self._pa)
        self.tracks = [Track("discord", devices["loopback"], self.clock, audio_dir)]
        if record_mic and devices["mic"] is not None:
            self.tracks.append(Track("mic", devices["mic"], self.clock, audio_dir))
        for t in self.tracks:
            t.open(self._pa)
        return [t.describe() for t in self.tracks]

    def levels(self) -> dict:
        return {t.name: round(t.level, 3) for t in self.tracks}

    def errors(self) -> list[str]:
        return [f"{t.name}: {t.error}" for t in self.tracks if t.error]

    def stop(self) -> None:
        for t in self.tracks:
            t.close()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
