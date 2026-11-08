"""Settings and the per-campaign character tags, stored in %APPDATA%\\SessionScribe."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "SessionScribe"


def default_data_dir() -> Path:
    return Path.home() / "Documents" / "SessionScribe"


@dataclass
class Settings:
    discord_client_id: str = ""
    discord_client_secret: str = ""
    redirect_uri: str = "http://localhost"
    data_dir: str = str(default_data_dir())
    whisper_model: str = "medium.en"
    language: str = "en"
    # Names and terms Whisper should spell correctly (places, NPCs, factions...)
    glossary: str = ""
    record_mic: bool = True
    last_campaign: str = "My Campaign"
    # campaign name -> discord user id -> {"character": str, "ignored": bool}
    campaigns: dict = field(default_factory=dict)


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "config.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return Settings()
        known = {k: v for k, v in raw.items() if k in Settings.__dataclass_fields__}
        return Settings(**known)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.settings), indent=2), "utf-8")
            os.replace(tmp, self.path)

    @property
    def token_path(self) -> Path:
        return self.path.parent / "discord_token.json"

    @property
    def sessions_dir(self) -> Path:
        return Path(self.settings.data_dir) / "sessions"

    # -- tags ---------------------------------------------------------------

    def tags_for(self, campaign: str) -> dict:
        return self.settings.campaigns.setdefault(campaign, {})

    def tag(self, campaign: str, user_id: str) -> dict | None:
        return self.settings.campaigns.get(campaign, {}).get(user_id)

    def set_tag(self, campaign: str, user_id: str, character: str | None = None,
                ignored: bool | None = None) -> dict:
        tags = self.tags_for(campaign)
        entry = tags.setdefault(user_id, {"character": "", "ignored": False})
        if character is not None:
            entry["character"] = character.strip()
        if ignored is not None:
            entry["ignored"] = bool(ignored)
        self.save()
        return entry
