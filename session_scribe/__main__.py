"""Entry point.

    python -m session_scribe                 open the app
    python -m session_scribe check           test Discord + audio (milestone 1)
    python -m session_scribe transcribe DIR  transcribe a session folder
    python -m session_scribe relabel DIR     rebuild transcript.md after editing tags
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

from .config import Config


async def _check_discord(config: Config, seconds: int) -> bool:
    from .discord_rpc import DiscordRPC, RPCError
    from .session import display_name

    s = config.settings
    if not (s.discord_client_id and s.discord_client_secret):
        print("  ! No Discord app ID/secret yet. Open the app, go to Settings, and add them.")
        return False

    names: dict[str, str] = {}

    def on_event(evt, data):
        uid = data.get("user_id")
        if evt in ("SPEAKING_START", "SPEAKING_STOP"):
            word = "started" if evt == "SPEAKING_START" else "stopped"
            print(f"    {time.strftime('%H:%M:%S')}  {names.get(uid, uid)} {word} speaking")

    rpc = DiscordRPC(s.discord_client_id.strip(), s.discord_client_secret.strip(),
                     config.token_path, s.redirect_uri.strip() or "http://localhost",
                     on_event=on_event, on_status=lambda m: print(f"  ... {m}"))
    try:
        await rpc.connect()
        print("  ok  Found the Discord app")
        user = await rpc.login()
        print(f"  ok  Logged in as {display_name(user)}")
        ch = await rpc.get_selected_voice_channel()
        if not ch:
            print("  !   You are not in a voice channel. Join one and run the check again.")
            return False
        print(f"  ok  In voice channel: {ch.get('name')}")
        for vs in ch.get("voice_states", []):
            u = vs.get("user") or {}
            names[u.get("id")] = display_name(u, vs.get("nick"))
            print(f"        - {names[u.get('id')]}{'  (bot)' if u.get('bot') else ''}")
        await rpc.watch_channel(ch["id"])
        print(f"  ... Listening for {seconds}s. Talk, or ask someone else to talk:")
        await asyncio.sleep(seconds)
        return True
    except RPCError as e:
        print(f"  !   Discord: {e}")
        return False
    finally:
        await rpc.close()


def _check_audio(seconds: int) -> bool:
    import numpy as np

    from .audio import Recorder
    from .clock import SessionClock

    clock = SessionClock()
    rec = Recorder(clock)
    out = Path(tempfile.mkdtemp(prefix="session-scribe-check-"))
    try:
        clock.start()
        tracks = rec.start(out)
    except Exception as e:
        print(f"  !   Could not open audio devices: {e}")
        return False
    for t in tracks:
        print(f"  ok  {t['name']}: {t['device']} at {t['sample_rate']} Hz")
    print(f"  ... Recording {seconds}s. Play some sound and say something.")
    time.sleep(seconds)
    clock.pause()
    rec.stop()
    import wave
    good = True
    for t in rec.tracks:
        chunks = sorted(out.glob(f"{t.name}_*.wav"))
        if not chunks:
            print(f"  !   {t.name}: nothing was written")
            good = False
            continue
        with wave.open(str(chunks[0])) as w:
            frames = w.readframes(w.getnframes())
            length = w.getnframes() / w.getframerate()
        peak = int(np.abs(np.frombuffer(frames, dtype=np.int16)).max(initial=0))
        status = "ok " if peak > 300 else "!  "
        hint = "" if peak > 300 else "  (very quiet: was anything playing / were you talking?)"
        print(f"  {status} {t.name}: {length:.1f}s recorded, peak level {peak}{hint}")
    print(f"  Test files are in {out}")
    return good


def cmd_check(args) -> int:
    config = Config()
    print("\n1. Discord connection and speaking events")
    d = asyncio.run(_check_discord(config, args.seconds))
    print("\n2. Audio capture")
    a = _check_audio(min(args.seconds, 10))
    print("\nAll good." if d and a else "\nSome checks need attention (see the ! lines above).")
    return 0 if d and a else 1


def cmd_transcribe(args) -> int:
    from . import transcribe

    s = Config().settings

    def progress(frac, msg):
        print(f"\r[{frac * 100:5.1f}%] {msg:<60}", end="", flush=True)

    path = transcribe.run(Path(args.dir), args.model or s.whisper_model, s.language, s.glossary, progress,
                         device=s.whisper_device)
    print(f"\nWrote {path}")
    return 0


def cmd_relabel(args) -> int:
    from . import transcribe
    from .session import Session

    print(f"Wrote {transcribe.write_transcript(Session(Path(args.dir)))}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="session_scribe")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="test Discord and audio capture")
    c.add_argument("--seconds", type=int, default=20)
    t = sub.add_parser("transcribe", help="transcribe a session folder")
    t.add_argument("dir")
    t.add_argument("--model", default=None)
    r = sub.add_parser("relabel", help="rebuild transcript.md from cached words")
    r.add_argument("dir")
    args = p.parse_args(argv)

    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "transcribe":
        return cmd_transcribe(args)
    if args.cmd == "relabel":
        return cmd_relabel(args)
    from .app import main as app_main
    app_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
