"""Optional UI smoke test: render and navigate without reading a real catalog."""

import pytest

from job_matcher.catalog_search import SearchPage


def test_dashboard_navigation_and_reset(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    import job_matcher.indexed_search as search

    calls = []

    def fake(database, index, policy, criteria, *, page, page_size):
        calls.append((criteria, page))
        rep = dict(
            source="lever",
            source_scope="one",
            source_job_id="1",
            title="Engineer",
            company="Synthetic",
            location=None,
            status="active",
            outcome="keep",
            first_seen_at="2026-11-04T07:42:16.209540+00:00",
            last_seen_at="2026-11-04T20:05:00+00:00",
            description="<script>alert('untrusted')</script>",
            job_url="https://example.com/job",
            apply_url="javascript:alert(1)",
            reasons="retained",
        )
        return SearchPage(
            26,
            (
                dict(
                    title="Engineer",
                    company="Synthetic",
                    outcome="keep",
                    first_seen="2026-11-04T07:42:16.209540+00:00",
                    last_seen="2026-11-04T20:05:00+00:00",
                    representations=[rep, dict(rep, source_scope="two")],
                ),
            ),
        )

    monkeypatch.setattr(search, "search_index", fake)
    # The query is mocked; no catalog or index contents are read.
    from pathlib import Path

    app = AppTest.from_file(Path("scripts/dashboard.py").resolve()).run()
    next(b for b in app.button if b.label == "Search").click().run()
    assert not app.exception
    assert calls[-1][1] == 1
    captions = [caption.value for caption in app.caption]
    assert any(
        "Sources:" in text and "2026-11-03 11:42 PM" in text for text in captions
    )
    assert any(
        text.startswith("First discovered (PST): 2026-11-03 11:42 PM")
        for text in captions
    )
    assert any("Last seen (PST): 2026-11-04 12:05 PM" in text for text in captions)
    next(b for b in app.button if b.label == "Next").click().run()
    assert not app.exception
    assert calls[-1][1] == 2
    next(s for s in app.selectbox if s.label == "Source").select("greenhouse").run()
    assert not app.exception
    assert calls[-1][1] == 1
    assert calls[-1][0].source == "greenhouse"


def test_dashboard_missing_index_diagnostic(monkeypatch):
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    import job_matcher.indexed_search as search
    from job_matcher.search_index import SearchIndexError

    def missing(*args, **kwargs):
        raise SearchIndexError("Search index is missing; rebuild")

    monkeypatch.setattr(search, "search_index", missing)
    app = AppTest.from_file(Path("scripts/dashboard.py").resolve()).run()
    next(b for b in app.button if b.label == "Search").click().run()
    assert not app.exception
    assert any("missing" in error.value for error in app.error)
    assert any("rebuild" in code.value for code in app.code)
