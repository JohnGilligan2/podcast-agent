"""Guardrail tests and an end-to-end agent-loop test with a scripted (fake) model."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from podcast_agent.agent import PodcastAgent
from podcast_agent.config import Settings
from podcast_agent.ingest import load_episode
from podcast_agent.knowledge_base import KnowledgeBase
from podcast_agent.render import to_markdown
from podcast_agent.tools import ToolExecutor, ToolRejected, quote_matches, x_weighted_length

ROOT = Path(__file__).resolve().parents[1]
GOOD_SUMMARY = " ".join(["word"] * 240)
GOOD_TWEET = "Remote work runs on writing things down: GitLab's public handbook shows how. #RemoteWork"


@pytest.fixture
def episode():
    return load_episode(ROOT / "data/transcripts/ep001_remote_work.json")


@pytest.fixture
def kb():
    return KnowledgeBase.load(ROOT / "data/knowledge_base.json")


def test_quote_matching():
    line = "They help, but tools aren’t the solution. Culture is."
    assert quote_matches("tools aren't the solution. Culture is.", line)
    assert quote_matches("They help ... Culture is", line)
    assert not quote_matches("Tools are never the solution", line)


def test_summary_word_limits(episode, kb):
    ex = ToolExecutor(episode, kb)
    with pytest.raises(ToolRejected, match="must be 200-300"):
        ex.submit_summary({"summary": "too short", "core_themes": ["a", "b"], "key_discussions": ["x"], "outcomes": ["y"]})
    assert "accepted" in ex.submit_summary(
        {"summary": GOOD_SUMMARY, "core_themes": ["a", "b"], "key_discussions": ["x"], "outcomes": ["y"]})


def test_key_notes_reject_paraphrased_quote(episode, kb):
    ex = ToolExecutor(episode, kb)
    args = {"takeaways": ["1", "2", "3", "4", "5"],
            "quotes": [{"timestamp": "02:45", "speaker": "Mark", "quote": "Tools never fix anything", "why_notable": "x"},
                       {"timestamp": "03:25", "speaker": "Mark", "quote": "Outcomes over hours.", "why_notable": "x"}],
            "topics": ["Remote Work", "async", "#culture"]}
    with pytest.raises(ToolRejected, match="not verbatim"):
        ex.submit_key_notes(args)
    args["quotes"][0]["quote"] = "tools aren’t the solution. Culture is."
    ex.submit_key_notes(args)
    assert ex.state.key_notes.topics == ["remote-work", "async", "culture"]
    assert ex.state.key_notes.quotes[0].speaker == "Mark Rivera"


def test_verdict_requires_retrieved_evidence(episode, kb):
    ex = ToolExecutor(episode, kb)
    ex.register_claims({"claims": [{"claim": "GitLab has a public handbook", "speaker": "Mark",
                                    "timestamp": "01:20", "claim_type": "factual"}]})
    base = {"claim_id": "C1", "verdict": "verified", "confidence": 0.9, "rationale": "r"}
    with pytest.raises(ToolRejected, match="needs at least one"):
        ex.record_verdict({**base, "evidence_ids": []})
    with pytest.raises(ToolRejected, match="never returned"):
        ex.record_verdict({**base, "evidence_ids": ["kb-gitlab-all-remote"]})  # not searched yet
    ex.search_knowledge_base({"query": "GitLab handbook"})
    ex.record_verdict({**base, "evidence_ids": ["kb-gitlab-all-remote"]})
    assert ex.state.verdicts["C1"].verdict_label.startswith("✅")


def test_finish_blocked_until_complete(episode, kb):
    ex = ToolExecutor(episode, kb)
    with pytest.raises(ToolRejected, match="Missing"):
        ex.finish({"reviewer_notes": []})


# ---------- end-to-end loop with a scripted model -----------------------------------------

def _tool(i, name, inp):
    return NS(type="tool_use", id=f"t{i}", name=name, input=inp)


def _resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, *, system, tools, messages):
        self.calls.append(messages[-1])
        return self.responses.pop(0)


def happy_script():
    """A full episode for ep001: plan, a rejected summary, the fix, notes, one claim, search, verdict, finish."""
    notes = {"takeaways": ["a", "b", "c", "d", "e"],
             "quotes": [{"timestamp": "03:25", "speaker": "Mark", "quote": "Outcomes over hours.", "why_notable": "x"},
                        {"timestamp": "04:10", "speaker": "Mark", "quote": "Remote doesn’t mean isolated", "why_notable": "y"}],
             "topics": ["remote-work", "async", "culture"]}
    return [
        _resp(NS(type="thinking", thinking="Plan first."),
              _tool(1, "create_plan", {"steps": [{"action": "summarise", "tool": "submit_summary", "rationale": "r"}]})),
        _resp(_tool(2, "submit_summary", {"summary": "short", "core_themes": ["a", "b"],
                                          "key_discussions": ["x"], "outcomes": ["y"]})),  # rejected
        _resp(_tool(3, "submit_summary", {"summary": GOOD_SUMMARY, "core_themes": ["a", "b"],
                                          "key_discussions": ["x"], "outcomes": ["y"]}),
              _tool(4, "submit_key_notes", notes),
              _tool(5, "register_claims", {"claims": [{"claim": "GitLab has a public handbook", "speaker": "Mark",
                                                       "timestamp": "01:20", "claim_type": "factual"}]})),
        _resp(_tool(6, "search_knowledge_base", {"query": "GitLab handbook"})),
        _resp(_tool(7, "record_verdict", {"claim_id": "C1", "verdict": "verified", "confidence": 0.9,
                                          "rationale": "KB confirms", "evidence_ids": ["kb-gitlab-all-remote"]}),
              _tool(8, "submit_tweet", {"tweet": GOOD_TWEET})),
        _resp(_tool(9, "finish", {"reviewer_notes": ["none"]})),
    ]


def test_agent_loop_self_corrects_and_completes(episode, kb, tmp_path):
    llm = ScriptedLLM(happy_script())
    agent = PodcastAgent(llm, kb, Settings(), today="2026-11-05")
    report = agent.run(episode, trace_path=tmp_path / "trace.jsonl")

    assert report.status == "complete"
    assert report.metrics.rejected_tool_calls == 1
    rejected = llm.calls[2]["content"][0]  # tool_result sent back after the short summary
    assert rejected["is_error"] is True and "must be 200-300" in rejected["content"]
    assert len(report.fact_checks) == 1 and report.fact_checks[0].evidence[0].source_id == "kb-gitlab-all-remote"
    assert report.tweet == GOOD_TWEET
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert trace.count('"event": "tool_call"') == 9
    # latency is always recorded, telemetry or not
    m = report.metrics
    assert len(m.turn_seconds) == 6 and m.model_seconds >= 0 and "submit_summary" in m.tool_seconds_by_name
    assert trace.count('"event": "model_call"') == 6 and '"duration_ms"' in trace


def test_unknown_blocks_and_missing_search_results_reach_the_trace(episode, kb, tmp_path):
    """A block type the logger doesn't know (e.g. a code execution result) must still be traced,
    and a web search that returns no result block must be flagged, never silently dropped."""
    script = happy_script()
    script[0].content[:0] = [NS(type="server_tool_use", id="s1", name="web_search", input={"query": "q"}),
                             NS(type="code_execution_tool_result", content="TypeError: str has no .get")]
    report = PodcastAgent(ScriptedLLM(script), kb, Settings(), today="2026-11-05").run(
        episode, trace_path=tmp_path / "trace.jsonl")
    assert 0 <= report.metrics.duration_seconds < 60   # the balance check must not clobber the start time
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "other_block"' in trace and "code_execution_tool_result" in trace
    assert '"event": "web_search_mismatch"' in trace and '"started": 1' in trace and '"returned": 0' in trace


def test_agent_nudges_when_model_stops_early(episode, kb):
    llm = ScriptedLLM([_resp(NS(type="text", text="All done!"), stop="end_turn")] * 3)
    report = PodcastAgent(llm, kb, Settings()).run(episode)
    assert report.status == "failed" and "without finishing" in report.error
    assert "You have not called finish" in llm.calls[1]["content"]


# ---------- tweet-sized summary -------------------------------------------------------------

def _fact_checked(episode, kb) -> ToolExecutor:
    """An executor where the one claim already has a verdict, so a tweet may be submitted."""
    ex = ToolExecutor(episode, kb)
    ex.execute("register_claims", {"claims": [{"claim": "GitLab has a public handbook", "speaker": "Mark",
                                               "timestamp": "01:20", "claim_type": "factual"}]})
    ex.execute("search_knowledge_base", {"query": "GitLab handbook"})
    ex.execute("record_verdict", {"claim_id": "C1", "verdict": "verified", "confidence": 0.9,
                                  "rationale": "KB confirms", "evidence_ids": ["kb-gitlab-all-remote"]})
    return ex


def test_x_weighted_length_counts_emoji_and_cjk_as_two():
    assert x_weighted_length("a" * 280) == 280
    assert x_weighted_length("café — “quoted”") == 15          # accents and punctuation weigh 1
    assert x_weighted_length("🎙️") >= 2 and x_weighted_length("播客") == 4


@pytest.mark.parametrize("tweet, reason", [
    ("x" * 281, "281 characters"),
    ("🎙" * 141, "282 characters"),                       # 141 emoji = 282 as X counts them
    ("Great episode #a #b #c", "3 hashtags"),
    ("Listen now https://example.com/ep1", "remove the link"),
    ("   ", "tweet is empty"),
])
def test_tweet_rejected_with_a_reason(episode, kb, tweet, reason):
    ex = _fact_checked(episode, kb)
    with pytest.raises(ToolRejected, match=reason):
        ex.execute("submit_tweet", {"tweet": tweet})
    assert ex.state.tweet is None


def test_tweet_at_the_limit_is_accepted(episode, kb):
    ex = _fact_checked(episode, kb)
    assert "280 of 280" in ex.execute("submit_tweet", {"tweet": "y" * 280})
    assert "140 of 280" in ex.execute("submit_tweet", {"tweet": "🎙" * 70})   # resubmitting replaces it
    assert ex.state.tweet == "🎙" * 70


def test_tweet_waits_for_the_fact_check(episode, kb):
    ex = ToolExecutor(episode, kb)
    with pytest.raises(ToolRejected, match="register_claims first"):
        ex.execute("submit_tweet", {"tweet": GOOD_TWEET})
    ex.execute("register_claims", {"claims": [{"claim": "GitLab has a public handbook", "speaker": "Mark",
                                               "timestamp": "01:20", "claim_type": "factual"}]})
    with pytest.raises(ToolRejected, match="record verdicts for C1 first"):
        ex.execute("submit_tweet", {"tweet": GOOD_TWEET})


def test_finish_requires_a_tweet(episode, kb):
    ex = _fact_checked(episode, kb)
    assert "tweet (submit_tweet)" in ex.state.missing()
    ex.execute("submit_tweet", {"tweet": GOOD_TWEET})
    assert "tweet (submit_tweet)" not in ex.state.missing()


def test_agent_fixes_a_rejected_tweet(episode, kb, tmp_path):
    """End to end: the model's first tweet is too long, the rejection goes back as is_error,
    and the corrected tweet is what reaches the report and the markdown."""
    script = happy_script()
    script[-2].content.insert(1, _tool(80, "submit_tweet", {"tweet": "z" * 300}))   # before the good one
    llm = ScriptedLLM(script)
    report = PodcastAgent(llm, kb, Settings(), today="2026-11-05").run(episode)
    assert report.status == "complete" and report.tweet == GOOD_TWEET
    assert report.metrics.rejected_tool_calls == 2          # the short summary and the long tweet
    sent_back = llm.calls[-1]["content"]                   # tool_results for the verdict + tweets turn
    assert any(r.get("is_error") and "300 characters" in r["content"] for r in sent_back)
    assert "## 🐦 Tweet" in to_markdown(report)
