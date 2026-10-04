import importlib
import json
import os
import sqlite3
import sys


def reload_module(monkeypatch, tmp_path, templates_path=None):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("JIVO_PROVIDER_ID", "123")
    monkeypatch.setenv("SUPPORT_DB_PATH", str(tmp_path / "support.sqlite3"))
    if templates_path is not None:
        monkeypatch.setenv("TEMPLATES_FILE", str(templates_path))
    else:
        monkeypatch.delenv("TEMPLATES_FILE", raising=False)
    sys.modules.pop("bot_search_improved", None)
    module = importlib.import_module("bot_search_improved")
    store = module.support_store
    claim_event = store.claim_event
    claim_outbound = store.claim_outbound
    monkeypatch.setattr(
        store,
        "claim_event",
        lambda event_id=None: claim_event(event_id) if event_id is not None else None,
    )
    monkeypatch.setattr(
        store,
        "claim_outbound",
        lambda job_id=None: claim_outbound(job_id) if job_id is not None else None,
    )
    return module


def test_find_template_returns_match_and_counts_metric(monkeypatch, tmp_path):
    module = reload_module(monkeypatch, tmp_path)
    module.reset_match_metrics()

    result = module.find_template("оплатили счет деньги не поступили в кабинет")

    assert result is not None
    assert result["id"] == 2
    assert module.get_match_metrics()["matched"] >= 1


def test_find_template_counts_not_found(monkeypatch, tmp_path):
    module = reload_module(monkeypatch, tmp_path)
    module.reset_match_metrics()

    result = module.find_template("qwerrtuiop asdfghjkl zxcvbnm qwerty")

    assert result is None
    assert module.get_match_metrics()["not_found"] >= 1


def test_find_template_counts_ambiguous_case(monkeypatch, tmp_path):
    templates_path = tmp_path / "templates.json"
    templates_path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": 101,
                        "status": "ready",
                        "title": "Проблема с оплатой",
                        "details": {
                            "trigger": "Проблема с оплатой",
                            "response": "Первый ответ",
                        },
                    },
                    {
                        "id": 102,
                        "status": "ready",
                        "title": "Оплата и проблема",
                        "details": {
                            "trigger": "Оплата и проблема",
                            "response": "Второй ответ",
                        },
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    module = reload_module(monkeypatch, tmp_path, templates_path)
    module.reset_match_metrics()

    result = module.find_template("проблема с оплатой")

    assert result is None
    assert module.get_match_metrics()["ambiguous"] >= 1


def test_metrics_endpoint_exposes_counters(monkeypatch, tmp_path):
    module = reload_module(monkeypatch, tmp_path)
    module.reset_match_metrics()
    module.record_match_metric("matched")
    module.record_match_metric("not_found")
    module.record_match_metric("ambiguous")

    client = module.app.test_client()
    response = client.get("/metrics")

    assert response.status_code == 200
    data = response.get_json()
    assert data["matched"] >= 1
    assert data["not_found"] >= 1
    assert data["ambiguous"] >= 1


def test_webhook_persists_event_before_acknowledging(monkeypatch, tmp_path):
    module = reload_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "submit_background", lambda *args: None)

    response = module.app.test_client().post(
        "/test-token",
        json={
            "id": "event-1",
            "event": "CLIENT_MESSAGE",
            "client_id": "client-1",
            "chat_id": "chat-1",
        },
    )

    assert response.status_code == 200
    queued = module.support_store.claim_event("event-1")
    assert queued is not None
    assert queued["payload"]["chat_id"] == "chat-1"


def test_failed_handoff_is_recorded_and_retried(monkeypatch, tmp_path):
    module = reload_module(monkeypatch, tmp_path)

    class Response:
        status_code = 503
        text = "temporarily unavailable"
        ok = False

    monkeypatch.setattr(module.http, "post", lambda *args, **kwargs: Response())

    assert module.invite_agent("client-1", "chat-1") is True
    [handoff] = module.support_store.recent_handoffs()
    assert handoff["status"] == "pending"
    assert handoff["attempts"] == 1
    assert "503" in handoff["last_error"]

    with sqlite3.connect(tmp_path / "support.sqlite3") as connection:
        connection.execute("UPDATE outbound_jobs SET next_attempt = 0")

    class Success:
        ok = True

    monkeypatch.setattr(module.http, "post", lambda *args, **kwargs: Success())
    assert module.dispatch_outbound(handoff["handoff_id"]) is True

    [handoff] = module.support_store.recent_handoffs()
    assert handoff["status"] == "sent"
    assert handoff["attempts"] == 2
