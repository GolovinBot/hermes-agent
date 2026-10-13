"""Reply-facing cron sessions, resolved through the same source and isolation rules as inbound."""

import logging
from typing import Optional

logger = logging.getLogger("cron.scheduler")


def _seed_cron_session(
    job: dict, adapter, platform_name: str, chat_id: str, text: str, *, thread_id: Optional[str],
    chat_type: str, user_id: Optional[str], user_name: Optional[str] = None,
    chat_name: Optional[str], scope_id: Optional[str],
    source=None,
) -> bool:
    """Create the session row (so the mirror has a target) and mirror the brief as a USER turn.
    The seeded key must equal the reply's ``build_session_key``: chat_type, user_id, thread_id and
    scope_id (Slack team id) are all part of it, so callers pass exactly what the reply carries."""
    from cron.scheduler_delivery import _cron_mirror_message
    from gateway.config import Platform
    from gateway.conversation import ConversationAddress, source_for_address
    from gateway.session import is_shared_multi_user_session
    from gateway.mirror import mirror_to_session
    seeded_session_id: Optional[str] = None
    session_store = getattr(adapter, "_session_store", None)
    if session_store is not None:
        try:
            platform_enum = Platform(platform_name.lower())
        except (ValueError, KeyError):
            platform_enum = None
        if platform_enum is not None:
            dest_source = source or source_for_address(
                ConversationAddress(platform_name, str(chat_id), thread_id, scope_id), chat_type,
                chat_name=chat_name, user_id=user_id, user_name=user_name)
            config = session_store.config
            shared = is_shared_multi_user_session(
                dest_source, group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
                thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False))
            if dest_source.chat_type != "dm" and not shared and not (dest_source.user_id_alt or dest_source.user_id):
                logger.warning("Job '%s': cannot seed a user-isolated conversation without its participant", job.get("id"))
                return False
            # Create the row and pass its exact id to the mirror — origin-heuristic rediscovery
            # bails on populated chats.
            _entry = session_store.get_or_create_session(dest_source)
            seeded_session_id = getattr(_entry, "session_id", None)
    if not seeded_session_id:
        return False
    return mirror_to_session(
        platform_name, str(chat_id), _cron_mirror_message(job, text),
        source_label="cron", thread_id=thread_id, user_id=user_id, role="user",
        session_id=seeded_session_id,
    )


def _seed_cron_thread_session(
    job: dict, adapter, platform_name: str, chat_id: str, thread_id: str, mirror_text: str,
    chat_name: Optional[str] = None, is_dm: bool = False, scope_id: Optional[str] = None,
    *, source=None, user_id: Optional[str] = None,
) -> bool:
    """Seed the freshly-opened cron thread's session with the brief (never raises), else the
    user's in-thread reply resolves to a transcript without it. Threads are participant-shared
    (no real user_id); a DM thread must seed ``chat_type="dm"`` — DM-thread replies route through
    the DM arm (``…:dm:<chat>:<thread>``), so a "thread"-typed seed is a row no DM reply hits.
    The resolved source preserves the real participant and account when isolation is enabled."""
    text = (mirror_text or "").strip()
    if not text:
        return False
    try:
        ok = _seed_cron_session(
            job, adapter, platform_name, chat_id, text,
            thread_id=str(thread_id),
            chat_type="dm" if is_dm else "group",
            user_id=user_id, chat_name=chat_name, scope_id=scope_id, source=source)
        if ok:
            logger.info(
                "Job '%s': opened continuable thread %s on %s:%s and seeded the brief",
                job.get("id", "?"), thread_id, platform_name, chat_id)
        else:
            logger.warning(
                "Job '%s': thread seed did NOT land on %s:%s thread=%s — an "
                "in-thread reply will not see this brief",
                job.get("id", "?"), platform_name, chat_id, thread_id)
        return bool(ok)
    except Exception as e:
        # WARNING, not debug: a silent seed failure IS the continuation-amnesia bug.
        logger.warning(
            "Job '%s': seeding cron thread session failed for %s:%s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, thread_id, e)
        return False


def _seed_cron_channel_session(
    job: dict, adapter, platform_name: str, chat_id: str, mirror_text: str, *, is_dm: bool,
    user_id: Optional[str], chat_name: Optional[str] = None, scope_id: Optional[str] = None,
    source=None,
) -> bool:
    """Seed the FLAT (thread_id=None) session for an ``in_channel`` delivery; True on success.
    ``mirror_to_session`` only APPENDS to an existing session and the flat row is only created by
    an inbound human message, so create the row first or the brief is silently dropped. Group keys
    are user-isolated (``…:group:<chat_id>:<user_id>``): the seed MUST carry the origin's real
    user_id, not ``system:cron``; DM keys omit user_id. chat_type mirrors the inbound handler."""
    text = (mirror_text or "").strip()
    if not text:
        return False
    try:
        chat_type = "dm" if is_dm else "group"
        ok = _seed_cron_session(
            job, adapter, platform_name, chat_id, text,
            thread_id=None,  # flat — the whole-channel/DM session
            chat_type=chat_type, user_id=str(user_id) if user_id else None,
            chat_name=chat_name, scope_id=scope_id,
            source=source,
        )
        if ok:
            logger.info(
                "Job '%s': seeded flat in_channel session on %s:%s (chat_type=%s)",
                job.get("id", "?"), platform_name, chat_id, chat_type)
        return bool(ok)
    except Exception as e:
        # WARNING, not debug: a silent seed failure IS the continuation-amnesia bug.
        logger.warning(
            "Job '%s': seeding in_channel session failed for %s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, e)
        return False


def _seed_live_delivery_sessions(t, delivered_message_id) -> None:
    """After a confirmed live send, seed continuation session(s) and run the generic mirror.
    Thread seeding is deferred here so open-succeeds/deliver-fails never seeds an unseen brief."""
    job = t.job
    if t.session_source is None:
        if t.mirror_this_target or t.in_channel_surface:
            logger.warning("Job '%s': continuation skipped because the conversation could not be resolved", job["id"])
        return
    from gateway.conversation import address_of, source_for_address
    from gateway.session_identity import replace_source
    from dataclasses import replace

    def reply_source(thread_id):
        source = source_for_address(
            replace(address_of(t.session_source), thread_id=thread_id),
            t.session_source.conversation_kind or t.session_source.chat_type,
            user_id=t.session_source.user_id, user_id_alt=t.session_source.user_id_alt,
            chat_name=t.session_source.chat_name)
        return replace_source(t.session_source, chat_id=source.chat_id, chat_type=source.chat_type,
                              thread_id=source.thread_id, parent_chat_id=source.parent_chat_id)
    origin = t.origin
    seed_kwargs = dict(
        chat_name=origin.get("chat_name"), is_dm=t.is_dm_target, scope_id=origin.get("scope_id"))
    thread_seeded = False
    inchannel_seeded = False
    if t.opened_thread_id:
        thread_seeded = _seed_cron_thread_session(
            job, t.runtime_adapter, t.platform_name, t.chat_id, t.opened_thread_id, t.mirror_text,
            **seed_kwargs, source=reply_source(t.opened_thread_id), user_id=t.origin_user_id,
        )
    # in_channel: CREATE + seed the flat session (the mirror only APPENDS to an existing one). Same
    # `inchannel_continuable` gate as the flatten in _deliver_result (must not drift). Origin
    # seed without mirror opt-in; others only via _inchannel_seed_allowed (user-less seed = orphan).
    if t.in_channel_surface and t.inchannel_continuable and not thread_seeded:
        inchannel_seeded = _seed_cron_channel_session(
            job, t.runtime_adapter, t.platform_name, t.chat_id, t.mirror_text,
            user_id=t.origin_user_id, **seed_kwargs, source=reply_source(None))
        if not inchannel_seeded:
            logger.warning(
                "Job '%s': in_channel seed did NOT land on %s:%s "
                "— a plain reply will not see this brief",
                job["id"], t.platform_name, t.chat_id)
        # Companion THREAD seed: a reply in the brief's own thread keys to (chat, thread=<ts>),
        # which the flat seed never touches. Seed it too so BOTH reply surfaces continue the job.
        if delivered_message_id:
            _seed_cron_thread_session(
                job, t.runtime_adapter, t.platform_name, t.chat_id, str(delivered_message_id),
                t.mirror_text,
                **seed_kwargs, source=reply_source(str(delivered_message_id)), user_id=t.origin_user_id)
    elif t.in_channel_surface and not t.inchannel_continuable:
        logger.warning(
            "Job '%s': in_channel delivery to %s:%s is not a "
            "continuable target (origin=%s:%s thread=%s; not the "
            "origin conversation, and not a mirror-eligible "
            "fallback/opted-in target the seed can key) — seed "
            "skipped; the plain mirror below may still apply",
            job["id"], t.platform_name, t.chat_id,
            origin.get("platform"), origin.get("chat_id"), origin.get("thread_id"))
    if t.mirror_this_target and not thread_seeded and not inchannel_seeded:
        _seed_cron_session(
            job, t.runtime_adapter, t.platform_name, t.chat_id, t.mirror_text,
            thread_id=t.thread_id, chat_type=t.session_source.chat_type, user_id=t.origin_user_id,
            chat_name=t.session_source.chat_name, scope_id=t.session_source.scope_id,
            source=reply_source(t.thread_id))
