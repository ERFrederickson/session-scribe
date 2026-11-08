"""Run the controller against a fake Discord IPC server (Unix socket stand-in
for the Windows named pipe) with a fake recorder."""

import asyncio
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from session_scribe import controller as ctl_mod
from session_scribe.config import Config


class FakeRecorder:
    def __init__(self, clock):
        self.clock = clock

    def start(self, audio_dir, record_mic=True, mic_name="", output_name=""):
        Path(audio_dir).mkdir(parents=True, exist_ok=True)
        return [{"name": "discord", "chunk_seconds": 600}]

    def levels(self):
        return {}

    def errors(self):
        return []

    def stop(self):
        pass


class FakeDiscord:
    def __init__(self, path):
        self.path = path
        self.writer = None
        self.voice_states = [
            {"nick": None, "user": {"id": "gm", "username": "erin"}},
            {"nick": "Kira", "user": {"id": "ash", "username": "kira1"}},
            {"nick": None, "user": {"id": "bot", "username": "Jukebox", "bot": True}},
        ]
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(asyncio.start_unix_server(self._client, self.path))
        self.ready.set()
        self.loop.run_forever()

    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self.writer.write(struct.pack("<II", op, len(data)) + data)

    def push(self, evt, data):
        self.loop.call_soon_threadsafe(self._send, 1, {"cmd": "DISPATCH", "evt": evt, "data": data})

    async def _client(self, reader, writer):
        self.writer = writer
        while True:
            try:
                op, n = struct.unpack("<II", await reader.readexactly(8))
                msg = json.loads(await reader.readexactly(n))
            except asyncio.IncompleteReadError:
                return
            if op == 0:
                self._send(1, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}})
                continue
            cmd, nonce = msg["cmd"], msg.get("nonce")
            data = None
            if cmd == "AUTHENTICATE":
                data = {"user": {"id": "gm", "username": "erin"}}
            elif cmd == "GET_SELECTED_VOICE_CHANNEL":
                data = {"id": "c1", "name": "Tavern", "voice_states": list(self.voice_states)}
            self._send(1, {"cmd": cmd, "nonce": nonce, "evt": msg.get("evt"), "data": data})


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class IntegrationTest(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_RUNTIME_DIR"] = d
            fake = FakeDiscord(os.path.join(d, "discord-ipc-0"))
            cfg = Config(Path(d) / "config.json")
            cfg.settings.discord_client_id = "123"
            cfg.settings.discord_client_secret = "s"
            cfg.settings.data_dir = str(Path(d) / "data")
            cfg.token_path.write_text(json.dumps({"access_token": "t", "expires_at": time.time() + 999}))
            ctl_mod.Recorder = FakeRecorder
            c = ctl_mod.Controller(cfg)
            c.transcribe_on_end = False
            c.connect()
            self.assertTrue(wait_for(lambda: c.rpc_status == "connected"), c.rpc_status)
            st = c.state()
            self.assertEqual(st["discord"]["channel"], "Tavern")
            bot = [p for p in st["participants"] if p["id"] == "bot"][0]
            self.assertTrue(bot["ignored"])
            c.set_tag("ash", character="Ash")

            c.start("Session 1")
            fake.push("SPEAKING_START", {"user_id": "ash"})
            self.assertTrue(wait_for(lambda: "ash" in c.speaking))
            c.pause()
            fake.push("SPEAKING_STOP", {"user_id": "ash"})
            self.assertTrue(wait_for(lambda: "ash" not in c.speaking))
            c.resume()
            # channel switch triggers a refresh from inside the event handler (no deadlock)
            fake.push("VOICE_CHANNEL_SELECT", {"channel_id": "c1"})
            time.sleep(0.3)
            self.assertEqual(c.rpc_status, "connected")
            # Someone joins without Discord sending an event; Refresh finds them.
            fake.voice_states.append({"nick": None, "user": {"id": "late", "username": "dex"}})
            self.assertNotIn("late", c.voice)
            self.assertIsNone(c.refresh_voice())
            self.assertIn("late", c.voice)
            self.assertEqual(c.rpc_status, "connected")
            session_dir = c.session.dir
            c.stop()

            meta = json.loads((session_dir / "session.json").read_text())
            self.assertEqual(meta["participants"]["ash"]["character"], "Ash")
            self.assertTrue(meta["participants"]["bot"]["ignored"])
            self.assertEqual(meta["self_user_id"], "gm")
            types = [json.loads(l)["type"] for l in (session_dir / "events.jsonl").read_text().splitlines()]
            self.assertEqual(types[:3], ["start", "speaking_start", "pause"])
            self.assertNotIn("speaking_stop", types[:4])  # stop happened while paused
            self.assertEqual(types[-1], "stop")
            self.assertIn("join", types)
            c.disconnect()


if __name__ == "__main__":
    unittest.main()
