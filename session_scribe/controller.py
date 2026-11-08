"""Glue between Discord, the recorder, the transcriber and the UI."""

from __future__ import annotations

import asyncio
import shutil
import logging
import threading
import time
import wave
from dataclasses import asdict
from pathlib import Path

from . import transcribe
from .audio import Recorder
from .clock import SessionClock
from .config import Config
from .discord_rpc import DiscordRPC, RPCError
from .session import Session, display_name, list_sessions

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()

        # Discord
        self.rpc: DiscordRPC | None = None
        self.rpc_status = "not connected"
        self.rpc_wanted = False
        self.user: dict | None = None
        self.channel: dict | None = None
        self.voice: dict[str, dict] = {}       # user id -> {id, name, is_bot}
        self.speaking: set[str] = set()

        # Recording
        self.clock = SessionClock()
        self.recorder: Recorder | None = None
        self.session: Session | None = None
        self.rec_state = "idle"                # idle | recording | paused
        self.rec_error: str | None = None
        self.campaign = config.settings.last_campaign
        self.title = ""
        # Ending a session always starts transcription (tests switch it off).
        self.transcribe_on_end = True

        # Transcription
        self.jobs: dict[str, dict] = {}
        self._job_queue: list[str] = []
        self._job_cancel: set[str] = set()
        self._job_thread: threading.Thread | None = None

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="asyncio", daemon=True).start()
        self._recover_interrupted()

    # ================================================================ state ==

    def state(self) -> dict:
        with self.lock:
            tags = self.config.tags_for(self.campaign)
            participants = []
            for uid, v in self.voice.items():
                tag = self._tag(uid, v.get("is_bot", False))
                participants.append({
                    **v,
                    "character": tag["character"],
                    "ignored": tag["ignored"],
                    "speaking": uid in self.speaking,
                    "is_self": bool(self.user and self.user.get("id") == uid),
                })
            participants.sort(key=lambda p: (not p["is_self"], p["ignored"], p["name"].lower()))
            s = self.config.settings
            return {
                "discord": {
                    "status": self.rpc_status,
                    "configured": bool(s.discord_client_id and s.discord_client_secret),
                    "user": display_name(self.user) if self.user else None,
                    "channel": (self.channel or {}).get("name"),
                },
                "participants": participants,
                "campaign": self.campaign,
                "campaigns": sorted(set(self.config.settings.campaigns) | {self.campaign}),
                "title": self.title,
                "recording": {
                    "state": self.rec_state,
                    "elapsed": self.clock.elapsed() if self.rec_state != "idle" else 0,
                    "levels": self.recorder.levels() if self.recorder else {},
                    "error": self.rec_error or "; ".join(self.recorder.errors() if self.recorder else []),
                    "dir": str(self.session.dir) if self.session else None,
                },
                "jobs": self.jobs,
                "settings": {k: v for k, v in asdict(s).items()
                             if k not in ("campaigns", "known_users")},
                "_known_tags": len(tags),
            }

    def _tag(self, uid: str, is_bot: bool) -> dict:
        tag = self.config.tag(self.campaign, uid)
        if tag is None:
            return {"character": "", "ignored": bool(is_bot)}
        return tag

    # ============================================================== discord ==

    def connect(self) -> None:
        s = self.config.settings
        if not (s.discord_client_id and s.discord_client_secret):
            self.rpc_status = "add your Discord app ID and secret in Settings first"
            return
        if self.rpc_wanted:
            return
        self.rpc_wanted = True
        asyncio.run_coroutine_threadsafe(self._rpc_main(), self.loop)

    def disconnect(self) -> None:
        self.rpc_wanted = False
        if self.rpc:
            asyncio.run_coroutine_threadsafe(self.rpc.close(), self.loop)

    def forget_login(self) -> None:
        self.disconnect()
        self.config.token_path.unlink(missing_ok=True)

    def _set_status(self, status: str) -> None:
        self.rpc_status = status

    async def _rpc_main(self) -> None:
        delay = 3
        while self.rpc_wanted:
            s = self.config.settings
            rpc = DiscordRPC(
                s.discord_client_id.strip(),
                s.discord_client_secret.strip(),
                self.config.token_path,
                s.redirect_uri.strip() or "http://localhost",
                on_event=self._on_event,
                on_status=self._set_status,
            )
            self.rpc = rpc
            try:
                self._set_status("connecting")
                await rpc.connect()
                user = await rpc.login()
                with self.lock:
                    self.user = user
                    if self.session:
                        self.session.meta["self_user_id"] = user.get("id")
                        self.session.save()
                await rpc.subscribe("VOICE_CHANNEL_SELECT")
                await self._refresh_channel()
                self._set_status("connected")
                delay = 3
                # Re-check the channel now and then in case a join/leave
                # event was missed (the Refresh button does the same).
                while not rpc.closed.is_set():
                    try:
                        await asyncio.wait_for(rpc.closed.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            await self._refresh_channel()
                        except (RPCError, asyncio.TimeoutError) as e:
                            log.info("Periodic channel refresh failed: %s", e)
                if self.rpc_wanted:
                    self._set_status("lost connection to Discord, retrying")
            except (RPCError, OSError, asyncio.TimeoutError) as e:
                self._set_status(f"error: {e}" if str(e) else "error: timed out")
                log.warning("Discord connection failed: %s", e)
            finally:
                await rpc.close()
                with self.lock:
                    self.speaking.clear()
                    if self.session and self.rec_state == "recording":
                        self.session.log(self.clock.elapsed(), "discord_disconnected")
            if self.rpc_wanted:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        self._set_status("not connected")

    async def _refresh_channel(self) -> None:
        assert self.rpc is not None
        old = (self.channel or {}).get("id")
        ch = await self.rpc.get_selected_voice_channel()
        if old and (not ch or ch.get("id") != old):
            await self.rpc.unwatch_channel(old)
        same_channel = bool(ch and old == ch.get("id"))
        with self.lock:
            before = set(self.voice)
            if not same_channel:
                self.voice.clear()
                self.speaking.clear()
            if not ch:
                self.channel = None
                return
            self.channel = {"id": ch.get("id"), "name": ch.get("name"), "guild_id": ch.get("guild_id")}
            present = set()
            for vs in ch.get("voice_states", []):
                self._upsert_voice(vs)
                present.add((vs.get("user") or {}).get("id"))
            # Catch joins/leaves Discord didn't tell us about.
            rec = self.session if self.rec_state == "recording" and same_channel else None
            t = self.clock.elapsed()
            for uid in set(self.voice) - present:
                self.voice.pop(uid, None)
                if uid in self.speaking:
                    self.speaking.discard(uid)
                    if rec:
                        rec.log(t, "speaking_stop", user_id=uid)
                if rec:
                    rec.log(t, "leave", user_id=uid)
            if rec:
                for uid in present - before:
                    if uid:
                        rec.log(t, "join", user_id=uid)
            if self.session:
                self.session.meta["channel"] = self.channel
                self.session.save()
        if not old or old != ch.get("id"):
            await self.rpc.watch_channel(ch["id"])

    def refresh_voice(self) -> str | None:
        """Ask Discord again who is in the voice channel (the Refresh button)."""
        if self.rpc is None or self.rpc_status != "connected":
            return "Connect to Discord first."
        fut = asyncio.run_coroutine_threadsafe(self._refresh_channel(), self.loop)
        try:
            fut.result(timeout=10)
        except Exception as e:
            return f"Couldn't refresh: {e}"
        return None

    def _upsert_voice(self, vs: dict) -> None:
        user = vs.get("user") or {}
        uid = user.get("id")
        if not uid:
            return
        entry = {"id": uid, "name": display_name(user, vs.get("nick")),
                 "username": user.get("username"), "is_bot": bool(user.get("bot"))}
        self.voice[uid] = entry
        self.config.remember_user(uid, entry["name"], entry["username"], entry["is_bot"])
        self._sync_participant(uid)

    def _sync_participant(self, uid: str) -> None:
        if not self.session or uid not in self.voice:
            return
        v = self.voice[uid]
        tag = self._tag(uid, v["is_bot"])
        self.session.upsert_participant(uid, v["name"], v["is_bot"], tag["character"], tag["ignored"])

    def _on_event(self, evt: str, data: dict):
        # Runs on the event loop. Kept synchronous so speaking events are
        # logged in the order Discord sent them.
        if evt == "VOICE_CHANNEL_SELECT":
            return self._refresh_channel()  # coroutine, scheduled by the client
        with self.lock:
            rec = self.session if self.rec_state == "recording" else None
            t = self.clock.elapsed()
            if evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
                is_new = ((data.get("user") or {}).get("id")) not in self.voice
                self._upsert_voice(data)
                if rec and is_new:
                    rec.log(t, "join", user_id=data["user"]["id"])
            elif evt == "VOICE_STATE_DELETE":
                uid = (data.get("user") or {}).get("id")
                self.voice.pop(uid, None)
                if uid in self.speaking:
                    self.speaking.discard(uid)
                    if rec:
                        rec.log(t, "speaking_stop", user_id=uid)
                if rec:
                    rec.log(t, "leave", user_id=uid)
            elif evt == "SPEAKING_START":
                uid = data.get("user_id")
                self.speaking.add(uid)
                if rec:
                    rec.log(t, "speaking_start", user_id=uid)
            elif evt == "SPEAKING_STOP":
                uid = data.get("user_id")
                self.speaking.discard(uid)
                if rec:
                    rec.log(t, "speaking_stop", user_id=uid)

    # ================================================================= tags ==

    def set_campaign(self, name: str) -> str | None:
        """Switch the active campaign; its player/character tags apply at once."""
        name = (name or "").strip() or "My Campaign"
        with self.lock:
            if self.rec_state != "idle":
                return "You can't switch campaigns while recording."
            self.campaign = name
            self.config.tags_for(name)
            self.config.settings.last_campaign = name
            self.config.save()
        return None

    def set_tag(self, uid: str, character: str | None = None, ignored: bool | None = None) -> None:
        self.set_campaign_tag(self.campaign, uid, character, ignored)

    def set_campaign_tag(self, campaign: str, uid: str, character: str | None = None,
                         ignored: bool | None = None) -> None:
        with self.lock:
            if self.config.tag(campaign, uid) is None:
                is_bot = self.voice.get(uid, self.config.settings.known_users.get(uid, {})).get("is_bot", False)
                self.config.set_tag(campaign, uid, "", bool(is_bot))
            self.config.set_tag(campaign, uid, character, ignored)
            if campaign == self.campaign:
                self._sync_participant(uid)

    def remove_campaign_tag(self, campaign: str, uid: str) -> None:
        with self.lock:
            self.config.remove_tag(campaign, uid)
            if campaign == self.campaign:
                self._sync_participant(uid)

    # ============================================================ campaigns ==

    def campaigns(self) -> dict:
        """Everything the Campaigns tab needs."""
        with self.lock:
            known = self.config.settings.known_users
            out = []
            for name in sorted(set(self.config.settings.campaigns) | {self.campaign}, key=str.lower):
                players = []
                for uid, tag in self.config.settings.campaigns.get(name, {}).items():
                    user = known.get(uid) or self.voice.get(uid) or {}
                    players.append({
                        "id": uid,
                        "name": user.get("name") or f"Unknown user {uid[-4:]}",
                        "username": user.get("username", ""),
                        "is_bot": bool(user.get("is_bot")),
                        "character": tag.get("character", ""),
                        "ignored": bool(tag.get("ignored")),
                    })
                players.sort(key=lambda p: (p["ignored"], p["name"].lower()))
                out.append({"name": name, "active": name == self.campaign, "players": players})
            users = [{"id": uid, **u} for uid, u in known.items()]
            users.sort(key=lambda u: u["name"].lower())
            return {"campaigns": out, "known_users": users,
                    "locked": self.rec_state != "idle"}

    def create_campaign(self, name: str) -> str | None:
        name = (name or "").strip()
        if not name:
            return "Give the campaign a name."
        with self.lock:
            if name in self.config.settings.campaigns:
                return f"There's already a campaign called {name}."
            self.config.create_campaign(name)
        return None

    def rename_campaign(self, old: str, new: str) -> str | None:
        new = (new or "").strip()
        with self.lock:
            if not new:
                return "Give the campaign a name."
            if new == old:
                return None
            if new in self.config.settings.campaigns:
                return f"There's already a campaign called {new}."
            if old == self.campaign and self.rec_state != "idle":
                return "You can't rename the campaign you're recording."
            self.config.rename_campaign(old, new)
            if self.campaign == old:
                self.campaign = new
        return None

    def delete_campaign(self, name: str) -> str | None:
        with self.lock:
            if name == self.campaign and self.rec_state != "idle":
                return "You can't delete the campaign you're recording."
            self.config.delete_campaign(name)
            if self.campaign == name:
                remaining = sorted(self.config.settings.campaigns, key=str.lower)
                self.campaign = remaining[0] if remaining else "My Campaign"
                self.config.tags_for(self.campaign)
                self.config.settings.last_campaign = self.campaign
                self.config.save()
        return None

    # ============================================================ recording ==

    def start(self, title: str = "") -> None:
        with self.lock:
            if self.rec_state != "idle":
                return
            self.rec_error = None
            self.title = title or ""
            session = Session.create(self.config.sessions_dir, self.campaign, self.title)
            session.meta["self_user_id"] = (self.user or {}).get("id")
            session.meta["channel"] = self.channel
            self.session = session
            for uid in self.voice:
                self._sync_participant(uid)
            self.clock = SessionClock()
            self.recorder = Recorder(self.clock)
            self.clock.start()
            try:
                st = self.config.settings
                session.meta["tracks"] = self.recorder.start(
                    session.audio_dir, st.record_mic, st.mic_device, st.output_device)
            except Exception as e:
                log.exception("Could not start recording")
                self.rec_error = f"Could not start recording: {e}"
                self.clock.pause()
                self.recorder.stop()
                self.recorder = None
                session.meta["status"] = "failed"
                session.save()
                session.close()
                self.session = None
                return
            session.save()
            session.log(0.0, "start")
            for uid in self.speaking:
                session.log(0.0, "speaking_start", user_id=uid)
            self.rec_state = "recording"

    def pause(self) -> None:
        with self.lock:
            if self.rec_state != "recording":
                return
            t = self.clock.pause()
            self.session.log(t, "pause")
            self.rec_state = "paused"

    def resume(self) -> None:
        with self.lock:
            if self.rec_state != "paused":
                return
            t = self.clock.resume()
            self.session.log(t, "resume")
            for uid in self.speaking:
                self.session.log(t, "speaking_start", user_id=uid)
            self.rec_state = "recording"

    def stop(self) -> None:
        with self.lock:
            if self.rec_state == "idle":
                return
            session, recorder = self.session, self.recorder
            t = self.clock.pause()
            session.log(t, "stop")
            self.rec_state = "idle"
        recorder.stop()  # outside the lock: may take a moment to flush
        with self.lock:
            session.meta.update({
                "status": "recorded",
                "duration": round(t, 3),
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            session.save()
            session.close()
            self.session = None
            self.recorder = None
        if self.transcribe_on_end:
            self.queue_transcription(str(session.dir))

    def _recover_interrupted(self) -> None:
        """Sessions left 'recording' after a crash are kept and marked interrupted."""
        for info in list_sessions(self.config.sessions_dir):
            if info["status"] != "recording":
                continue
            s = Session(Path(info["dir"]))
            length = 0.0
            for chunk in s.audio_chunks("discord"):
                try:
                    with wave.open(str(chunk)) as w:
                        length = max(length, transcribe.chunk_offset(chunk, 600) + w.getnframes() / w.getframerate())
                except (wave.Error, EOFError, OSError):
                    pass  # last chunk may be unreadable after a hard crash
            last_event = max((e.get("t", 0) for e in s.events()), default=0)
            s.meta["duration"] = max(length, last_event)
            s.meta["status"] = "interrupted"
            s.save()

    # ======================================================== transcription ==

    def queue_transcription(self, session_dir: str, fresh: bool = False) -> None:
        with self.lock:
            if session_dir in self._job_queue or self.jobs.get(session_dir, {}).get("status") == "running":
                return
            if fresh:
                # Re-transcribe from scratch (e.g. after an app update or model change).
                shutil.rmtree(Path(session_dir) / "transcribe_cache", ignore_errors=True)
            self.jobs[session_dir] = {"status": "queued", "progress": 0.0, "message": "Waiting"}
            self._job_queue.append(session_dir)
            if not self._job_thread or not self._job_thread.is_alive():
                self._job_thread = threading.Thread(target=self._job_worker, name="transcriber", daemon=True)
                self._job_thread.start()

    def cancel_transcription(self, session_dir: str) -> None:
        with self.lock:
            self._job_cancel.add(session_dir)
            if session_dir in self._job_queue:
                self._job_queue.remove(session_dir)
                self.jobs.pop(session_dir, None)

    def _job_worker(self) -> None:
        while True:
            with self.lock:
                if not self._job_queue:
                    return
                d = self._job_queue.pop(0)
                self._job_cancel.discard(d)
                job = self.jobs[d]
                job["status"] = "running"

            def progress(frac: float, msg: str, job=job) -> None:
                job["progress"] = round(frac, 4)
                job["message"] = msg

            s = self.config.settings
            try:
                path = transcribe.run(Path(d), s.whisper_model, s.language, s.glossary,
                                      progress, cancel=lambda d=d: d in self._job_cancel,
                                      device=s.whisper_device)
                job["status"] = "done" if path else "cancelled"
            except Exception as e:
                log.exception("Transcription failed for %s", d)
                job["status"] = "error"
                job["message"] = str(e)

    def relabel(self, session_dir: str) -> str:
        """Rebuild transcript.md from cached words, e.g. after fixing tags."""
        s = Session(Path(session_dir))
        return str(transcribe.write_transcript(s))

    # ============================================================= sessions ==

    def sessions(self) -> list[dict]:
        return list_sessions(self.config.sessions_dir)

    def delete_session(self, session_dir: str) -> str | None:
        """Move a whole session folder (audio + transcript) to the Recycle Bin.

        Returns an error message for the UI, or None on success.
        """
        path = Path(session_dir).resolve()
        root = self.config.sessions_dir.resolve()
        if path.parent != root or not (path / "session.json").exists():
            return "That isn't a Session Scribe session folder, so it wasn't deleted."
        with self.lock:
            if self.session and self.session.dir.resolve() == path:
                return "This session is still recording. End it first."
            job = self.jobs.get(session_dir)
            if job and job.get("status") == "running":
                return "This session is being transcribed. Cancel the transcription first."
            if session_dir in self._job_queue:
                self._job_queue.remove(session_dir)
        try:
            from send2trash import send2trash
            send2trash(str(path))
        except Exception as e:
            log.exception("Could not delete %s", path)
            return f"Couldn't move the session to the Recycle Bin: {e}"
        with self.lock:
            self.jobs.pop(session_dir, None)
        return None

    def save_settings(self, values: dict) -> None:
        with self.lock:
            s = self.config.settings
            for k, v in values.items():
                if k in s.__dataclass_fields__ and k not in ("campaigns", "known_users"):
                    setattr(s, k, v)
            self.config.save()
