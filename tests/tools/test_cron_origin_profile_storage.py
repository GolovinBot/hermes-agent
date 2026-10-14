"""Cron origins in children recover from the runtime DB, independent of the central index."""

from dataclasses import replace
from pathlib import Path

import pytest


@pytest.mark.parametrize("mismatch", [None, "chat", "participant", "key", "profile"])
def test_child_can_capture_only_its_multiplexed_conversation(tmp_path, monkeypatch, mismatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_identity import RoutingIdentity
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.cronjob_job_args import _origin_from_env

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    runtime_home = home / "profiles" / "ops"
    runtime_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(multiplex_profiles=True)
    gateway_store = SessionStore(config.sessions_dir, config)
    source = SessionSource(Platform.MATRIX, "!room:example.org", chat_type="group",
                           user_id="@alice:example.org", profile="ops")
    source._identity = RoutingIdentity("default", "ops", home, runtime_home)
    profile_token = set_hermes_home_override(str(runtime_home))
    try:
        entry = gateway_store.get_or_create_session(source)
        assert gateway_store._routing_home == home
        assert gateway_store._db_for_key(entry.session_key).get_session(entry.session_id)
        # A real child reads config after HERMES_HOME is set to the routed profile.
        monkeypatch.setattr("gateway.config.load_gateway_config",
                            lambda: GatewayConfig(multiplex_profiles=True))
        tokens = set_session_vars(platform="matrix", chat_id="!other:example.org" if mismatch == "chat" else source.chat_id,
                                  chat_type="group", user_id="bob" if mismatch == "participant" else source.user_id,
                                  profile="default" if mismatch == "profile" else "ops",
                                  session_key=entry.session_key + ":missing" if mismatch == "key" else entry.session_key)
        try:
            if mismatch:
                with pytest.raises(ValueError):
                    _origin_from_env()
                return
            origin = _origin_from_env()
            assert origin["conversation"]["address"]["account"] == "default"
        finally:
            clear_session_vars(tokens)
    finally:
        reset_hermes_home_override(profile_token)
        gateway_store.close_all_db_handles()


def test_child_of_second_shared_thread_participant_can_capture_origin(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.cronjob_job_args import _origin_from_env

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated"))
    config = GatewayConfig(thread_sessions_per_user=False)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    store = SessionStore(config.sessions_dir, config)
    alice = SessionSource(Platform.MATRIX, "!room:example.org", chat_type="group", user_id="alice", user_id_alt="stable-alice", thread_id="$root")
    bob = replace(alice, user_id="bob", user_id_alt=None)
    entry = store.get_or_create_session(alice)
    assert store.get_or_create_session(bob).session_id == entry.session_id
    tokens = set_session_vars(platform="matrix", chat_id=bob.chat_id, chat_type="group", user_id="bob", thread_id="$root", session_key=entry.session_key)
    try:
        origin = _origin_from_env()
        assert origin["user_id"] == "bob"
        assert origin["user_id_alt"] is None
    finally:
        clear_session_vars(tokens)
        store.close_all_db_handles()

def test_child_cannot_reuse_another_users_old_private_key_after_policy_relaxes(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.cronjob_job_args import _origin_from_env

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated"))
    config = GatewayConfig(thread_sessions_per_user=True)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    store = SessionStore(config.sessions_dir, config)
    alice = SessionSource(Platform.MATRIX, "!room:example.org", chat_type="group", user_id="alice", thread_id="$root")
    entry = store.get_or_create_session(alice)
    config.thread_sessions_per_user = False
    tokens = set_session_vars(platform="matrix", chat_id=alice.chat_id, chat_type="group", user_id="bob", thread_id="$root", session_key=entry.session_key)
    try:
        with pytest.raises(ValueError):
            _origin_from_env()
    finally:
        clear_session_vars(tokens)
        store.close_all_db_handles()
