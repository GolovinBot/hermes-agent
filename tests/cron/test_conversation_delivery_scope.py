"""A persisted workspace address must survive the standalone delivery lane."""

from unittest.mock import patch

import pytest

from cron.scheduler_delivery import _deliver_result
from gateway.config import GatewayConfig, Platform, PlatformConfig
from plugins.platforms.slack import adapter as slack


@pytest.mark.parametrize("scope", ["TA", "missing"])
def test_standalone_cron_never_tries_a_token_from_another_workspace(scope, monkeypatch):
    posted = []

    async def post(session, token, method, payload, req_kw):
        if method == "auth.test":
            return {"ok": True, "team_id": "TA" if token == "xoxb-a" else "TB"}
        assert method == "chat.postMessage"
        posted.append(token)
        return {"ok": True, "ts": "1.2"}

    monkeypatch.setattr(slack, "_slack_json_post", post)
    monkeypatch.setattr(slack, "_load_slack_bot_tokens", lambda raw, quiet: ["xoxb-b", "xoxb-a"])
    monkeypatch.setattr("tools.send_message_tool._live_adapter", lambda platform: (None, None))
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    monkeypatch.setattr(platform_registry.get("slack"), "standalone_sender_fn", slack._standalone_send)
    config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="xoxb-b,xoxb-a")})
    job = {"id": "digest", "deliver": "origin", "origin": {
        "platform": "slack", "chat_id": "D1", "scope_id": scope, "chat_type": "dm",
        "conversation": {"version": 1, "kind": "dm", "address": {
            "platform": "slack", "chat_id": "D1", "scope_id": scope,
            "thread_id": None, "profile": "default", "account": None,
        }},
    }}
    with patch("gateway.config.load_gateway_config", return_value=config):
        error = _deliver_result(job, "private report")
    if scope == "TA":
        assert error is None
        assert posted == ["xoxb-a"]
    else:
        assert error
        assert not posted
