# Changelog

Releases use semantic versions: `v0.2.0` for a feature, `v0.1.1` for a fix. Every stored result
records the release (`code_version`), the prompt fingerprint (`prompt_version`) and the model it ran
with, so any verdict can be traced to the release that produced it. Compare two releases with
`git diff v0.1.0 v0.2.0`, or rerun the golden set on both and compare with `podcast_agent eval`.

## [Unreleased]

## [0.1.1] - 2026-11-06

### Fixed
- Review GUI: curly apostrophes and dashes in the raw transcript were struck through as if cleaning had
  removed them. Words are now compared after straightening quotes, so only real removals are marked.
- Review GUI: results produced before v0.1.0 show "not recorded" for release and prompt, not "unknown".
- Review GUI: Streamlit's developer toolbar and Deploy button are hidden.
- CI: the golden-set step could not find `data/` because the package was installed outside the repo.
  CI now installs in editable mode, and the project root falls back to the working directory.
- Release workflow: GHCR image names must be lowercase; the repository owner is now lowercased.

## [0.1.0] - 2026-11-06

First release.

### Added
- Agent loop on Claude (`claude-opus-5-5`, adaptive thinking): plan, tool calls, guardrail
  rejections and self-correction, `finish` only when every deliverable is complete.
- Summary (200–300 words), top 5 takeaways, verbatim quotes with timestamps, topic tags.
- Fact-check with ✅ / ⚠️ / ❓ verdicts, confidence, cited sources, and human-review flags.
- Trusted sources per client: trusted and blocked domains, pinned facts, evidence tiers, a confidence
  cap when only untrusted sources are cited, and an offline mode (local KB only).
- Forgiving transcript ingest: `.json` and messy `.txt`, encodings, filler and stutter removal with the
  raw text kept, and an ingest report.
- Episodes processed in parallel threads; latency breakdown per turn and per tool.
- Optional OpenTelemetry traces and metrics, with a Grafana dashboard under `observability/`.
- **Review GUI** (Streamlit): upload, live job status, report review with raw and clean transcript side
  by side, jump from a quote or claim to its line, history with full-text search and verdict filter,
  and disputes (flag, note, resolve).
- **Results store** (SQLite on a Docker volume) behind one interface, ready for Postgres + OpenSearch.
- **Queue worker** that runs uploaded jobs and stops cleanly on SIGTERM.
- **Golden set** and the `eval` command: claim recall, verdict accuracy, quote validity, and a
  regression report against the last release.
- CI (lint, tests, golden set), a release workflow (image to GHCR, Helm tag update), a Helm chart and an
  Argo CD application.

### Next (v0.2)
- Approve-and-publish workflow; trusted and blocked sources admin page.
- User login and per-client access.
- Postgres + OpenSearch store (enables more than one web or worker pod, and KEDA autoscaling).
- MCP server for Claude and other chat tools.
- Resolved disputes fed into the golden set automatically.
