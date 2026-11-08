"""Match transcribed words to the Discord user who was speaking at that moment.

Pure functions, no audio or Discord dependencies, so they are easy to test.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass

# Discord's speaking indicator lags speech a little and lingers after it ends,
# so we widen each word by this much when looking for a speaker.
TOLERANCE = 0.5
# Start a new transcript line if the same person pauses this long.
MAX_GAP = 2.0
UNKNOWN = "unknown"


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Line:
    speaker_id: str
    start: float
    end: float
    text: str


class Intervals:
    """Sorted, non-overlapping (start, end) spans for one user."""

    def __init__(self, spans: list[tuple[float, float]]):
        spans = sorted(spans)
        merged: list[list[float]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        self.starts = [s for s, _ in merged]
        self.ends = [e for _, e in merged]

    def __len__(self) -> int:
        return len(self.starts)

    def overlap(self, a: float, b: float) -> float:
        total = 0.0
        j = bisect_left(self.starts, b) - 1
        while j >= 0 and self.ends[j] > a:
            total += min(b, self.ends[j]) - max(a, self.starts[j])
            j -= 1
        return total


def speaking_intervals(events: list[dict], end_time: float) -> dict[str, Intervals]:
    open_: dict[str, float] = {}
    spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for ev in sorted(events, key=lambda e: e.get("t", 0.0)):
        t = float(ev.get("t", 0.0))
        kind = ev.get("type")
        uid = ev.get("user_id")
        if kind == "speaking_start" and uid and uid not in open_:
            open_[uid] = t
        elif kind == "speaking_stop" and uid in open_:
            spans[uid].append((open_.pop(uid), t))
        elif kind in ("pause", "stop"):
            for u, s in open_.items():
                spans[u].append((s, t))
            open_.clear()
    for u, s in open_.items():
        spans[u].append((s, end_time))
    return {u: Intervals(v) for u, v in spans.items()}


def pauses(events: list[dict]) -> list[tuple[float, float]]:
    """Return (session_time, break_length_seconds) for each pause."""
    out = []
    paused_wall: float | None = None
    paused_t = 0.0
    for ev in sorted(events, key=lambda e: e.get("wall", 0.0)):
        if ev.get("type") == "pause":
            paused_wall, paused_t = ev.get("wall"), ev.get("t", 0.0)
        elif ev.get("type") == "resume" and paused_wall is not None:
            out.append((paused_t, max(0.0, ev.get("wall", paused_wall) - paused_wall)))
            paused_wall = None
    return out


def attribute_words(
    words: list[Word],
    candidates: list[str],
    intervals: dict[str, Intervals],
    tolerance: float = TOLERANCE,
    fallback: str | None = None,
) -> list[tuple[str, Word]]:
    """Give each word to the candidate whose speaking time overlaps it most.

    Words nobody (among the candidates) was speaking for are dropped. That is
    what removes ignored users such as a music bot: they are simply never
    candidates. If none of the candidates has any speaking data at all (for
    example Discord was disconnected), every word goes to ``fallback``.
    """
    usable = {c: intervals[c] for c in candidates if c in intervals and len(intervals[c])}
    out: list[tuple[str, Word]] = []
    if not usable:
        if fallback is None:
            return out
        return [(fallback, w) for w in words]
    for w in words:
        a, b = w.start - tolerance, w.end + tolerance
        best, best_ov = None, 0.0
        for uid, iv in usable.items():
            ov = iv.overlap(a, b)
            if ov > best_ov:
                best, best_ov = uid, ov
        if best is not None:
            out.append((best, w))
    return out


def group_lines(attributed: list[tuple[str, Word]], max_gap: float = MAX_GAP) -> list[Line]:
    lines: list[Line] = []
    for uid, w in sorted(attributed, key=lambda x: x[1].start):
        text = w.text
        if lines and lines[-1].speaker_id == uid and w.start - lines[-1].end <= max_gap:
            cur = lines[-1]
            cur.text += text
            cur.end = max(cur.end, w.end)
        else:
            lines.append(Line(uid, w.start, w.end, text))
    for line in lines:
        line.text = " ".join(line.text.split())
    return [ln for ln in lines if ln.text]


def merge_tracks(*tracks: list[Line]) -> list[Line]:
    merged = [ln for t in tracks for ln in t]
    merged.sort(key=lambda ln: (ln.start, ln.end))
    return merged
