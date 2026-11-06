"""GUI smoke tests with Streamlit's AppTest (runs the page script headless; no browser, no API key).
Skipped when the optional GUI extra is not installed:  pip install ".[gui]" """

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from podcast_agent.evaluate import load_reports  # noqa: E402
from podcast_agent.ingest import load_episode  # noqa: E402
from podcast_agent.store import SQLiteStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "src" / "podcast_agent" / "gui.py")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "gui.db"
    monkeypatch.setenv("PODCAST_AGENT_DB", str(path))
    store = SQLiteStore(path)
    reports = load_reports(ROOT / "examples" / "sample_run")
    store.save_episode(reports["ep900"], load_episode(ROOT / "data/samples/ep900_messy.txt"))
    store.save_episode(reports["ep002"], load_episode(ROOT / "data/transcripts/ep002_ai_healthcare.json"))
    return store


def app(page: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["page"] = page
    return at.run()


@pytest.mark.parametrize("page", ["Upload", "Jobs", "Report review", "History", "Disputes"])
def test_every_page_renders(db, page):
    at = app(page)
    assert not at.exception, at.exception


def test_report_shows_notes_verdicts_and_raw_vs_clean(db):
    at = app("Report review")
    at.selectbox(key="episode").set_value("ep900").run()
    text = " ".join(m.value for m in at.markdown)
    assert "Top 5 takeaways" in " ".join(s.value for s in at.subheader)
    assert "⚠️ Possibly outdated/inaccurate" in text            # a verdict card
    assert "<del>" in text and "Raw (file)" in text             # side-by-side view strikes removed filler


def test_transcript_html_is_escaped(db, tmp_path):
    from podcast_agent.schemas import EpisodeReport, RunMetrics
    f = tmp_path / "ep_x.txt"
    f.write_text("[00:01] Ann: <script>alert(1)</script> <b>bold</b> um hello\n", encoding="utf-8")
    ep = load_episode(f)
    db.save_episode(EpisodeReport(episode_id=ep.episode_id, title="<img src=x onerror=alert(1)>", host=None,
                                  guests=[], source_file=f.name, status="complete",
                                  metrics=RunMetrics(model="m", effort="low"), generated_at="2099-01-01T00:00:00+00:00"),
                    ep)
    at = app("Report review")
    at.selectbox(key="episode").set_value(ep.episode_id).run()
    html_out = " ".join(m.value for m in at.markdown if "class='tx'" in m.value)
    assert "&lt;script&gt;" in html_out and "<script>" not in html_out and "<b>" not in html_out


def test_show_line_focuses_the_transcript(db):
    at = app("Report review")
    at.selectbox(key="episode").set_value("ep002").run()
    ts = db.get_report("ep002").fact_checks[0].timestamp
    at.button(key="c_jump_C1").click().run()
    assert at.session_state["focus"] == ts
    assert "class='focus" in " ".join(m.value for m in at.markdown)


def test_dispute_is_saved_and_listed(db):
    db.add_dispute("ep002", "claim", "C2", "FDA claim", "verdict should be outdated", "ana")
    at = app("Disputes")
    assert any("verdict should be outdated" in m.value for m in at.markdown)
    at.text_input(key="res_1").input("confirmed")
    next(b for b in at.button if b.label == "Mark resolved").click().run()
    assert not at.exception
    resolved = db.disputes(status="resolved")
    assert resolved and resolved[0]["resolution"] == "confirmed"


def test_history_search_and_verdict_filter(db):
    at = app("History")
    at.text_input(key="h_query").input("FDA").run()
    assert at.dataframe and not at.exception
    at.text_input(key="h_query").input("").run()
    at.multiselect(key="h_verdicts").set_value(["possibly_outdated_or_inaccurate"]).run()
    df = at.dataframe[0].value
    assert len(df) >= 2 and set(df["Episode"]) <= {"ep002", "ep900"}
