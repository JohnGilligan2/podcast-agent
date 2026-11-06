"""Per-client source policy: domain tiers, blocked domains, confidence caps and pinned facts."""

import json
from pathlib import Path

import pytest

from podcast_agent.ingest import load_episode
from podcast_agent.knowledge_base import KnowledgeBase
from podcast_agent.prompts import build_user_prompt
from podcast_agent.sources import ClientSources, domain_of
from podcast_agent.tools import ToolExecutor, ToolRejected

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ROOT / "data" / "clients"


@pytest.fixture
def default():
    return ClientSources.load(CLIENTS / "default.json")


@pytest.fixture
def strict():
    return ClientSources.load(CLIENTS / "example_healthtech.json")


@pytest.fixture
def executor(default):
    ep = load_episode(ROOT / "data/transcripts/ep002_ai_healthcare.json")
    ex = ToolExecutor(ep, KnowledgeBase.load(ROOT / "data/knowledge_base.json"), default)
    ts = ep.utterances[3].timestamp
    ex.register_claims({"claims": [{"claim": "The FDA has cleared AI devices", "speaker": "x",
                                    "timestamp": ts, "claim_type": "factual"}]})
    return ex


def verdict(**kw):
    base = {"claim_id": "C1", "verdict": "verified", "confidence": 0.9, "rationale": "r",
            "evidence_ids": [], "needs_human_review": False, "review_reason": ""}
    return {**base, **kw}


def test_domain_matching_covers_subdomains(default):
    assert domain_of("https://www.fda.gov/x") == "fda.gov"
    assert default.is_trusted("https://accessdata.fda.gov/scripts/cdrh")
    assert not default.is_trusted("https://notfda.gov/")
    assert default.is_blocked("https://www.reddit.com/r/medicine")


def test_web_search_tool_sends_allow_or_block_list_never_both(default, strict):
    open_tool = default.web_search_tool(8)
    assert open_tool["blocked_domains"] and "allowed_domains" not in open_tool
    strict_tool = strict.web_search_tool(8)
    assert "fda.gov" in strict_tool["allowed_domains"] and "blocked_domains" not in strict_tool


def test_invalid_policies_are_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"client_id": "x", "trusted_domains": ["a.com"], "blocked_domains": ["a.com"]}))
    with pytest.raises(ValueError, match="both trusted and blocked"):
        ClientSources.load(bad)
    bad.write_text(json.dumps({"client_id": "x", "search_mode": "trusted_only"}))
    with pytest.raises(ValueError, match="at least one trusted domain"):
        ClientSources.load(bad)


def test_blocked_results_never_become_evidence(executor):
    dropped = executor.register_web_results([
        {"url": "https://www.reddit.com/r/x", "title": "thread"},
        {"url": "https://www.fda.gov/ai-devices", "title": "FDA list"},
        {"url": "https://example-blog.com/post", "title": "blog"},
    ])
    assert dropped == ["https://www.reddit.com/r/x"]
    seen = executor.state.seen_sources
    assert "https://www.reddit.com/r/x" not in seen
    assert seen["https://www.fda.gov/ai-devices"].tier == "trusted_web"
    assert seen["https://example-blog.com/post"].tier == "other_web"
    with pytest.raises(ToolRejected, match="never returned by a search"):
        executor.record_verdict(verdict(evidence_ids=["https://www.reddit.com/r/x"]))


def test_untrusted_only_evidence_is_capped_and_sent_to_review(executor):
    executor.register_web_results([{"url": "https://example-blog.com/post", "title": "blog"}])
    with pytest.raises(ToolRejected, match="at most 0.6"):
        executor.record_verdict(verdict(evidence_ids=["https://example-blog.com/post"]))
    executor.record_verdict(verdict(evidence_ids=["https://example-blog.com/post"], confidence=0.55))
    fc = executor.state.verdicts["C1"]
    assert fc.needs_human_review and "trusted list" in fc.review_reason


def test_trusted_evidence_keeps_full_confidence(executor):
    executor.register_web_results([{"url": "https://www.fda.gov/ai-devices", "title": "FDA list"},
                                   {"url": "https://example-blog.com/post", "title": "blog"}])
    executor.record_verdict(verdict(evidence_ids=["https://www.fda.gov/ai-devices", "https://example-blog.com/post"]))
    assert executor.state.verdicts["C1"].confidence == 0.9
    assert not executor.state.verdicts["C1"].needs_human_review


def test_review_flag_needs_a_reason(executor):
    with pytest.raises(ToolRejected, match="review_reason"):
        executor.record_verdict(verdict(verdict="unverifiable", confidence=0.4, needs_human_review=True))
    executor.record_verdict(verdict(verdict="unverifiable", confidence=0.4, needs_human_review=True,
                                    review_reason="Pinned fact and live result disagree"))
    assert executor.state.verdicts["C1"].review_reason == "Pinned fact and live result disagree"


def test_pinned_facts_are_searchable_with_their_tier(strict):
    kb = KnowledgeBase.load(ROOT / "data/knowledge_base.json").with_documents(strict.pinned_facts)
    hits = kb.search("Northwind Triage FDA cleared symptom checker")
    assert hits[0].doc.id == "pin-northwind-triage-status" and hits[0].as_dict()["tier"] == "pinned"


def test_prompt_carries_the_client_policy(strict):
    ep = load_episode(ROOT / "data/transcripts/ep002_ai_healthcare.json")
    prompt = build_user_prompt(ep, "2026-11-05", True, strict)
    assert "Northwind Health" in prompt and "fda.gov" in prompt and "reddit.com" in prompt
    assert "Offline mode" in build_user_prompt(ep, "2026-11-05", False, strict)
