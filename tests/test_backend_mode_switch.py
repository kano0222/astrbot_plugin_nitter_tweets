"""Tests for fetch_backend three-mode switching (mix | nitter | fx),

incremental Nitter fallback, and physical isolation of Twitter Lists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from command_handlers.manual import ManualCommandMixin
from media_support.client import SchedulerFetchResult
from media_support.fxtwitter_client import (
    FxTwitterError,
    FxTwitterNotFoundError,
)
from media_support.search_session_buffer import SearchSessionStore
from scheduler.config import SchedulerConfigReader
from scheduler.models import SourceStatus
from scheduler.runner_fetch import SchedulerFetchMixin
from shared.utils import TweetItem, TweetMedia


def _make_tweet(username: str, status_id: str, is_retweet: bool = False) -> TweetItem:
    return TweetItem(
        text=f"tweet from {username} id {status_id}",
        link=f"https://x.com/{username}/status/{status_id}",
        published="2026-09-16 12:00:00",
        media=[],
        is_retweet=is_retweet,
    )


class DummyRunner(SchedulerFetchMixin):
    def __init__(
        self, config: dict, nitter: MagicMock, fxtwitter: MagicMock | None = None
    ):
        self.config = config
        self.nitter = nitter
        if fxtwitter is not None:
            self.fxtwitter = fxtwitter
            self.nitter.fxtwitter = fxtwitter
        self._log_verbose_info = MagicMock()


class DummyManualHost(ManualCommandMixin):
    def __init__(
        self, config: dict, nitter: MagicMock, fxtwitter: MagicMock | None = None
    ):
        self.config = config
        self.default_limit = 5
        self.search_default_limit = 5
        self.search_max_limit = 20
        self.search_cooldown_seconds = 0.0
        self.cooldown_seconds = 0.0
        self._cooldowns: dict = {}
        self._search_session_store = SearchSessionStore()
        self.nitter = nitter
        if fxtwitter is not None:
            self.fxtwitter = fxtwitter
        self.sender = MagicMock()
        self.sender.should_merge_for_event = MagicMock(return_value=False)
        self.media = MagicMock()
        self.media.cleanup_after_send = MagicMock()

    def _cooldown_left(self, event, scope="tweet") -> float:
        return 0.0

    def _mark_cooldown(self, event, scope="tweet") -> None:
        pass

    async def _send_tweets_response(
        self, event, username: str, instance: str, tweets, on_sent_progress=None
    ) -> int:
        return len(tweets)


def _blogger_group(users: list[str], filter_reposts: bool = True):
    config = {
        "tweet_groups": [
            {
                "group_id": "test_bloggers",
                "group_type": "blogger",
                "watch_users": users,
                "filter_reposts_enabled": filter_reposts,
                "concurrent_fetch_enabled": False,
                "send_user_interval": 0.0,
            }
        ]
    }
    reader = SchedulerConfigReader(config, context=None)
    return reader.schedule_groups()[0]


def _tag_group(queries: list[str]):
    config = {
        "tweet_groups": [
            {
                "group_id": "test_tags",
                "group_type": "tag",
                "watch_queries": queries,
                "filter_reposts_enabled": True,
                "send_user_interval": 0.0,
            }
        ]
    }
    reader = SchedulerConfigReader(config, context=None)
    return reader.schedule_groups()[0]


def _list_group(list_ids: list[str]):
    config = {
        "tweet_groups": [
            {
                "group_id": "test_lists",
                "group_type": "list",
                "watch_lists": list_ids,
                "filter_reposts_enabled": True,
                "send_user_interval": 0.0,
            }
        ]
    }
    reader = SchedulerConfigReader(config, context=None)
    return reader.schedule_groups()[0]


# ==============================================================================
# 1. Multi-blogger: all success on FX -> Nitter never called
# ==============================================================================
@pytest.mark.asyncio
async def test_multi_blogger_all_success_does_not_call_nitter():
    mock_nitter = MagicMock()
    mock_nitter.fetch_merged_for_scheduler = AsyncMock()
    mock_nitter.fetch_tweets_for_scheduler = AsyncMock()

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"

    def fx_fetch(username, count=10, **kw):
        return [_make_tweet(username, "1001")], None

    mock_fx.fetch_user_timeline = MagicMock(side_effect=fx_fetch)

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _blogger_group(["alice", "bob", "carol"])

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert len(results) == 3
    assert mock_fx.fetch_user_timeline.call_count == 3
    mock_nitter.fetch_merged_for_scheduler.assert_not_called()
    mock_nitter.fetch_tweets_for_scheduler.assert_not_called()

    for r in results:
        assert r.error is None
        assert r.instance == "api.fxtwitter.com"
        assert len(r.tweets) == 1
        assert r.fetch_status == SourceStatus.SUCCESS


# ==============================================================================
# 2. Multi-blogger: partial success (incremental fallback to Nitter)
# ==============================================================================
@pytest.mark.asyncio
async def test_multi_blogger_partial_success_incremental_nitter_fallback():
    mock_nitter = MagicMock()
    # Nitter merged RSS returns results for failed accounts
    mock_nitter.fetch_merged_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter.test",
            {
                "bob": SchedulerFetchResult(
                    tweets=[_make_tweet("bob", "2001")],
                    scanned_status_ids=["2001"],
                    complete=True,
                ),
                "carol": SchedulerFetchResult(
                    tweets=[_make_tweet("carol", "3001")],
                    scanned_status_ids=["3001"],
                    complete=True,
                ),
            },
        )
    )
    mock_nitter.fetch_tweets_for_scheduler = AsyncMock()

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"

    def fx_fetch(username, count=10, **kw):
        if username == "alice":
            return [_make_tweet("alice", "1001")], None
        raise FxTwitterError(f"HTTP 429 rate limit for {username}")

    mock_fx.fetch_user_timeline = MagicMock(side_effect=fx_fetch)

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _blogger_group(["alice", "bob", "carol"], filter_reposts=True)

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    # FX called for all 3
    assert mock_fx.fetch_user_timeline.call_count == 3

    # Nitter merged RSS called ONLY for failed accounts: bob and carol
    mock_nitter.fetch_merged_for_scheduler.assert_called_once()
    called_batch = mock_nitter.fetch_merged_for_scheduler.call_args[0][0]
    assert set(called_batch) == {"bob", "carol"}
    assert "alice" not in called_batch

    # Results merged
    assert len(results) == 3
    results_by_user = {r.username: r for r in results}
    assert results_by_user["alice"].instance == "api.fxtwitter.com"
    assert results_by_user["bob"].instance == "http://nitter.test"
    assert results_by_user["carol"].instance == "http://nitter.test"


@pytest.mark.asyncio
async def test_multi_blogger_single_failure_falls_back_to_single_nitter_fetch():
    """When only 1 account fails, it falls back to single user fetch instead of merged."""
    mock_nitter = MagicMock()
    mock_nitter.fetch_merged_for_scheduler = AsyncMock()
    mock_nitter.fetch_tweets_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter.test",
            SchedulerFetchResult(
                tweets=[_make_tweet("bob", "2001")],
                scanned_status_ids=["2001"],
                complete=True,
            ),
        )
    )

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"

    def fx_fetch(username, count=10, **kw):
        if username == "alice":
            return [_make_tweet("alice", "1001")], None
        raise FxTwitterError("500 Server Error")

    mock_fx.fetch_user_timeline = MagicMock(side_effect=fx_fetch)

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _blogger_group(["alice", "bob"], filter_reposts=True)

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert mock_fx.fetch_user_timeline.call_count == 2
    mock_nitter.fetch_merged_for_scheduler.assert_not_called()
    mock_nitter.fetch_tweets_for_scheduler.assert_called_once()
    assert len(results) == 2


# ==============================================================================
# 3. Multi-blogger: all fail on FX -> full fallback to Nitter
# ==============================================================================
@pytest.mark.asyncio
async def test_multi_blogger_all_fail_falls_back_to_nitter():
    mock_nitter = MagicMock()
    mock_nitter.fetch_merged_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter.test",
            {
                "alice": SchedulerFetchResult(
                    tweets=[_make_tweet("alice", "1001")],
                    scanned_status_ids=["1001"],
                    complete=True,
                ),
                "bob": SchedulerFetchResult(
                    tweets=[_make_tweet("bob", "2001")],
                    scanned_status_ids=["2001"],
                    complete=True,
                ),
            },
        )
    )

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"
    mock_fx.fetch_user_timeline = MagicMock(
        side_effect=FxTwitterError("FX unavailable")
    )

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _blogger_group(["alice", "bob"], filter_reposts=True)

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert mock_fx.fetch_user_timeline.call_count == 2
    mock_nitter.fetch_merged_for_scheduler.assert_called_once()
    called_batch = mock_nitter.fetch_merged_for_scheduler.call_args[0][0]
    assert set(called_batch) == {"alice", "bob"}

    assert len(results) == 2
    for r in results:
        assert r.instance == "http://nitter.test"


# ==============================================================================
# 4. Tag search: 404 (SafeSearch) smooth fallback to Nitter HTML
# ==============================================================================
@pytest.mark.asyncio
async def test_tag_search_404_smooth_fallback_to_nitter_html():
    mock_nitter = MagicMock()
    nitter_tweet = _make_tweet("taguser", "9999")
    mock_nitter.search = MagicMock(
        return_value=("http://nitter-html.test", [nitter_tweet])
    )

    mock_fx = MagicMock()
    mock_fx.search_tweets = MagicMock(
        side_effect=FxTwitterNotFoundError(
            "FxTwitter returned code 404: SafeSearch restricted"
        )
    )

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _tag_group(["#nsfw"])

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert len(results) == 1
    mock_fx.search_tweets.assert_called_once()
    mock_nitter.search.assert_called_once()
    assert results[0].error is None
    assert results[0].instance == "http://nitter-html.test"
    assert len(results[0].tweets) == 1


@pytest.mark.asyncio
async def test_tag_search_mix_fx_success_does_not_call_nitter():
    mock_nitter = MagicMock()
    mock_nitter.search = MagicMock()

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"
    fx_tweet = _make_tweet("news", "5555")
    mock_fx.search_tweets = MagicMock(return_value=([fx_tweet], None))

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _tag_group(["#news"])

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert len(results) == 1
    mock_fx.search_tweets.assert_called_once()
    mock_nitter.search.assert_not_called()
    assert results[0].instance == "api.fxtwitter.com"
    assert len(results[0].tweets) == 1


# ==============================================================================
# 5. List subscription: 100% locked to Nitter, physically isolated from FX
# ==============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("backend_mode", ["mix", "fx", "nitter"])
async def test_list_subscription_always_uses_nitter_never_fx(backend_mode: str):
    mock_nitter = MagicMock()
    mock_nitter.fetch_list_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter-list.test",
            SchedulerFetchResult(
                tweets=[_make_tweet("listuser", "8888")],
                scanned_status_ids=["8888"],
                complete=True,
            ),
        )
    )

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock()
    mock_fx.search_tweets = MagicMock()

    runner = DummyRunner({"fetch_backend": backend_mode}, mock_nitter, mock_fx)
    group = _list_group(["1234567890"])

    results = await runner._fetch_group_users(
        group, fetch_limit=10, skip_plain_text=False, scan_watermarks={}
    )

    assert len(results) == 1
    # FX must NEVER be touched under any backend mode
    mock_fx.fetch_user_timeline.assert_not_called()
    mock_fx.search_tweets.assert_not_called()

    # Nitter list RSS was called
    mock_nitter.fetch_list_for_scheduler.assert_called_once()
    assert results[0].instance == "http://nitter-list.test"


# ==============================================================================
# 6. fetch_backend = "nitter": never calls FX
# ==============================================================================
@pytest.mark.asyncio
async def test_fetch_backend_nitter_mode_never_calls_fx():
    mock_nitter = MagicMock()
    mock_nitter.fetch_merged_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter.test",
            {
                "alice": SchedulerFetchResult(
                    tweets=[_make_tweet("alice", "1001")],
                    scanned_status_ids=["1001"],
                    complete=True,
                )
            },
        )
    )
    mock_nitter.search = MagicMock(
        return_value=("http://nitter.test", [_make_tweet("t", "2001")])
    )

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock()
    mock_fx.search_tweets = MagicMock()

    runner = DummyRunner({"fetch_backend": "nitter"}, mock_nitter, mock_fx)

    # Multi-blogger
    b_group = _blogger_group(["alice", "bob"])
    await runner._fetch_group_users(b_group, 10, False, {})
    mock_fx.fetch_user_timeline.assert_not_called()

    # Tag
    t_group = _tag_group(["#test"])
    await runner._fetch_group_users(t_group, 10, False, {})
    mock_fx.search_tweets.assert_not_called()


# ==============================================================================
# 7. fetch_backend = "fx": never calls Nitter, reports errors directly
# ==============================================================================
@pytest.mark.asyncio
async def test_fetch_backend_fx_mode_never_calls_nitter():
    mock_nitter = MagicMock()
    mock_nitter.fetch_merged_for_scheduler = AsyncMock()
    mock_nitter.fetch_tweets_for_scheduler = AsyncMock()
    mock_nitter.search = MagicMock()

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock(side_effect=FxTwitterError("Timeline 500"))
    mock_fx.search_tweets = MagicMock(side_effect=FxTwitterNotFoundError("Search 404"))

    runner = DummyRunner({"fetch_backend": "fx"}, mock_nitter, mock_fx)

    # Blogger
    b_group = _blogger_group(["alice", "bob"])
    b_results = await runner._fetch_group_users(b_group, 10, False, {})

    assert len(b_results) == 2
    mock_nitter.fetch_merged_for_scheduler.assert_not_called()
    mock_nitter.fetch_tweets_for_scheduler.assert_not_called()
    for r in b_results:
        assert r.error is not None
        assert "Timeline 500" in r.error.message

    # Tag
    t_group = _tag_group(["#test"])
    t_results = await runner._fetch_group_users(t_group, 10, False, {})

    assert len(t_results) == 1
    mock_nitter.search.assert_not_called()
    assert t_results[0].error is not None
    assert "Search 404" in t_results[0].error.message


# ==============================================================================
# 8. Single blogger fetch routing and fallback
# ==============================================================================
@pytest.mark.asyncio
async def test_single_blogger_fetch_user_mix_fallback():
    mock_nitter = MagicMock()
    mock_nitter.fetch_tweets_for_scheduler = AsyncMock(
        return_value=(
            "http://nitter.test",
            SchedulerFetchResult(
                tweets=[_make_tweet("alice", "1001")],
                scanned_status_ids=["1001"],
                complete=True,
            ),
        )
    )

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock(side_effect=FxTwitterError("Temporary 429"))

    runner = DummyRunner({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    group = _blogger_group(["alice"])

    result = await runner._fetch_group_user(
        group,
        0,
        "alice",
        10,
        skip_plain_text=False,
        scan_watermark=None,
        concurrent=False,
    )

    mock_fx.fetch_user_timeline.assert_called_once()
    mock_nitter.fetch_tweets_for_scheduler.assert_called_once()
    assert result.instance == "http://nitter.test"
    assert len(result.tweets) == 1


# ==============================================================================
# 9. Manual command /推文 routing and fallback
# ==============================================================================
@pytest.mark.asyncio
async def test_manual_cmd_tweets_mix_fx_success():
    mock_nitter = MagicMock()
    mock_nitter.fetch_user = AsyncMock()

    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"
    mock_fx.fetch_user_timeline = MagicMock(
        return_value=([_make_tweet("nasa", "1001")], None)
    )

    host = DummyManualHost({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    event = MagicMock()
    event.send = AsyncMock()
    event.stop_event = MagicMock()
    event.plain_result.side_effect = lambda v: v

    await host._cmd_tweets_impl(event, "nasa", "5")

    mock_fx.fetch_user_timeline.assert_called_once()
    mock_nitter.fetch_user.assert_not_called()


@pytest.mark.asyncio
async def test_manual_cmd_tweets_mix_fx_error_falls_back_to_nitter():
    mock_nitter = MagicMock()
    mock_nitter.fetch_user = AsyncMock(
        return_value=("http://nitter.test", [_make_tweet("nasa", "1001")])
    )

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock(
        side_effect=FxTwitterNotFoundError("User not found 404")
    )

    host = DummyManualHost({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    event = MagicMock()
    event.send = AsyncMock()
    event.stop_event = MagicMock()
    event.plain_result.side_effect = lambda v: v

    await host._cmd_tweets_impl(event, "nasa", "5")

    mock_fx.fetch_user_timeline.assert_called_once()
    mock_nitter.fetch_user.assert_called_once()


@pytest.mark.asyncio
async def test_manual_cmd_tweets_fx_mode_does_not_fallback():
    mock_nitter = MagicMock()
    mock_nitter.fetch_user = AsyncMock()

    mock_fx = MagicMock()
    mock_fx.fetch_user_timeline = MagicMock(
        side_effect=FxTwitterError("Rate limited 429")
    )

    host = DummyManualHost({"fetch_backend": "fx"}, mock_nitter, mock_fx)
    event = MagicMock()
    event.send = AsyncMock()
    event.stop_event = MagicMock()
    event.plain_result.side_effect = lambda v: v

    await host._cmd_tweets_impl(event, "nasa", "5")

    mock_fx.fetch_user_timeline.assert_called_once()
    mock_nitter.fetch_user.assert_not_called()
    sent_msgs = [call.args[0] for call in event.send.await_args_list]
    assert any("获取 @nasa 推文失败" in msg for msg in sent_msgs)


# ==============================================================================
# 10. Manual command /推文搜索 and /推文搜图 routing and fallback
# ==============================================================================
@pytest.mark.asyncio
async def test_manual_cmd_tweet_search_mix_fallback():
    mock_nitter = MagicMock()
    mock_nitter.search = MagicMock(
        return_value=("http://nitter.test", [_make_tweet("t", "9001")])
    )

    mock_fx = MagicMock()
    mock_fx.search_tweets = MagicMock(
        side_effect=FxTwitterNotFoundError("Search 404 SafeSearch")
    )

    host = DummyManualHost({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    event = MagicMock()
    event.unified_msg_origin = "session:test_search_fallback"
    event.send = AsyncMock()
    event.stop_event = MagicMock()
    event.plain_result.side_effect = lambda v: v

    await host._cmd_tweet_search_impl(event, "#test 2")

    mock_fx.search_tweets.assert_called_once()
    mock_nitter.search.assert_called_once()


@pytest.mark.asyncio
async def test_manual_cmd_tweet_pic_search_passes_is_media():
    mock_nitter = MagicMock()
    mock_fx = MagicMock()
    mock_fx.base_url = "https://api.fxtwitter.com"
    tw = _make_tweet("pic", "7777")
    tw.media = [TweetMedia(kind="image", url="http://img.test")]
    mock_fx.search_tweets = MagicMock(return_value=([tw], None))

    host = DummyManualHost({"fetch_backend": "mix"}, mock_nitter, mock_fx)
    event = MagicMock()
    event.unified_msg_origin = "session:test_pic_search"
    event.send = AsyncMock()
    event.stop_event = MagicMock()
    event.plain_result.side_effect = lambda v: v

    await host._cmd_tweet_search_impl(event, "#art 1", is_media_search=True)

    mock_fx.search_tweets.assert_called_once()
    call_kwargs = mock_fx.search_tweets.call_args[1]
    assert call_kwargs.get("is_media") is True


# ==============================================================================
# 11. Property default & case-insensitivity
# ==============================================================================
def test_fetch_backend_property():
    runner = DummyRunner({}, MagicMock())
    assert runner.fetch_backend == "mix"

    runner.config = {"fetch_backend": "NITTER"}
    assert runner.fetch_backend == "nitter"

    runner.config = {"fetch_backend": "  Fx  "}
    assert runner.fetch_backend == "fx"

    runner.config = {"fetch_backend": None}
    assert runner.fetch_backend == "mix"
