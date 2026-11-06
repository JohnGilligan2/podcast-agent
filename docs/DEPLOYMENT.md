# Deployment strategy

How the podcast agent would run in production for an ad agency: dozens to thousands of
episodes a month, several client brands, editors who review everything before it is
published, and a budget someone has to answer for.

> **Design principle:** the container that runs on a laptop (`docker compose up`) is the
> same container that runs in production. Production adds a queue in front of it, durable
> storage behind it, and observability around it. It does not change the agent.

---

## 1. Architecture at a glance

```mermaid
flowchart LR
    subgraph Clients["Agency users & systems"]
        ED["Editors / producers<br/>(web review app)"]
        INT["Integrations<br/>(podcast host RSS, Drive, Slack)"]
    end

    subgraph Edge["Edge & auth"]
        CF["CloudFront + WAF"]
        COG["Cognito<br/>(SSO via agency IdP)"]
        APIGW["API Gateway"]
    end

    subgraph Control["Control plane (serverless)"]
        SUBMIT["Lambda: submit job<br/>idempotency key = hash(transcript + config version)"]
        DDB[("DynamoDB<br/>jobs, status, step checkpoints")]
        EB["EventBridge<br/>job.completed / job.failed"]
    end

    subgraph Work["Data plane"]
        S3RAW[("S3: raw transcripts<br/>KMS key per client")]
        TRN["Amazon Transcribe<br/>(audio → transcript, optional)"]
        SQS["SQS work queue"]
        DLQ["SQS dead-letter queue"]
        ECS["ECS Fargate workers<br/>podcast-agent container<br/>autoscale on queue depth"]
        S3OUT[("S3: reports<br/>report.json / report.md / trace.jsonl")]
    end

    subgraph AI["Model & retrieval"]
        BR["Claude on Amazon Bedrock<br/>(PrivateLink, cross-region inference)"]
        ANT["Anthropic API<br/>(secondary provider)"]
        KB[("OpenSearch Serverless<br/>hybrid BM25 + vector KB")]
        WEB["Web search<br/>(domain allow-list)"]
    end

    subgraph Ops["Observability & governance"]
        CW["CloudWatch logs/metrics/alarms"]
        OTEL["OpenTelemetry traces<br/>→ X-Ray / LLM observability tool"]
        SM["Secrets Manager"]
        BUD["AWS Budgets + per-client quotas"]
    end

    ED --> CF --> APIGW
    INT --> APIGW
    APIGW --> COG
    APIGW --> SUBMIT
    SUBMIT --> S3RAW
    SUBMIT --> DDB
    SUBMIT --> SQS
    S3RAW -. audio .-> TRN -.-> S3RAW
    SQS --> ECS
    SQS -- "3 failed attempts" --> DLQ
    ECS --> BR
    ECS -. failover .-> ANT
    ECS --> KB
    ECS --> WEB
    ECS --> S3OUT
    ECS --> DDB
    ECS --> EB
    EB --> ED
    ECS --> CW
    ECS --> OTEL
    SM --> ECS
    BUD --> CW
```

### One episode, end to end

```mermaid
sequenceDiagram
    autonumber
    participant U as Editor / integration
    participant API as API Gateway + Lambda
    participant Q as SQS
    participant W as Fargate worker (agent)
    participant M as Claude (Bedrock)
    participant K as Knowledge base
    participant S as S3 + DynamoDB
    U->>API: POST /episodes (transcript or audio URL)
    API->>S: store transcript, create job (status=queued)
    API->>Q: enqueue job id
    API-->>U: 202 Accepted + job id
    Q->>W: deliver message (visibility timeout 15 min)
    loop plan → act → validate (≤ 25 turns)
        W->>M: messages + tools
        M-->>W: reasoning + tool calls
        W->>K: search_knowledge_base
        W->>W: guardrails (verbatim quotes, word count, evidence provenance)
        W->>S: checkpoint step state
    end
    W->>S: report.json / report.md / trace.jsonl, status=needs_review
    W->>Q: delete message
    S-->>U: EventBridge → Slack/email "ready for review"
    U->>API: approve / edit / reject (feedback stored for evals)
```

---

## 2. End-to-end flow: from upload to published show notes

```mermaid
flowchart TD
    subgraph IN[Input]
        A1[CLI and Docker]
        A2[Web GUI upload]
        A3[MCP server]
        A4[API or S3 drop]
    end
    A1 --> G
    A2 --> G
    A3 --> G
    A4 --> G
    G[Auth gateway: per-client scope, file checks] --> Q[Job queue]
    Q --> W[Agent worker: same Docker image as local]
    subgraph RUN[Per-episode agent run]
        W --> I[Ingest and clean: filler, speakers, timestamps]
        I --> P[Claude plans the work]
        P --> T[Tools: summary, key notes, claims]
        T --> V[Verify claims]
        V --> V1[Pinned client facts]
        V --> V2[Live search on trusted domains]
        V1 --> R[Record verdicts]
        V2 --> R
        R --> GR{Guardrails pass?}
        GR -- no, reason sent back --> P
        GR -- yes --> F[Finish]
    end
    F --> S[(Results store: reports, verdicts, traces)]
    S --> X[Search index]
    S --> H{Human review: any warning, unverifiable or flagged?}
    H -- approved --> PUB[Ready to publish]
    X --> O1[GUI: search and history]
    X --> O2[MCP server: search from Claude]
    Q -- 3 failures --> DLQ[Dead-letter queue and alert]
```

**What runs in parallel.** Episodes run in parallel (today: a thread pool, `--concurrency`; in
production: more workers pulling from the queue). Inside one episode Claude batches independent
tool calls (several searches, many verdicts) in one turn, but the turns themselves stay sequential
because each depends on the last. The work is almost all waiting on the network, so threads give the
full speed-up locally; scale-out in production comes from the queue and worker count, not from
switching to asyncio. Total concurrency is kept under the account's tokens-per-minute limit, and the
SDK retries rate-limit errors with backoff.

---

## 3. Component choices and why

| Concern | Choice | Why this over the alternatives |
|---|---|---|
| Compute | **ECS Fargate** running the existing container | Same image as local dev (no "works on my laptop"). Agent runs take 1–4 min with long-held state; Fargate has no 15-minute cap and no cold-start penalty per step. Lambda + Step Functions is a good fit at very low volume; EKS is overkill for one service. |
| Work distribution | **SQS + DLQ** | Decouples upload bursts from model rate limits; at-least-once delivery with redrive; queue depth is the autoscaling signal. |
| Job state | **DynamoDB** | Job status, idempotency keys and per-step checkpoints; single-digit-ms writes, no capacity planning. |
| Model access | **Claude on Bedrock** (primary), **Anthropic API** (secondary) | Bedrock keeps traffic inside the AWS account (IAM, PrivateLink, no API keys, data stays in-region). `llm.py` is the only file that changes to swap providers. Cross-region inference absorbs regional capacity issues. |
| Evidence | **Live web search steered by a per-client source policy** (trusted domains, blocked domains, pinned facts) | The customer decides what counts as evidence; live results are fresher than stored facts. Pinned facts and the offline KB move to a managed index behind the same `search()` interface. See §4. |
| Results store | **S3** (reports, traces) + **DynamoDB** (job status, metadata) + **OpenSearch** (search over summaries, quotes, claims) | Editors search history and verdict trends; see §5. |
| Storage | **S3** with a KMS key per agency client | Reports, traces and transcripts; lifecycle rules move old artefacts to Glacier. |
| Front end | Small review app on **CloudFront + S3**, **Cognito** SSO | Editors approve/edit/reject before anything is published; their edits become evaluation data. |
| Delivery | GitHub Actions → ECR → ECS blue/green (CodeDeploy) | Each prompt/model change ships as a new image version, gated by the evaluation suite (§11). |

---

## 4. Trusted sources in production

The build already applies a per-client policy from a JSON file (`data/clients/<client>.json`,
enforced in `sources.py` and `tools.py`). Production adds ownership, change control and an audit trail.

```mermaid
flowchart LR
    ADM["Client admin"] --> GUI["Admin screen<br/>(web GUI)"]
    ADM --> AMCP["Admin MCP server<br/>(separate from processing)"]
    GUI --> CFG[("Source policy store<br/>versioned per client")]
    AMCP -- "confirm step" --> CFG
    CFG --> LOG[("Change log<br/>who · what · when · old · new")]
    CFG -- "version at job start" --> JOB["Agent run"]
    JOB -. "never writes" .-x CFG
```

| Concern | Design |
|---|---|
| Onboarding | Agree the trusted and blocked lists (by topic) and any pinned facts with each agency before go-live |
| Who can change it | Admin users of that client only; each user is scoped to their own client's policy |
| How | Admin screen in the GUI, or tools on a **separate admin MCP server**: read lists; add/remove trusted domain; add/remove blocked domain; add/edit/remove pinned fact |
| Safety | Every write needs an explicit confirmation step; every change is logged (who, what, when, old value, new value) |
| Prompt injection | The agent has **no** tool that writes the policy. A transcript is untrusted and could ask Claude to "trust" a domain; keeping writes out of the processing session closes that hole |
| Consistency | Changes apply from the next run, never mid-run. Each report stores the policy version (`source_policy_version`) it was produced under |
| Re-checking | When a policy changes, past episodes can be re-run and the new verdicts compared with the old ones |
| Precedence | Pinned client facts first for client-specific claims; trusted domains carry full confidence; evidence only from other sites is capped and goes to a person; pinned-vs-live conflicts always go to a person |

---

## 5. Results store and search

The agent, the worker and the GUI all write and read through one small interface
([`store.py`](../src/podcast_agent/store.py) `ResultStore`), so the backend can change without touching them.

| | v0.1 (built, ships with the submission) | Production (v0.2) |
|---|---|---|
| Tables | SQLite file on a Docker volume shared by the `web` and `worker` containers | Postgres |
| Search | SQLite FTS5 full-text index | OpenSearch |
| Report files | `output/<episode>/report.json`, `report.md`, `trace.jsonl` (the brief asks for files) | Object storage, one prefix per client |
| Extra services to run | None | Managed Postgres + OpenSearch |

| Table | Holds |
|---|---|
| `episodes` | id, client, title, host, guests, processed time, model, cost, status, **release and prompt version**, full report |
| `transcript_lines` | episode id, timestamp, speaker, raw text, clean text, ingest flags |
| `notes` | episode id, summary, takeaways, quotes with timestamps, tags |
| `claims` | episode id, claim, speaker, timestamp, type, verdict, confidence, rationale, sources, review flag |
| `disputes` | claim or quote id, a copy of its text, note, open/resolved, who, when, resolution |
| `jobs` | the upload queue: file, client, status, live progress message, attempts, cost |

A rerun replaces an episode's results in one transaction; disputes are kept, because each carries a
copy of the text it disputes. What clients can do with it: search past episodes, quotes and claims;
filter claims by verdict (every ⚠️ this quarter); see how a recurring claim was judged over time; rerun
old episodes after a source-policy change and compare verdicts; feed resolved disputes into the golden set.

---|---|
| Metadata: client, show, episode, title, host, guests, processed date | DynamoDB |
| Summary, takeaways, quotes with timestamps, tags | S3 (`report.json`) + OpenSearch |
| Every claim with verdict, confidence, rationale, evidence tiers, review flag | S3 + OpenSearch |
| Source policy version; model and prompt version; cost; run time; full trace | S3 (`trace.jsonl`) + DynamoDB |
| Review status (pending / approved / edited / rejected) and approver | DynamoDB |

Raw reports and traces live in S3, one prefix per client, encrypted with a key per client. What clients
can do with it: search past episodes by topic, guest, quote or claim; filter claims by verdict (e.g.
every ⚠️ in the last quarter); see how a recurring claim was judged over time; re-run old episodes
after a source-policy change and diff the verdicts; and feed editor corrections into the evaluation
set. Every query is scoped to the client making it.

---

## 6. Delivery: how people use it

One results store sits behind every front end; a client uses whichever fits.

| Option | Who it's for | What it does | Effort |
|---|---|---|---|
| **CLI / Docker** (built) | Engineers, the panel | Batch-process files, write reports | Done |
| **Web GUI** (built, v0.1) | Editors and producers | Upload transcripts, watch job status live, review the report next to the raw and clean transcript, jump from a quote or claim to its line, search history, raise and resolve disputes. v0.2 adds approve-and-publish, the sources admin page and login | Done (Streamlit); a React front end only if editors outgrow it |
| **MCP server** | Teams who live in Claude or another MCP-capable chat | `submit_episode`, `get_report`, `search_results` from inside the chat. Read/search tools here; source-list writes only on the separate admin MCP server | Small: wraps the same `PodcastAgent.run` and results store |
| **API** | Agencies plugging into their own tools, podcast-host or RSS integrations | `POST /episodes` → `202` + job id; webhooks / EventBridge on completion | Small: API Gateway + the submit Lambda below |
| **Slack notifications** | Everyone | "Ready for review" with a link | Small |

All of them reuse the agent unchanged; only the entry point differs.

---

## 7. Scalability: threads today, Kubernetes next, asyncio when it pays

**Where the time goes** (measured, ep003): 35.9 s waiting on Claude across 4 turns, 0.14 s running our own
tools and guardrails. The work is network-bound, so concurrency, not CPU, is what scales it. The AWS-native
variant in §1 (ECS Fargate + SQS) is the same design with Fargate tasks in place of pods.

| Stage | Runs on | Concurrency | Move on when |
|---|---|---|---|
| **1. Today** | One container (laptop, VM) | `ThreadPoolExecutor`, `--concurrency` episodes at once (default 3) | More than one machine is needed |
| **2. Kubernetes workers** | The same image as a Deployment on the platform's cluster (Seekr's cloud, EKS/AKS/GKE/OpenShift, or a client's) | More **pods**, each running N threads; pods pull jobs from a queue | One pod needs hundreds of calls in flight |
| **3. asyncio in the pod** | Same pods | `AsyncAnthropic` + `asyncio.gather`: hundreds of in-flight calls per process | It is a density/cost gain, not a requirement |

```mermaid
flowchart LR
    API["Ingest API / GUI<br/>(Deployment + Ingress)"] --> Q[("Queue<br/>SQS · Kafka · Redis Streams")]
    Q --> KEDA["KEDA ScaledObject<br/>scale on queue depth, 0 → N"]
    KEDA --> W1["worker pod<br/>N threads"] & W2["worker pod<br/>N threads"] & W3["worker pod<br/>N threads"]
    W1 & W2 & W3 --> RL[("Shared rate limiter<br/>token bucket in Redis<br/>sized to the TPM quota")]
    RL --> GW["Model gateway<br/>Claude API · Bedrock · other hosting"]
    W1 & W2 & W3 --> OT["OTel Collector<br/>(DaemonSet or sidecar)"]
```

| Concern | Kubernetes answer |
|---|---|
| Packaging | One Helm chart: worker Deployment, ingest Deployment, KEDA ScaledObject, PodDisruptionBudget, NetworkPolicies |
| Autoscaling | **KEDA** on queue depth (scale to zero overnight); HPA on CPU is the wrong signal for network-bound work |
| The real ceiling | The model's tokens-per-minute quota. Pod retries are not coordinated, so a **cluster-wide token bucket** (Redis) gates every call; the SDK still backs off on 429 |
| Config | Client source policies as a versioned store (or ConfigMaps at first); secrets via External Secrets / the cloud's secret store |
| Model access | Behind a gateway, so the provider or a self-hosted model can change without touching the agent (`llm.py` is already the only provider-specific file) |
| Graceful shutdown | `terminationGracePeriodSeconds` > one episode (~2 min); unfinished jobs return to the queue and resume from the checkpoint |

| Load | What scales | How |
|---|---|---|
| Upload bursts (a client drops a back catalogue of 500 episodes) | The queue absorbs it | Work is queued, not rejected; the API answers `202` immediately |
| Steady throughput | Worker pods × threads per pod | KEDA target: queue depth per pod; total in-flight calls sized under the TPM quota |
| Long episodes (2–3 h, ~30k words) | Context | ~45k tokens fits comfortably in a 1M-token context, so no chunking is needed for normal shows. For very long or multi-part content, a map-reduce step summarises segments first |
| Back-catalogue jobs that are not urgent | Batch API | Route to the Message Batches API at 50% of the price, accepting up to 24 h turnaround |

---

### How the GUI scales

```mermaid
flowchart LR
    U[Editors] --> I[Ingress + TLS]
    I --> W1[web pod] & W2[web pod] & W3[web pod]
    W1 & W2 & W3 -->|enqueue / read| DB[(Results store<br/>jobs · episodes · disputes)]
    DB -->|claim job| K1[worker pod] & K2[worker pod] & KN[worker pod …]
    K1 & K2 & KN -->|Messages API| C[Claude]
    HPA[HPA on CPU] -.-> W1
    KEDA[KEDA on queued jobs] -.-> K1
```

- The web GUI is its own container and is **stateless**: uploads, jobs and results live in storage.
- In Kubernetes: 2–3 web pods behind an ingress, scaled by a horizontal pod autoscaler on traffic.
- Agent workers are a separate Deployment, scaled on job-queue depth by KEDA (0 → N).
- There is no web server per worker or per thread. The layers scale independently and talk only
  through the job queue and the results store.
- Locally, docker-compose runs one `web` and one `worker` container on a shared volume.

---

## 8. Fault tolerance

```mermaid
flowchart TD
    A[Model call fails] --> B{Retryable?<br/>429 / 5xx / network}
    B -- yes --> C[SDK retries with backoff<br/>max 4]
    C -- still failing --> D[Fail over to secondary<br/>region / provider]
    B -- no: 400 --> E[Mark job failed<br/>with error]
    F[Model declines<br/>safety classifier] --> G[Server-side fallback model]
    H[Worker crashes mid-episode] --> I[SQS visibility timeout expires]
    I --> J[Another worker resumes<br/>from DynamoDB checkpoint]
    J --> K{3rd failure?}
    K -- yes --> L[Dead-letter queue + alarm<br/>human triage]
    M[Agent runs out of steps] --> N[Report saved as 'incomplete'<br/>with what is missing]
    O[Output fails a guardrail] --> P[Rejected back to the model<br/>to self-correct]
```

- **Idempotency.** The job key is `hash(transcript + prompt version + model + KB version)`. A duplicate upload returns the existing job; a retried message never produces two reports.
- **Graceful degradation.** A run that cannot finish still writes what it has, marked `incomplete` and naming what is missing, instead of losing the work.
- **No single points of failure.** Every managed service above is multi-AZ; workers run in at least two AZs.
- **Poison messages** land in the DLQ after three attempts, raise an alarm, and can be replayed after a fix.

---

## 9. Cost

**Measured locally** with `claude-opus-5-5` at `medium` effort and live web search on five short episodes (~5–6 minutes of audio each): about $0.15–0.30 per episode, $1.15 for the run. See [`examples/sample_run/index.md`](../examples/sample_run/index.md) for per-episode figures. Token use scales with transcript length and the number of claims checked; web search results add input tokens, and searches are billed per use on top of tokens. Offline mode (`--no-web-search`) is cheaper but less fresh.

| Lever | Effect | Trade-off |
|---|---|---|
| Prompt caching (already on) | The growing conversation is re-read each turn at ~10% of the input price | None |
| Effort `low` / `medium` | Fewer reasoning tokens per turn | Measure quality on the eval set before lowering |
| Smaller model for the bulk steps (e.g. Claude Sonnet 5.5 or Haiku 5.5 for claim extraction; Opus for verdicts) | Large share of tokens moves to a cheaper model | More moving parts; validate per step |
| Message Batches API for back catalogues | 50% off | Up to 24 h latency |
| Fargate Spot for workers | ~70% off compute | Interruptions are already handled by checkpoints + SQS redelivery |
| Per-client quotas + AWS Budgets alarms | Caps runaway spend | Needs a quota policy agreed with account leads |

Infrastructure cost is small next to model spend: at a few thousand episodes a month, Fargate, SQS, DynamoDB, S3 and OpenSearch Serverless together are typically low hundreds of dollars a month, dominated by the OpenSearch Serverless minimum capacity (a managed vector store with lower floor pricing is an option at small scale).

---

## 10. Security, privacy and compliance

| Risk | Control |
|---|---|
| Client content leaking between brands | Tenant id on every job; S3 prefix + KMS key per client; IAM conditions on the tenant tag; reports never mixed in one index. |
| Prompt injection inside a transcript ("ignore your instructions…") | The transcript is wrapped and labelled as untrusted data in the prompt; the agent's tools have **no side effects outside the job** (no email, no publishing), so the worst case is a bad report that a human reviews. |
| Hallucinated facts or quotes | Guardrails in code: quotes must match the transcript verbatim; a "verified" verdict must cite a source the agent actually retrieved; summaries must meet the length spec. Editors approve before publishing. |
| Secrets | No long-lived keys with Bedrock (IAM task role). Anthropic key, if used, lives in Secrets Manager and is injected at task start. |
| Data residency / retention | Bedrock in the client's region; S3 lifecycle and DynamoDB TTL implement the retention period in each client contract; model providers do not train on API data. |
| Network | Workers in private subnets; VPC endpoints for S3, DynamoDB, SQS, Bedrock; egress only through a NAT with an allow-list for the web-search provider. |
| Audit | `trace.jsonl` records every reasoning summary, tool call and result for each report; CloudTrail covers infrastructure actions. |

---

## 11. Observability: latency, tokens, cost and quality

**Built in the repo.** Every report already records latency per model turn and per tool, tokens and cost
(`metrics` in `report.json`, `model_call` / `tool_timing` events in `trace.jsonl`). With `--otel` the agent
also emits OpenTelemetry traces and metrics (`src/podcast_agent/telemetry.py`), and
`docker compose -f observability/docker-compose.yml up -d` starts an optional local Collector, Prometheus, Jaeger and Grafana
with a ready-made dashboard. In production the same instrumentation points at the cluster's collector.

```mermaid
flowchart LR
    A["agent pods<br/>telemetry.py + Anthropic SDK spans"] -- OTLP --> C["OTel Collector"]
    C -- metrics --> P[("Prometheus")] --> G["Grafana<br/>cost · latency · tokens · verdicts<br/>+ alerts"]
    C -- traces --> L["Langfuse (self-hosted)<br/>or Jaeger / Tempo"]
    L -. "editor feedback as scores" .- R["Review app"]
```

| Tool | Answers | For |
|---|---|---|
| **Langfuse** (traces) | What did the agent do on *this* episode: each turn, tool call, latency, tokens, cost, plus editor feedback as quality scores | AI engineers, quality review |
| **Prometheus + Grafana** (metrics) | How is the system doing: cost per episode by client, p95 latency, tokens/min against quota, rejection rate, verdict mix, queue age; alerting | Ops, account leads, finance |
| **Claude API tracing** (optional) | Server-side spans from the API itself (each model request inside a call, server-side web search), joined to ours by `traceparent` | Deep latency debugging; an org admin adds an OTLP destination in the Console |

Trace shape: `podcast_agent.episode` → `podcast_agent.turn` (×N, each containing the SDK's
`anthropic.messages.create`) and `podcast_agent.tool` (×M). SDK spans are **metadata only** (model, tokens,
timing), never prompts. Traces in content mode would contain client transcripts, so Langfuse runs
**self-hosted inside the cluster** with per-client retention, not as a SaaS.

| Metric (Prometheus name) | Labels | Alert when |
|---|---|---|
| `podcast_agent_episode_duration_seconds` (histogram), `podcast_agent_episode_time_seconds_total` | client, model, status | p95 > 10 min (the SLO) |
| `podcast_agent_model_call_duration_seconds` | client, model | p95 doubles vs the 7-day baseline |
| `podcast_agent_tool_call_duration_seconds` | tool, outcome | — (diagnostic) |
| `podcast_agent_tokens_total` | type (input/output/cache_read/cache_write) | tokens/min > 80% of the quota |
| `podcast_agent_cost_usd_total` | client, model | cost per episode > 2× the 7-day median; budget burn |
| `podcast_agent_guardrail_rejections_total` | tool | sudden rise (prompt or model regression) |
| `podcast_agent_verdicts_total`, `podcast_agent_needs_review_total` | verdict | mix shifts after a model, prompt or policy change |
| `podcast_agent_episodes_total` | status | failures > 2% in an hour |
| Queue age / depth, DLQ depth | (from the queue) | oldest message > 15 min; DLQ > 0 |

Episode ids go on spans, never on metric labels (cardinality). Counters are published at zero when a worker
starts, so `increase()` also counts the first episode of a short-lived process.

**Evaluation gate in CI.** A golden set of transcripts with expected claims, verdicts and quote checks runs on every change to prompts, tools, model or KB. A release is blocked if verdict accuracy or guardrail pass rate drops. Editor corrections from the review app are added to the golden set over time; in Langfuse they attach to the episode's trace as scores.

---

## 12. Rollout plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. v0.1 (submission) | Docker: CLI, review GUI + queue worker, SQLite results store, client source policies, threads, golden set, CI, Helm chart, optional OTel stack | Panel review |
| 1. Pilot (2–3 weeks) | One client, Fargate + SQS + S3, editors review in a shared folder | ≥ 90% of reports approved with minor edits |
| 2. Production | Kubernetes workers + KEDA, shared rate limiter, review app, results store, self-hosted Langfuse + Grafana alerts, per-client isolation, CI eval gate | SLOs: 95% of episodes ready < 10 min; < 1% failures |
| 3. Scale-out | asyncio workers where density pays, audio ingestion, Batch API for back catalogues, cheaper model routing per step, social-post generation | Cost per episode down 40% at equal quality |

---

## 13. Delivery pipeline: branches, CI, Helm and Argo CD

```mermaid
flowchart LR
    F[feature branch] -->|PR + review| D[develop]
    D -->|PR + review| M[main]
    M -->|tag v0.x.y| R[release workflow]
    R -->|build + push image| G[(ghcr.io)]
    R -->|write image tag| H[Helm values on main]
    H -->|watched by| A[Argo CD]
    A -->|sync| K[Kubernetes]
    CI{{CI on every PR:<br/>lint · tests · golden set}} -. blocks merge .-> D
    CI -. blocks merge .-> M
```

| Step | What happens | Where |
|---|---|---|
| Develop | Work on `develop`, starting at v0.1. Merge to `main` for production through a reviewed pull request | Branch protection |
| CI on every PR | `ruff`, unit + agent-loop + GUI smoke tests (scripted model, no key), golden-set evaluation; any failure blocks the merge | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) |
| Release | Tag `v0.2.0` → build the image, push it with that tag, write the tag into the Helm values | [`.github/workflows/release.yml`](../.github/workflows/release.yml) |
| Deploy | Argo CD watches the chart on `main` and syncs the cluster | [`deploy/argocd/application.yaml`](../deploy/argocd/application.yaml) |
| Later | A staging environment synced from `develop` before production | — |

### What goes where

| Setting | Helm values | Database | Kubernetes Secret (outside git) |
|---|---|---|---|
| GUI on/off, MCP server on/off | ✅ | | |
| Replicas and autoscaling limits (web, workers) | ✅ | | |
| Model and reasoning effort, concurrency per worker | ✅ | | |
| Image tag (set by CI), resources, ingress host and TLS | ✅ | | |
| Name of the secret holding the API key | ✅ | | |
| Default trusted-source list to seed a new client | ✅ optional | | |
| Each client's trusted / blocked sources and pinned facts | | ✅ changed often via GUI or MCP; a redeploy per edit would be wrong | |
| The API key | | | ✅ e.g. External Secrets from a vault |

| In GitHub | Never in GitHub (enforced by `.gitignore` and `.dockerignore`) |
|---|---|
| Source code and tests; Dockerfile and compose files | API keys; any `.env` with real values |
| Helm chart with default values; Argo CD application | Each client's real source lists and pinned facts |
| `.env.example`; the fictional sample client policies as seed files | The results database, uploaded transcripts and client reports |
| Docs, sample outputs under `examples/`, CI workflows, `CHANGELOG.md` | |

The chart is [`deploy/helm/podcast-agent`](../deploy/helm/podcast-agent). v0.1 targets the SQLite store, so it
runs one web pod and one worker pod on one volume, and refuses, with a clear message, settings that need the
v0.2 Postgres store: more replicas, the HPA, KEDA, or the MCP server. Pods run as non-root with a read-only
root filesystem, and the web pod has no API key at all because it never calls Claude. Until the v0.2 sources
admin page moves client policies into the database, real policy files are mounted from a ConfigMap created
outside git (`clients.existingConfigMap`).

## 14. Versioning, regression and rollback

**Versioning.** Releases use semantic version tags (v0.1.0; v0.2.0 for a feature, v0.1.1 for a fix), and
[`CHANGELOG.md`](../CHANGELOG.md) lists what changed. Prompts, the model name and the source policies are
versioned with the code, never edited live. Every stored result records `code_version` (release + build
commit), `prompt_version` (a fingerprint of the system prompt and tool contracts), the model and the
source-policy version, so any verdict traces to the exact release that produced it.

**Regression.** The golden set ([`data/golden/golden.json`](../data/golden/golden.json)) lists the claims an
editor expects in each sample episode and the verdicts they accept. `podcast_agent eval` scores a run on
claim recall, verdict accuracy and quote validity, compares it with the last release's run, and fails if
accuracy drops or a quote stops matching. CI posts the report on the pull request: verdicts that changed,
cost and run-time differences.

```bash
python -m podcast_agent run data/transcripts data/samples --out new_run
python -m podcast_agent eval new_run --baseline examples/sample_run --md regression.md
```

**Seeing what changed.** Code: a GitHub compare of two tags, or `git diff v0.1.0 v0.2.0`. Output: rerun an
episode on both versions and compare the reports, which shows which verdicts or quotes changed.

**Adding a feature:**

1. Branch from `develop`, build the feature, add its tests.
2. Open a pull request. CI runs the tests and the golden-set regression.
3. Review the code diff and the regression report, then merge to `develop`.
4. Merge `develop` to `main` and tag the release. Argo CD deploys it.
5. If it breaks, roll back.

**Rollback.** Revert the release commit on `main`, or set the Helm image tag back to the previous version;
Argo CD syncs the cluster back within minutes, and its history can redeploy an earlier release from the UI.
Because every result records its version, anything the bad release produced can be found and rerun.
Later: feature flags, so a feature can be switched off without a redeploy.

## 15. Testing approach

A one-to-one ratio of test lines to code lines is not the goal; it is easy to hit without testing what
matters. Tests sit where the risk is:

| Area | What is tested |
|---|---|
| Guardrails | A paraphrased quote is rejected; a verdict citing an unseen source is rejected; word counts enforced; `finish` blocked until complete |
| Ingest | Timestamp formats, speaker mapping, filler removal, stutters, raw text never modified, hostile files |
| Agent loop | End-to-end run with a scripted fake model, including self-correction after a rejection |
| Store and worker | A rerun keeps disputes; two workers never take the same job; a bad upload fails only its job; odd search input never breaks a query |
| GUI | Every page renders; the report shows verdicts and raw vs clean; a quote jumps to its line; transcript HTML is escaped; disputes resolve |
| Quality | The golden set, run in CI on every prompt, model or source change |

Coverage is tracked on the guardrail, ingest, tools and store modules, and kept high there rather than
across the whole codebase.
