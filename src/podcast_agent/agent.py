"""The agent loop: plan -> act with tools -> validate -> self-correct -> finish.

    ┌──────────────┐  tool_use   ┌───────────────┐
    │    Claude    │ ──────────▶ │ ToolExecutor  │──▶ guardrails (verbatim quotes, word count,
    │ (plans+acts) │ ◀────────── │ (+ KB search) │    evidence provenance, completeness)
    └──────────────┘ tool_result └───────────────┘
           ▲   is_error=True on a failed check → model revises and resubmits
           └── loop ends when `finish` is accepted, or the step budget runs out

The model decides the order of work and what to search for; code decides what counts as
done. Every turn is logged (summarised reasoning, tool calls, results) to the console and
to a JSONL trace file.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .ingest import Episode
from .knowledge_base import KnowledgeBase
from .llm import LLMClient
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import EpisodeReport, RunMetrics
from .sources import ClientSources
from .tools import TOOL_DEFINITIONS, ToolExecutor, ToolRejected

MAX_NUDGES = 2


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TraceWriter:
    """Append-only JSONL trace of every agent event, for audit and debugging."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8")

    def write(self, event: str, **data: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **data}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class PodcastAgent:
    def __init__(self, llm: LLMClient, kb: KnowledgeBase, settings: Settings,
                 logger: logging.Logger | None = None, today: str | None = None,
                 sources: ClientSources | None = None):
        self.llm = llm
        self.sources = sources or ClientSources()
        self.kb = kb.with_documents(self.sources.pinned_facts)   # pinned client facts are searchable
        self.settings = settings
        self.base_logger = logger or logging.getLogger("podcast_agent")
        self.today = today or date.today().isoformat()

    def _tools(self) -> list[dict]:
        tools = list(TOOL_DEFINITIONS)
        if self.settings.enable_web_search:
            tools.append(self.sources.web_search_tool(self.settings.web_search_max_uses))
        return tools

    def run(self, episode: Episode, trace_path: Path | None = None) -> EpisodeReport:
        log = logging.LoggerAdapter(self.base_logger, {"episode": episode.episode_id})
        trace = TraceWriter(trace_path) if trace_path else None
        executor = ToolExecutor(episode, self.kb, self.sources)
        metrics = RunMetrics(model=self.settings.model, effort=self.settings.effort)
        started = time.monotonic()
        status, error = "incomplete", None

        def emit(event: str, **data: Any) -> None:
            if trace:
                trace.write(event, episode=episode.episode_id, **data)

        log.info("▶ start  %s — %s  (%d lines, %d cleaned of filler) client=%s policy=%s web=%s",
                 episode.episode_id, episode.title, len(episode.utterances), episode.cleaning_edits,
                 self.sources.client_id, self.sources.version, self.settings.enable_web_search)
        emit("start", title=episode.title, utterances=len(episode.utterances), model=self.settings.model,
             client=self.sources.client_id, source_policy_version=self.sources.version,
             web_search=self.settings.enable_web_search)
        ing = episode.ingest_report
        if ing:
            log.info("  🧹 ingest: %d lines parsed, %d kept, %d cleaned, %d dropped, %d flagged; speakers %s",
                     ing["lines_parsed"], ing["lines_kept"], ing["lines_cleaned"], len(ing["lines_dropped"]),
                     ing["lines_flagged"], _short(ing["speakers_mapped"], 200))
            for d in ing["lines_dropped"]:
                log.info("     dropped filler-only line %s %s: %r", d["timestamp"], d["speaker"], d["raw_text"])
            emit("ingest", **ing)

        messages: list[dict] = [{"role": "user", "content": build_user_prompt(
            episode, self.today, self.settings.enable_web_search, self.sources)}]
        tools = self._tools()
        nudges = 0

        try:
            for step in range(1, self.settings.max_steps + 1):
                response = self.llm.create(system=SYSTEM_PROMPT, tools=tools, messages=messages)
                metrics.model_turns += 1
                self._add_usage(metrics, response)
                # Append the full content unchanged (thinking blocks included) so the next turn is valid.
                messages.append({"role": "assistant", "content": response.content})
                self._log_response(log, emit, step, response, executor)

                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    raise RuntimeError(f"model declined the request: {getattr(details, 'category', None)}")
                if response.stop_reason == "pause_turn":   # long server-tool turn; send it back to continue
                    continue

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    if response.stop_reason == "max_tokens":
                        messages.append({"role": "user", "content": "Your last response hit the output limit. "
                                         "Continue with smaller tool calls."})
                        continue
                    nudges += 1
                    missing = executor.state.missing()
                    if nudges > MAX_NUDGES:
                        raise RuntimeError(f"agent stopped without finishing; missing {missing}")
                    log.warning("  ↺ step %d: model stopped early; nudging (missing: %s)", step, missing)
                    emit("nudge", step=step, missing=missing)
                    messages.append({"role": "user", "content": "You have not called finish. Still missing: "
                                     + "; ".join(missing or ["finish"]) + ". Continue."})
                    continue

                results = [self._run_tool(log, emit, step, executor, metrics, tu) for tu in tool_uses]
                messages.append({"role": "user", "content": results})   # all results in one message

                if executor.state.finished:
                    status = "complete"
                    break
            else:
                error = f"step budget of {self.settings.max_steps} exhausted; missing {executor.state.missing()}"
                log.error("  ✖ %s", error)
        except Exception as exc:  # noqa: BLE001 — any failure becomes a 'failed' report, not a crash
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("  ✖ episode failed: %s", error)

        metrics.searches = executor.state.searches
        metrics.web_searches = executor.state.web_searches
        metrics.blocked_results_dropped = executor.state.blocked_results_dropped
        metrics.duration_seconds = round(time.monotonic() - started, 1)
        metrics.estimated_cost_usd = self.settings.estimate_cost(
            metrics.input_tokens, metrics.output_tokens, metrics.cache_read_tokens, metrics.cache_write_tokens)
        s = executor.state
        report = EpisodeReport(
            episode_id=episode.episode_id, title=episode.title, host=episode.host, guests=episode.guests,
            source_file=Path(episode.source_path).name, client_id=self.sources.client_id,
            source_policy_version=self.sources.version, status=status, error=error,
            plan=s.plan, summary=s.summary, key_notes=s.key_notes,
            fact_checks=[s.verdicts[c] for c in s.claims if c in s.verdicts],
            reviewer_notes=s.reviewer_notes, ingest=episode.ingest_report, metrics=metrics,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        log.info("■ %s  %s  turns=%d tools=%d rejected=%d searches=%d kb+%d web tokens in/out=%d/%d cost≈$%s %.1fs",
                 status.upper(), episode.episode_id, metrics.model_turns, metrics.tool_calls,
                 metrics.rejected_tool_calls, metrics.searches, metrics.web_searches, metrics.input_tokens + metrics.cache_read_tokens
                 + metrics.cache_write_tokens, metrics.output_tokens, metrics.estimated_cost_usd,
                 metrics.duration_seconds)
        emit("end", status=status, error=error, metrics=metrics.model_dump())
        if trace:
            trace.close()
        return report

    # ---- helpers -------------------------------------------------------------------------

    def _run_tool(self, log, emit, step: int, executor: ToolExecutor, metrics: RunMetrics, tu) -> dict:
        metrics.tool_calls += 1
        args = tu.input if isinstance(tu.input, dict) else {}
        log.info("  🔧 step %d → %s(%s)", step, tu.name, _short(args, 140))
        emit("tool_call", step=step, tool=tu.name, input=args)
        try:
            output = executor.execute(tu.name, args)
            log.info("     ✓ %s", _short(output))
            if tu.name == "create_plan":
                for p in executor.state.plan:
                    log.info("     📋 plan %d. %s [%s] — %s", p.step, p.action, p.tool, _short(p.rationale, 120))
            emit("tool_result", step=step, tool=tu.name, ok=True, output=output)
            return {"type": "tool_result", "tool_use_id": tu.id, "content": output}
        except ToolRejected as exc:
            metrics.rejected_tool_calls += 1
            log.warning("     ✗ rejected: %s", _short(str(exc), 300))
            emit("tool_result", step=step, tool=tu.name, ok=False, error=str(exc))
            return {"type": "tool_result", "tool_use_id": tu.id, "content": str(exc), "is_error": True}
        except Exception as exc:  # noqa: BLE001 — a tool bug should not kill the run
            metrics.rejected_tool_calls += 1
            log.exception("     ✗ tool error")
            emit("tool_result", step=step, tool=tu.name, ok=False, error=repr(exc))
            return {"type": "tool_result", "tool_use_id": tu.id,
                    "content": f"Tool failed with an internal error: {exc!r}", "is_error": True}

    @staticmethod
    def _add_usage(metrics: RunMetrics, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        metrics.input_tokens += getattr(usage, "input_tokens", 0) or 0
        metrics.output_tokens += getattr(usage, "output_tokens", 0) or 0
        metrics.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        metrics.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @staticmethod
    def _log_response(log, emit, step: int, response, executor: ToolExecutor) -> None:
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "thinking" and getattr(block, "thinking", ""):
                log.info("  🧠 step %d reasoning: %s", step, _short(block.thinking, 400))
                emit("reasoning", step=step, text=block.thinking)
            elif btype == "text" and block.text.strip():
                log.info("  💬 step %d: %s", step, _short(block.text, 300))
                emit("text", step=step, text=block.text)
            elif btype == "server_tool_use":
                if block.name == "web_search":   # (code_execution here is web search's own filtering)
                    executor.state.web_searches += 1
                log.info("  🌐 step %d → %s(%s)", step, block.name, _short(block.input, 140))
                emit("server_tool_call", step=step, tool=block.name, input=block.input)
            elif btype == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    results = [{"url": getattr(r, "url", None), "title": getattr(r, "title", None),
                                "page_age": getattr(r, "page_age", None)} for r in content]
                    dropped = executor.register_web_results(results)
                    tiers = [executor.state.seen_sources[r["url"]].tier for r in results
                             if r["url"] in executor.state.seen_sources]
                    log.info("     ✓ web results: %d (%d on trusted domains)%s: %s", len(results),
                             tiers.count("trusted_web"), f", {len(dropped)} blocked and dropped" if dropped else "",
                             _short([r["url"] for r in results], 200))
                    emit("server_tool_result", step=step, results=results, blocked_dropped=dropped)
                else:
                    log.warning("     ✗ web search error: %s", getattr(content, "error_code", content))
                    emit("server_tool_result", step=step, error=str(getattr(content, "error_code", content)))
            elif btype == "fallback":
                log.warning("  ⤳ step %d: request served by fallback model", step)
                emit("fallback", step=step)
