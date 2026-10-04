from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class MessageContext:
    channel: str
    chat_id: str
    client_id: str | None = None
    message_text: str = ""
    sender_name: str = ""
    page: str = ""
    raw: dict[str, Any] | None = None


def handle_out_of_hours(
    ctx: MessageContext,
    *,
    match_fn: Callable[[str], Optional[dict[str, Any]]],
    default_reply_fn: Callable[[], str],
    send_text_fn: Callable[[str, str | None, str, str], bool],
    invite_agent_fn: Optional[Callable[[str, str | None, str], bool]] = None,
    notify_telegram_fn: Optional[Callable[[dict[str, Any]], bool]] = None,
    state: Optional[Any] = None,
    reply_key_fn: Optional[Callable[[str, str], str]] = None,
    reply_cooldown: int = 0,
    on_match: Optional[Callable[[dict[str, Any]], None]] = None,
    on_default: Optional[Callable[[str], None]] = None,
) -> bool:
    """Обработка сценария вне рабочего времени для любого канала: Jivo, OmniDesk и т.п."""
    cooldown_key = None
    if state is not None and reply_key_fn is not None and reply_cooldown:
        cooldown_key = reply_key_fn(ctx.channel, ctx.chat_id)
        if not state.set_if_absent(cooldown_key, reply_cooldown):
            return False

    try:
        match = match_fn(ctx.message_text)
        if match:
            answer = str(match.get("text") or "").strip()
            if not answer:
                answer = default_reply_fn()
            if not send_text_fn(ctx.channel, ctx.client_id, ctx.chat_id, answer):
                raise RuntimeError("Ответ не удалось сохранить в очередь отправки")
            if on_match:
                on_match(match)
        else:
            answer = default_reply_fn()
            if not send_text_fn(ctx.channel, ctx.client_id, ctx.chat_id, answer):
                raise RuntimeError("Автоответ не удалось сохранить в очередь отправки")
            if on_default:
                on_default(answer)

        if invite_agent_fn:
            if not invite_agent_fn(ctx.channel, ctx.client_id, ctx.chat_id):
                raise RuntimeError("Передачу оператору не удалось сохранить в очередь")
    except Exception:
        if state is not None and cooldown_key is not None:
            state.delete(cooldown_key)
        raise

    if notify_telegram_fn and ctx.raw is not None:
        notify_telegram_fn(ctx.raw)
    return True
