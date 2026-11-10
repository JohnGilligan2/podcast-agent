"""OpenTelemetry: one trace per episode (episode → turns, tools) and cost/latency/token metrics.
Uses in-memory exporters, so no collector is needed. Skipped when the [otel] extra is not installed."""

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from test_tools_and_agent import ScriptedLLM, happy_script  # noqa: E402

from podcast_agent.agent import PodcastAgent  # noqa: E402
from podcast_agent.config import Settings  # noqa: E402
from podcast_agent.ingest import load_episode  # noqa: E402
from podcast_agent.knowledge_base import KnowledgeBase  # noqa: E402
from podcast_agent.telemetry import OtelTelemetry, Telemetry, setup_telemetry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_telemetry_is_a_no_op_when_off():
    tel = setup_telemetry(False)
    assert type(tel) is Telemetry and not tel.enabled
    with tel.span("x") as span:
        span.set_attribute("a", 1)   # must not raise


def test_episode_trace_and_metrics():
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tel = OtelTelemetry(span_exporter=spans, metric_reader=reader, register_global=False)
    ep = load_episode(ROOT / "data/transcripts/ep001_remote_work.json")
    kb = KnowledgeBase.load(ROOT / "data/knowledge_base.json")
    report = PodcastAgent(ScriptedLLM(happy_script()), kb, Settings(), today="2026-11-05", telemetry=tel).run(ep)
    assert report.status == "complete"

    finished = spans.get_finished_spans()
    names = [s.name for s in finished]
    assert names.count("podcast_agent.turn") == 6 and names.count("podcast_agent.tool") == 9
    root = next(s for s in finished if s.name == "podcast_agent.episode")
    assert root.attributes["episode.id"] == "ep001" and root.attributes["status"] == "complete"
    assert root.attributes["guardrail_rejections"] == 1
    assert all(s.parent.span_id == root.context.span_id for s in finished if s is not root)
    turn = next(s for s in finished if s.name == "podcast_agent.turn")
    assert turn.attributes["gen_ai.usage.input_tokens"] == 100

    data = reader.get_metrics_data()
    points = {m.name: m.data.data_points for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {"podcast_agent.episode.duration", "podcast_agent.model_call.duration", "podcast_agent.tool_call.duration",
            "podcast_agent.tokens", "podcast_agent.cost_usd", "podcast_agent.episodes",
            "podcast_agent.guardrail_rejections", "podcast_agent.verdicts"} <= set(points)
    input_tokens = sum(p.value for p in points["podcast_agent.tokens"] if p.attributes["type"] == "input")
    assert input_tokens == 600   # 6 turns × 100
    assert all("episode" not in p.attributes for ps in points.values() for p in ps)   # low-cardinality labels
    rejected = {p.attributes["tool"]: p.value for p in points["podcast_agent.guardrail_rejections"]}
    assert rejected["submit_summary"] == 1 and rejected["finish"] == 0   # every tool pre-published at 0
    statuses = {p.attributes["status"]: p.value for p in points["podcast_agent.episodes"]}
    assert statuses == {"complete": 1, "incomplete": 0, "failed": 0}
