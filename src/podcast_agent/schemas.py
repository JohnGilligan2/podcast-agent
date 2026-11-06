"""Output data model. `EpisodeReport` is what gets written to report.json."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["verified", "possibly_outdated_or_inaccurate", "unverifiable"]
ClaimType = Literal["factual", "statistic", "prediction", "anecdote"]

VERDICT_LABELS: dict[str, str] = {
    "verified": "✅ Verified true",
    "possibly_outdated_or_inaccurate": "⚠️ Possibly outdated/inaccurate",
    "unverifiable": "❓ Unverifiable",
}


class PlanStep(BaseModel):
    step: int
    action: str
    tool: str
    rationale: str


class Summary(BaseModel):
    text: str
    word_count: int
    core_themes: list[str]
    key_discussions: list[str]
    outcomes: list[str]


class Quote(BaseModel):
    timestamp: str
    speaker: str
    quote: str
    why_notable: str
    raw_line: str | None = None   # the line exactly as in the file, for audit


class KeyNotes(BaseModel):
    takeaways: list[str]
    quotes: list[Quote]
    topics: list[str]


class Claim(BaseModel):
    claim_id: str
    claim: str
    speaker: str
    timestamp: str
    claim_type: ClaimType


class Evidence(BaseModel):
    source_id: str
    title: str
    url: str | None = None
    as_of: str | None = None
    tier: str | None = None   # pinned | reference | trusted_web | other_web (see sources.py)


class FactCheck(BaseModel):
    claim_id: str
    claim: str
    speaker: str
    timestamp: str
    claim_type: ClaimType
    verdict: Verdict
    verdict_label: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence: list[Evidence]
    needs_human_review: bool = False
    review_reason: str | None = None


class RunMetrics(BaseModel):
    model: str
    effort: str
    model_turns: int = 0
    tool_calls: int = 0
    rejected_tool_calls: int = 0
    searches: int = 0
    web_searches: int = 0
    blocked_results_dropped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: float | None = None
    duration_seconds: float = 0.0
    # Latency breakdown: where the wall time went
    model_seconds: float = 0.0                       # waiting on Claude (incl. server-side web search)
    tool_seconds: float = 0.0                        # running our own tools and guardrails
    turn_seconds: list[float] = []                   # each model call, in order
    tool_seconds_by_name: dict[str, float] = {}


class EpisodeReport(BaseModel):
    episode_id: str
    title: str
    host: str | None
    guests: list[str]
    source_file: str
    client_id: str = "none"
    source_policy_version: str = "none"
    code_version: str = "unknown"     # release that produced this result (semantic version + build)
    prompt_version: str = "unknown"   # fingerprint of the system prompt and tool contracts
    status: Literal["complete", "incomplete", "failed"]
    error: str | None = None
    plan: list[PlanStep] = []
    summary: Summary | None = None
    key_notes: KeyNotes | None = None
    fact_checks: list[FactCheck] = []
    reviewer_notes: list[str] = []
    ingest: dict = {}             # lines parsed / kept / cleaned / dropped / flagged, speaker map
    metrics: RunMetrics
    generated_at: str
