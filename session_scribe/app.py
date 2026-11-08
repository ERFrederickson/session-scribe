"""Desktop window. The UI is plain HTML/JS in ui/, talking to Api below."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import Config, app_dir
from .controller import Controller

UI_DIR = Path(__file__).parent / "ui"


class Api:
    """Every public method here is callable from JavaScript as
    window.pywebview.api.<name>(...). Keep the controller private (leading
    underscore) so pywebview does not try to expose its internals."""

    def __init__(self, controller: Controller):
        self._c = controller

    def state(self):
        return self._c.state()

    def connect(self):
        self._c.connect()

    def disconnect(self):
        self._c.disconnect()

    def forget_login(self):
        self._c.forget_login()

    def set_campaign(self, name):
        self._c.set_campaign(name)

    def set_character(self, user_id, character):
        self._c.set_tag(user_id, character=character)

    def set_ignored(self, user_id, ignored):
        self._c.set_tag(user_id, ignored=bool(ignored))

    def start(self, title=""):
        self._c.start(title)

    def pause(self):
        self._c.pause()

    def resume(self):
        self._c.resume()

    def end_session(self):
        """Halt recording, close the session and start transcribing it."""
        self._c.stop()

    def sessions(self):
        return self._c.sessions()

    def transcribe(self, session_dir, fresh=False):
        self._c.queue_transcription(session_dir, bool(fresh))

    def cancel_transcription(self, session_dir):
        self._c.cancel_transcription(session_dir)

    def delete_session(self, session_dir):
        return self._c.delete_session(session_dir)

    def relabel(self, session_dir):
        return self._c.relabel(session_dir)

    def open_path(self, path):
        if not path or not Path(path).exists():
            return
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])

    def audio_devices(self):
        try:
            from .audio import list_devices
            return list_devices()
        except Exception as e:  # e.g. not on Windows
            return {"mics": [], "outputs": [], "error": str(e)}

    def save_settings(self, values):
        self._c.save_settings(values or {})


def setup_logging() -> Path:
    log_path = app_dir() / "session-scribe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    return log_path


def main() -> None:
    import webview  # pywebview

    setup_logging()
    controller = Controller(Config())
    window = webview.create_window(
        "Session Scribe",
        url=str(UI_DIR / "index.html"),
        js_api=Api(controller),
        width=980,
        height=760,
        min_size=(760, 560),
    )

    def on_closing():
        if controller.rec_state != "idle":
            # Never lose a recording because the window was closed.
            controller.stop()
        return True

    window.events.closing += on_closing
    webview.start()
