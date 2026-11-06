"""Report rendering: the reading-time line under the summary."""

import json
from pathlib import Path

import pytest

from podcast_agent.render import reading_minutes, to_markdown
from podcast_agent.schemas import EpisodeReport

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_run" / "all_reports.json"


@pytest.mark.parametrize(("words", "minutes"), [(0, 1), (50, 1), (250, 1), (300, 2), (450, 2), (600, 3)])
def test_reading_minutes(words, minutes):
    assert reading_minutes(words) == minutes


def test_summary_shows_reading_time():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    reports = data if isinstance(data, list) else data.get("reports", data)
    report = EpisodeReport.model_validate(reports[0])
    md = to_markdown(report)
    assert f"{report.summary.word_count} words · about {reading_minutes(report.summary.word_count)} min read" in md
