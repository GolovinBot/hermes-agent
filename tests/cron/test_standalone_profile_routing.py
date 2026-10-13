"""Standalone legacy keys do not erase the runtime profile owning cron delivery."""

from unittest.mock import AsyncMock, patch

import pytest

from cron.scheduler_delivery import _deliver_result
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.conversation import conversation_origin, existing_delivery_session
from gateway.session import SessionSource, SessionStore
from gateway.session_identity import RoutingIdentity


@pytest.mark.parametrize("stored_profile", [None, "ops"])
def test_standalone_mirror_resolves_the_owner_of_a_legacy_main_key(tmp_path, monkeypatch, stored_profile):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "ops")
    config = GatewayConfig(multiplex_profiles=False)
    source = SessionSource(Platform.MATRIX, "!room:example.org", chat_type="dm", profile=stored_profile)
    source._identity = RoutingIdentity("ops", "ops", tmp_path, tmp_path, multiplexed=False)
    store = SessionStore(config.sessions_dir, config)
    entry = store.get_or_create_session(source)
    origin = {**source.to_dict(), "conversation": conversation_origin(source, thread_id=None)}
    # Durable state has no pinned identity; the owning profile is the bound runtime.
    restored = SessionStore(config.sessions_dir, config)
    matched = existing_delivery_session(restored, "matrix", source.chat_id, None, origin)
    assert matched is not None
    assert matched.session_id == entry.session_id
    origin["conversation"]["address"]["profile"] = "other"
    assert existing_delivery_session(restored, "matrix", source.chat_id, None, origin) is None


@pytest.mark.parametrize("origin_profile", ["ops", "default", "other"])
def test_standalone_send_requires_the_origin_runtime_profile(tmp_path, monkeypatch, origin_profile):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "ops")
    source = SessionSource(Platform.MATRIX, "!room:example.org", chat_type="dm", profile=origin_profile)
    job = {"id": "digest", "deliver": "origin", "attach_to_session": False,
           "origin": {**source.to_dict(), "conversation": conversation_origin(source, thread_id=None)}}
    config = GatewayConfig(platforms={Platform.MATRIX: PlatformConfig(enabled=True, token="test-token")})
    sender = AsyncMock(return_value={"success": True})
    with patch("gateway.config.load_gateway_config", return_value=config), \
            patch("tools.send_message_tool._send_to_platform", sender):
        error = _deliver_result(job, "private brief")
    if origin_profile == "ops":
        assert error is None
        sender.assert_awaited_once()
    else:
        assert error
        sender.assert_not_awaited()
