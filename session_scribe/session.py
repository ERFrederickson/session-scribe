"""A recording session on disk.

sessions/<date>_<campaign>/
    session.json      who was there, their character tags, ignored users, tracks
    events.jsonl      speaking start/stop, pauses, joins/leaves (appended live)
    audio/            chunked WAV tracks
    transcript.md     written by the transcriber
    transcript.json
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path


def slug(text: str) -> str:
    text = re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "-")
    return text or "session"


def display_name(user: dict, nick: str | None = None) -> str:
    return nick or user.get("global_name") or user.get("username") or str(user.get("id"))


class Session:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.meta_path = self.dir / "session.json"
        self.events_path = self.dir / "events.jsonl"
        self.audio_dir = self.dir / "audio"
        self._lock = threading.Lock()
        self._events = None
        self.meta: dict = {}
        if self.meta_path.exists():
            self.meta = json.loads(self.meta_path.read_text("utf-8"))

    @classmethod
    def create(cls, sessions_dir: Path, campaign: str, title: str = "") -> "Session":
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        name = f"{stamp}_{slug(campaign)}"
        directory = Path(sessions_dir) / name
        n = 2
        while directory.exists():
            directory = Path(sessions_dir) / f"{name}-{n}"
            n += 1
        directory.mkdir(parents=True)
        s = cls(directory)
        s.meta = {
            "campaign": campaign,
            "title": title,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": None,
            "duration": 0.0,
            "channel": None,
            "self_user_id": None,
            "participants": {},
            "tracks": [],
            "status": "recording",
        }
        s.save()
        return s

    def save(self) -> None:
        with self._lock:
            tmp = self.meta_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.meta, indent=2), "utf-8")
            os.replace(tmp, self.meta_path)

    # -- events -------------------------------------------------------------

    def log(self, t: float, type_: str, **fields) -> None:
        rec = {"t": round(t, 3), "type": type_, "wall": round(time.time(), 3), **fields}
        with self._lock:
            if self._events is None:
                self._events = open(self.events_path, "a", encoding="utf-8")
            self._events.write(json.dumps(rec) + "\n")
            self._events.flush()

    def close(self) -> None:
        with self._lock:
            if self._events is not None:
                self._events.close()
                self._events = None

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        with open(self.events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass  # a half-written last line after a crash
        return out

    # -- participants -------------------------------------------------------

    def upsert_participant(self, user_id: str, name: str, is_bot: bool,
                           character: str, ignored: bool) -> None:
        p = self.meta["participants"].setdefault(user_id, {})
        p.update({"name": name, "is_bot": is_bot, "character": character, "ignored": ignored})
        self.save()

    def audio_chunks(self, track: str) -> list[Path]:
        return sorted(self.audio_dir.glob(f"{track}_*.wav"))


def list_sessions(sessions_dir: Path) -> list[dict]:
    out = []
    if not Path(sessions_dir).exists():
        return out
    for d in sorted(Path(sessions_dir).iterdir(), reverse=True):
        meta_path = d / "session.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except ValueError:
            continue
        out.append({
            "dir": str(d),
            "name": d.name,
            "campaign": meta.get("campaign"),
            "title": meta.get("title"),
            "started_at": meta.get("started_at"),
            "duration": meta.get("duration", 0),
            "status": meta.get("status"),
            "has_transcript": (d / "transcript.md").exists(),
        })
    return out
