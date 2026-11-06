"""Golden-set evaluation and release-to-release regression report.

    python -m podcast_agent eval output/ --baseline examples/sample_run --md regression.md

The golden set (data/golden/golden.json) lists, per episode, the claims a careful editor expects
the agent to find and the verdict(s) an editor accepts for each. A run is scored on:

- claim recall      share of golden claims the run registered
- verdict accuracy  share of found golden claims whose verdict is one the editor accepts
- quote validity    share of quotes that still match their transcript line word for word
- completion        every episode finished

With --baseline (the last release's run), the report lists every verdict that changed and the
cost and time differences, and the gate fails if verdict accuracy dropped. CI runs this on every
pull request and posts the markdown on the PR.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT
from .ingest import load_episode
from .schemas import VERDICT_LABELS, EpisodeReport, FactCheck
from .tools import quote_matches

DEFAULT_GOLDEN = PROJECT_ROOT / "data" / "golden" / "golden.json"
ICON = {k: v.split()[0] for k, v in VERDICT_LABELS.items()}


def load_reports(path: str | Path) -> dict[str, EpisodeReport]:
    """Reports from all_reports.json, a folder holding it, or a folder of <episode>/report.json."""
    p = Path(path)
    if p.is_dir() and (p / "all_reports.json").exists():
        p = p / "all_reports.json"
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        reports = [EpisodeReport.model_validate(r) for r in items]
    elif p.is_dir():
        reports = [EpisodeReport.model_validate_json(f.read_text(encoding="utf-8")) for f in sorted(p.glob("*/report.json"))]
    else:
        raise ValueError(f"no reports found at {p}")
    return {r.episode_id: r for r in reports}


def _find(claim_spec: dict, checks: list[FactCheck]) -> FactCheck | None:
    words = [w.lower() for w in claim_spec["match"]]
    for f in checks:
        text = f.claim.lower()
        if all(w in text for w in words):
            return f
    return None


@dataclass
class Score:
    golden_claims: int = 0
    found: int = 0
    correct: int = 0
    quotes: int = 0
    quotes_valid: int = 0
    episodes: int = 0
    complete: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    misses: list[str] = field(default_factory=list)       # human-readable problems
    verdicts: dict[str, str] = field(default_factory=dict)  # golden claim key -> verdict found

    @property
    def recall(self) -> float:
        return self.found / self.golden_claims if self.golden_claims else 1.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.found if self.found else 0.0

    @property
    def quote_validity(self) -> float:
        return self.quotes_valid / self.quotes if self.quotes else 1.0


def score(reports: dict[str, EpisodeReport], golden: dict, root: Path = PROJECT_ROOT) -> Score:
    s = Score()
    for eid, spec in golden["episodes"].items():
        r = reports.get(eid)
        s.golden_claims += len(spec["claims"])
        if r is None:
            s.misses.append(f"{eid}: no report in this run")
            continue
        s.episodes += 1
        s.complete += r.status == "complete"
        s.cost += r.metrics.estimated_cost_usd or 0.0
        s.seconds += r.metrics.duration_seconds
        if r.status != "complete":
            s.misses.append(f"{eid}: status {r.status}")
        for c in spec["claims"]:
            key = f"{eid}/{c['id']}"
            f = _find(c, r.fact_checks)
            if f is None:
                s.misses.append(f"{key}: claim not registered (expected one mentioning {c['match']})")
                continue
            s.found += 1
            s.verdicts[key] = f.verdict
            if f.verdict in c["verdicts"]:
                s.correct += 1
            else:
                s.misses.append(f"{key}: verdict {f.verdict}, editor accepts {c['verdicts']}")
        if r.key_notes:
            episode = load_episode(root / spec["transcript"])
            for q in r.key_notes.quotes:
                s.quotes += 1
                lines = episode.find(q.timestamp)
                if any(quote_matches(q.quote, u.text) for u in lines):
                    s.quotes_valid += 1
                else:
                    s.misses.append(f"{eid}: quote at {q.timestamp} no longer matches the transcript")
    return s


def gate(new: Score, golden: dict, baseline: Score | None = None) -> list[str]:
    g = golden.get("gates", {})
    failures = []
    if new.recall < g.get("min_claim_recall", 0.8):
        failures.append(f"claim recall {new.recall:.0%} is below {g.get('min_claim_recall', 0.8):.0%}")
    if new.accuracy < g.get("min_verdict_accuracy", 0.8):
        failures.append(f"verdict accuracy {new.accuracy:.0%} is below {g.get('min_verdict_accuracy', 0.8):.0%}")
    if new.quote_validity < g.get("min_quote_validity", 1.0):
        failures.append(f"quote validity {new.quote_validity:.0%} is below {g.get('min_quote_validity', 1.0):.0%}")
    if new.complete < new.episodes:
        failures.append(f"{new.episodes - new.complete} episode(s) did not complete")
    if baseline is not None and new.accuracy < baseline.accuracy - 1e-9:
        failures.append(f"verdict accuracy dropped from {baseline.accuracy:.0%} to {new.accuracy:.0%}")
    return failures


def report_markdown(new: Score, failures: list[str], baseline: Score | None = None,
                    labels: tuple[str, str] = ("this change", "last release")) -> str:
    def row(name, fmt, a, b=None):
        return f"| {name} | {fmt(a)} | {fmt(b) if b is not None else '—'} |"
    pct, money, secs = (lambda v: f"{v:.0%}"), (lambda v: f"${v:.3f}"), (lambda v: f"{v:.0f}s")
    b = baseline
    out = ["## Golden-set regression report", "",
           f"**{'❌ FAIL' if failures else '✅ PASS'}**" + (": " + "; ".join(failures) if failures else ""), "",
           f"| Metric | {labels[0]} | {labels[1]} |", "|---|---|---|",
           row("Claim recall", pct, new.recall, b and b.recall),
           row("Verdict accuracy", pct, new.accuracy, b and b.accuracy),
           row("Quote validity", pct, new.quote_validity, b and b.quote_validity),
           row("Episodes complete", lambda v: f"{v}", new.complete, b and b.complete),
           row("Total cost", money, new.cost, b and b.cost),
           row("Total run time", secs, new.seconds, b and b.seconds), ""]
    if b is not None:
        changed = [(k, b.verdicts.get(k), v) for k, v in new.verdicts.items() if b.verdicts.get(k) != v]
        changed += [(k, v, None) for k, v in b.verdicts.items() if k not in new.verdicts]
        out += ["### Verdicts that changed", ""]
        if changed:
            out += ["| Golden claim | Before | After |", "|---|---|---|"]
            out += [f"| {k} | {ICON.get(old or '', '—')} {old or 'not found'} | {ICON.get(nw or '', '—')} {nw or 'not found'} |"
                    for k, old, nw in sorted(changed)]
        else:
            out.append("None.")
        out.append("")
    if new.misses:
        out += ["### Problems in this run", ""] + [f"- {m}" for m in new.misses] + [""]
    return "\n".join(out)


def run_eval(reports_path: str | Path, golden_path: str | Path = DEFAULT_GOLDEN,
             baseline_path: str | Path | None = None) -> tuple[bool, str]:
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    new = score(load_reports(reports_path), golden)
    base = score(load_reports(baseline_path), golden) if baseline_path else None
    failures = gate(new, golden, base)
    return not failures, report_markdown(new, failures, base)
