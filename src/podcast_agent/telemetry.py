"""Optional OpenTelemetry: traces and metrics for latency, tokens and cost. Off by default.

Turn on with PODCAST_AGENT_OTEL=true (or `run --otel`) after `pip install ".[otel]"`. Where the
data goes is set by the standard OpenTelemetry variables, e.g.
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318      (an OTel Collector; the default)
  OTEL_SERVICE_NAME=podcast-agent

  agent ──OTLP──▶ OTel Collector ──▶ Prometheus ──▶ Grafana   (metrics: cost, tokens, latency, verdicts)
                                 └─▶ Jaeger or Langfuse        (traces: one per episode)

Trace shape:  podcast_agent.episode
                ├─ podcast_agent.turn  (×N)  ── anthropic.messages.create   (span added by the Anthropic SDK)
                └─ podcast_agent.tool  (×M)
The SDK spans carry metadata only (model, tokens, timing), never prompts or transcripts.

If the packages are missing or the collector is down, the run carries on: telemetry never fails a job.
"""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from typing import Any

from . import __version__

log = logging.getLogger("podcast_agent")

DURATION_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300]


class _NoSpan:
    def set_attribute(self, *_: Any) -> None: ...
    def set_attributes(self, *_: Any) -> None: ...
    def add_event(self, *_: Any, **__: Any) -> None: ...


class Telemetry:
    """No-op implementation, used when telemetry is off. Same interface as OtelTelemetry."""
    enabled = False

    def span(self, name: str, attributes: dict | None = None):
        return nullcontext(_NoSpan())

    def model_call(self, seconds: float, attrs: dict, usage: dict) -> None: ...
    def tool_call(self, tool: str, seconds: float, outcome: str, attrs: dict) -> None: ...
    def episode(self, report: Any, attrs: dict) -> None: ...
    def prewarm(self, attrs: dict, tools: list[str]) -> None: ...
    def mark_error(self, span: Any, message: str) -> None: ...
    def shutdown(self) -> None: ...


class OtelTelemetry(Telemetry):
    enabled = True

    def __init__(self, service_name: str = "podcast-agent", span_exporter: Any = None,
                 metric_reader: Any = None, register_global: bool = True):
        """Exports over OTLP by default; tests pass in-memory exporters and register_global=False."""
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import Status, StatusCode

        self._Status, self._StatusCode = Status, StatusCode
        resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
                                    "service.version": __version__})
        # Registered globally BEFORE the first Claude call, so the Anthropic SDK's own spans join our traces.
        self._tp = TracerProvider(resource=resource)
        if span_exporter is None:
            self._tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        else:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            self._tp.add_span_processor(SimpleSpanProcessor(span_exporter))
        reader = metric_reader or PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=2000)
        self._mp = MeterProvider(
            resource=resource,
            metric_readers=[reader],
            views=[View(instrument_name="*duration*",
                        aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS))])
        if register_global:
            trace.set_tracer_provider(self._tp)
            metrics.set_meter_provider(self._mp)
        self.tracer = self._tp.get_tracer("podcast_agent", __version__)
        m = self._mp.get_meter("podcast_agent", __version__)
        # Prometheus names after the collector: podcast_agent_episode_duration_seconds_bucket, ..._total
        self.episode_duration = m.create_histogram("podcast_agent.episode.duration", unit="s",
                                                   description="Wall time per episode")
        self.model_duration = m.create_histogram("podcast_agent.model_call.duration", unit="s",
                                                 description="Latency of one Claude request (one agent turn)")
        self.tool_duration = m.create_histogram("podcast_agent.tool_call.duration", unit="s",
                                                description="Latency of one tool call, by tool")
        self.tokens = m.create_counter("podcast_agent.tokens", description="Tokens by type: input, output, "
                                       "cache_read, cache_write")
        self.cost = m.create_counter("podcast_agent.cost_usd", description="Estimated spend in USD")
        self.episodes = m.create_counter("podcast_agent.episodes", description="Episodes by final status")
        self.episode_time = m.create_counter("podcast_agent.episode.time", unit="s",
                                             description="Total episode wall time (avg = this / episodes)")
        self.rejections = m.create_counter("podcast_agent.guardrail_rejections",
                                           description="Tool calls rejected by a guardrail, by tool")
        self.verdicts = m.create_counter("podcast_agent.verdicts", description="Fact-check verdicts")
        self.reviews = m.create_counter("podcast_agent.needs_review", description="Claims sent to a person")
        self.web_searches = m.create_counter("podcast_agent.web_searches", description="Web searches run")

    def span(self, name: str, attributes: dict | None = None):
        return self.tracer.start_as_current_span(name, attributes=_clean(attributes or {}))

    def model_call(self, seconds: float, attrs: dict, usage: dict) -> None:
        a = _clean(attrs)
        self.model_duration.record(seconds, a)
        for kind, n in usage.items():
            if n:
                self.tokens.add(n, {**a, "type": kind})

    def tool_call(self, tool: str, seconds: float, outcome: str, attrs: dict) -> None:
        a = _clean({**attrs, "tool": tool, "outcome": outcome})
        self.tool_duration.record(seconds, a)
        if outcome != "ok":
            self.rejections.add(1, a)

    def episode(self, report: Any, attrs: dict) -> None:
        a = _clean(attrs)
        m = report.metrics
        self.episode_duration.record(m.duration_seconds, {**a, "status": report.status})
        self.episodes.add(1, {**a, "status": report.status})
        self.episode_time.add(m.duration_seconds, a)
        if m.estimated_cost_usd:
            self.cost.add(m.estimated_cost_usd, a)
        if m.web_searches:
            self.web_searches.add(m.web_searches, a)
        for f in report.fact_checks:
            self.verdicts.add(1, {**a, "verdict": f.verdict})
            if f.needs_human_review:
                self.reviews.add(1, a)

    def prewarm(self, attrs: dict, tools: list[str]) -> None:
        """Publish every counter at 0 before any work, so Prometheus' increase()/rate() count the first
        episode of a short-lived process too (a series that first appears non-zero loses that jump)."""
        a = _clean(attrs)
        for status in ("complete", "incomplete", "failed"):
            self.episodes.add(0, {**a, "status": status})
        for counter in (self.cost, self.web_searches, self.reviews, self.episode_time):
            counter.add(0, a)
        for kind in ("input", "output", "cache_read", "cache_write"):
            self.tokens.add(0, {**a, "type": kind})
        for verdict in ("verified", "possibly_outdated_or_inaccurate", "unverifiable"):
            self.verdicts.add(0, {**a, "verdict": verdict})
        for tool in tools:
            self.rejections.add(0, {**a, "tool": tool, "outcome": "rejected"})

    def mark_error(self, span: Any, message: str) -> None:
        span.set_status(self._Status(self._StatusCode.ERROR, message))

    def shutdown(self) -> None:
        for provider in (self._tp, self._mp):
            try:
                provider.shutdown()   # flushes anything still buffered
            except Exception:  # noqa: BLE001
                log.debug("telemetry flush failed", exc_info=True)


def _clean(attrs: dict) -> dict:
    """OpenTelemetry attributes must be str/bool/int/float (or lists of them); drop None."""
    return {k: v for k, v in attrs.items() if v is not None and isinstance(v, (str, bool, int, float))}


def setup_telemetry(enabled: bool) -> Telemetry:
    if not enabled:
        return Telemetry()
    try:
        tel = OtelTelemetry()
    except ImportError:
        log.warning("telemetry requested but OpenTelemetry is not installed; run: pip install '.[otel]'",
                    extra={"episode": "run"})
        return Telemetry()
    log.info("telemetry on: OTLP → %s", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
             extra={"episode": "run"})
    return tel
