import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path

import numpy as np

from session_scribe import transcribe
from session_scribe.session import Session

created = []


class FakeModel:
    def __init__(self, name, device, compute_type):
        created.append(device)
        self.device = device

    def transcribe(self, audio, **kw):
        info = types.SimpleNamespace(duration=len(audio) / 16000)

        def gen():
            if self.device != "cpu":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            w = types.SimpleNamespace(start=0.5, end=1.0, word=" hello")
            yield types.SimpleNamespace(end=1.0, words=[w])
        return gen(), info


class GpuFallbackTest(unittest.TestCase):
    def test_switches_to_cpu(self):
        sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=FakeModel)
        with tempfile.TemporaryDirectory() as d:
            s = Session.create(Path(d), "Test")
            s.meta["tracks"] = [{"name": "discord", "chunk_seconds": 600}]
            s.save()
            s.audio_dir.mkdir()
            for i in range(2):
                with wave.open(str(s.audio_dir / f"discord_00{i}.wav"), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
                    w.writeframes(np.full(48000, 500, dtype=np.int16).tobytes())
            msgs = []
            transcribe.transcribe_audio(s, progress=lambda f, m: msgs.append(m))
            self.assertEqual(created, ["auto", "cpu"])  # CPU model loaded once, reused
            self.assertTrue(any("switching to CPU" in m for m in msgs))
            self.assertTrue((s.dir / "transcribe_cache" / "discord_001.json").exists())


if __name__ == "__main__":
    unittest.main()
