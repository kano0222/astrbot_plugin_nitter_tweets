"""自建实例地址不得进入摘要消息与聚合错误文本的回归测试。"""

from __future__ import annotations

from media_support.client import NitterClient
from scheduler.models import ScheduledCheckResult
from shared.observability import redact_instance_urls


def test_redact_instance_urls_replaces_bare_urls():
    text = "http://64.112.40.168:8080: timed out (轮次 1/2); https://nitter.example: timed out"
    redacted = redact_instance_urls(text)
    assert "http://" not in redacted
    assert "https://" not in redacted
    assert "64.112.40.168" not in redacted
    assert "实例地址 timed out" in redacted


def test_redact_instance_urls_keeps_plain_text():
    assert (
        redact_instance_urls("#1: timed out (轮次 1/2)") == "#1: timed out (轮次 1/2)"
    )
    assert redact_instance_urls("") == ""
    assert redact_instance_urls(None) == ""


def _client() -> NitterClient:
    return NitterClient({"instances": ["https://a.example", "https://b.example"]})


def test_fetch_error_summary_redacts_leftover_urls():
    client = _client()
    message = client._format_fetch_errors(
        ["http://64.112.40.168:8080: timed out (轮次 1/2)"],
        total_count=1,
    )
    assert "http://" not in message
    assert "64.112.40.168" not in message
    assert "实例地址" in message


def test_fetch_error_summary_multi_round_copy():
    client = _client()
    message = client._format_fetch_errors(
        [
            "#1: timed out (轮次 1/2)",
            "#1: timed out (轮次 2/2)",
        ],
        total_count=1,
    )
    assert "已尝试 1 个 Nitter 实例共 2 次请求（含重试）" in message
    # 旧文案会把轮次累计误当实例数：2/1
    assert "2/1" not in message


def test_fetch_error_summary_single_round_copy():
    client = _client()
    message = client._format_fetch_errors(
        ["#1: timed out (轮次 1/2)"],
        total_count=2,
    )
    assert "已尝试 1/2 个 Nitter 实例" in message


def test_summary_failure_line_redacts_instance_urls():
    result = ScheduledCheckResult(
        reason="manual_command",
        group_name="qqcoser",
        group_id="group_1",
        failed_users={
            "@KOKOMO5512": (
                "已尝试 2/1 个 Nitter 实例，未获得可用 RSS；错误: "
                "http://64.112.40.168:8080: timed out (轮次 1/2)"
            )
        },
    )
    message = result.format_message()
    assert "http://" not in message
    assert "64.112.40.168" not in message
    assert "失败: @KOKOMO5512" in message


def test_summary_baseline_rebuild_failed_line_redacts_urls():
    result = ScheduledCheckResult(
        reason="manual_command",
        baseline_rebuild_failed_users={
            "@nasa": "http://64.112.40.168:8080: timed out",
        },
    )
    message = result.format_message()
    assert "http://" not in message
    assert "64.112.40.168" not in message
