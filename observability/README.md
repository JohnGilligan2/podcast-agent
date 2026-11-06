# Observability scaffolding (optional)

**Not needed to run the agent, its tests, or the Docker image.** Nothing here is installed or started by
default. It shows how the agent plugs into production monitoring later.

What you get without any of this: every report already records latency per Claude turn and per tool,
tokens and cost (`report.json` → `metrics`, `trace.jsonl` → `model_call` / `tool_timing` events).

## Try it locally

```bash
docker compose -f observability/docker-compose.yml up -d          # Collector, Prometheus, Jaeger, Grafana

# Agent on the host:
pip install -e ".[otel]"
python -m podcast_agent run --otel                                  # sends OTLP to localhost:4318

# Or the agent in Docker:
PODCAST_AGENT_EXTRAS=dev,otel docker compose build
docker compose run --rm -e OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 agent run --otel

docker compose -f observability/docker-compose.yml down             # stop it
```

Grafana http://localhost:3000 opens the *Podcast agent: cost, latency, quality* dashboard; Jaeger
http://localhost:16686 shows one trace per episode.

```text
agent (telemetry.py + Anthropic SDK spans) ──OTLP──▶ OTel Collector ──▶ Prometheus ──▶ Grafana
                                                                 └──▶ Jaeger (locally) / Langfuse (production)
```

| File | Purpose |
|---|---|
| `otel-collector.yaml` | Receives OTLP; metrics → Prometheus exporter, traces → Jaeger. A commented Langfuse exporter shows the swap. |
| `prometheus.yml` | Scrapes the collector |
| `grafana/` | Datasources and the dashboard (cost, cost per episode, latency p50/p95, tokens/min, verdict mix, rejections) |

If `--otel` is passed without the packages or without a collector, the agent logs a warning and runs
normally: telemetry never fails a job. Production design: [`docs/DEPLOYMENT.md` §11](../docs/DEPLOYMENT.md).
