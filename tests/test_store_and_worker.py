"""Results store, job queue, worker and golden-set evaluation. No API key needed."""

import json
import logging
import threading
from pathlib import Path

import pytest
from test_tools_and_agent import ScriptedLLM, happy_script

from podcast_agent import CODE_VERSION
from podcast_agent.agent import PodcastAgent
from podcast_agent.config import Settings
from podcast_agent.evaluate import load_reports, run_eval
from podcast_agent.ingest import load_episode
from podcast_agent.knowledge_base import KnowledgeBase
from podcast_agent.prompts import PROMPT_VERSION
from podcast_agent.store import MAX_JOB_ATTEMPTS, SQLiteStore, fts_query
from podcast_agent.worker import process_job, run_worker, save_upload

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_run"
LOG = logging.getLogger("test")


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "var" / "test.db")


@pytest.fixture
def reports():
    return load_reports(SAMPLE)


# ---- store --------------------------------------------------------------------------------------

def test_save_and_read_back_an_episode(store, reports):
    ep = load_episode(ROOT / "data/transcripts/ep001_remote_work.json")
    store.save_episode(reports["ep001"], ep)
    assert store.get_report("ep001") == reports["ep001"]
    lines = store.transcript("ep001")
    assert len(lines) == len(ep.utterances) and lines[0]["raw_text"] == ep.utterances[0].raw_text
    row = store.list_episodes()[0]
    assert row["id"] == "ep001" and row["claims"] == len(reports["ep001"].fact_checks)


def test_rerun_replaces_results_but_keeps_disputes(store, reports):
    store.save_episode(reports["ep001"])
    store.add_dispute("ep001", "claim", "C1", "GitLab handbook", "verdict too confident", "ana")
    store.save_episode(reports["ep001"])
    assert len(store.list_episodes()) == 1
    assert len(store.search("handbook")) == len([h for h in store.search("handbook")])   # no duplicate rows
    assert store.disputes("ep001")[0]["note"] == "verdict too confident"


def test_full_text_search_and_verdict_filter(store, reports):
    for r in reports.values():
        store.save_episode(r)
    hits = store.search("FDA")
    assert any(h.episode_id == "ep002" and h.kind == "claim" for h in hits)
    outdated = store.search("", ["possibly_outdated_or_inaccurate"])
    assert outdated and all(h.verdict == "possibly_outdated_or_inaccurate" for h in outdated)
    assert {h.episode_id for h in outdated} >= {"ep000", "ep002", "ep900"}


@pytest.mark.parametrize("query", ['"', "*", "NEAR(", "AND OR NOT", "ep:001", "'; DROP TABLE claims; --", "  "])
def test_search_never_breaks_on_odd_input(store, reports, query):
    store.save_episode(reports["ep001"])
    store.search(query)   # must not raise
    assert store.get_report("ep001") is not None


def test_fts_query_is_sanitised():
    assert fts_query('He said "50%" -- NEAR(x)') == '"he"* "said"* "50"* "near"* "x"*'


def test_dispute_lifecycle(store):
    with pytest.raises(ValueError):
        store.add_dispute("ep1", "claim", "C1", "x", "   ", "ana")
    with pytest.raises(ValueError):
        store.add_dispute("ep1", "summary", "x", "x", "note", "ana")
    d = store.add_dispute("ep1", "quote", "00:01:00", "a quote", "wrong speaker", "")
    assert store.disputes(status="open")[0]["who"] == "editor"
    store.resolve_dispute(d, "ben", "fixed in transcript")
    assert store.disputes(status="open") == [] and store.disputes(status="resolved")[0]["resolved_by"] == "ben"
    store.reopen_dispute(d)
    assert store.disputes(status="open")[0]["id"] == d


# ---- job queue ------------------------------------------------------------------------------------

def test_two_workers_never_claim_the_same_job(store):
    for i in range(30):
        store.enqueue(f"f{i}.txt", f"/x/f{i}.txt", "default", True)
    claimed, lock = [], threading.Lock()

    def take():
        while (job := store.claim_job()) is not None:
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=take) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == list(range(1, 31))


def test_stale_running_jobs_are_requeued_then_given_up(store):
    job_id = store.enqueue("a.txt", "/x/a.txt", "default", True)
    for _ in range(MAX_JOB_ATTEMPTS - 1):
        store.claim_job()
        assert store.requeue_stale() == 1
    store.claim_job()
    store.requeue_stale()
    assert store.jobs()[0]["status"] == "failed" and store.jobs()[0]["id"] == job_id


def test_unknown_job_fields_are_refused(store):
    job_id = store.enqueue("a.txt", "/x/a.txt", "default", True)
    with pytest.raises(ValueError):
        store.update_job(job_id, path="/etc/passwd")


def test_upload_keeps_its_name_and_rejects_other_types(store, tmp_path):
    data = (ROOT / "data/samples/ep900_messy.txt").read_bytes()
    a = save_upload(store, tmp_path / "up", "ep900_messy.txt", data, "default", False)
    b = save_upload(store, tmp_path / "up", "../../ep900_messy.txt", data, "default", False)
    paths = [Path(j["path"]) for j in store.jobs()]
    assert {p.name for p in paths} == {"ep900_messy.txt"} and len(set(paths)) == 2 and a != b
    assert all((tmp_path / "up") in p.parents for p in paths)
    with pytest.raises(ValueError):
        save_upload(store, tmp_path / "up", "slides.pdf", b"%PDF", "default", True)


# ---- worker ---------------------------------------------------------------------------------------

def _make_agent(tmp_path):
    kb = KnowledgeBase.load(ROOT / "data/knowledge_base.json")
    settings = Settings(output_dir=tmp_path / "out", enable_web_search=False)
    return settings, (lambda client, web: PodcastAgent(ScriptedLLM(happy_script()), kb, settings, LOG))


def test_worker_runs_a_job_end_to_end(store, tmp_path):
    settings, make_agent = _make_agent(tmp_path)
    data = (ROOT / "data/transcripts/ep001_remote_work.json").read_bytes()
    save_upload(store, tmp_path / "up", "ep001_remote_work.json", data, "default", False)
    assert run_worker(store, settings, LOG, make_agent, once=True) == 1
    job = store.jobs()[0]
    assert job["status"] == "complete" and job["episode_id"] == "ep001" and job["finished_at"]
    report = store.get_report("ep001")
    assert report.code_version == CODE_VERSION and report.prompt_version == PROMPT_VERSION
    assert store.transcript("ep001") and (tmp_path / "out" / "ep001" / "report.md").exists()


def test_bad_upload_fails_its_job_and_the_worker_carries_on(store, tmp_path):
    settings, make_agent = _make_agent(tmp_path)
    save_upload(store, tmp_path / "up", "broken.json", b'{"transcript": [', "default", False)
    save_upload(store, tmp_path / "up", "ep001_remote_work.json",
                (ROOT / "data/transcripts/ep001_remote_work.json").read_bytes(), "default", False)
    assert run_worker(store, settings, LOG, make_agent, once=True) == 2
    by_file = {j["filename"]: j for j in store.jobs()}
    assert by_file["broken.json"]["status"] == "failed" and "could not read" in by_file["broken.json"]["error"]
    assert by_file["ep001_remote_work.json"]["status"] == "complete"


def test_crash_inside_the_agent_fails_only_that_job(store, tmp_path):
    settings, _ = _make_agent(tmp_path)

    def exploding(client, web):
        raise RuntimeError("boom")

    job_id = store.enqueue("ep001.json", str(ROOT / "data/transcripts/ep001_remote_work.json"), "default", False)
    assert process_job(store.claim_job(), store, exploding, settings, LOG) == "failed"
    assert store.jobs()[0]["id"] == job_id and "boom" in store.jobs()[0]["error"]


# ---- golden set ---------------------------------------------------------------------------------

def test_committed_sample_run_passes_the_golden_set():
    ok, md = run_eval(SAMPLE, baseline_path=SAMPLE)
    assert ok, md
    assert "Verdicts that changed" in md and "None." in md


def test_regression_is_caught_and_reported(tmp_path):
    data = json.loads((SAMPLE / "all_reports.json").read_text(encoding="utf-8"))
    for r in data:
        for f in r["fact_checks"]:
            if r["episode_id"] == "ep002" and "FDA" in f["claim"]:
                f["verdict"] = "verified"                     # the outdated claim now wrongly 'verified'
        if r["episode_id"] == "ep001":
            r["key_notes"]["quotes"][0]["quote"] = "words nobody said"   # a quote that no longer matches
    (tmp_path / "all_reports.json").write_text(json.dumps(data), encoding="utf-8")
    ok, md = run_eval(tmp_path, baseline_path=SAMPLE)
    assert not ok
    assert "ep002/fda-autonomy" in md and "quote validity" in md and "verdict accuracy dropped" in md
