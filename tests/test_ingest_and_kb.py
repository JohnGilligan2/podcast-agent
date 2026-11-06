from pathlib import Path

import pytest

from podcast_agent.ingest import clean_text, load_episode, normalize_timestamp
from podcast_agent.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[1]
MESSY = ROOT / "data/samples/ep900_messy.txt"


@pytest.fixture(scope="module")
def messy():
    return load_episode(MESSY)


@pytest.mark.parametrize("raw, expected", [
    ("0:00", "00:00:00"), ("00:12", "00:00:12"), ("1:05", "00:01:05"),
    ("[00:02:14]", "00:02:14"), ("00:02:30.500", "00:02:30"), ("[1:02:03,250]", "01:02:03"),
])
def test_every_timestamp_format_normalises(raw, expected):
    assert normalize_timestamp(raw) == expected


def test_bad_timestamp_rejected():
    with pytest.raises(ValueError):
        normalize_timestamp("1:75")


def test_pure_filler_and_stutters():
    assert clean_text("Yes, um, it's launching — uh, soon") == "Yes, it's launching - soon"
    assert clean_text("Welcome back to, uh, the show.") == "Welcome back to the show."
    assert clean_text("that's that's exciting") == "that's exciting"
    assert clean_text("I I think so") == "I think so"
    assert clean_text("I like the, the momentum") == "I like the momentum"
    assert clean_text("Umbrella humming") == "Umbrella humming"  # no false positives inside words


def test_context_filler_only_removed_when_it_is_filler():
    assert clean_text("Thanks. So, like, Amazon's ad business, you know, passed $50 billion.") == \
        "Thanks. Amazon's ad business passed $50 billion."
    assert clean_text("I like the momentum there.") == "I like the momentum there."
    assert clean_text("So NASA announced a mission.") == "So NASA announced a mission."


def test_header_and_speaker_variants_map_to_full_names(messy):
    assert messy.title == "Retail Media Roundup" and messy.host == "Jordan Lee"
    mapped = messy.ingest_report["speakers_mapped"]
    assert mapped["JORDAN"] == "Jordan Lee"
    assert mapped["Dr. Okafor"] == "Dr. Maya Okafor"
    assert mapped["Maya"] == "Dr. Maya Okafor"
    assert mapped["Tom"] == "Tom Brandt"   # dash separator: "00:31 Tom - ..."


def test_filler_only_lines_are_dropped_and_logged(messy):
    dropped = {d["raw_text"] for d in messy.ingest_report["lines_dropped"]}
    assert dropped == {"Um. Uh. Yeah.", "Hmm. [crosstalk] Okay."}
    assert all("Um. Uh." not in u.text for u in messy.utterances)


def test_missing_timestamp_inherits_previous_and_is_flagged(messy):
    last = messy.utterances[-1]
    assert last.text == "Thanks everyone, that's the show."
    assert last.timestamp == "00:02:30" and "no timestamp" in last.flags[0]


def test_raw_text_is_never_modified(messy):
    lines = [ln for ln in MESSY.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for u in messy.utterances:
        assert any(u.raw_text in ln for ln in lines)
    tom = next(u for u in messy.utterances if "async" in u.raw_text)
    assert tom.raw_text == "We use async yo for for our pipelines, it’s fast."   # transcription error kept
    assert tom.clean_text == "We use async yo for our pipelines, it's fast."


def test_duplicate_timestamps_are_kept_in_order(tmp_path):
    f = tmp_path / "dup.txt"
    f.write_text("[02:10] A: First point here.\n[02:10] B: Second point here.\n", encoding="utf-8")
    ep = load_episode(f)
    assert [u.speaker for u in ep.find("02:10")] == ["A", "B"]
    assert ep.title == "dup" and ep.guests == ["A", "B"]   # no header: filename + speakers found


def test_load_json_resolves_speakers():
    ep = load_episode(ROOT / "data/transcripts/ep002_ai_healthcare.json")
    assert ep.episode_id == "ep002"
    assert ep.utterances[1].speaker == "Dr. Priya Patel"
    assert ep.utterances[0].role == "host"
    assert ep.find("00:55")[0].speaker == "Dr. Priya Patel"


def test_load_simple_txt_format():
    ep = load_episode(ROOT / "data/samples/ep000_mars_sample.txt")
    assert ep.title == "Space Weekly - Mars Update"
    assert ep.utterances[0].timestamp == "00:02:14"
    assert "um" not in ep.utterances[1].text.split()
    assert ep.cleaning_edits >= 2


def test_kb_search_ranks_relevant_doc_first():
    kb = KnowledgeBase.load(ROOT / "data/knowledge_base.json")
    assert kb.search("GitLab handbook remote")[0].doc.id == "kb-gitlab-all-remote"
    assert kb.search("FDA autonomous AI diagnosis")[0].doc.id == "kb-idx-dr-autonomous"
    assert kb.search("Mailchimp bootstrapped acquisition")[0].doc.id == "kb-mailchimp-bootstrapped"
    assert kb.search("zebra quantum lasagna") == []
