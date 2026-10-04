import importlib
import sys
import uuid

import pytest

from omnidesk_client import OmniDeskClient, normalize_omnidesk_payload
from omni_adapter import normalize_omni_payload
from out_of_hours import MessageContext, handle_out_of_hours


class FakeResponse:
    ok = True

    def raise_for_status(self):
        return None


class FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse()

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse()


def test_omnidesk_client_sends_reply_and_assignment_requests(monkeypatch):
    monkeypatch.setenv("OMNI_API_URL", "https://redsms.omnidesk.ru")
    monkeypatch.setenv("OMNI_STAFF_EMAIL", "bot@redsms.ru")
    monkeypatch.setenv("OMNI_API_KEY", "api-key")
    monkeypatch.setenv("OMNI_GROUP_ID", "42")
    monkeypatch.setenv("OMNI_ASSIGNEE_STAFF_ID", "17")
    http = FakeHttp()
    client = OmniDeskClient(http)

    client.send_reply("12345", "Проверочный ответ")
    client.assign_operator("12345")

    assert http.calls[0] == (
        "POST",
        "https://redsms.omnidesk.ru/api/cases/12345/messages.json",
        {
            "json": {"message": {"content": "Проверочный ответ"}},
            "auth": ("bot@redsms.ru", "api-key"),
            "timeout": (3, 7),
        },
    )
    assert http.calls[1][0:2] == (
        "PUT",
        "https://redsms.omnidesk.ru/api/cases/12345.json",
    )
    assert http.calls[1][2]["json"] == {
        "case": {"status": "open", "group_id": "42", "staff_id": "17"}
    }


def test_normalize_omnidesk_webhook_payload():
    payload = {
        "event_id": "evt-12",
        "case": {"case_id": 321, "description": "Не работает API"},
        "user": {"user_id": 7, "full_name": "Иван"},
        "message": {"message_id": 88, "staff_id": 0},
    }

    normalized = normalize_omnidesk_payload(payload)

    assert normalized["case_id"] == "321"
    assert normalized["message_text"] == "Не работает API"
    assert normalized["client_id"] == "7"
    assert normalized["event_id"] == "evt-12"
    assert normalized["is_staff_message"] == "0"


def reload_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "test-jivo-token")
    monkeypatch.setenv("JIVO_PROVIDER_ID", "123")
    monkeypatch.setenv("OMNI_WEBHOOK_TOKEN", "test-omni-token")
    monkeypatch.setenv("SUPPORT_DB_PATH", str(tmp_path / "support.sqlite3"))
    monkeypatch.setenv("TEMPLATES_FILE", str(tmp_path / "empty-templates.json"))
    sys.modules.pop("bot_search_improved", None)
    module = importlib.import_module("bot_search_improved")
    monkeypatch.setattr(module, "submit_background", lambda *args: None)
    return module


def test_omnidesk_webhook_validates_and_persists_event(monkeypatch, tmp_path):
    module = reload_app(monkeypatch, tmp_path)
    module.omnidesk.base_url = "https://redsms.omnidesk.ru"
    module.omnidesk.email = "bot@redsms.ru"
    module.omnidesk.api_key = "api-key"
    module.omnidesk.group_id = "42"
    client = module.app.test_client()
    payload = {
        "event_id": "evt-1",
        "case": {"case_id": 321},
        "message": {"content": "Нужна помощь", "staff_id": 0},
    }

    assert client.post("/omni/wrong-token", json=payload).status_code == 401
    accepted = client.post("/omni/test-omni-token", json=payload)
    duplicate = client.post("/omni/test-omni-token", json=payload)

    assert accepted.status_code == 200
    assert duplicate.status_code == 200
    queued = module.support_store.claim_event("omni:evt-1")
    assert queued is not None
    assert queued["payload"]["_provider"] == "omnidesk"


def test_omnidesk_handoff_queues_reply_and_assignment(monkeypatch, tmp_path):
    module = reload_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "dispatch_outbound", lambda job_id=None: True)
    module.handle_omnidesk_message(
        {
            "event_id": "evt-2",
            "case": {"case_id": 456},
            "message": {"content": "Не работает API", "staff_id": 0},
        }
    )

    reply_job = module.support_store.claim_outbound(
        str(uuid.uuid5(uuid.NAMESPACE_URL, "omnidesk:evt-2:reply"))
    )
    assign_job = module.support_store.claim_outbound(
        str(uuid.uuid5(uuid.NAMESPACE_URL, "omnidesk:evt-2:assign"))
    )

    assert reply_job["payload"]["operation"] == "reply"
    assert reply_job["payload"]["content"]
    assert assign_job["payload"]["operation"] == "assign"


def test_omnidesk_message_dispatches_reply_and_operator_assignment(monkeypatch, tmp_path):
    module = reload_app(monkeypatch, tmp_path)
    module.omnidesk.base_url = "https://redsms.omnidesk.ru"
    module.omnidesk.email = "bot@redsms.ru"
    module.omnidesk.api_key = "api-key"
    module.omnidesk.group_id = "42"
    calls = []

    class Response:
        ok = True

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return Response()

    def fake_put(url, **kwargs):
        calls.append(("PUT", url, kwargs))
        return Response()

    monkeypatch.setattr(module.http, "post", fake_post)
    monkeypatch.setattr(module.http, "put", fake_put)
    module.handle_omnidesk_message(
        {
            "event_id": "evt-3",
            "case": {"case_id": 789},
            "message": {"content": "Не пришел отчет", "staff_id": 0},
        }
    )

    assert [(method, url) for method, url, _ in calls] == [
        ("POST", "https://redsms.omnidesk.ru/api/cases/789/messages.json"),
        ("PUT", "https://redsms.omnidesk.ru/api/cases/789.json"),
    ]
    assert calls[0][2]["json"]["message"]["content"]
    assert calls[1][2]["json"] == {
        "case": {"status": "open", "group_id": "42"}
    }
    [handoff] = module.support_store.recent_handoffs()
    assert handoff["status"] == "sent"


def test_omnidesk_webhook_ignores_staff_message(monkeypatch, tmp_path):
    module = reload_app(monkeypatch, tmp_path)
    module.omnidesk.base_url = "https://redsms.omnidesk.ru"
    module.omnidesk.email = "bot@redsms.ru"
    module.omnidesk.api_key = "api-key"
    module.omnidesk.group_id = "42"

    response = module.app.test_client().post(
        "/omni/test-omni-token",
        json={
            "event_id": "staff-evt",
            "case": {"case_id": 321},
            "message": {"content": "Ответ оператора", "staff_id": 17},
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ignored": "staff_message"}


def test_normalize_omni_payload_extracts_chat_and_message():
    payload = {
        "event": "message",
        "message": {
            "text": "Не пришли деньги на баланс",
            "sender": {"name": "Иван", "url": "https://example.com/u/1"},
        },
        "chat": {"id": "omni-42"},
        "customer": {"id": "cust-7"},
    }

    ctx = normalize_omni_payload(payload)

    assert ctx.channel == "omni"
    assert ctx.chat_id == "omni-42"
    assert ctx.client_id == "cust-7"
    assert ctx.message_text == "Не пришли деньги на баланс"
    assert ctx.sender_name == "Иван"


def test_handle_out_of_hours_uses_template_and_default_reply():
    calls = []

    def fake_match(text):
        if text == "не пришли деньги на баланс":
            return {"text": "Платежи обрабатываются бухгалтерией вручную."}
        return None

    def fake_default_reply():
        return "Общий автоответ"

    def fake_send_text(channel, client_id, chat_id, text):
        calls.append((channel, client_id, chat_id, text))
        return True

    def fake_invite_agent(channel, client_id, chat_id):
        calls.append((channel, "invite", client_id, chat_id))
        return True

    def fake_notify_telegram(raw):
        calls.append(("telegram", raw.get("chat", {}).get("id")))
        return True

    ctx = MessageContext(
        channel="omni",
        chat_id="omni-42",
        client_id="cust-7",
        message_text="не пришли деньги на баланс",
        sender_name="Иван",
        page="https://example.com/u/1",
        raw={"chat": {"id": "omni-42"}},
    )

    result = handle_out_of_hours(
        ctx,
        match_fn=fake_match,
        default_reply_fn=fake_default_reply,
        send_text_fn=fake_send_text,
        invite_agent_fn=fake_invite_agent,
        notify_telegram_fn=fake_notify_telegram,
        state=None,
    )

    assert result is True
    assert any(call[0] == "omni" and call[3] == "Платежи обрабатываются бухгалтерией вручную." for call in calls)
    assert any(call[0] == "omni" and call[1] == "invite" for call in calls)


def test_failed_reply_releases_cooldown_for_retry():
    class State:
        values = set()

        def set_if_absent(self, key, ttl):
            if key in self.values:
                return False
            self.values.add(key)
            return True

        def delete(self, key):
            self.values.discard(key)

    state = State()
    ctx = MessageContext(channel="omni", chat_id="omni-42", message_text="Нужна помощь")

    with pytest.raises(RuntimeError, match="не удалось сохранить в очередь"):
        handle_out_of_hours(
            ctx,
            match_fn=lambda text: None,
            default_reply_fn=lambda: "Ответ",
            send_text_fn=lambda channel, client_id, chat_id, text: False,
            state=state,
            reply_key_fn=lambda channel, chat_id: f"{channel}:{chat_id}",
            reply_cooldown=60,
        )

    assert state.values == set()
