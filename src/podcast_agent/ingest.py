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
ASCII_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "...",
                           "\u00a0": " "})
# Invisible characters that break parsing: NUL, other control chars (not tab), zero-width marks, stray BOMs.
INVISIBLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u2060\ufeff]")
MAX_FILE_BYTES = 5_000_000      # ~10x a 3-hour episode; anything bigger is not a transcript

TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
LINE_RE = re.compile(
    rf"^\s*(?:\[?(?P<ts>{TS})\]?\s*)?"                       # optional timestamp, brackets optional
    r"(?!https?:)(?P<speaker>[^\W\d_][\w.'’ -]{0,40}?)\s*"  # speaker label (any script, not a URL)
    r"(?::|\s[-–—]\s)\s*(?P<text>.*)$")                      # colon or spaced dash, then the text
META_RE = re.compile(r"^\s*#?\s*(?P<key>title|host|guests?|episode_id)\s*:\s*(?P<value>.+)$", re.IGNORECASE)


class TranscriptError(ValueError):
    """Raised when a transcript file cannot be parsed."""


def safe_id(value: str) -> str:
    """An episode id that is safe as a folder name: letters, digits, '.', '_' and '-' only
    ('../../x' -> 'x', 'ep:01' -> 'ep_01')."""
    return re.sub(r"[^\w.-]", "_", value, flags=re.ASCII).strip("._-")[:64]


def normalize_timestamp(ts: str | int | float) -> str:
    """'0:00' -> '00:00:00', '1:05' -> '00:01:05', '75:30' -> '01:15:30', '[00:02:30.500]' -> '00:02:30',
    75 (seconds, as some JSON exports give) -> '00:01:15'. Raises ValueError on garbage."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= 0:
        h, rest = divmod(int(ts), 3600)
        return f"{h:02d}:{rest // 60:02d}:{rest % 60:02d}"
    core = re.split(r"[.,]", str(ts).strip().strip("[]"))[0]
    parts = core.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"bad timestamp {ts!r}")
    nums = [int(p) for p in parts]
    if len(nums) == 2:                       # M:SS — minutes may run past 60 in long episodes
        nums = [*divmod(nums[0], 60), nums[1]]
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
        # The transcript is wrapped in <transcript> tags in the prompt; a line can't close them early.
        return "\n".join(lines).replace("</transcript", "<\\/transcript")


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


def read_text(path: Path) -> str:
    """Decode a transcript whatever encoding it was saved in: UTF-16 (BOM), UTF-8 (with or without BOM),
    else Windows-1252 with undecodable bytes replaced. Never raises on content."""
    try:
        size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        raise TranscriptError(f"{path.name}: cannot read file ({exc.strerror or exc})") from exc
    if size > MAX_FILE_BYTES:
        raise TranscriptError(f"{path.name}: {size / 1e6:.1f} MB is too large for a transcript "
                              f"(limit {MAX_FILE_BYTES / 1e6:.0f} MB)")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _text(value) -> str:
    """JSON values as clean strings: null -> '', numbers -> '5', invisible characters removed."""
    return "" if value is None else INVISIBLE_RE.sub("", str(value)).strip()


def _load_json(path: Path) -> tuple[dict, list[dict]]:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise TranscriptError(f"{path.name}: expected a JSON object with a 'transcript' list")
    rows = [r for r in data.get("transcript") or [] if isinstance(r, dict)] \
        if isinstance(data.get("transcript"), list) else []
    if not rows:
        raise TranscriptError(f"{path.name}: missing non-empty 'transcript' list of objects")
    return data, rows


def _load_txt(path: Path) -> tuple[dict, list[dict]]:
    meta: dict = {}
    rows: list[dict] = []
    for raw in read_text(path).splitlines():
        raw = INVISIBLE_RE.sub("", raw)
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

    host = _text(meta.get("host")) or None
    guests_raw = meta.get("guests") or []
    if isinstance(guests_raw, str):                       # "Alice, Bob" rather than a list
        guests_raw = guests_raw.split(",")
    guests = [g for g in (_text(x) for x in (guests_raw if isinstance(guests_raw, list) else [])) if g]
    utterances: list[Utterance] = []
    dropped: list[dict] = []
    speaker_map: dict[str, str] = {}
    edits = 0
    last_ts = None
    for row in rows:
        flags: list[str] = []
        ts_raw = row.get("timestamp")
        try:
            ts = normalize_timestamp(ts_raw) if ts_raw not in (None, "") else None
        except ValueError:
            ts = None
        if ts is None:                                    # missing or unreadable: keep the line, flag it
            ts = last_ts or "00:00:00"
            what = "no timestamp" if ts_raw in (None, "") else f"unreadable timestamp {str(ts_raw)[:20]!r}"
            flags.append(f"{what} in file; using {ts} from the previous line")
        last_ts = ts
        raw_text = _text(row.get("text"))
        text = clean_text(raw_text)
        removed = [d.strip() for d in STAGE_RE.findall(raw_text.translate(ASCII_MAP))]
        if removed:
            flags.append("stage direction removed: " + ", ".join(removed))
        label = _text(row.get("speaker")) or "Unknown"
        if not text or is_only_filler(raw_text, text):
            dropped.append({"timestamp": ts, "speaker": label, "raw_text": raw_text})
            continue
        edits += text != re.sub(r"\s+", " ", raw_text.translate(ASCII_MAP)).strip()  # real edits, not quote style
        speaker, role = _resolve_speaker(label, host, guests)
        speaker_map[label] = speaker
        if role == "unknown" and (host or guests):
            flags.append(f"speaker '{label}' not in the header")
        utterances.append(Utterance(len(utterances), ts, label, speaker, role, _text(row.get("section")) or None,
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
        episode_id=safe_id(_text(meta.get("episode_id")) or path.stem.split("_")[0]) or safe_id(path.stem) or "episode",
        title=_text(meta.get("title")).replace("\n", " ") or path.stem.replace("_", " "),
        host=host,
        guests=guests,
        utterances=utterances,
        source_path=str(path),
        cleaning_edits=edits,
        speakers=speakers,
        ingest_report=report,
    )
