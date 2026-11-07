"""Local Streamlit composition root; run with python -m streamlit run."""

import sqlite3
from pathlib import Path

import streamlit as st

from job_matcher.catalog_search import SearchCriteria, safe_url
from job_matcher.dashboard_display import format_posted_date, format_pst_timestamp
from job_matcher.filter_policy import load_filter_policy
from job_matcher.indexed_search import search_index

ROOT = Path(__file__).resolve().parents[1]
st.set_page_config(page_title="Job Matcher", layout="wide")
st.title("Job Matcher catalog")
st.caption("Read-only browsing · no acquisition or AI calls")


with st.sidebar:
    database = st.text_input("SQLite catalog", str(ROOT / "phase2_validation.sqlite3"))
    index = st.text_input("Search index", str(ROOT / "job_search.sqlite3"))
    policy_path = st.text_input(
        "Filter policy", str(ROOT / "examples/filter_policy.example.toml")
    )
    st.caption(
        "Search uses versioned per-posting screening results. Run sync after acquisition."
    )
    st.button("Refresh catalog")
    source = st.selectbox("Source", ["all", "lever", "greenhouse"])
    outcome = st.selectbox(
        "Screening outcome", ["keep", "exclude", "defer", "all"], format_func=str.upper
    )
    company = st.text_input("Company contains")
    location = st.text_input("Location contains")
    employment = st.selectbox(
        "Employment type",
        [
            "",
            "full_time",
            "part_time",
            "internship",
            "contract",
            "temporary",
            "other",
            "unknown",
        ],
        format_func=lambda x: x.replace("_", " ").title() if x else "All",
    )
    workplace = st.selectbox(
        "Workplace type",
        ["", "remote", "hybrid", "onsite", "unknown"],
        format_func=lambda x: x.title() if x else "All",
    )
    inactive = st.checkbox("Include inactive postings")
    recent = st.checkbox("Filter by discovery date")
    since = st.date_input(
        "Scraper first discovered on or after (UTC)", disabled=not recent
    )
    st.caption("Discovery date is first_seen, not the original publication date.")

with st.form("search"):
    keywords = st.text_input(
        "Keywords",
        placeholder='scraping OR scraper, Python AND automation, "web scraping"',
    )
    left, right = st.columns(2)
    mode = left.selectbox("Match keywords", ["AND", "OR"])
    fields = right.selectbox(
        "Search in",
        ["title_description", "title", "all"],
        format_func=lambda x: {
            "title_description": "Title + description",
            "title": "Title only",
            "all": "Title, description, company, location",
        }[x],
    )
    submitted = st.form_submit_button("Search")
if submitted:
    st.session_state.searched = True
left, right = st.columns(2)
sort = left.selectbox(
    "Sort",
    ["first_seen", "posted_newest", "posted_oldest", "last_seen", "company", "title"],
    format_func=lambda x: {
        "first_seen": "First discovered, newest first",
        "posted_newest": "Newest posted (earliest active source date)",
        "posted_oldest": "Oldest posted (earliest active source date)",
        "last_seen": "Last seen, newest first",
        "company": "Company",
        "title": "Job title",
    }[x],
)
size = right.selectbox("Results per page", [25, 50, 100])
criteria = SearchCriteria(
    keywords,
    mode,
    fields,
    source,
    outcome,
    company,
    location,
    inactive,
    since.isoformat() if recent else "",
    sort,
    employment,
    workplace,
)
signature = (database, index, policy_path, criteria, size)
if st.session_state.get("criteria") != signature:
    st.session_state.criteria = signature
    st.session_state.page = 1
st.session_state.setdefault("page", 1)
st.caption(
    "A logical job matches when one representation matches all search filters. Screening considers every active representation; all sources remain inspectable. Unlinked postings are omitted."
)
if not st.session_state.get("searched"):
    st.info(
        "Enter keywords and select Search. An empty search browses the indexed catalog."
    )
    st.stop()
try:
    with st.spinner("Searching indexed logical jobs…"):
        result = search_index(
            database,
            index,
            load_filter_policy(policy_path),
            criteria,
            page=st.session_state.page,
            page_size=size,
        )
except (OSError, ValueError, sqlite3.Error) as error:
    st.error(f"Could not search catalog: {error}")
    st.code(".\\.venv\\Scripts\\python.exe -m job_matcher.search_index rebuild")
    st.code(".\\.venv\\Scripts\\python.exe -m job_matcher.search_index sync")
    st.stop()
pages = max(1, (result.total + size - 1) // size)
if st.session_state.page > pages:
    st.session_state.page = pages
    st.rerun()
st.write(
    f"{result.total:,} matching logical jobs · page {st.session_state.page} of {pages}"
)
prev, nxt = st.columns(2)
if prev.button("Previous", disabled=st.session_state.page == 1):
    st.session_state.page -= 1
    st.rerun()
if nxt.button("Next", disabled=st.session_state.page >= pages):
    st.session_state.page += 1
    st.rerun()
if not result.jobs:
    st.info("No matching jobs. Try fewer keywords or include other screening outcomes.")
for job in result.jobs:
    with st.container(border=True):
        st.subheader(job["title"])
        st.text(f"{job['company']} · {job['outcome'].upper()}")
        st.caption(
            f"Sources: {', '.join(sorted({r['source'] for r in job['representations']}))} · First discovered (PST): {format_pst_timestamp(job['first_seen'])} · Last seen (PST): {format_pst_timestamp(job['last_seen'])}"
        )
        for rep in job["representations"]:
            st.text(
                f"{rep['source']} / {rep['source_scope']} · {rep['title']} · {rep['company']} · {rep['location'] or 'Location unavailable'} · {rep['status']} · {rep['outcome'].upper()}"
            )
            st.caption(
                f"First discovered (PST): {format_pst_timestamp(rep['first_seen_at'])} · Last seen (PST): {format_pst_timestamp(rep['last_seen_at'])}"
            )
            labels = {
                "full_time": "Full-time",
                "part_time": "Part-time",
                "onsite": "On-site",
            }
            st.caption(
                "Employment: "
                + labels.get(
                    rep.get("employment_type"),
                    (rep.get("employment_type") or "Unknown").title(),
                )
                + " · Workplace: "
                + labels.get(
                    rep.get("workplace_type"),
                    (rep.get("workplace_type") or "Unknown").title(),
                )
                + " · Posted (UTC): "
                + format_posted_date(rep.get("posted_at"))
                + (" (Lever creation time)" if rep["source"] == "lever" else "")
            )
            st.text(
                " ".join(rep["description"].split())[:320] or "Description unavailable"
            )
            for label, field in (
                ("Original posting", "job_url"),
                ("Apply", "apply_url"),
            ):
                if url := safe_url(rep[field]):
                    st.link_button(
                        f"{label} — {rep['source']}/{rep['source_scope']}/{rep['source_job_id']}",
                        url,
                    )
            with st.expander(
                f"Full description and screening — {rep['source']}/{rep['source_scope']}/{rep['source_job_id']}"
            ):
                st.text(rep["description"] or "Description unavailable")
                st.text(f"Screening reasons: {rep['reasons']}")
