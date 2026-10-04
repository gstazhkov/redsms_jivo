from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import requests


class OmniDeskClient:
    def __init__(self, http: requests.Session | Any | None = None) -> None:
        self.base_url = os.getenv("OMNI_API_URL", "").strip().rstrip("/")
        self.email = os.getenv("OMNI_STAFF_EMAIL", "").strip()
        self.api_key = os.getenv("OMNI_API_KEY", "").strip()
        self.group_id = os.getenv("OMNI_GROUP_ID", "").strip()
        self.staff_id = os.getenv("OMNI_ASSIGNEE_STAFF_ID", "").strip()
        self.http = http or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_key)

    @property
    def handoff_configured(self) -> bool:
        return bool(self.group_id or self.staff_id)

    def _url(self, path: str) -> str:
        if not self.configured:
            raise RuntimeError("OmniDesk API is not configured")
        if not re.fullmatch(r"/api(?:/[A-Za-z0-9_.,-]+)+\.json", path):
            raise ValueError("Invalid OmniDesk API path")
        return f"{self.base_url}{path}"

    def send_reply(self, case_id: str, text: str) -> None:
        case_id = quote(str(case_id), safe="-_")
        response = self.http.post(
            self._url(f"/api/cases/{case_id}/messages.json"),
            json={"message": {"content": text}},
            auth=(self.email, self.api_key),
            timeout=(3, 7),
        )
        response.raise_for_status()

    def assign_operator(self, case_id: str) -> None:
        if not self.handoff_configured:
            raise RuntimeError("Set OMNI_GROUP_ID or OMNI_ASSIGNEE_STAFF_ID to enable handoff")
        case_id = quote(str(case_id), safe="-_")
        assignment: dict[str, Any] = {"status": "open"}
        if self.group_id:
            assignment["group_id"] = self.group_id
        if self.staff_id:
            assignment["staff_id"] = self.staff_id
        response = self.http.put(
            self._url(f"/api/cases/{case_id}.json"),
            json={"case": assignment},
            auth=(self.email, self.api_key),
            timeout=(3, 7),
        )
        response.raise_for_status()


def normalize_omnidesk_payload(raw: dict[str, Any]) -> dict[str, str]:
    case = raw.get("case") if isinstance(raw.get("case"), dict) else {}
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}

    case_id = case.get("case_id") or raw.get("case_id") or raw.get("ticket_id")
    if case_id is None:
        ticket = raw.get("ticket") if isinstance(raw.get("ticket"), dict) else {}
        case_id = ticket.get("id")

    text = (
        message.get("content")
        or message.get("text")
        or raw.get("message_text")
        or raw.get("text")
        or case.get("description")
        or raw.get("case_description")
        or ""
    )
    event_id = (
        raw.get("event_id")
        or raw.get("id")
        or message.get("message_id")
        or raw.get("message_id")
    )
    customer_id = (
        user.get("user_id")
        or user.get("id")
        or raw.get("user_id")
        or raw.get("customer_id")
        or "unknown"
    )
    customer_name = user.get("full_name") or raw.get("user_full_name") or "без имени"
    is_staff_message = bool(message.get("staff_id"))

    if case_id is None or not str(case_id).strip():
        raise ValueError("OmniDesk webhook must contain case_id")
    if not str(text).strip():
        raise ValueError("OmniDesk webhook must contain message text")

    return {
        "case_id": str(case_id).strip(),
        "chat_id": str(case_id).strip(),
        "client_id": str(customer_id),
        "event_id": str(event_id or ""),
        "message_text": str(text).strip(),
        "sender_name": str(customer_name),
        "is_staff_message": "1" if is_staff_message else "0",
    }
