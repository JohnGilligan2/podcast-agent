"""Write report.json and a human-readable report.md for each episode, plus a run index."""

from __future__ import annotations

import json
from pathlib import Path

from .schemas import EpisodeReport


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


TIER_TAGS = {"pinned": "📌 pinned", "reference": "📚 KB", "trusted_web": "🛡️ trusted web",
             "other_web": "🌐 other web"}


def _evidence(e) -> str:
    link = f"[{e.source_id}]({e.url})" if e.url and e.url != e.source_id else (
        f"[{e.title}]({e.url})" if e.url else e.source_id)
    return f"{link} ({TIER_TAGS.get(e.tier or '', e.tier or '?')})"


def to_markdown(r: EpisodeReport) -> str:
    out: list[str] = [f"# {r.title}", ""]
    people = ", ".join(filter(None, [f"**Host:** {r.host}" if r.host else None,
                                     f"**Guests:** {', '.join(r.guests)}" if r.guests else None]))
    out += [f"`{r.episode_id}` · {people} · status: **{r.status}** · client `{r.client_id}` "
            f"(source policy `{r.source_policy_version}`)", ""]
    if r.error:
        out += [f"> ⚠️ Run error: {r.error}", ""]

    if r.summary:
        out += ["## Summary", "", r.summary.text, "", f"*{r.summary.word_count} words*", "",
                "| Core themes | Key discussions | Outcomes & opinions |", "|---|---|---|"]
        rows = max(len(r.summary.core_themes), len(r.summary.key_discussions), len(r.summary.outcomes))
        for i in range(rows):
            cols = [lst[i] if i < len(lst) else "" for lst in
                    (r.summary.core_themes, r.summary.key_discussions, r.summary.outcomes)]
            out.append("| " + " | ".join(_cell(c) for c in cols) + " |")
        out.append("")

    if r.key_notes:
        out += ["## 🔹 Top 5 takeaways", ""]
        out += [f"{i}. {t}" for i, t in enumerate(r.key_notes.takeaways, 1)]
        out += ["", "## 💬 Notable quotes", ""]
        for q in r.key_notes.quotes:
            out += [f"> “{q.quote}”", f"> — **{q.speaker}**, `{q.timestamp}`", "",
                    f"*Why it matters:* {q.why_notable}", ""]
            if q.raw_line and q.raw_line.strip() != q.quote.strip():
                out += [f"<sub>Original line in the file: “{q.raw_line.strip()}”</sub>", ""]
        out += ["## 🧭 Topics", "", " ".join(f"`#{t}`" for t in r.key_notes.topics), ""]

    if r.fact_checks:
        counts: dict[str, int] = {}
        for f in r.fact_checks:
            counts[f.verdict_label] = counts.get(f.verdict_label, 0) + 1
        out += ["## Fact-check", "", " · ".join(f"{k}: **{v}**" for k, v in counts.items()), "",
                "| # | Claim | Speaker · time | Verification | Confidence | Evidence | Human review |",
                "|---|---|---|---|---|---|---|"]
        for f in r.fact_checks:
            ev = "<br>".join(_cell(_evidence(e)) for e in f.evidence) or "—"
            review = f"🔎 {_cell(f.review_reason or 'yes')}" if f.needs_human_review else "—"
            out.append(f"| {f.claim_id} | {_cell(f.claim)} | {f.speaker} · `{f.timestamp}` | "
                       f"{f.verdict_label} | {f.confidence:.2f} | {ev} | {review} |")
        out += ["", "<details><summary>Fact-check reasoning</summary>", ""]
        for f in r.fact_checks:
            out += [f"- **{f.claim_id}** ({f.claim_type}) — {f.rationale}"]
        out += ["", "</details>", ""]

    if r.reviewer_notes:
        out += ["## 📝 Notes for the human editor", ""] + [f"- {n}" for n in r.reviewer_notes] + [""]

    ing = r.ingest
    if ing:
        out += ["<details><summary>Transcript cleaning</summary>", "",
                f"{ing['lines_parsed']} lines parsed · {ing['lines_kept']} kept · {ing['lines_cleaned']} had filler "
                f"removed · {len(ing['lines_dropped'])} dropped · {ing['lines_flagged']} flagged", ""]
        if ing.get("speakers_mapped"):
            out += ["Speakers: " + ", ".join(f"`{k}` → {v}" for k, v in ing["speakers_mapped"].items()), ""]
        out += [f"- Dropped `{d['timestamp']}` {d['speaker']}: “{d['raw_text']}”" for d in ing["lines_dropped"]]
        out += [f"- Flagged `{f['timestamp']}` {f['speaker']}: {'; '.join(f['flags'])}" for f in ing.get("flags", [])]
        out += ["", "</details>", ""]

    if r.plan:
        out += ["<details><summary>Agent plan</summary>", ""]
        out += [f"{p.step}. **{p.action}** (`{p.tool}`) — {p.rationale}" for p in r.plan]
        out += ["", "</details>", ""]

    m = r.metrics
    out += ["---", f"*Generated {r.generated_at} by `{m.model}` (effort {m.effort}) · {m.model_turns} model turns · "
            f"{m.tool_calls} tool calls ({m.rejected_tool_calls} rejected by guardrails) · {m.searches} KB searches · "
            f"{m.web_searches} web searches ({m.blocked_results_dropped} blocked results dropped) · "
            f"≈${m.estimated_cost_usd} · {m.duration_seconds}s*", ""]
    return "\n".join(out)


def write_episode(report: EpisodeReport, out_dir: Path) -> Path:
    ep_dir = out_dir / report.episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (ep_dir / "report.md").write_text(to_markdown(report), encoding="utf-8")
    return ep_dir


def write_index(reports: list[EpisodeReport], out_dir: Path) -> Path:
    lines = ["# Podcast agent run", "",
             "| Episode | Title | Status | Claims ✅ / ⚠️ / ❓ | Needs review | Web searches | Turns | "
             "Rejected calls | Cost (USD) | Report |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    total = 0.0
    for r in reports:
        v = [sum(f.verdict == k for f in r.fact_checks)
             for k in ("verified", "possibly_outdated_or_inaccurate", "unverifiable")]
        cost = r.metrics.estimated_cost_usd or 0.0
        total += cost
        lines.append(f"| {r.episode_id} | {_cell(r.title)} | {r.status} | {v[0]} / {v[1]} / {v[2]} | "
                     f"{sum(f.needs_human_review for f in r.fact_checks)} | {r.metrics.web_searches} | "
                     f"{r.metrics.model_turns} | {r.metrics.rejected_tool_calls} | {cost:.3f} | "
                     f"[report.md]({r.episode_id}/report.md) |")
    clients = sorted({f"`{r.client_id}` (policy `{r.source_policy_version}`)" for r in reports})
    lines += ["", f"**Client source policy:** {', '.join(clients)}", "",
              f"**Total estimated cost:** ${total:.3f}", ""]
    path = out_dir / "index.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "all_reports.json").write_text(
        json.dumps([json.loads(r.model_dump_json()) for r in reports], indent=2, ensure_ascii=False),
        encoding="utf-8")
    return path
