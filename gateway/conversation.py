"""Conversation location and kind, independent of session isolation and legacy key spelling."""

import re
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class ConversationAddress:
    platform: str
    chat_id: str
    thread_id: Optional[str] = None
    scope_id: Optional[str] = None
    profile: Optional[str] = None
    account: Optional[str] = None

    def __post_init__(self):
        for value in (self.platform, self.chat_id):
            if not isinstance(value, str):
                raise ValueError("Conversation platform and chat ID must be strings")
        for value in (self.thread_id, self.scope_id, self.profile, self.account):
            if value is not None and not isinstance(value, str):
                raise ValueError("Conversation address fields must be strings or null")


def conversation_kind(chat_type: str) -> str:
    # Old adapters used "thread" as a kind; topology now lives in the address.
    kind = {"thread": "group", "forum": "group", "c2c": "dm", "guild": "group", "private": "dm"}.get(chat_type, chat_type)
    if kind not in {"dm", "group", "channel"}:
        raise ValueError(f"Unknown conversation kind: {chat_type!r}")
    return kind


def address_of(source) -> ConversationAddress:
    from gateway.session_identity import identity_of, transport_profile_of

    identity = identity_of(source)
    is_dm = (getattr(source, "conversation_kind", None) or source.chat_type) == "dm"
    thread = source.thread_id or (None if is_dm else getattr(source, "prospective_thread_id", None))
    parent = getattr(source, "parent_chat_id", None) or source.chat_id
    return ConversationAddress(
        source.platform.value, str(parent), str(thread) if thread else None,
        getattr(source, "scope_id", None), identity.runtime_profile if identity else getattr(source, "profile", None),
        transport_profile_of(source),
    )


def conversation_origin(source, *, thread_id: Optional[str]) -> dict:
    from dataclasses import replace

    address = replace(address_of(source), thread_id=thread_id)
    return {"version": 1, "address": asdict(address),
            "kind": conversation_kind(source.conversation_kind or source.chat_type)}


def source_for_address(address: ConversationAddress, kind: str, **fields):
    """Encode an address using existing adapter/key layouts, without guessing its kind."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    kind = conversation_kind(kind)
    chat_type = kind
    chat_id = address.chat_id
    if address.thread_id and kind != "dm" and address.platform == "discord":
        chat_type, chat_id = "thread", address.thread_id
    elif address.platform == "discord" and kind != "dm":
        chat_type = "group"
    return SessionSource(
        platform=Platform(address.platform), chat_id=chat_id, chat_type=chat_type,
        thread_id=address.thread_id, parent_chat_id=address.chat_id if address.thread_id else None,
        scope_id=address.scope_id, profile=address.profile, conversation_kind=kind, **fields,
    )


def receiving_adapter_for_origin(adapter, platform: str, chat_id: str, origin: dict):
    """A captured receiver takes precedence over the runtime profile's own bot."""
    captured = origin.get("conversation")
    if (not isinstance(captured, dict) or captured.get("version") != 1
            or origin.get("platform") != platform
            or str(origin.get("parent_chat_id") or origin.get("chat_id", "")) != str(chat_id)):
        return adapter
    address = ConversationAddress(**captured["address"])
    runner = getattr(adapter, "gateway_runner", None)
    if not address.account or not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return adapter
    if (address.platform, address.chat_id) != (platform, str(chat_id)):
        raise PermissionError("Cron conversation address disagrees with its delivery target")
    from gateway.session_identity import restore_identity
    source = source_for_address(address, captured["kind"], user_id=origin.get("user_id"),
                                user_id_alt=origin.get("user_id_alt"))
    restore_identity(source, runner=runner, transport_profile=address.account)
    receiving = runner._delivery_adapter_for(source)
    if receiving is None:
        raise PermissionError("The cron conversation's receiving bot is offline; another bot cannot replace it")
    return receiving


async def resolve_delivery_source(adapter, platform: str, chat_id: str, thread_id: Optional[str], origin: dict):
    """Resolve a reply-facing source through the receiving adapter; ambiguous legacy origins refuse."""
    from gateway.session_identity import identity_of

    same_chat = (str(origin.get("platform", "")).lower() == platform
                 and str(origin.get("parent_chat_id") or origin.get("chat_id", "")) == str(chat_id))
    captured = origin.get("conversation") if same_chat else None
    if captured is not None:
        if not isinstance(captured, dict) or captured.get("version") != 1:
            raise PermissionError("Unsupported cron conversation origin; recreate or repair the job")
        stored = captured.get("address")
        if not isinstance(stored, dict) or stored.get("platform") != platform or stored.get("chat_id") != str(chat_id):
            raise PermissionError("Cron conversation address disagrees with its delivery target")
        ConversationAddress(**stored)
        kind = conversation_kind(captured.get("kind"))
    else:
        stored = {}
        import inspect
        scope = origin.get("scope_id") if same_chat else None
        kwargs = {"scope_id": scope} if scope and "scope_id" in inspect.signature(adapter.get_chat_info).parameters else {}
        info = await adapter.get_chat_info(str(chat_id), **kwargs)
        if not isinstance(info, dict) or info.get("error"):
            raise ValueError("Adapter could not resolve the legacy cron conversation")
        kind = conversation_kind(info.get("type"))
        stored["scope_id"] = (origin.get("scope_id") if same_chat else None) or info.get("scope_id")
    if platform == "slack" and not stored.get("scope_id"):
        raise ValueError("Cannot seed a Slack conversation without its workspace scope")
    address = ConversationAddress(
        platform, str(chat_id), str(thread_id) if thread_id else None,
        stored.get("scope_id") or (origin.get("scope_id") if same_chat else None),
        stored.get("profile") or (origin.get("profile") if same_chat else None),
        stored.get("account"),
    )
    source = source_for_address(
        address, kind, user_id=origin.get("user_id") if same_chat else None,
        user_id_alt=origin.get("user_id_alt") if same_chat else None,
        chat_name=origin.get("chat_name") if same_chat else None,
    )
    adapter._canonicalize(source)
    if source.profile_route_rejected:
        raise PermissionError("Cron conversation routes to an unserved profile")
    identity = identity_of(source)
    if address.account and (identity is None or identity.transport_profile != address.account):
        raise PermissionError("Cron conversation belongs to another bot account")
    if address.profile and identity and identity.runtime_profile != address.profile:
        raise PermissionError("Cron conversation belongs to another runtime profile")
    return source


def existing_delivery_session(store, platform: str, chat_id: str, thread_id: Optional[str], origin: dict):
    """Compatibility for sends without a live adapter: one exact address and allowed participant."""
    from gateway.session import is_shared_multi_user_session

    same_chat = (origin.get("platform") == platform
                 and str(origin.get("parent_chat_id") or origin.get("chat_id", "")) == str(chat_id))
    captured = origin.get("conversation") if same_chat else None
    if captured is not None and (not isinstance(captured, dict) or captured.get("version") != 1):
        return None
    expected = captured.get("address", {}) if captured else (origin if same_chat else {})
    if captured:
        parsed = ConversationAddress(**expected)
        if (parsed.platform, parsed.chat_id) != (platform, str(chat_id)):
            return None
    participant = (origin.get("user_id_alt") or origin.get("user_id")) if same_chat else None
    kind = captured.get("kind") if captured else (origin.get("chat_type") if same_chat else None)
    candidates = []
    for entry in store.list_sessions():
        source = entry.origin
        if source is None:
            continue
        address = address_of(source)
        if (address.platform, address.chat_id, address.thread_id) != (platform, str(chat_id), thread_id):
            continue
        if kind and conversation_kind(kind) != conversation_kind(source.conversation_kind or source.chat_type):
            continue
        if expected.get("account") and entry.transport_profile != expected["account"]:
            continue
        if expected.get("scope_id") and address.scope_id != expected["scope_id"]:
            continue
        profile = (store._profile_from_session_key(entry.session_key) if store.config.multiplex_profiles
                   else store._active_profile_name())
        if expected.get("profile") and profile != expected["profile"]:
            continue
        shared = is_shared_multi_user_session(
            source, group_sessions_per_user=store.config.group_sessions_per_user,
            thread_sessions_per_user=store.config.thread_sessions_per_user)
        if source.chat_type != "dm" and not shared:
            if not participant or participant != (source.user_id_alt or source.user_id):
                continue
        candidates.append(entry)
    return candidates[0] if len(candidates) == 1 else None


_PROFILE_ID_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _parse_session_key(session_key: str) -> dict | None:
    """Parse a session key (``agent:{ns}:{platform}:{chat_type}:{chat_id}[:{extra}...]``).

    ``{ns}`` is ``main`` for the default profile, ``main~`` for a profile literally named ``main``
    (``gateway.session._session_key_namespace``), or a named-profile id (profile ids match
    ``[a-z0-9][a-z0-9_-]{0,63}`` — never contain ``:`` — so a plain split stays unambiguous).
    For group/channel sessions the suffix may be a user_id, not a thread_id, so ``thread_id``
    is omitted. Named profiles are reported as ``profile``; ``main`` keys keep their historical
    shape exactly (no ``profile`` key) so equality assertions on parsed dicts stay stable.
    """
    if ":account~:" in session_key:
        return None  # A receiving bot requires a persisted origin, never a raw-key guess.
    from gateway.session import profile_from_session_key_namespace
    parts = session_key.split(":")
    if (
        len(parts) >= 5
        and parts[0] == "agent"
        and (parts[1] in ("main", "main~") or _PROFILE_ID_KEY_RE.match(parts[1]))
    ):
        result = {"platform": parts[2], "chat_type": parts[3], "chat_id": parts[4]}
        if parts[1] != "main":
            result["profile"] = profile_from_session_key_namespace(parts[1])
        if len(parts) > 5 and parts[3] in {"dm", "thread"}:
            result["thread_id"] = parts[5]
        return result
    return None
