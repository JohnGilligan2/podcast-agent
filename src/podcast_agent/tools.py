"""Tools the agent can call, and the guardrails that validate each call.

Every deliverable is submitted through a tool. Each tool validates its input against the
transcript and the agent's own retrieval history; a failed check raises ToolRejected, which
goes back to the model as an `is_error` tool_result so it can correct itself. That
self-correction loop is the main reliability mechanism: the model never "free-writes" the
final report, and nothing that fails a check reaches the output.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ingest import HONORIFICS, Episode, normalize_timestamp
from .knowledge_base import KnowledgeBase
from .schemas import (
    VERDICT_LABELS,
    Claim,
    Evidence,
    FactCheck,
    KeyNotes,
    PlanStep,
    Quote,
    Summary,
)
from .sources import TRUSTED_TIERS, UNTRUSTED_MAX_CONFIDENCE, ClientSources

SUMMARY_MIN_WORDS, SUMMARY_MAX_WORDS = 200, 300
TWEET_MAX_CHARS, TWEET_MAX_HASHTAGS = 280, 2
HASHTAG_RE = re.compile(r"#\w+")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
# X counts characters by weight, not by len(): code points in these ranges (Latin, Greek, Cyrillic,
# Hebrew, Arabic, Indic scripts and common punctuation) count 1 and everything else, such as emoji
# and Chinese/Japanese/Korean, counts 2 (docs.x.com/fundamentals/counting-characters, twitter-text v3).
_X_WEIGHT_1_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def x_weighted_length(text: str) -> int:
    """Length as X counts it. Slightly conservative: a multi-part emoji (e.g. a family or a skin
    tone) is counted per code point, so the result can be a little high, never too low."""
    text = unicodedata.normalize("NFC", text)
    return sum(1 if any(lo <= ord(ch) <= hi for lo, hi in _X_WEIGHT_1_RANGES) else 2 for ch in text)
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")


class ToolRejected(Exception):
    """Validation failure; the message is sent back to the model as an error tool_result."""


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def _norm(text: str) -> str:
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[^\w\s'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_matches(quote: str, source: str) -> bool:
    """True if `quote` appears verbatim in `source` (case/punctuation-insensitive).
    An ellipsis may join fragments, which must appear in order."""
    fragments = [_norm(f) for f in re.split(r"\.\.\.|…", quote) if _norm(f)]
    if not fragments:
        return False
    hay = f" {_norm(source)} "          # padded so fragments match whole words only ("art" ≠ "startup")
    pos = 0
    for frag in fragments:
        idx = hay.find(f" {frag} ", pos)
        if idx < 0:
            return False
        pos = idx + len(frag) + 1       # keep the trailing space for the next fragment
    return True


def slugify_tag(tag: str) -> str:
    tag = re.sub(r"[^a-z0-9]+", "-", tag.lower().lstrip("#")).strip("-")
    return tag


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[^\W\d_]+", name.lower()) if t not in HONORIFICS}


def _speaker_matches(given: str, actual: str, label: str) -> bool:
    """Whole-name-word match: "Mark", "Dr. Priya" and "Priya Patel" match "Dr. Priya Patel"; "n" does not."""
    g = _name_tokens(given)
    return bool(g) and (g <= _name_tokens(actual) or g <= _name_tokens(label))


# --------------------------------------------------------------------------------------
# Tool definitions sent to the API. `strict: True` guarantees the inputs match the schema;
# the semantic checks (word counts, verbatim quotes, evidence provenance) happen below.
# --------------------------------------------------------------------------------------

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or list(props), "additionalProperties": False}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "create_plan",
        "description": "Record your step-by-step plan for processing this episode. Call this first. "
                       "You may call it again later to revise the plan if you change approach.",
        "input_schema": _obj({"steps": {"type": "array", "items": _obj({
            "action": STR, "tool": STR, "rationale": STR})}}),
    },
    {
        "name": "submit_summary",
        "description": f"Submit the episode summary ({SUMMARY_MIN_WORDS}-{SUMMARY_MAX_WORDS} words of prose) "
                       "plus short lists of core themes, key discussions and outcomes/opinions. "
                       "Rejected with a reason if it is out of range; resubmit to replace.",
        "input_schema": _obj({"summary": STR, "core_themes": STR_LIST,
                              "key_discussions": STR_LIST, "outcomes": STR_LIST}),
    },
    {
        "name": "submit_key_notes",
        "description": "Submit show notes: exactly 5 takeaways, 2-5 notable quotes copied verbatim from the "
                       "transcript (with the line's timestamp and speaker), and 3-8 topic tags. "
                       "Quotes are checked against the transcript; use '...' to join trimmed fragments.",
        "input_schema": _obj({
            "takeaways": STR_LIST,
            "quotes": {"type": "array", "items": _obj({
                "timestamp": STR, "speaker": STR, "quote": STR, "why_notable": STR})},
            "topics": STR_LIST,
        }),
    },
    {
        "name": "register_claims",
        "description": "Register the check-worthy claims made in the episode so each can be fact-checked. "
                       "Returns an id for each claim. Can be called more than once; claims accumulate.",
        "input_schema": _obj({"claims": {"type": "array", "items": _obj({
            "claim": STR, "speaker": STR, "timestamp": STR,
            "claim_type": {"type": "string", "enum": ["factual", "statistic", "prediction", "anecdote"]},
        })}}),
    },
    {
        "name": "search_knowledge_base",
        "description": "Keyword (BM25) search over the client's pinned facts (tier 'pinned') and the curated "
                       "reference knowledge base (tier 'reference'). Returns up to 4 documents with source_id, "
                       "tier, as_of date and content. Use specific entity and topic words; try a reformulated "
                       "query if the first one misses.",
        "input_schema": _obj({"query": STR}),
    },
    {
        "name": "record_verdict",
        "description": "Record the fact-check verdict for one registered claim. evidence_ids must be "
                       "source_ids (or URLs) returned by your searches in this session. Set "
                       "needs_human_review (with a short review_reason) when sources conflict, e.g. a pinned "
                       "client fact disagrees with a live result. Re-recording a claim replaces the earlier verdict.",
        "input_schema": _obj({
            "claim_id": STR,
            "verdict": {"type": "string", "enum": list(VERDICT_LABELS)},
            "confidence": {"type": "number"},
            "rationale": STR,
            "evidence_ids": STR_LIST,
            "needs_human_review": {"type": "boolean"},
            "review_reason": STR,
        }),
    },
    {
        "name": "submit_tweet",
        "description": f"Submit a tweet-sized summary of the episode for social media: at most {TWEET_MAX_CHARS} "
                       f"characters, at most {TWEET_MAX_HASHTAGS} hashtags, no links. Submit it after every "
                       "registered claim has a verdict, and never state as fact a claim you did not verify. "
                       "Rejected with a reason if it breaks a rule; resubmit to replace.",
        "input_schema": _obj({"tweet": STR}),
    },
    {
        "name": "finish",
        "description": "Call when every deliverable is submitted and every registered claim has a verdict. "
                       "reviewer_notes: anything a human editor should check (likely transcription errors, "
                       "ambiguous speakers, claims needing expert review). Rejected if anything is missing.",
        "input_schema": _obj({"reviewer_notes": STR_LIST}),
    },
]

for _tool in TOOL_DEFINITIONS:
    _tool["strict"] = True


@dataclass
class AgentState:
    """Everything the agent has produced so far for one episode."""
    plan: list[PlanStep] = field(default_factory=list)
    summary: Summary | None = None
    key_notes: KeyNotes | None = None
    tweet: str | None = None
    claims: dict[str, Claim] = field(default_factory=dict)
    verdicts: dict[str, FactCheck] = field(default_factory=dict)
    seen_sources: dict[str, Evidence] = field(default_factory=dict)  # provenance whitelist
    reviewer_notes: list[str] = field(default_factory=list)
    claims_registered: bool = False
    finished: bool = False
    searches: int = 0
    web_searches: int = 0
    web_results_returned: int = 0   # result blocks received; should end equal to web_searches
    blocked_results_dropped: int = 0

    def missing(self) -> list[str]:
        out = []
        if not self.plan:
            out.append("plan (create_plan)")
        if not self.summary:
            out.append("summary (submit_summary)")
        if not self.key_notes:
            out.append("key notes (submit_key_notes)")
        if not self.claims_registered:
            out.append("claims (register_claims)")
        if not self.tweet:
            out.append("tweet (submit_tweet)")
        pending = [cid for cid in self.claims if cid not in self.verdicts]
        if pending:
            out.append(f"verdicts for {', '.join(pending)} (record_verdict)")
        return out


class ToolExecutor:
    def __init__(self, episode: Episode, kb: KnowledgeBase, sources: ClientSources | None = None):
        self.episode = episode
        self.kb = kb
        self.sources = sources or ClientSources()
        self.state = AgentState()
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "create_plan": self.create_plan,
            "submit_summary": self.submit_summary,
            "submit_key_notes": self.submit_key_notes,
            "submit_tweet": self.submit_tweet,
            "register_claims": self.register_claims,
            "search_knowledge_base": self.search_knowledge_base,
            "record_verdict": self.record_verdict,
            "finish": self.finish,
        }

    def execute(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolRejected(f"Unknown tool '{name}'.")
        result = handler(args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    # Server-side web_search results are captured so they can be cited as evidence. Results on a
    # blocked domain never enter the whitelist, so they can never be cited, whatever the model says.
    def register_web_results(self, results: list[dict]) -> list[str]:
        dropped = []
        for r in results:
            url = r.get("url")
            if not url:
                continue
            if self.sources.is_blocked(url):
                dropped.append(url)
                continue
            if url not in self.state.seen_sources:
                self.state.seen_sources[url] = Evidence(source_id=url, title=r.get("title") or url, url=url,
                                                        as_of=r.get("page_age"), tier=self.sources.web_tier(url))
        self.state.blocked_results_dropped += len(dropped)
        return dropped

    # ---- handlers ---------------------------------------------------------------------

    def create_plan(self, args: dict) -> str:
        steps = args.get("steps") or []
        if not steps:
            raise ToolRejected("Plan has no steps.")
        self.state.plan = [PlanStep(step=i + 1, **s) for i, s in enumerate(steps)]
        return f"Plan recorded ({len(steps)} steps). Proceed."

    def submit_summary(self, args: dict) -> str:
        text = args["summary"].strip()
        words = count_words(text)
        problems = []
        if not SUMMARY_MIN_WORDS <= words <= SUMMARY_MAX_WORDS:
            problems.append(f"summary is {words} words; it must be {SUMMARY_MIN_WORDS}-{SUMMARY_MAX_WORDS}")
        if len(args["core_themes"]) < 2:
            problems.append("give at least 2 core_themes")
        if not args["key_discussions"]:
            problems.append("key_discussions is empty")
        if not args["outcomes"]:
            problems.append("outcomes is empty")
        if problems:
            raise ToolRejected("Summary rejected: " + "; ".join(problems) + ". Revise and resubmit.")
        self.state.summary = Summary(text=text, word_count=words, core_themes=args["core_themes"],
                                     key_discussions=args["key_discussions"], outcomes=args["outcomes"])
        return f"Summary accepted ({words} words)."

    def submit_tweet(self, args: dict) -> str:
        """The tweet goes out publicly, so it is written last: only once every claim has a verdict,
        so Claude can leave out anything it could not verify."""
        tweet = " ".join(args["tweet"].split())
        problems = []
        if not tweet:
            problems.append("tweet is empty")
        length = x_weighted_length(tweet)
        if length > TWEET_MAX_CHARS:
            problems.append(f"tweet is {length} characters as X counts them (emoji count 2); "
                            f"the limit is {TWEET_MAX_CHARS}")
        hashtags = HASHTAG_RE.findall(tweet)
        if len(hashtags) > TWEET_MAX_HASHTAGS:
            problems.append(f"{len(hashtags)} hashtags; use at most {TWEET_MAX_HASHTAGS}")
        if URL_RE.search(tweet):
            problems.append("remove the link; links are added at publishing time")
        if not self.state.claims_registered:
            problems.append("register_claims first; the tweet is written after fact-checking")
        pending = [cid for cid in self.state.claims if cid not in self.state.verdicts]
        if pending:
            problems.append(f"record verdicts for {', '.join(pending)} first, so the tweet reflects the fact-check")
        if problems:
            raise ToolRejected("Tweet rejected: " + "; ".join(problems) + ". Revise and resubmit.")
        self.state.tweet = tweet
        return f"Tweet accepted ({x_weighted_length(tweet)} of {TWEET_MAX_CHARS} characters)."

    def submit_key_notes(self, args: dict) -> str:
        problems: list[str] = []
        takeaways = [t.strip() for t in args["takeaways"] if t.strip()]
        if len(takeaways) != 5:
            problems.append(f"need exactly 5 takeaways, got {len(takeaways)}")
        quotes_in = args["quotes"]
        if not 2 <= len(quotes_in) <= 5:
            problems.append(f"need 2-5 quotes, got {len(quotes_in)}")
        quotes: list[Quote] = []
        for q in quotes_in:
            try:
                ts = normalize_timestamp(q["timestamp"])
            except ValueError:
                problems.append(f"quote timestamp {q['timestamp']!r} is not a valid timestamp")
                continue
            lines = self.episode.find(ts)
            if not lines:
                problems.append(f"no transcript line at {ts}")
                continue
            # Several lines can share a timestamp (crosstalk); the quote must match one of them.
            matching = [ln for ln in lines if quote_matches(q["quote"], ln.text)]
            if not matching:
                reads = " / ".join(f"{ln.speaker}: \"{ln.text}\"" for ln in lines)
                problems.append(f"quote at {ts} is not verbatim. The line{'s' if len(lines) > 1 else ''} "
                                f"at that time read{'' if len(lines) > 1 else 's'}: {reads}")
                continue
            line = next((ln for ln in matching if _speaker_matches(q["speaker"], ln.speaker, ln.speaker_label)),
                        matching[0])
            if not _speaker_matches(q["speaker"], line.speaker, line.speaker_label):
                problems.append(f"quote at {ts} is spoken by {line.speaker}, not {q['speaker']}")
                continue
            quotes.append(Quote(timestamp=ts, speaker=line.speaker, quote=q["quote"].strip(),
                                why_notable=q["why_notable"], raw_line=line.raw_text))
        topics = list(dict.fromkeys(t for t in (slugify_tag(x) for x in args["topics"]) if t))
        if not 3 <= len(topics) <= 8:
            problems.append(f"need 3-8 topic tags, got {len(topics)}")
        if problems:
            raise ToolRejected("Key notes rejected: " + "; ".join(problems) + ". Fix and resubmit all key notes.")
        self.state.key_notes = KeyNotes(takeaways=takeaways, quotes=quotes, topics=topics)
        return f"Key notes accepted: 5 takeaways, {len(quotes)} verified quotes, tags {topics}."

    def register_claims(self, args: dict) -> dict:
        accepted, problems, notes = [], [], []
        for c in args["claims"]:
            try:
                ts = normalize_timestamp(c["timestamp"])
            except ValueError:
                problems.append(f"claim '{c['claim'][:40]}': bad timestamp {c['timestamp']!r}")
                continue
            lines = self.episode.find(ts)
            if not lines:
                problems.append(f"claim '{c['claim'][:40]}': no transcript line at {ts}")
                continue
            cid = f"C{len(self.state.claims) + 1}"
            line = next((ln for ln in lines if _speaker_matches(c["speaker"], ln.speaker, ln.speaker_label)), None)
            if line is None:   # the transcript is the authority on who spoke: use it, and say so
                line = lines[0]
                notes.append(f"{cid}: speaker at {ts} is {line.speaker}, not {c['speaker']!r}; corrected")
            claim = Claim(claim_id=cid, claim=c["claim"].strip(), speaker=line.speaker,
                          timestamp=ts, claim_type=c["claim_type"])
            self.state.claims[cid] = claim
            accepted.append({"claim_id": cid, "claim": claim.claim})
        if problems and not accepted:
            raise ToolRejected("No claims registered: " + "; ".join(problems))
        self.state.claims_registered = True   # only once something (or an explicit empty list) was accepted
        return {"registered": accepted, "rejected": problems, "notes": notes}

    def search_knowledge_base(self, args: dict) -> dict:
        self.state.searches += 1
        hits = self.kb.search(args["query"])
        for h in hits:
            d = h.doc
            self.state.seen_sources[d.id] = Evidence(source_id=d.id, title=d.title, url=d.url, as_of=d.as_of,
                                                     tier=d.tier)
        if not hits:
            return {"query": args["query"], "results": [],
                    "note": "No matching documents. Try other keywords, or treat the claim as unverifiable."}
        return {"query": args["query"], "results": [h.as_dict() for h in hits]}

    def record_verdict(self, args: dict) -> str:
        cid = args["claim_id"].strip()
        claim = self.state.claims.get(cid)
        if claim is None:
            raise ToolRejected(f"Unknown claim_id '{cid}'. Registered: {list(self.state.claims)}")
        conf = float(args["confidence"])
        if not 0.0 <= conf <= 1.0:
            raise ToolRejected("confidence must be between 0 and 1.")
        evidence_ids = list(dict.fromkeys(e.strip() for e in args["evidence_ids"] if e.strip()))
        unknown = [e for e in evidence_ids if e not in self.state.seen_sources]
        if unknown:
            raise ToolRejected(f"Evidence {unknown} was never returned by a search in this session. "
                               "Cite only sources you retrieved.")
        verdict = args["verdict"]
        if verdict == "verified" and not evidence_ids:
            raise ToolRejected("A 'verified' verdict needs at least one retrieved source in evidence_ids. "
                               "Search first, or use 'unverifiable'.")
        if verdict != "verified" and not evidence_ids and conf > 0.7:
            raise ToolRejected("Without retrieved evidence, confidence should not exceed 0.7. Lower it or cite a source.")
        evidence = [self.state.seen_sources[e] for e in evidence_ids]
        blocked = [e.source_id for e in evidence if e.url and self.sources.is_blocked(e.url)]
        if blocked:
            raise ToolRejected(f"Evidence {blocked} is on a domain this client has blocked. Remove it.")
        review = bool(args.get("needs_human_review", False))
        reason = (args.get("review_reason") or "").strip() or None
        # Source policy: a claim backed only by non-trusted web pages is marked down and sent to a person.
        if evidence and not any(e.tier in TRUSTED_TIERS for e in evidence):
            if conf > UNTRUSTED_MAX_CONFIDENCE:
                raise ToolRejected(
                    f"All evidence for {cid} is from domains outside the client's trusted list, so confidence "
                    f"must be at most {UNTRUSTED_MAX_CONFIDENCE}. Lower it, or search for a trusted source.")
            review = True
            reason = reason or "Supported only by sources outside the client's trusted list."
        if review and not reason:
            raise ToolRejected("needs_human_review is true: give a short review_reason for the editor.")
        self.state.verdicts[cid] = FactCheck(
            **claim.model_dump(), verdict=verdict, verdict_label=VERDICT_LABELS[verdict],
            confidence=round(conf, 2), rationale=args["rationale"].strip(), evidence=evidence,
            needs_human_review=review, review_reason=reason if review else None,
        )
        remaining = [c for c in self.state.claims if c not in self.state.verdicts]
        return f"Verdict for {cid} recorded. Claims still without a verdict: {remaining or 'none'}."

    def finish(self, args: dict) -> str:
        missing = self.state.missing()
        if missing:
            raise ToolRejected("Cannot finish yet. Missing: " + "; ".join(missing))
        self.state.reviewer_notes = [n.strip() for n in args["reviewer_notes"] if n.strip()]
        self.state.finished = True
        return "Episode complete."
