"""Thin review GUI (Streamlit). Start with:  python -m podcast_agent gui   (or the `web` compose service)

Pages: Upload · Jobs · Report review · History · Disputes.
The GUI is stateless: uploads go to the shared uploads folder, jobs to the queue, results come from
the store. It never calls Claude itself; the worker does. So web pods and agent workers scale
independently and talk only through the job queue and the results store.
"""

from __future__ import annotations

import difflib
import html
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

from podcast_agent.config import PROJECT_ROOT, Settings
from podcast_agent.ingest import ASCII_MAP, MAX_FILE_BYTES, load_episode
from podcast_agent.render import TIER_TAGS
from podcast_agent.schemas import VERDICT_LABELS
from podcast_agent.store import SQLiteStore, open_store
from podcast_agent.worker import save_upload

PAGES = ["Upload", "Jobs", "Report review", "History", "Disputes"]
STATUS_ICON = {"queued": "🕓", "running": "⏳", "complete": "✅", "incomplete": "⚠️", "failed": "❌"}
VERDICT_ICON = {k: v.split()[0] for k, v in VERDICT_LABELS.items()}

st.set_page_config(page_title="Podcast agent", page_icon="🎙️", layout="wide")
st.markdown("""<style>
.tx {font-size: 0.86rem; border-collapse: collapse; width: 100%;}
.tx td {padding: 3px 6px; vertical-align: top; border-bottom: 1px solid rgba(128,128,128,.18);}
.tx td.ts {white-space: nowrap; opacity: .7; font-family: monospace;}
.tx td.sp {white-space: nowrap; font-weight: 600;}
.tx tr.focus td {background: rgba(255,190,0,.28);}
.tx tr.changed td.raw {border-left: 3px solid rgba(255,120,60,.6);}
.tx del {background: rgba(255,80,80,.18); text-decoration: line-through;}
.tx .flag {font-size: .75rem; opacity: .75;}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_store(path: str) -> SQLiteStore:
    return open_store(path)


settings = Settings.from_env()
store = get_store(str(settings.db_path))


def go(page: str, episode: str | None = None, focus: str | None = None) -> None:
    """Callback: switch page (and optionally the episode / transcript line in focus)."""
    st.session_state.page = page
    if episode:
        st.session_state.episode = episode
    st.session_state.focus = focus


def focus_line(timestamp: str | None) -> None:
    st.session_state.focus = timestamp


def clients() -> list[str]:
    found = sorted(p.stem for p in (PROJECT_ROOT / "data" / "clients").glob("*.json"))
    return found or ["default"]


def elapsed(job: dict) -> str:
    if not job.get("started_at"):
        return ""
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["finished_at"]) if job.get("finished_at") else datetime.now(UTC)
    return f"{(end - start).total_seconds():.0f}s"


def version(value: str | None) -> str:
    return "not recorded" if value in (None, "", "unknown") else value   # results from before v0.1.0


def who() -> str:
    return st.session_state.get("who") or "editor"


# ---- sidebar ---------------------------------------------------------------------------------

if "started" not in st.session_state:   # deep links: ?page=Report%20review&episode=ep002
    st.session_state.started = True
    if st.query_params.get("page") in PAGES:
        st.session_state.page = st.query_params["page"]
    if st.query_params.get("episode"):
        st.session_state.episode = st.query_params["episode"]
st.session_state.setdefault("page", "Upload")
st.session_state.setdefault("focus", None)
with st.sidebar:
    st.title("🎙️ Podcast agent")
    st.radio("Page", PAGES, key="page", label_visibility="collapsed")
    st.text_input("Your name (recorded on disputes)", key="who", placeholder="editor")
    open_disputes = len(store.disputes(status="open"))
    st.caption(f"{len(store.list_episodes())} episodes · {open_disputes} open disputes")
    st.caption(f"Store: `{settings.db_path.name}`")


# ---- Upload ----------------------------------------------------------------------------------

def page_upload() -> None:
    st.header("Upload transcripts")
    st.write("Upload `.json` or `.txt` transcripts. Each file becomes a job; the worker runs the agent on it.")
    c1, c2 = st.columns(2)
    client = c1.selectbox("Client source policy", clients(), key="up_client")
    web = c2.toggle("Live web search", value=True, key="up_web",
                    help="Off = offline mode: the client's pinned facts and the local knowledge base only.")
    files = st.file_uploader("Transcripts", type=["json", "txt"], accept_multiple_files=True, key="up_files")
    if st.button("Start run", type="primary", disabled=not files):
        queued, problems = [], []
        for f in files or []:
            data = f.getvalue()
            if len(data) > MAX_FILE_BYTES:
                problems.append(f"{f.name}: larger than {MAX_FILE_BYTES // 1_000_000} MB")
                continue
            with tempfile.TemporaryDirectory() as tmp:   # check it parses before queueing: instant feedback
                probe = Path(tmp) / Path(f.name).name
                probe.write_bytes(data)
                try:
                    ep = load_episode(probe)
                except Exception as exc:  # noqa: BLE001 — any bad file is reported, never crashes the page
                    problems.append(f"{f.name}: {exc}")
                    continue
            job_id = save_upload(store, settings.uploads_dir, f.name, data, client, web)
            queued.append(f"job {job_id}: {f.name} ({ep.episode_id}, {len(ep.utterances)} lines)")
        for q in queued:
            st.success(f"Queued {q}")
        for p in problems:
            st.error(f"Not queued: {p}")
        if queued:
            st.button("Go to jobs →", on_click=go, args=("Jobs",))
    st.info("The `worker` service processes the queue. Locally: `docker compose up web worker`, "
            "or `python -m podcast_agent worker` next to the GUI.", icon="ℹ️")


# ---- Jobs ------------------------------------------------------------------------------------

@st.fragment(run_every=3)
def jobs_table() -> None:
    jobs = store.jobs()
    if not jobs:
        st.write("No jobs yet. Upload a transcript to start one.")
        return
    st.dataframe([{
        "Job": j["id"], "File": j["filename"], "Status": f"{STATUS_ICON.get(j['status'], '')} {j['status']}",
        "Progress": j["error"] or j["message"] or "", "Time": elapsed(j),
        "Cost $": f"{j['cost']:.3f}" if j["cost"] is not None else "", "Episode": j["episode_id"] or "",
        "Client": j["client"], "Web": "on" if j["web_search"] else "off",
    } for j in jobs], hide_index=True, use_container_width=True)
    active = sum(j["status"] in ("queued", "running") for j in jobs)
    st.caption(f"{active} job(s) waiting or running · refreshes every 3 s")


def page_jobs() -> None:
    st.header("Jobs")
    jobs_table()
    done = [j for j in store.jobs() if j["episode_id"] and j["status"] in ("complete", "incomplete", "failed")
            and store.get_report(j["episode_id"]) is not None]
    if done:
        c1, c2 = st.columns([3, 1])
        pick = c1.selectbox("Open the report for", sorted({j["episode_id"] for j in done}), key="jobs_open")
        c2.button("Open report", on_click=go, args=("Report review", pick), use_container_width=True)


# ---- Report review ---------------------------------------------------------------------------

def _raw_with_removals(raw: str, clean: str) -> str:
    """Raw line with the words cleaning removed struck through, so editors see exactly what went."""
    a, b = raw.split(), clean.split()

    def key(word: str) -> str:   # compare words, not quote style: ’ and ' or — and - are the same word
        return word.translate(ASCII_MAP).lower().strip(".,!?;:")

    out = []
    for op, i1, i2, _j1, _j2 in difflib.SequenceMatcher(a=[key(w) for w in a], b=[key(w) for w in b],
                                                        autojunk=False).get_opcodes():
        chunk = html.escape(" ".join(a[i1:i2]))
        if chunk:
            out.append(f"<del>{chunk}</del>" if op in ("delete", "replace") else chunk)
    return " ".join(out)


def transcript_html(lines: list[dict], view: str, focus: str | None, changed_only: bool) -> str:
    head = {"Side by side": "<th>Time</th><th>Speaker</th><th>Raw (file)</th><th>Clean (what the agent saw)</th>",
            "Clean": "<th>Time</th><th>Speaker</th><th>Clean</th>",
            "Raw": "<th>Time</th><th>Speaker</th><th>Raw</th>"}[view]
    rows = []
    for ln in lines:
        raw, clean = ln["raw_text"] or "", ln["clean_text"] or ""
        changed = " ".join(raw.translate(ASCII_MAP).split()) != clean   # quote style alone is not a change
        if changed_only and not changed:
            continue
        cls = " ".join(filter(None, ["focus" if focus and ln["timestamp"] == focus else "",
                                     "changed" if changed else ""]))
        flags = f"<div class='flag'>⚑ {html.escape('; '.join(ln['flags']))}</div>" if ln["flags"] else ""
        cells = [f"<td class='ts'>{ln['timestamp']}</td>", f"<td class='sp'>{html.escape(ln['speaker'] or '')}</td>"]
        if view == "Side by side":
            cells += [f"<td class='raw'>{_raw_with_removals(raw, clean) if changed else html.escape(raw)}</td>",
                      f"<td>{html.escape(clean)}{flags}</td>"]
        elif view == "Clean":
            cells.append(f"<td>{html.escape(clean)}{flags}</td>")
        else:
            cells.append(f"<td class='raw'>{html.escape(raw)}</td>")
        rows.append(f"<tr class='{cls}'>{''.join(cells)}</tr>")
    return f"<table class='tx'><tr>{head}</tr>{''.join(rows)}</table>"


def dispute_box(episode_id: str, kind: str, target_id: str, text: str, key: str) -> None:
    with st.popover("⚑ Dispute", use_container_width=True):
        with st.form(f"dispute_{key}", clear_on_submit=True):
            st.caption(f"Flag this {kind} for review")
            note = st.text_area("What is wrong?", key=f"note_{key}")
            if st.form_submit_button("Submit dispute"):
                if note.strip():
                    store.add_dispute(episode_id, kind, target_id, text, note, who())
                    st.toast("Dispute saved")
                else:
                    st.warning("Add a note saying what is wrong.")


def page_report() -> None:
    episodes = store.list_episodes()
    if not episodes:
        st.header("Report review")
        st.write("No results yet. Upload a transcript, or run `python -m podcast_agent run`.")
        return
    ids = [e["id"] for e in episodes]
    if st.session_state.get("episode") not in ids:
        st.session_state.episode = ids[0]
    titles = {e["id"]: e["title"] for e in episodes}
    eid = st.selectbox("Episode", ids, key="episode", format_func=lambda i: f"{i} · {titles.get(i, '')}",
                       on_change=focus_line, args=(None,))
    r = store.get_report(eid)
    if r is None:
        st.error("This episode's report could not be read.")
        return
    m = r.metrics
    st.header(r.title)
    st.caption(f"`{r.episode_id}` · {STATUS_ICON.get(r.status, '')} {r.status} · client `{r.client_id}` · "
               f"release `{version(r.code_version)}` · prompt `{version(r.prompt_version)}` · `{m.model}` · "
               f"${m.estimated_cost_usd or 0:.3f} · {m.duration_seconds:.0f}s")
    if r.error:
        st.warning(f"Run error: {r.error}")
    disputes = store.disputes(episode_id=eid)
    open_for = {(d["target_kind"], d["target_id"]) for d in disputes if d["status"] == "open"}
    reviewed = store.reviews(eid)

    left, right = st.columns([11, 9], gap="large")
    with left:
        if r.summary:
            st.subheader("Summary")
            st.write(r.summary.text)
            st.caption(f"{r.summary.word_count} words")
        if r.key_notes:
            st.subheader("Top 5 takeaways")
            st.markdown("\n".join(f"{i}. {t}" for i, t in enumerate(r.key_notes.takeaways, 1)))
            st.subheader("Notable quotes")
            for i, q in enumerate(r.key_notes.quotes):
                with st.container(border=True):
                    st.markdown(f"> “{q.quote}”  \n> — **{q.speaker}**, `{q.timestamp}`")
                    st.caption(q.why_notable)
                    if ("quote", q.timestamp) in open_for:
                        st.caption("⚑ open dispute")
                    b1, b2 = st.columns(2)
                    b1.button("↪ Show line", key=f"q_jump_{i}", on_click=focus_line, args=(q.timestamp,),
                              use_container_width=True)
                    with b2:
                        dispute_box(eid, "quote", q.timestamp, q.quote, f"q{i}")
            st.subheader("Topics")
            st.write(" ".join(f"`#{t}`" for t in r.key_notes.topics))
        if r.fact_checks:
            st.subheader("Fact-check")
            st.dataframe([{"#": f.claim_id, "Claim": f.claim, "Verdict": f.verdict_label,
                           "Confidence": f.confidence, "Review": ("✔️" if f.claim_id in reviewed else "🔎") if f.needs_human_review else "",
                           "Sources": len(f.evidence)} for f in r.fact_checks], hide_index=True,
                         use_container_width=True,
                         column_config={"Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1,
                                                                                      format="%.2f")})
            for f in r.fact_checks:
                with st.container(border=True):
                    st.markdown(f"**{f.claim_id} · {f.verdict_label}** · confidence {f.confidence:.2f}  \n{f.claim}")
                    st.caption(f"{f.speaker} · `{f.timestamp}` · {f.claim_type}")
                    done = reviewed.get(f.claim_id) if f.needs_human_review else None
                    if done:
                        st.success(f"Reviewed by {done['who']} at {done['reviewed_at'][:16].replace('T', ' ')} UTC. "
                                   f"Flagged because: {f.review_reason or 'flagged'}", icon="✔️")
                    elif f.needs_human_review:
                        st.warning(f"Needs human review: {f.review_reason or 'flagged'}", icon="🔎")
                    st.write(f.rationale)
                    for e in f.evidence:
                        tier = TIER_TAGS.get(e.tier or "", e.tier or "?")
                        label = e.title or e.source_id
                        st.markdown(f"- [{label}]({e.url}) · {tier}" if e.url else f"- {label} · {tier}")
                    if ("claim", f.claim_id) in open_for:
                        st.caption("⚑ open dispute")
                    b1, b2, b3 = st.columns(3) if f.needs_human_review else (*st.columns(2), None)
                    b1.button("↪ Show line", key=f"c_jump_{f.claim_id}", on_click=focus_line, args=(f.timestamp,),
                              use_container_width=True)
                    if b3 is not None:   # sign-off only for claims the agent sent to a person
                        if done:
                            b2.button("↩ Undo review", key=f"c_unrev_{f.claim_id}", on_click=store.unmark_reviewed,
                                      args=(eid, f.claim_id), use_container_width=True)
                        else:
                            b2.button("✔ Mark reviewed", key=f"c_rev_{f.claim_id}", on_click=store.mark_reviewed,
                                      args=(eid, f.claim_id, who()), use_container_width=True,
                                      help="You checked the line and the sources and the verdict stands. "
                                           "If something is wrong, use Dispute instead.")
                    with b3 if b3 is not None else b2:
                        dispute_box(eid, "claim", f.claim_id, f.claim, f"c_{f.claim_id}")
        if r.reviewer_notes:
            st.subheader("Notes for the editor")
            st.markdown("\n".join(f"- {n}" for n in r.reviewer_notes))

    with right:
        st.subheader("Transcript")
        lines = store.transcript(eid)
        if not lines:
            st.write("No transcript stored for this episode.")
            return
        c1, c2 = st.columns([3, 2])
        view = c1.radio("View", ["Side by side", "Clean", "Raw"], horizontal=True, key="tx_view")
        changed_only = c2.checkbox("Only lines cleaning changed", key="tx_changed")
        focus = st.session_state.get("focus")
        shown = lines
        if focus:
            at = next((i for i, ln in enumerate(lines) if ln["timestamp"] == focus), None)
            if at is None:
                st.warning(f"No line at {focus} in the stored transcript.")
            else:
                shown = lines[max(0, at - 2):]
                st.caption(f"Showing from the line at `{focus}` (highlighted).")
                st.button("Show whole transcript", on_click=focus_line, args=(None,))
        ing = r.ingest or {}
        if ing:
            st.caption(f"{ing.get('lines_parsed', '?')} lines parsed · {ing.get('lines_cleaned', 0)} cleaned · "
                       f"{len(ing.get('lines_dropped', []))} dropped (filler only) · {ing.get('lines_flagged', 0)} flagged. "
                       "Struck-through words were removed by cleaning.")
        with st.container(height=720):
            st.markdown(transcript_html(shown, view, focus, changed_only), unsafe_allow_html=True)


# ---- History ---------------------------------------------------------------------------------

def page_history() -> None:
    st.header("History")
    c1, c2, c3 = st.columns([3, 2, 1])
    query = c1.text_input("Search episodes, quotes, claims and transcript lines", key="h_query")
    verdicts = c2.multiselect("Claims with verdict", list(VERDICT_LABELS), format_func=lambda v: VERDICT_LABELS[v],
                              key="h_verdicts")
    client = c3.selectbox("Client", ["all"] + clients(), key="h_client")
    episodes = store.list_episodes(None if client == "all" else client)
    allowed = {e["id"] for e in episodes}

    if query or verdicts:
        hits = [h for h in store.search(query, verdicts or None) if h.episode_id in allowed]
        st.caption(f"{len(hits)} match(es)")
        st.dataframe([{"Episode": h.episode_id, "Kind": h.kind, "Where": h.ref,
                       "Verdict": f"{VERDICT_ICON.get(h.verdict or '', '')} {h.verdict or ''}".strip(), "Text": h.text}
                      for h in hits], hide_index=True, use_container_width=True)
        hit_eps = sorted({h.episode_id for h in hits})
        if hit_eps:
            c1, c2 = st.columns([3, 1])
            pick = c1.selectbox("Open", hit_eps, key="h_open")
            c2.button("Open report", key="h_open_btn", on_click=go, args=("Report review", pick),
                      use_container_width=True)
        return

    st.dataframe([{"Episode": e["id"], "Title": e["title"], "Client": e["client"], "Status": e["status"],
                   "Claims": e["claims"], "Needs review": e["needs_review"], "Open disputes": e["open_disputes"],
                   "Processed": e["processed_at"], "Release": version(e["code_version"]), "Prompt": version(e["prompt_version"]),
                   "Cost $": round(e["cost"] or 0, 3)} for e in episodes], hide_index=True, use_container_width=True)
    if episodes:
        c1, c2 = st.columns([3, 1])
        pick = c1.selectbox("Open", [e["id"] for e in episodes], key="h_open_all")
        c2.button("Open report", key="h_open_all_btn", on_click=go, args=("Report review", pick),
                  use_container_width=True)


# ---- Disputes --------------------------------------------------------------------------------

def page_disputes() -> None:
    st.header("Disputes")
    status = st.radio("Show", ["open", "resolved", "all"], horizontal=True, key="d_status")
    items = store.disputes(status=None if status == "all" else status)
    if not items:
        st.write("No disputes.")
        return
    for d in items:
        with st.container(border=True):
            icon = "⚑" if d["status"] == "open" else "✔️"
            st.markdown(f"{icon} **{d['episode_id']} · {d['target_kind']} {d['target_id']}** — {d['status']}  \n"
                        f"_{d['target_text'] or ''}_")
            st.write(d["note"])
            st.caption(f"raised by {d['who']} at {d['created_at']}"
                       + (f" · resolved by {d['resolved_by']} at {d['resolved_at']}: {d['resolution']}"
                          if d["status"] == "resolved" else ""))
            c1, c2 = st.columns([3, 1])
            focus = d["target_id"] if d["target_kind"] == "quote" else None
            c2.button("Open report", key=f"d_open_{d['id']}", on_click=go,
                      args=("Report review", d["episode_id"], focus), use_container_width=True)
            if d["status"] == "open":
                with c1.form(f"resolve_{d['id']}", clear_on_submit=True):
                    resolution = st.text_input("Resolution", key=f"res_{d['id']}",
                                               placeholder="e.g. verdict confirmed wrong; added to the golden set")
                    if st.form_submit_button("Mark resolved"):
                        store.resolve_dispute(d["id"], who(), resolution)
                        st.rerun()
            else:
                c1.button("Reopen", key=f"d_reopen_{d['id']}", on_click=store.reopen_dispute, args=(d["id"],))


{"Upload": page_upload, "Jobs": page_jobs, "Report review": page_report,
 "History": page_history, "Disputes": page_disputes}[st.session_state.page]()
