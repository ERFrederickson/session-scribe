"""Turn a recorded session into a speaker-labelled transcript, on this computer.

Runs faster-whisper over each audio chunk. Results for every finished chunk are
cached in transcribe_cache/, so if the job is interrupted (or the computer goes
to sleep) running it again picks up where it stopped. Re-running after editing
character tags in session.json only redoes the cheap labelling step.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import attribution as attr
from .session import Session

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m = seconds // 3600, seconds % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def chunk_offset(path: Path, chunk_seconds: float) -> float:
    m = re.search(r"_(\d+)\.wav$", path.name)
    return int(m.group(1)) * chunk_seconds if m else 0.0


def build_prompt(session: Session, glossary: str) -> str:
    names = []
    for p in session.meta.get("participants", {}).values():
        if p.get("ignored"):
            continue
        names += [p.get("character", ""), p.get("name", "")]
    terms = [t.strip() for t in re.split(r"[,\n]", glossary or "") if t.strip()]
    words = [w for w in dict.fromkeys(names + terms) if w]
    return ("A tabletop roleplaying game session. Names: " + ", ".join(words) + ".") if words else ""


# ------------------------------------------------------------- audio input --

WHISPER_RATE = 16000


def _lowpass_kernel(cutoff: float, taps: int = 101):
    """Windowed-sinc low-pass filter; cutoff is a fraction of the sample rate."""
    import numpy as np

    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


def load_wav_16k(path: Path):
    """Read one of our mono 16-bit WAV chunks as float32 audio at 16 kHz.

    We decode the WAV ourselves instead of letting faster-whisper do it, so
    transcription doesn't depend on which PyAV version pip happened to install.
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit audio, got {8 * width}-bit")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio[: len(audio) - len(audio) % channels].reshape(-1, channels).mean(axis=1)
    if rate == WHISPER_RATE or len(audio) == 0:
        return audio
    if rate > WHISPER_RATE:
        # Remove everything above 8 kHz first so downsampling doesn't alias.
        audio = np.convolve(audio, _lowpass_kernel(0.45 * WHISPER_RATE / rate), mode="same")
    if rate % WHISPER_RATE == 0:
        return np.ascontiguousarray(audio[:: rate // WHISPER_RATE])
    n_out = int(len(audio) * WHISPER_RATE / rate)
    positions = np.arange(n_out, dtype=np.float64) * (rate / WHISPER_RATE)
    return np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)


# ------------------------------------------------------------- whisper step --

def transcribe_audio(session: Session, model_name: str = "medium.en", language: str = "en",
                     glossary: str = "", progress: ProgressFn | None = None,
                     cancel: Callable[[], bool] | None = None) -> None:
    from faster_whisper import WhisperModel  # heavy import, only when needed

    progress = progress or (lambda f, m: None)
    cache = session.dir / "transcribe_cache"
    cache.mkdir(exist_ok=True)

    jobs: list[tuple[str, Path]] = []
    for track in session.meta.get("tracks", []):
        for chunk in session.audio_chunks(track["name"]):
            if not (cache / (chunk.stem + ".json")).exists():
                jobs.append((track["name"], chunk))
    if not jobs:
        progress(1.0, "Audio already transcribed")
        return

    progress(0.0, f"Loading speech model ({model_name}). The first run downloads it.")
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    prompt = build_prompt(session, glossary) or None

    total = sum(max(1, c.stat().st_size) for _, c in jobs)
    done = 0
    for i, (track, chunk) in enumerate(jobs, 1):
        if cancel and cancel():
            progress(done / total, "Cancelled")
            return
        label = f"{track} part {i}/{len(jobs)}"
        progress(done / total, f"Transcribing {label}")
        segments, info = model.transcribe(
            load_wav_16k(chunk),
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        words = []
        size = max(1, chunk.stat().st_size)
        for seg in segments:
            for w in seg.words or []:
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "text": w.word})
            if info.duration:
                frac = min(1.0, seg.end / info.duration)
                progress((done + frac * size) / total, f"Transcribing {label}")
            if cancel and cancel():
                progress(done / total, "Cancelled")
                return
        tmp = cache / (chunk.stem + ".tmp")
        tmp.write_text(json.dumps(words), "utf-8")
        os.replace(tmp, cache / (chunk.stem + ".json"))
        done += size
    progress(1.0, "Audio transcribed")


# ------------------------------------------------------------- labelling ----

def load_words(session: Session, track: str, chunk_seconds: float) -> list[attr.Word]:
    words = []
    for chunk in session.audio_chunks(track):
        cached = session.dir / "transcribe_cache" / (chunk.stem + ".json")
        if not cached.exists():
            continue
        offset = chunk_offset(chunk, chunk_seconds)
        for w in json.loads(cached.read_text("utf-8")):
            words.append(attr.Word(w["start"] + offset, w["end"] + offset, w["text"]))
    return words


def speaker_label(participants: dict, uid: str) -> str:
    if uid == attr.UNKNOWN:
        return "Unknown speaker"
    p = participants.get(uid, {})
    name = p.get("name") or uid
    character = p.get("character")
    return f"{character} ({name})" if character else name


def build_transcript(session: Session) -> list[attr.Line]:
    meta = session.meta
    participants = meta.get("participants", {})
    self_id = meta.get("self_user_id")
    events = session.events()
    duration = float(meta.get("duration") or 0) or max((e.get("t", 0) for e in events), default=0)
    intervals = attr.speaking_intervals(events, duration)
    active = [u for u, p in participants.items() if not p.get("ignored")]

    tracks = []
    for track in meta.get("tracks", []):
        words = load_words(session, track["name"], track.get("chunk_seconds", 600))
        if track["name"] == "mic":
            cands = [self_id] if self_id and self_id in active else []
            fallback = self_id if self_id in active else None
        else:
            cands = [u for u in active if u != self_id]
            fallback = attr.UNKNOWN
        tracks.append(attr.group_lines(attr.attribute_words(words, cands, intervals, fallback=fallback)))
    return attr.merge_tracks(*tracks)


def render_markdown(session: Session, lines: list[attr.Line]) -> str:
    meta = session.meta
    participants = meta.get("participants", {})
    started = meta.get("started_at") or ""
    date = started[:10]
    title = meta.get("title") or f"{meta.get('campaign', 'Session')} {date}"

    out = [
        "---",
        f"campaign: \"{meta.get('campaign', '')}\"",
        f"date: {date}",
        f"recorded: \"{_fmt_duration(meta.get('duration', 0))}\"",
        f"channel: \"{(meta.get('channel') or {}).get('name', '')}\"",
        "---",
        "",
        f"# {title}",
        "",
        "## Cast",
        "",
    ]
    for uid, p in participants.items():
        if p.get("ignored"):
            continue
        who = p.get("name") or uid
        out.append(f"- {who} as {p['character']}" if p.get("character") else f"- {who}")
    out += ["", "## Transcript", ""]

    breaks = list(attr.pauses(session.events()))
    for line in lines:
        while breaks and breaks[0][0] <= line.start:
            t, length = breaks.pop(0)
            out += [f"*[{_fmt_time(t)}] Break ({_fmt_duration(length)})*", ""]
        out += [f"**[{_fmt_time(line.start)}] {speaker_label(participants, line.speaker_id)}:** {line.text}", ""]
    for t, length in breaks:
        out += [f"*[{_fmt_time(t)}] Break ({_fmt_duration(length)})*", ""]
    return "\n".join(out)


def write_transcript(session: Session) -> Path:
    lines = build_transcript(session)
    participants = session.meta.get("participants", {})
    md_path = session.dir / "transcript.md"
    md_path.write_text(render_markdown(session, lines), "utf-8")
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session": session.meta,
        "lines": [
            {
                "start": round(ln.start, 2),
                "end": round(ln.end, 2),
                "speaker_id": ln.speaker_id,
                "speaker": (participants.get(ln.speaker_id) or {}).get("name", ln.speaker_id),
                "character": (participants.get(ln.speaker_id) or {}).get("character", ""),
                "text": ln.text,
            }
            for ln in lines
        ],
    }
    (session.dir / "transcript.json").write_text(json.dumps(data, indent=2), "utf-8")
    return md_path


def run(session_dir: Path, model_name: str, language: str, glossary: str,
        progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None) -> Path | None:
    session = Session(session_dir)
    transcribe_audio(session, model_name, language, glossary, progress, cancel)
    if cancel and cancel():
        return None
    path = write_transcript(session)
    session.meta["status"] = "transcribed"
    session.save()
    if progress:
        progress(1.0, "Transcript ready")
    return path
