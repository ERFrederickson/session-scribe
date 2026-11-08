import types
import unittest

import numpy as np

from session_scribe import transcribe


class EarlyStopper:
    """Pretends to be Whisper: one word every 0.5 s, but gives up after 6 s of
    each window (the behaviour that lost most of the test session)."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        dur = len(audio) / 16000
        words = []
        t = 0.25
        while t + 0.3 <= min(dur, 6.0):
            words.append(types.SimpleNamespace(start=t, end=t + 0.3, word=" w"))
            t += 0.5
        return iter([types.SimpleNamespace(words=words)]), None


class WindowTests(unittest.TestCase):
    def test_no_gaps_when_whisper_stops_early(self):
        audio = np.full(16000 * 60, 0.01, dtype=np.float32)
        model = EarlyStopper()
        words = transcribe._transcribe_chunk(model, audio, "en", None, lambda f: None, lambda: False)
        starts = [w["start"] for w in words]
        self.assertEqual(starts, sorted(starts))
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertLess(max(gaps), 1.0)              # nothing skipped
        self.assertGreater(starts[-1], 58.5)          # reached the end
        self.assertEqual(len(starts), len(set(starts)))  # no duplicates

    def test_silence_is_skipped_quickly(self):
        model = EarlyStopper()
        words = transcribe._transcribe_chunk(model, np.zeros(16000 * 600, dtype=np.float32),
                                             "en", None, lambda f: None, lambda: False)
        self.assertEqual(words, [])
        self.assertEqual(model.calls, 0)

    def test_quiet_audio_is_boosted(self):
        quiet = (np.sin(np.arange(16000) / 5) * 0.05).astype(np.float32)
        self.assertGreater(np.abs(transcribe._normalize(quiet)).max(), 0.3)
        loud = (np.sin(np.arange(16000) / 5) * 0.8).astype(np.float32)
        self.assertIs(transcribe._normalize(loud), loud)


if __name__ == "__main__":
    unittest.main()
