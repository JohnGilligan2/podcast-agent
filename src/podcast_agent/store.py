"""Results store: finished runs, their transcripts, editor reviews and disputes, and the job queue.

The agent, the worker and the GUI all go through `ResultStore`, so the backend can change
without touching them. Today it ships SQLite (one file on a Docker volume, no extra service).
Production swaps in Postgres for the tables and OpenSearch for search, behind the same methods.

SQLite rules followed here, for reliability under several threads and two containers:
- one short-lived connection per call (connections are never shared between threads);
- WAL journal + a 30 s busy timeout, so a reader never blocks a writer;
- every multi-row write is one transaction, so a crash never leaves half an episode;
- job claiming is a single UPDATE ... RETURNING, so two workers can never take the same job.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .ingest import Episode
from .schemas import EpisodeReport

JOB_STATES = ("queued", "running", "complete", "incomplete", "failed")
DISPUTE_STATES = ("open", "resolved")
MAX_JOB_ATTEMPTS = 3   # a job whose worker died this many times is marked failed, not retried forever

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, client TEXT, title TEXT, host TEXT, guests TEXT,
    processed_at TEXT, model TEXT, cost REAL, status TEXT,
    code_version TEXT, prompt_version TEXT, source_policy_version TEXT,
    source_file TEXT, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_lines (
    episode_id TEXT NOT NULL, idx INTEGER NOT NULL, timestamp TEXT, speaker TEXT,
    raw_text TEXT, clean_text TEXT, flags TEXT, PRIMARY KEY (episode_id, idx)
);
CREATE TABLE IF NOT EXISTS notes (
    episode_id TEXT PRIMARY KEY, summary TEXT, takeaways TEXT, quotes TEXT, tags TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    episode_id TEXT NOT NULL, claim_id TEXT NOT NULL, claim TEXT, speaker TEXT, timestamp TEXT,
    type TEXT, verdict TEXT, confidence REAL, rationale TEXT, sources TEXT, needs_review INTEGER,
    PRIMARY KEY (episode_id, claim_id)
);
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT NOT NULL,
    target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_text TEXT,
    note TEXT, status TEXT NOT NULL DEFAULT 'open', who TEXT, created_at TEXT,
    resolved_by TEXT, resolved_at TEXT, resolution TEXT
);
CREATE INDEX IF NOT EXISTS disputes_episode ON disputes (episode_id);
CREATE TABLE IF NOT EXISTS reviews (
    episode_id TEXT NOT NULL, claim_id TEXT NOT NULL, who TEXT, reviewed_at TEXT,
    PRIMARY KEY (episode_id, claim_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, path TEXT, client TEXT,
    web_search INTEGER, status TEXT NOT NULL DEFAULT 'queued', message TEXT, episode_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT, started_at TEXT, finished_at TEXT,
    cost REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    episode_id UNINDEXED, kind UNINDEXED, ref UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class SearchHit:
    episode_id: str
    kind: str        # episode | quote | claim | line
    ref: str         # claim id, quote timestamp or line timestamp
    text: str
    verdict: str | None = None


class ResultStore(Protocol):
    """What the agent, worker and GUI need from storage. SQLiteStore implements it today."""

    def save_episode(self, report: EpisodeReport, episode: Episode | None = None) -> None: ...
    def get_report(self, episode_id: str) -> EpisodeReport | None: ...
    def list_episodes(self, client: str | None = None) -> list[dict]: ...
    def transcript(self, episode_id: str) -> list[dict]: ...
    def search(self, query: str, verdicts: list[str] | None = None, limit: int = 100) -> list[SearchHit]: ...
    def add_dispute(self, episode_id: str, target_kind: str, target_id: str, target_text: str,
                    note: str, who: str) -> int: ...
    def resolve_dispute(self, dispute_id: int, who: str, resolution: str) -> None: ...
    def reopen_dispute(self, dispute_id: int) -> None: ...
    def mark_reviewed(self, episode_id: str, claim_id: str, who: str) -> None: ...
    def unmark_reviewed(self, episode_id: str, claim_id: str) -> None: ...
    def reviews(self, episode_id: str) -> dict[str, dict]: ...
    def disputes(self, episode_id: str | None = None, status: str | None = None) -> list[dict]: ...
    def enqueue(self, filename: str, path: str, client: str, web_search: bool) -> int: ...
    def claim_job(self) -> dict | None: ...
    def update_job(self, job_id: int, **fields) -> None: ...
    def jobs(self, limit: int = 200) -> list[dict]: ...


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word must appear, each matched as a prefix.
    Quotes, stars, colons and operators typed by a user can never break the query syntax."""
    words = re.findall(r"\w+", text.lower())
    return " ".join(f'"{w}"*' for w in words[:20])


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            try:
                c.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:   # SQLite built without FTS5: fall back to LIKE search
                self.fts = False

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:   # commit on success, roll back on any exception
                yield conn
        finally:
            conn.close()

    # ---- episodes ---------------------------------------------------------------------------

    def save_episode(self, report: EpisodeReport, episode: Episode | None = None) -> None:
        """Insert or replace one episode and everything derived from it, in one transaction.
        A rerun replaces the previous result; disputes are kept (they carry a copy of their text).
        Reviews are cleared: a new run can change the verdicts, so a person has to look again."""
        eid = report.episode_id
        with self._conn() as c:
            for table in ("transcript_lines", "notes", "claims", "reviews"):
                c.execute(f"DELETE FROM {table} WHERE episode_id = ?", (eid,))
            if self.fts:
                c.execute("DELETE FROM search_index WHERE episode_id = ?", (eid,))
            c.execute(
                "INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, report.client_id, report.title, report.host, json.dumps(report.guests),
                 report.generated_at, report.metrics.model, report.metrics.estimated_cost_usd, report.status,
                 report.code_version, report.prompt_version, report.source_policy_version,
                 report.source_file, report.model_dump_json()))
            index: list[tuple[str, str, str, str]] = [(eid, "episode", eid, f"{report.title} {report.host or ''} "
                                                       f"{' '.join(report.guests)}")]
            if episode is not None:
                c.executemany("INSERT INTO transcript_lines VALUES (?,?,?,?,?,?,?)", [
                    (eid, u.index, u.timestamp, u.speaker, u.raw_text, u.text, json.dumps(u.flags))
                    for u in episode.utterances])
                index += [(eid, "line", u.timestamp, f"{u.speaker}: {u.text}") for u in episode.utterances]
            if report.summary or report.key_notes:
                kn = report.key_notes
                c.execute("INSERT INTO notes VALUES (?,?,?,?,?)", (
                    eid, report.summary.text if report.summary else None,
                    json.dumps(kn.takeaways if kn else []),
                    json.dumps([q.model_dump() for q in kn.quotes] if kn else []),
                    json.dumps(kn.topics if kn else [])))
                if report.summary:
                    index.append((eid, "summary", eid, report.summary.text))
                if kn:
                    index += [(eid, "quote", q.timestamp, f"{q.speaker}: {q.quote}") for q in kn.quotes]
            c.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                (eid, f.claim_id, f.claim, f.speaker, f.timestamp, f.claim_type, f.verdict, f.confidence,
                 f.rationale, json.dumps([e.model_dump() for e in f.evidence]), int(f.needs_human_review))
                for f in report.fact_checks])
            index += [(eid, "claim", f.claim_id, f.claim) for f in report.fact_checks]
            if self.fts:
                c.executemany("INSERT INTO search_index VALUES (?,?,?,?)", index)

    def get_report(self, episode_id: str) -> EpisodeReport | None:
        with self._conn() as c:
            row = c.execute("SELECT report_json FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return EpisodeReport.model_validate_json(row["report_json"]) if row else None

    def list_episodes(self, client: str | None = None) -> list[dict]:
        sql = ("SELECT e.id, e.title, e.client, e.status, e.processed_at, e.model, e.cost, e.code_version, "
               "e.prompt_version, "
               "(SELECT COUNT(*) FROM claims k WHERE k.episode_id = e.id) AS claims, "
               "(SELECT COUNT(*) FROM claims k WHERE k.episode_id = e.id AND k.needs_review = 1 AND NOT EXISTS "
               "(SELECT 1 FROM reviews v WHERE v.episode_id = k.episode_id AND v.claim_id = k.claim_id)) AS needs_review, "
               "(SELECT COUNT(*) FROM disputes d WHERE d.episode_id = e.id AND d.status = 'open') AS open_disputes "
               "FROM episodes e")
        args: tuple = ()
        if client:
            sql, args = sql + " WHERE e.client = ?", (client,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY e.processed_at DESC", args)]

    def transcript(self, episode_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT idx, timestamp, speaker, raw_text, clean_text, flags FROM transcript_lines "
                             "WHERE episode_id = ? ORDER BY idx", (episode_id,)).fetchall()
        return [{**dict(r), "flags": json.loads(r["flags"] or "[]")} for r in rows]

    def search(self, query: str, verdicts: list[str] | None = None, limit: int = 100) -> list[SearchHit]:
        """Full-text search over episodes, summaries, quotes, claims and transcript lines.
        With `verdicts`, only claims with one of those verdicts are returned."""
        q = fts_query(query)
        with self._conn() as c:
            if not q:   # no words typed: list claims (optionally by verdict), newest episodes first
                rows = c.execute("SELECT k.episode_id, 'claim' AS kind, k.claim_id AS ref, k.claim AS text "
                                 "FROM claims k JOIN episodes e ON e.id = k.episode_id "
                                 "ORDER BY e.processed_at DESC, k.claim_id LIMIT ?", (limit * 5,)).fetchall()
            elif self.fts:
                rows = c.execute("SELECT episode_id, kind, ref, text FROM search_index WHERE search_index MATCH ? "
                                 "ORDER BY rank LIMIT ?", (q, limit * 5)).fetchall()
            else:
                like = f"%{query.strip()}%"
                rows = c.execute(
                    "SELECT episode_id, 'claim' AS kind, claim_id AS ref, claim AS text FROM claims WHERE claim LIKE ? "
                    "UNION ALL SELECT episode_id, 'line', timestamp, clean_text FROM transcript_lines "
                    "WHERE clean_text LIKE ? LIMIT ?", (like, like, limit * 5)).fetchall()
            verdict_of = {(r["episode_id"], r["claim_id"]): r["verdict"]
                          for r in c.execute("SELECT episode_id, claim_id, verdict FROM claims")}
        hits = [SearchHit(r["episode_id"], r["kind"], r["ref"], r["text"],
                          verdict_of.get((r["episode_id"], r["ref"])) if r["kind"] == "claim" else None)
                for r in rows]
        if verdicts:
            hits = [h for h in hits if h.kind == "claim" and h.verdict in verdicts]
        return hits[:limit]

    # ---- reviews ----------------------------------------------------------------------------
    # A claim flagged for human review stays flagged until a person signs it off here.
    # (Something actually wrong is a dispute instead.)

    def mark_reviewed(self, episode_id: str, claim_id: str, who: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)",
                      (episode_id, claim_id, who.strip() or "editor", now()))

    def unmark_reviewed(self, episode_id: str, claim_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reviews WHERE episode_id = ? AND claim_id = ?", (episode_id, claim_id))

    def reviews(self, episode_id: str) -> dict[str, dict]:
        """claim_id -> {who, reviewed_at} for every signed-off claim in one episode."""
        with self._conn() as c:
            rows = c.execute("SELECT claim_id, who, reviewed_at FROM reviews WHERE episode_id = ?",
                             (episode_id,)).fetchall()
        return {r["claim_id"]: {"who": r["who"], "reviewed_at": r["reviewed_at"]} for r in rows}

    # ---- disputes ---------------------------------------------------------------------------

    def add_dispute(self, episode_id: str, target_kind: str, target_id: str, target_text: str,
                    note: str, who: str) -> int:
        if target_kind not in ("claim", "quote"):
            raise ValueError("a dispute is about a claim or a quote")
        if not note.strip():
            raise ValueError("a dispute needs a note saying what is wrong")
        with self._conn() as c:
            cur = c.execute("INSERT INTO disputes (episode_id, target_kind, target_id, target_text, note, status, who, "
                            "created_at) VALUES (?,?,?,?,?,'open',?,?)",
                            (episode_id, target_kind, target_id, target_text, note.strip(), who.strip() or "editor", now()))
            return int(cur.lastrowid)

    def resolve_dispute(self, dispute_id: int, who: str, resolution: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE disputes SET status='resolved', resolved_by=?, resolved_at=?, resolution=? WHERE id=?",
                      (who.strip() or "editor", now(), resolution.strip(), dispute_id))

    def reopen_dispute(self, dispute_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE disputes SET status='open', resolved_by=NULL, resolved_at=NULL WHERE id=?", (dispute_id,))

    def disputes(self, episode_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, args, where = "SELECT * FROM disputes", [], []
        if episode_id:
            where.append("episode_id = ?")
            args.append(episode_id)
        if status:
            where.append("status = ?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id DESC", args)]

    # ---- job queue ----------------------------------------------------------------------------

    def enqueue(self, filename: str, path: str, client: str, web_search: bool) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO jobs (filename, path, client, web_search, status, message, created_at) "
                            "VALUES (?,?,?,?,'queued','waiting for a worker',?)",
                            (filename, path, client, int(web_search), now()))
            return int(cur.lastrowid)

    def claim_job(self) -> dict | None:
        """Atomically take the oldest queued job. Safe with any number of worker threads or containers."""
        with self._conn() as c:
            row = c.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1, message='starting' "
                "WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1) AND status='queued' "
                "RETURNING *", (now(),)).fetchone()
        return dict(row) if row else None

    def requeue_stale(self) -> int:
        """At worker start-up: jobs left 'running' by a worker that died go back in the queue,
        unless they have already been tried MAX_JOB_ATTEMPTS times."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='failed', finished_at=?, error='worker stopped during the run too many times' "
                      "WHERE status='running' AND attempts >= ?", (now(), MAX_JOB_ATTEMPTS))
            return c.execute("UPDATE jobs SET status='queued', message='requeued after a worker restart' "
                             "WHERE status='running'").rowcount

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"status", "message", "episode_id", "finished_at", "cost", "error"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown job fields: {sorted(bad)}")
        if not fields:
            return
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      (*fields.values(), job_id))

    def jobs(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


def open_store(path: str | Path) -> SQLiteStore:
    """The one place a backend is chosen. Production would return a Postgres + OpenSearch store here."""
    return SQLiteStore(path)
