"""search_sort config: f=tweets (latest) vs f=top."""

from __future__ import annotations

from unittest.mock import MagicMock


from media_support.html_backend.pool import HtmlNitterPool, PoolConfig
from shared.utils import TweetItem


def _pool(search_sort: str = "latest") -> HtmlNitterPool:
    from media_support.host_score import HostScoreBook

    pool = HtmlNitterPool.__new__(HtmlNitterPool)
    pool.config = PoolConfig(
        instances=["http://nitter:8080"],
        max_pages=1,
        search_sort=search_sort,
    )
    pool.instances = [pool._norm("http://nitter:8080")]
    pool.scores = HostScoreBook()
    pool.log = MagicMock()
    pool.session = MagicMock()
    pool.session.host_of = lambda base: "nitter:8080"
    pool.limiter = MagicMock()
    pool.limiter.is_cooling = MagicMock(return_value=False)
    pool.limiter.cooldown_remaining = MagicMock(return_value=0.0)
    pool.limiter.wait = MagicMock()
    pool.limiter.reward = MagicMock()
    pool.limiter.punish = MagicMock(return_value=30.0)
    return pool


def _mock_html_response(items: list[TweetItem]) -> bytes:
    """Build minimal HTML that parse_timeline_html can parse."""
    if not items:
        return b"<html></html>"
    parts = ["<html><body>"]
    for t in items:
        parts.append(
            f'<div class="timeline-item">'
            f'<div class="tweet-body">'
            f'<div class="tweet-content">{t.text}</div>'
            f"</div>"
            f'<span class="tweet-date"><a title="2026-01-01T00:00:00Z"></a></span>'
            f'<a href="/{t.username}/status/{t.status_id}">link</a>'
            f"</div>"
        )
    parts.append("</body></html>")
    return "".join(parts).encode()


def test_search_sort_default_is_latest():
    """Default search_sort='latest' sends f=tweets."""
    pool = _pool(search_sort="latest")
    captured_paths: list[str] = []

    def fake_get_html(base, path):
        captured_paths.append(path)
        resp = MagicMock()
        resp.code = 200
        resp.body = _mock_html_response([])
        return resp.body

    pool._get_html = fake_get_html
    pool._paginate_search("http://nitter:8080", "test", 5, kind="phrase")
    assert any("f=tweets" in p for p in captured_paths)


def test_search_sort_top_sends_f_top():
    """search_sort='top' sends f=top."""
    pool = _pool(search_sort="top")
    captured_paths: list[str] = []

    def fake_get_html(base, path):
        captured_paths.append(path)
        return _mock_html_response([])

    pool._get_html = fake_get_html
    pool._paginate_search("http://nitter:8080", "test", 5, kind="phrase")
    assert any("f=top" in p for p in captured_paths)
    assert not any("f=tweets" in p for p in captured_paths)


def test_search_sort_invalid_falls_back_to_tweets():
    """Unknown search_sort value defaults to f=tweets."""
    pool = _pool(search_sort="bogus")
    captured_paths: list[str] = []

    def fake_get_html(base, path):
        captured_paths.append(path)
        return _mock_html_response([])

    pool._get_html = fake_get_html
    pool._paginate_search("http://nitter:8080", "test", 5, kind="phrase")
    assert any("f=tweets" in p for p in captured_paths)
    assert not any("f=top" in p for p in captured_paths)
