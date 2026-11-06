"""Queue worker: takes uploaded transcripts off the job queue and runs the agent on them.

    GUI upload ──▶ jobs table (queued) ──▶ worker thread claims one ──▶ PodcastAgent.run
                                                                         │
           report.json / report.md on disk ◀── write_episode ◀───────────┤
           episodes, claims, notes, lines in the store ◀── save_episode ◀┘

The same loop is what a Kubernetes worker Deployment runs (scaled on queue depth by KEDA);
only the store backend changes. Reliability rules:
- a bad file or a crashed episode fails *that job* with a reason; the worker keeps going;
- SIGTERM/Ctrl-C stops taking new jobs and lets running ones finish (graceful shutdown);
- jobs left 'running' by a worker that died are requeued at start-up, up to MAX_JOB_ATTEMPTS.
"""

from __future__ import annotations

import logging
import signal
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import PodcastAgent
from .config import Settings
from .ingest import load_episode
from .render import write_episode
from .store import ResultStore, SQLiteStore, now

AgentFactory = Callable[[str, bool], PodcastAgent]   # (client id, web search on?) -> agent


class JobProgress(logging.Handler):
    """Mirrors each episode's latest log line into its job row, so the GUI can show live status."""

    def __init__(self, store: ResultStore):
        super().__init__(logging.INFO)
        self.store = store
        self.jobs: dict[str, int] = {}
        self._lock = threading.Lock()

    def track(self, episode_id: str, job_id: int) -> None:
        with self._lock:
            self.jobs[episode_id] = job_id

    def untrack(self, episode_id: str) -> None:
        with self._lock:
            self.jobs.pop(episode_id, None)

    def emit(self, record: logging.LogRecord) -> None:
        job_id = self.jobs.get(getattr(record, "episode", ""))
        if job_id is None:
            return
        try:
            self.store.update_job(job_id, message=" ".join(record.getMessage().split())[:200])
        except Exception:  # noqa: BLE001 — status updates are best-effort and must never stop a run
            pass


def process_job(job: dict, store: ResultStore, make_agent: AgentFactory, settings: Settings,
                logger: logging.Logger, progress: JobProgress | None = None) -> str:
    """Run one job to an end state. Never raises: every failure is recorded on the job."""
    job_id = job["id"]
    try:
        episode = load_episode(job["path"])
    except Exception as exc:  # noqa: BLE001 — wrong type, broken JSON, empty, too large ...
        store.update_job(job_id, status="failed", error=f"could not read the transcript: {exc}",
                         message="rejected at ingest", finished_at=now())
        logger.warning("job %s rejected: %s", job_id, exc, extra={"episode": "worker"})
        return "failed"

    store.update_job(job_id, episode_id=episode.episode_id, message="analysing transcript")
    if progress:
        progress.track(episode.episode_id, job_id)
    try:
        agent = make_agent(job["client"], bool(job["web_search"]))
        report = agent.run(episode, trace_path=settings.output_dir / episode.episode_id / "trace.jsonl")
        write_episode(report, settings.output_dir)
        store.save_episode(report, episode)
    except Exception as exc:  # noqa: BLE001 — agent.run already turns model errors into a 'failed' report
        logger.exception("job %s crashed", job_id, extra={"episode": episode.episode_id})
        store.update_job(job_id, status="failed", error=str(exc)[:500], message="crashed", finished_at=now())
        return "failed"
    finally:
        if progress:
            progress.untrack(episode.episode_id)
    store.update_job(job_id, status=report.status, error=report.error, cost=report.metrics.estimated_cost_usd,
                     message=f"{report.status}: {len(report.fact_checks)} claims checked", finished_at=now())
    return report.status


def run_worker(store: SQLiteStore, settings: Settings, logger: logging.Logger, make_agent: AgentFactory,
               once: bool = False, poll_seconds: float = 2.0, stop: threading.Event | None = None) -> int:
    """Run `settings.concurrency` worker threads until stopped. With once=True, drain the queue and return.
    Returns the number of jobs processed."""
    stop = stop or threading.Event()
    requeued = store.requeue_stale()
    if requeued:
        logger.warning("requeued %d job(s) left running by a stopped worker", requeued, extra={"episode": "worker"})
    progress = JobProgress(store)
    logger.addHandler(progress)
    processed = 0
    count_lock = threading.Lock()

    def loop() -> None:
        nonlocal processed
        while not stop.is_set():
            job = store.claim_job()
            if job is None:
                if once:
                    return
                stop.wait(poll_seconds)
                continue
            logger.info("job %s: %s (client %s)", job["id"], job["filename"], job["client"], extra={"episode": "worker"})
            process_job(job, store, make_agent, settings, logger, progress)
            with count_lock:
                processed += 1

    if threading.current_thread() is threading.main_thread() and not once:
        def _graceful(signum, _frame):
            logger.info("signal %s: finishing running jobs, taking no new ones", signum, extra={"episode": "worker"})
            stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _graceful)

    threads = max(1, settings.concurrency)
    logger.info("worker started: %d thread(s), store %s", threads, store.path, extra={"episode": "worker"})
    try:
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="worker") as pool:
            for f in [pool.submit(loop) for _ in range(threads)]:
                f.result()
    finally:
        logger.removeHandler(progress)
    logger.info("worker stopped after %d job(s)", processed, extra={"episode": "worker"})
    return processed


def save_upload(store: ResultStore, uploads_dir: Path, filename: str, data: bytes, client: str,
                web_search: bool) -> int:
    """Store an uploaded file under a safe name and queue it. Used by the GUI."""
    from .ingest import safe_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    name = Path(filename).name
    stem, suffix = safe_id(Path(name).stem) or "upload", Path(name).suffix.lower()
    if suffix not in (".json", ".txt"):
        raise ValueError(f"{name}: only .json and .txt transcripts are accepted")
    # Each upload gets its own folder, so the file keeps its own name (the episode id comes from it)
    # and two uploads of the same file never overwrite each other.
    folder = Path(tempfile.mkdtemp(prefix=now().replace(":", "").replace("-", "") + "_", dir=uploads_dir))
    path = folder / f"{stem}{suffix}"
    path.write_bytes(data)
    return store.enqueue(name, str(path), client, web_search)
