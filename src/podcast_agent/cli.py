"""Command-line entry point.

    python -m podcast_agent run                      # all transcripts in data/transcripts
    python -m podcast_agent run path/to/ep.json -v   # one file, verbose
    python -m podcast_agent run --client example_healthtech   # another client's source policy
    python -m podcast_agent run --no-web-search      # offline: pinned facts + local KB only
    python -m podcast_agent kb-search "FDA AI devices"
    python -m podcast_agent sources                  # show the client's source policy
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .agent import PodcastAgent
from .config import PROJECT_ROOT, Settings, resolve_client
from .ingest import Episode, load_episode
from .knowledge_base import KnowledgeBase
from .render import write_episode, write_index
from .sources import ClientSources
from .telemetry import setup_telemetry

console = Console()


class EpisodeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.ep = f"[{getattr(record, 'episode', '-')}]"
        return True


def setup_logging(verbose: bool, log_file: Path) -> logging.Logger:
    logger = logging.getLogger("podcast_agent")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    console_handler = RichHandler(console=console, show_path=False, markup=False, rich_tracebacks=True,
                                  log_time_format="%H:%M:%S")
    console_handler.setFormatter(logging.Formatter("%(ep)s %(message)s"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(ep)s %(message)s"))
    for h in (console_handler, file_handler):
        h.addFilter(EpisodeFilter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


def collect_inputs(paths: list[str]) -> list[Path]:
    targets = [Path(p) for p in paths] or [PROJECT_ROOT / "data" / "transcripts"]
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files += sorted(p for p in t.iterdir() if p.is_file() and p.suffix.lower() in {".json", ".txt"})
        elif t.exists():
            files.append(t)
        else:
            raise SystemExit(f"Not found: {t}")
    return files


def load_episodes(files: list[Path], logger: logging.Logger) -> list[Episode]:
    """Load every file it can. A bad file (wrong type, broken JSON, unreadable, empty) is logged and
    skipped; it never stops the others. Episode ids are made unique so no two runs share an output folder."""
    episodes: list[Episode] = []
    seen: set[str] = set()
    for f in files:
        try:
            ep = load_episode(f)
        except Exception as exc:  # noqa: BLE001 — one bad input must not sink the run
            logger.error("skipping %s: %s", f.name, exc, extra={"episode": "ingest"})
            continue
        base, n = ep.episode_id, 2
        while ep.episode_id in seen:
            ep.episode_id = f"{base}-{n}"
            n += 1
        if ep.episode_id != base:
            logger.warning("%s: episode id %r already used; using %r", f.name, base, ep.episode_id,
                           extra={"episode": "ingest"})
        seen.add(ep.episode_id)
        episodes.append(ep)
    return episodes


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    overrides = {k: v for k, v in {
        "model": args.model, "effort": args.effort, "concurrency": args.concurrency,
        "max_steps": args.max_steps, "output_dir": Path(args.out) if args.out else None,
    }.items() if v is not None}
    if args.web_search is not None:
        overrides["enable_web_search"] = args.web_search
    if args.client:
        overrides["client_config"] = resolve_client(args.client)
    if args.otel:
        overrides["otel"] = True
    settings = replace(settings, **overrides)

    logger = setup_logging(args.verbose, settings.output_dir / "run.log")
    kb = KnowledgeBase.load(settings.kb_path)
    sources = ClientSources.load(settings.client_config)

    episodes = load_episodes(collect_inputs(args.inputs), logger)
    if not episodes:
        console.print("[red]No valid transcripts to process.[/red]")
        return 2
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        console.print("[red]ANTHROPIC_API_KEY is not set.[/red] Put it in .env (see .env.example).")
        return 2

    from .llm import ClaudeClient  # imported here so `kb-search` works without the SDK configured
    telemetry = setup_telemetry(settings.otel)   # before the client, so the SDK's spans join our traces
    agent = PodcastAgent(ClaudeClient(settings), kb, settings, logger, sources=sources, telemetry=telemetry)
    logger.info("model=%s effort=%s web_search=%s episodes=%d concurrency=%d kb_docs=%d",
                settings.model, settings.effort, settings.enable_web_search, len(episodes),
                settings.concurrency, len(kb.documents), extra={"episode": "run"})
    logger.info("client=%s policy=%s mode=%s trusted=%d blocked=%d pinned=%d", sources.client_id,
                sources.version, sources.search_mode, len(sources.trusted_domains),
                len(sources.blocked_domains), len(sources.pinned_facts), extra={"episode": "run"})

    def work(ep):
        report = agent.run(ep, trace_path=settings.output_dir / ep.episode_id / "trace.jsonl")
        write_episode(report, settings.output_dir)
        return report

    # Episodes run in parallel threads: the work is almost all waiting on the network (Claude and
    # search), so threads overlap the waiting. Turns *within* one episode stay sequential, because
    # each turn depends on the last. One failed episode is logged and never stops the others.
    done: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=max(1, settings.concurrency)) as pool:
        futures = {pool.submit(work, ep): ep for ep in episodes}
        for future in as_completed(futures):
            ep = futures[future]
            try:
                done[ep.episode_id] = future.result()
            except Exception:  # noqa: BLE001 — agent.run already turns model errors into 'failed' reports
                logger.exception("episode crashed outside the agent loop", extra={"episode": ep.episode_id})
    reports = [done[ep.episode_id] for ep in episodes if ep.episode_id in done]   # input order
    telemetry.shutdown()   # flush spans and metrics before exit
    index = write_index(reports, settings.output_dir)

    table = Table(title="Run summary")
    for col in ("Episode", "Status", "✅", "⚠️", "❓", "Review", "Turns", "Rejected", "Cost $"):
        table.add_column(col)
    for r in reports:
        v = [str(sum(f.verdict == k for f in r.fact_checks))
             for k in ("verified", "possibly_outdated_or_inaccurate", "unverifiable")]
        table.add_row(r.episode_id, r.status, *v, str(sum(f.needs_human_review for f in r.fact_checks)),
                      str(r.metrics.model_turns), str(r.metrics.rejected_tool_calls),
                      f"{r.metrics.estimated_cost_usd or 0:.3f}")
    console.print(table)
    console.print(f"Reports written to [bold]{settings.output_dir}[/bold] (index: {index.name})")
    return 0 if len(reports) == len(episodes) and all(r.status == "complete" for r in reports) else 1


def _client(args: argparse.Namespace) -> ClientSources:
    settings = Settings.from_env()
    return ClientSources.load(resolve_client(args.client) if args.client else settings.client_config)


def cmd_kb_search(args: argparse.Namespace) -> int:
    kb = KnowledgeBase.load(Settings.from_env().kb_path).with_documents(_client(args).pinned_facts)
    hits = kb.search(args.query, top_k=args.top_k)
    table = Table(title=f"KB search: {args.query!r}")
    for col in ("score", "tier", "source_id", "as_of", "title"):
        table.add_column(col)
    for h in hits:
        table.add_row(f"{h.score:.2f}", h.doc.tier, h.doc.id, h.doc.as_of or "", h.doc.title)
    console.print(table if hits else "[yellow]No results[/yellow]")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    s = _client(args)
    console.print(f"[bold]{s.name}[/bold] ({s.client_id}) · policy {s.version} · search mode {s.search_mode}")
    console.print(f"web_search tool sent to the API: {s.web_search_tool(Settings.from_env().web_search_max_uses)}")
    t = Table(title="Trusted domains")
    t.add_column("domain")
    t.add_column("topics")
    for d in s.trusted_domains:
        t.add_row(d.domain, ", ".join(d.topics))
    console.print(t)
    console.print(f"Blocked: {', '.join(s.blocked_domains) or 'none'}")
    console.print(f"Pinned facts: {', '.join(f.id for f in s.pinned_facts) or 'none'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podcast_agent", description="Agentic podcast summariser and fact-checker")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process transcripts")
    run.add_argument("inputs", nargs="*", help="transcript files or folders (default: data/transcripts)")
    run.add_argument("--out", help="output folder (default: ./output)")
    run.add_argument("--model", help="Claude model id (default: claude-opus-5-5)")
    run.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--concurrency", type=int)
    run.add_argument("--max-steps", type=int)
    run.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=None,
                     help="live web search (default on); --no-web-search = offline, pinned facts + local KB only")
    run.add_argument("--otel", action="store_true",
                     help="send OpenTelemetry traces and metrics (OTEL_EXPORTER_OTLP_ENDPOINT, default localhost:4318)")
    run.add_argument("--client", help="client source policy: id under data/clients/ or a path (default: default)")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    kbs = sub.add_parser("kb-search", help="query the local knowledge base")
    kbs.add_argument("query")
    kbs.add_argument("--top-k", type=int, default=5)
    kbs.add_argument("--client", help="also search this client's pinned facts")
    kbs.set_defaults(func=cmd_kb_search)

    src = sub.add_parser("sources", help="show a client's source policy")
    src.add_argument("--client", help="client id under data/clients/ or a path")
    src.set_defaults(func=cmd_sources)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # never crash on a console that can't show ✅ (Windows cp1252)
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return args.func(args)
    except (ValueError, KeyError, OSError) as exc:   # bad config, missing or malformed data file
        console.print(f"[red]Error:[/red] {exc}")
        return 2
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
