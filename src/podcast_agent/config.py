"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

# Where data/ and output/ live. Defaults to the repo root; the Docker image sets it to /app.
# When installed as a package (not from the repo), data/ is not next to the code: use the working directory.
_REPO = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(os.getenv("PODCAST_AGENT_HOME") or (_REPO if (_REPO / "data").is_dir() else Path.cwd()))

# USD per million tokens (input, output). Used only for the cost estimate in run metrics.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}
CACHE_READ_MULTIPLIER = 0.1   # cache reads bill at ~10% of the input rate
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes bill at 125% of the input rate


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_client(value: str) -> Path:
    """A client is given as a path to its JSON config, or as an id under data/clients/."""
    path = Path(value)
    if path.suffix == ".json" or path.exists():
        return path
    return PROJECT_ROOT / "data" / "clients" / f"{value}.json"


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "medium"                 # low | medium | high | xhigh | max
    max_tokens: int = 16000                # per model response
    max_steps: int = 25                    # model turns per episode before the agent gives up
    concurrency: int = 3                   # episodes processed in parallel
    enable_web_search: bool = True         # live web search (server tool); False = offline, KB only
    web_search_max_uses: int = 8           # web searches per model turn (the API limit is per request)
    refusal_fallback: bool = True          # server-side fallback if a safety classifier declines
    kb_path: Path = field(default=PROJECT_ROOT / "data" / "knowledge_base.json")
    output_dir: Path = field(default=PROJECT_ROOT / "output")
    client_config: Path = field(default=PROJECT_ROOT / "data" / "clients" / "default.json")
    otel: bool = False                     # OpenTelemetry traces + metrics (pip install ".[otel]")
    db_path: Path = field(default=PROJECT_ROOT / "var" / "podcast_agent.db")   # results store (SQLite)

    @property
    def uploads_dir(self) -> Path:
        return self.db_path.parent / "uploads"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(PROJECT_ROOT / ".env")
        base = cls()
        return replace(
            base,
            model=os.getenv("PODCAST_AGENT_MODEL", base.model),
            effort=os.getenv("PODCAST_AGENT_EFFORT", base.effort),
            max_steps=_int("PODCAST_AGENT_MAX_STEPS", base.max_steps),
            concurrency=_int("PODCAST_AGENT_CONCURRENCY", base.concurrency),
            enable_web_search=_bool("PODCAST_AGENT_WEB_SEARCH", base.enable_web_search),
            refusal_fallback=_bool("PODCAST_AGENT_REFUSAL_FALLBACK", base.refusal_fallback),
            otel=_bool("PODCAST_AGENT_OTEL", base.otel),
            kb_path=Path(os.getenv("PODCAST_AGENT_KB", str(base.kb_path))),
            output_dir=Path(os.getenv("PODCAST_AGENT_OUTPUT", str(base.output_dir))),
            db_path=Path(os.getenv("PODCAST_AGENT_DB", str(base.db_path))),
            client_config=resolve_client(os.getenv("PODCAST_AGENT_CLIENT") or str(base.client_config)),
            web_search_max_uses=_int("PODCAST_AGENT_WEB_SEARCH_MAX_USES", base.web_search_max_uses),
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int,
                      cache_read: int = 0, cache_write: int = 0) -> float | None:
        price = PRICING_PER_MTOK.get(self.model)
        if price is None:
            return None
        in_rate, out_rate = price
        return round(
            (input_tokens * in_rate
             + cache_read * in_rate * CACHE_READ_MULTIPLIER
             + cache_write * in_rate * CACHE_WRITE_MULTIPLIER
             + output_tokens * out_rate) / 1_000_000,
            4,
        )
