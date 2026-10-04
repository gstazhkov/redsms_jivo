from __future__ import annotations

from typing import Any, Callable, Optional

from out_of_hours import MessageContext, handle_out_of_hours


def normalize_omni_payload(raw: dict[str, Any]) -> MessageContext:
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
    customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}

    text = str(message.get("text") or raw.get("text") or "").strip()
    chat_id = str(chat.get("id") or raw.get("chat_id") or raw.get("id") or "unknown")
    client_id = str(customer.get("id") or raw.get("client_id") or raw.get("user_id") or "unknown")

    return MessageContext(
        channel="omni",
        chat_id=chat_id,
        client_id=client_id,
        message_text=text,
        sender_name=str(sender.get("name") or raw.get("sender_name") or "без имени"),
        page=str(sender.get("url") or raw.get("url") or ""),
        raw=raw,
    )


def handle_omni_message(
    raw: dict[str, Any],
    *,
    match_fn: Callable[[str], Optional[dict[str, Any]]],
    default_reply_fn: Callable[[], str],
    send_text_fn: Callable[[str, str | None, str, str], bool],
    invite_agent_fn: Optional[Callable[[str, str | None, str], bool]] = None,
    notify_telegram_fn: Optional[Callable[[dict[str, Any]], bool]] = None,
    state: Optional[Any] = None,
    reply_key_fn: Optional[Callable[[str, str], str]] = None,
    reply_cooldown: int = 0,
) -> bool:
    ctx = normalize_omni_payload(raw)
    return handle_out_of_hours(
        ctx,
        match_fn=match_fn,
        default_reply_fn=default_reply_fn,
        send_text_fn=send_text_fn,
        invite_agent_fn=invite_agent_fn,
        notify_telegram_fn=notify_telegram_fn,
        state=state,
        reply_key_fn=reply_key_fn,
        reply_cooldown=reply_cooldown,
    )
