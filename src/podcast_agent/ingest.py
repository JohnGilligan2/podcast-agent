"""Load podcast transcripts (.json or messy .txt) into one clean Episode model.

Every line becomes (timestamp, speaker, text). Each utterance keeps BOTH:
  raw_text    exactly as in the file (never modified)
  clean_text  filler removed, punctuation normalised: what the agent reads, quotes and counts

Cleaning runs in a fixed order (see clean_text):
  1. normalise curly quotes and dashes to plain ASCII
  2. remove stage directions such as [crosstalk]
  3. remove pure filler (um, uh, er, hmm ...)
  4. remove context-dependent filler (like, you know, I mean, so) ONLY where it is used as filler:
     set off by commas, or opening a sentence with a comma. "I like the momentum" keeps "like".
  5. collapse stutters ("I I think", "the, the")
  6. tidy spaces and punctuation
Nothing that changes meaning is rewritten: transcription errors ("async yo") are left as spoken and
flagged by the agent for the editor. Every decision is recorded in Episode.ingest_report.

To add a filler word during a live change, edit the lists just below.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---- edit these lists to change what counts as filler ------------------------------------
PURE_FILLERS = ["um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "mm"]
CONTEXT_FILLERS = ["you know", "i mean", "like", "so"]          # removed only when used as filler
BACKCHANNEL = {"yeah", "yes", "okay", "ok", "right", "mhm", "sure"}  # left alone unless the line is only filler
STAGE_DIRECTIONS = ["crosstalk", "cross talk", "laughter", "laughs", "inaudible", "unintelligible",
                    "music", "applause", "pause", "silence", "overlapping", "background noise"]
HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "sir", "dame"}
# -------------------------------------------------------------------------------------------

_FILLER_ALT = "|".join(sorted((re.escape(f) for f in PURE_FILLERS), key=len, reverse=True))
PURE_FILLER_RE = re.compile(rf"(?<![\w'])(?:{_FILLER_ALT})(?![\w'])[,.!?]*\s*", re.IGNORECASE)
# "word, um, word": drop the filler and its commas ("to, uh, Retail" -> "to Retail"), but keep the
# comma after an interjection ("Yes, um, it's" -> "Yes, it's").
COMMA_FILLER_RE = re.compile(rf"(\w+)\s*,\s*(?:{_FILLER_ALT}),\s*", re.IGNORECASE)
_CTX_ALT = "|".join(re.escape(f) for f in sorted(CONTEXT_FILLERS, key=len, reverse=True))
CTX_MID_RE = re.compile(rf",\s*(?:{_CTX_ALT}),\s*", re.IGNORECASE)              # "..., you know, ..."
CTX_START_RE = re.compile(rf"(^|[.!?]\s+)(?:{_CTX_ALT}),\s*", re.IGNORECASE)    # "So, ..." at sentence start
STAGE_RE = re.compile(r"\s*[\[(](?:" + "|".join(re.escape(s) for s in STAGE_DIRECTIONS) + r")[\])]\s*",
                      re.IGNORECASE)
STUTTER_RE = re.compile(r"\b(\w+(?:'\w+)?)(?:,?\s+\1\b)+", re.IGNORECASE)
ASCII_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "..."})

TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
LINE_RE = re.compile(
    rf"^\s*(?:\[?(?P<ts>{TS})\]?\s*)?"                       # optional timestamp, brackets optional
    r"(?P<speaker>[A-Za-z][\w.'’ -]{0,40}?)\s*"              # speaker label
    r"(?::|\s[-–—]\s)\s*(?P<text>.*)$")                      # colon or spaced dash, then the text
META_RE = re.compile(r"^\s*#?\s*(?P<key>title|host|guests?|episode_id)\s*:\s*(?P<value>.+)$", re.IGNORECASE)


class TranscriptError(ValueError):
    """Raised when a transcript file cannot be parsed."""


def normalize_timestamp(ts: str) -> str:
    """'0:00' -> '00:00:00', '1:05' -> '00:01:05', '[00:02:30.500]' -> '00:02:30'.
    Accepts M:SS, MM:SS, H:MM:SS, optional milliseconds and brackets. Raises ValueError on garbage."""
    core = re.split(r"[.,]", ts.strip().strip("[]"))[0]
    parts = core.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"bad timestamp {ts!r}")
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        nums.insert(0, 0)
    h, m, s = nums
    if m >= 60 or s >= 60:
        raise ValueError(f"bad timestamp {ts!r}")
    return f"{h:02d}:{m:02d}:{s:02d}"


def clean_text(text: str) -> str:
    text = text.translate(ASCII_MAP)                                   # 1. plain ASCII
    starts_upper = text.lstrip()[:1].isupper()
    text = STAGE_RE.sub(" ", text)                                     # 2. stage directions
    text = COMMA_FILLER_RE.sub(                                        # 3. pure filler
        lambda m: m.group(1) + (", " if m.group(1).lower() in BACKCHANNEL else " "), text)
    text = PURE_FILLER_RE.sub("", text)
    for _ in range(3):                                                 # 4. context filler; sentence
        text = CTX_START_RE.sub(lambda m: m.group(1), text)            #    start first, repeated so
        text = CTX_MID_RE.sub(" ", text)                               #    "So, like, X" peels to "X"
    text = STUTTER_RE.sub(r"\1", text)                                 # 5. stutters
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)                       # 6. tidy
    text = re.sub(r"([,;:])\1+", r"\1", text)
    text = re.sub(r"^[\s,.;:!?-]+", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if starts_upper and text[:1].islower():                            # "Um, welcome" -> "Welcome"
        text = text[0].upper() + text[1:]
    return text


def is_only_filler(raw: str, clean: str) -> bool:
    """True when a line had filler removed and nothing meaningful is left ("Um. Uh. Yeah.")."""
    words = re.findall(r"[a-z']+", clean.lower())
    return clean != raw.strip() and all(w in BACKCHANNEL for w in words)


@dataclass
class Utterance:
    index: int
    timestamp: str        # HH:MM:SS
    speaker_label: str    # as written in the transcript
    speaker: str          # resolved full name
    role: str             # host | guest | unknown
    section: str | None
    text: str             # clean text: what the agent sees, quotes and counts
    raw_text: str         # exactly as in the file, never modified
    flags: list[str] = field(default_factory=list)

    @property
    def clean_text(self) -> str:
        return self.text


@dataclass
class Episode:
    episode_id: str
    title: str
    host: str | None
    guests: list[str]
    utterances: list[Utterance]
    source_path: str
    cleaning_edits: int = 0
    speakers: list[str] = field(default_factory=list)
    ingest_report: dict = field(default_factory=dict)

    def find(self, timestamp: str) -> list[Utterance]:
        """All lines at a timestamp, in file order (duplicates are kept)."""
        ts = normalize_timestamp(timestamp)
        return [u for u in self.utterances if u.timestamp == ts]

    def render_for_prompt(self) -> str:
        lines = []
        for u in self.utterances:
            section = f" ({u.section})" if u.section else ""
            note = f"  [ingest note: {'; '.join(u.flags)}]" if u.flags else ""
            lines.append(f"[{u.timestamp}] {u.speaker} [{u.role}]{section}: {u.text}{note}")
        return "\n".join(lines)


def _tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", name.lower()) if t not in HONORIFICS]


def _resolve_speaker(label: str, host: str | None, guests: list[str]) -> tuple[str, str]:
    """Map a label to a full name from the header: exact, then first-name, last-name or
    honorific + surname, case-insensitive. Unknown labels are kept as written."""
    lab = label.strip()
    low = lab.lower()
    if low == "host" and host:
        return host, "host"
    if low == "guest" and len(guests) == 1:
        return guests[0], "guest"
    people = ([(host, "host")] if host else []) + [(g, "guest") for g in guests]
    for name, role in people:
        if name.lower() == low:
            return name, role
    label_tokens = _tokens(lab)
    if label_tokens:
        for name, role in people:
            name_tokens = _tokens(name)
            if name_tokens and (label_tokens == name_tokens or
                                (len(label_tokens) == 1 and label_tokens[0] in (name_tokens[0], name_tokens[-1]))):
                return name, role
    return lab, "unknown"


def _load_json(path: Path) -> tuple[dict, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"{path.name}: invalid JSON ({exc})") from exc
    rows = data.get("transcript")
    if not isinstance(rows, list) or not rows:
        raise TranscriptError(f"{path.name}: missing non-empty 'transcript' list")
    return data, rows


def _load_txt(path: Path) -> tuple[dict, list[dict]]:
    meta: dict = {}
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip():
            continue
        if not rows and (m := META_RE.match(raw)):        # header lines come before the first utterance
            key, val = m["key"].lower(), m["value"].strip()
            if key.startswith("guest"):
                meta["guests"] = [g.strip() for g in val.split(",") if g.strip()]
            else:
                meta[key] = val
        elif m := LINE_RE.match(raw):
            rows.append({"timestamp": m["ts"], "speaker": m["speaker"].strip(), "text": m["text"].strip()})
        elif rows:                                        # continuation of the previous speaker's line
            rows[-1]["text"] += " " + raw.strip()
    if not rows:
        raise TranscriptError(f"{path.name}: no 'Speaker: text' lines found")
    return meta, rows


def load_episode(path: str | Path) -> Episode:
    path = Path(path)
    if path.suffix.lower() == ".json":
        meta, rows = _load_json(path)
    elif path.suffix.lower() in {".txt", ".md"}:
        meta, rows = _load_txt(path)
    else:
        raise TranscriptError(f"{path.name}: unsupported file type (use .json or .txt)")

    host = meta.get("host")
    guests = list(meta.get("guests") or [])
    utterances: list[Utterance] = []
    dropped: list[dict] = []
    speaker_map: dict[str, str] = {}
    edits = 0
    last_ts = None
    for i, row in enumerate(rows):
        flags: list[str] = []
        ts_raw = row.get("timestamp")
        if ts_raw in (None, ""):
            ts = last_ts or "00:00:00"
            flags.append(f"no timestamp in file; using {ts} from the previous line")
        else:
            try:
                ts = normalize_timestamp(str(ts_raw))
            except ValueError as exc:
                raise TranscriptError(f"{path.name}: row {i}: {exc}") from exc
        last_ts = ts
        raw_text = str(row.get("text", "")).strip()
        text = clean_text(raw_text)
        removed = [d.strip() for d in STAGE_RE.findall(raw_text.translate(ASCII_MAP))]
        if removed:
            flags.append("stage direction removed: " + ", ".join(removed))
        label = str(row.get("speaker", "Unknown"))
        if not text or is_only_filler(raw_text, text):
            dropped.append({"timestamp": ts, "speaker": label, "raw_text": raw_text})
            continue
        edits += text != re.sub(r"\s+", " ", raw_text.translate(ASCII_MAP)).strip()  # real edits, not quote style
        speaker, role = _resolve_speaker(label, host, guests)
        speaker_map[label] = speaker
        if role == "unknown" and (host or guests):
            flags.append(f"speaker '{label}' not in the header")
        utterances.append(Utterance(len(utterances), ts, label, speaker, role, row.get("section"),
                                    text, raw_text, flags))
    if not utterances:
        raise TranscriptError(f"{path.name}: nothing left after cleaning")

    speakers = list(dict.fromkeys(u.speaker for u in utterances))
    if not host and not guests:                           # no header: speakers found become the guests
        guests = speakers
    report = {
        "lines_parsed": len(rows),
        "lines_kept": len(utterances),
        "lines_cleaned": edits,
        "lines_flagged": sum(bool(u.flags) for u in utterances),
        "lines_dropped": dropped,
        "speakers_mapped": speaker_map,
        "flags": [{"timestamp": u.timestamp, "speaker": u.speaker, "flags": u.flags}
                  for u in utterances if u.flags],
    }
    return Episode(
        episode_id=str(meta.get("episode_id") or path.stem.split("_")[0]),
        title=str(meta.get("title") or path.stem.replace("_", " ")),
        host=host,
        guests=guests,
        utterances=utterances,
        source_path=str(path),
        cleaning_edits=edits,
        speakers=speakers,
        ingest_report=report,
    )
