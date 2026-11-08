import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from session_scribe import attribution as attr
from session_scribe import transcribe
from session_scribe.clock import SessionClock
from session_scribe.discord_rpc import decode_header, encode_frame
from session_scribe.session import Session
from session_scribe.wavchunks import ChunkedWavWriter, to_mono_int16


class FakeTime:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ClockTests(unittest.TestCase):
    def test_pause_excludes_time(self):
        ft = FakeTime()
        c = SessionClock(ft)
        c.start()
        ft.t += 10
        self.assertAlmostEqual(c.pause(), 10)
        ft.t += 600  # break
        self.assertAlmostEqual(c.elapsed(), 10)
        c.resume()
        ft.t += 5
        self.assertAlmostEqual(c.elapsed(), 15)


class FramingTests(unittest.TestCase):
    def test_roundtrip(self):
        frame = encode_frame(1, {"cmd": "X"})
        op, length = decode_header(frame[:8])
        self.assertEqual(op, 1)
        self.assertEqual(json.loads(frame[8:8 + length]), {"cmd": "X"})


class WavTests(unittest.TestCase):
    def test_chunks_split_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            w = ChunkedWavWriter(Path(d), "t", sample_rate=100, chunk_seconds=1)
            w.write(np.ones(250, dtype=np.int16))
            w.pad_to(3.2)
            w.close()
            files = sorted(Path(d).glob("t_*.wav"))
            self.assertEqual([f.name for f in files], ["t_000.wav", "t_001.wav", "t_002.wav", "t_003.wav"])
            lengths = []
            for f in files:
                with wave.open(str(f)) as r:
                    lengths.append(r.getnframes())
            self.assertEqual(lengths, [100, 100, 100, 20])

    def test_downmix(self):
        stereo = np.array([100, 300, -100, -300], dtype=np.int16).tobytes()
        self.assertEqual(to_mono_int16(stereo, 2).tolist(), [200, -200])


class AttributionTests(unittest.TestCase):
    def events(self):
        return [
            {"t": 0.0, "type": "start", "wall": 0},
            {"t": 1.0, "type": "speaking_start", "user_id": "ash", "wall": 1},
            {"t": 3.0, "type": "speaking_stop", "user_id": "ash", "wall": 3},
            {"t": 2.0, "type": "speaking_start", "user_id": "bot", "wall": 2},
            {"t": 4.0, "type": "speaking_start", "user_id": "bea", "wall": 4},
            {"t": 6.0, "type": "pause", "wall": 6},
            {"t": 6.0, "type": "resume", "wall": 906},
            {"t": 7.0, "type": "speaking_stop", "user_id": "bea", "wall": 907},
        ]

    def test_intervals_close_at_pause(self):
        iv = attr.speaking_intervals(self.events(), end_time=10)
        self.assertEqual(iv["bea"].ends, [6.0])
        self.assertEqual(iv["bot"].ends, [6.0])
        self.assertAlmostEqual(iv["ash"].overlap(2, 5), 1.0)

    def test_ignored_bot_dropped_and_lines_grouped(self):
        iv = attr.speaking_intervals(self.events(), end_time=10)
        words = [
            attr.Word(1.2, 1.5, " Hello"), attr.Word(1.6, 2.0, " there."),
            attr.Word(4.2, 4.5, " Hi"), attr.Word(4.6, 5.0, " Ash."),
            attr.Word(8.5, 9.0, " la-la"),  # only the ignored bot is "speaking" here: dropped
        ]
        got = attr.attribute_words(words, ["ash", "bea"], iv)
        lines = attr.group_lines(got)
        self.assertEqual([(l.speaker_id, l.text) for l in lines], [("ash", "Hello there."), ("bea", "Hi Ash.")])

    def test_fallback_without_discord(self):
        words = [attr.Word(0, 1, " hi")]
        self.assertEqual(attr.attribute_words(words, ["me"], {}, fallback="me")[0][0], "me")
        self.assertEqual(attr.attribute_words(words, ["a"], {}, fallback=None), [])

    def test_pauses(self):
        self.assertEqual(attr.pauses(self.events()), [(6.0, 900)])


class AudioLoadTests(unittest.TestCase):
    def _wav(self, d, rate, signal):
        path = Path(d) / "a_000.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes((signal * 16000).astype(np.int16).tobytes())
        return path

    def test_resample_48k_keeps_speech_band_and_drops_aliasing(self):
        rate = 48000
        t = np.arange(rate * 2) / rate
        with tempfile.TemporaryDirectory() as d:
            speech = transcribe.load_wav_16k(self._wav(d, rate, np.sin(2 * np.pi * 1000 * t)))
            self.assertEqual(len(speech), 32000)
            self.assertEqual(speech.dtype, np.float32)
            self.assertGreater(np.abs(speech[1000:-1000]).max(), 0.4)
            high = transcribe.load_wav_16k(self._wav(d, rate, np.sin(2 * np.pi * 12000 * t)))
            self.assertLess(np.abs(high[1000:-1000]).max(), 0.02)

    def test_odd_rate(self):
        rate = 44100
        t = np.arange(rate) / rate
        with tempfile.TemporaryDirectory() as d:
            out = transcribe.load_wav_16k(self._wav(d, rate, np.sin(2 * np.pi * 440 * t)))
            self.assertEqual(len(out), 16000)


class TranscriptTests(unittest.TestCase):
    def test_end_to_end_from_cache(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session.create(Path(d), "Mythropolis", "Session 1")
            s.meta.update({
                "self_user_id": "gm",
                "duration": 700,
                "tracks": [{"name": "discord", "chunk_seconds": 600}, {"name": "mic", "chunk_seconds": 600}],
                "participants": {
                    "gm": {"name": "Erin", "character": "GM", "ignored": False, "is_bot": False},
                    "ash": {"name": "Kira", "character": "Ash", "ignored": False, "is_bot": False},
                    "bot": {"name": "Jukebox", "character": "", "ignored": True, "is_bot": True},
                },
            })
            s.save()
            for ev in [("speaking_start", "gm", 1), ("speaking_stop", "gm", 3),
                       ("speaking_start", "bot", 0), ("speaking_start", "ash", 605), ("speaking_stop", "ash", 607)]:
                s.log(ev[2], ev[0], user_id=ev[1])
            s.close()
            s.audio_dir.mkdir()
            for name in ("discord_000", "discord_001", "mic_000"):
                (s.audio_dir / f"{name}.wav").write_bytes(b"")
            cache = s.dir / "transcribe_cache"
            cache.mkdir()
            (cache / "mic_000.json").write_text(json.dumps([{"start": 1.2, "end": 2.0, "text": " Welcome back."}]))
            (cache / "discord_000.json").write_text(json.dumps([{"start": 100, "end": 101, "text": " lyrics"}]))
            (cache / "discord_001.json").write_text(json.dumps([{"start": 5.2, "end": 6.0, "text": " I draw my sword."}]))
            md = transcribe.write_transcript(Session(s.dir)).read_text()
            self.assertIn("**[00:00:01] GM (Erin):** Welcome back.", md)
            self.assertIn("**[00:10:05] Ash (Kira):** I draw my sword.", md)
            self.assertNotIn("lyrics", md)
            self.assertNotIn("Jukebox", md)


if __name__ == "__main__":
    unittest.main()
