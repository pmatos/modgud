"""Postmark domain operations and their shared JSON request policy."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from modgud.config import SecretValue

_POSTMARK_API_URL = "https://api.postmarkapp.com"
_RETRY_DELAYS = (1.0, 2.0, 4.0)


@dataclass(frozen=True, slots=True)
class DigestEmail:
    """One self-contained digest ready for an email service."""

    from_address: str
    to_address: str
    subject: str
    html_body: str
    text_body: str


class PostmarkError(RuntimeError):
    """Postmark could not return a usable API response."""


class PostmarkDeliveryError(RuntimeError):
    """Postmark could not accept an outbound digest."""


class _PostmarkRequests:
    def __init__(
        self,
        server_token: SecretValue,
        *,
        base_url: str,
        sleep: Callable[[float], None],
    ) -> None:
        self._server_token = server_token
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    def request[T](
        self,
        path: str,
        *,
        parse: Callable[[dict[str, Any]], T],
        error_type: type[RuntimeError],
        failure_prefix: str,
        payload: Mapping[str, Any] | None = None,
    ) -> T:
        headers = {
            "Accept": "application/json",
            "User-Agent": "modgud/0.1",
            "X-Postmark-Server-Token": self._server_token.reveal(),
        }
        body = None
        if payload is not None:
            body = json.dumps(dict(payload), separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                with urlopen(request, timeout=30) as response:
                    document = json.loads(response.read())
                if not isinstance(document, dict):
                    raise TypeError("response is not a JSON object")
                return parse(cast("dict[str, Any]", document))
            except (
                HTTPError,
                URLError,
                TimeoutError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                if attempt == len(_RETRY_DELAYS):
                    detail = (
                        f"HTTP {error.code}"
                        if isinstance(error, HTTPError)
                        else type(error).__name__
                    )
                    raise error_type(
                        f"{failure_prefix} failed after {attempt + 1} attempts ({detail})"
                    ) from error
                self._sleep(_RETRY_DELAYS[attempt])

        raise AssertionError("unreachable")


class PostmarkClient:
    """Small adapter for the Postmark inbound Messages API."""

    def __init__(
        self,
        server_token: SecretValue,
        *,
        base_url: str = _POSTMARK_API_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._requests = _PostmarkRequests(server_token, base_url=base_url, sleep=sleep)

    def search_inbound(self, *, count: int, offset: int) -> Mapping[str, Any]:
        query = urlencode({"count": count, "offset": offset, "status": "processed"})
        return self._get_json(f"/messages/inbound?{query}")

    def get_inbound(self, message_id: str) -> Mapping[str, Any]:
        encoded_id = quote(message_id, safe="")
        return self._get_json(f"/messages/inbound/{encoded_id}/details")

    def _get_json(self, path: str) -> Mapping[str, Any]:
        return self._requests.request(
            path,
            parse=lambda document: document,
            error_type=PostmarkError,
            failure_prefix="Postmark API request",
        )


class PostmarkEmailClient:
    """Small adapter for Postmark's single-email API."""

    def __init__(
        self,
        server_token: SecretValue,
        *,
        base_url: str = _POSTMARK_API_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._requests = _PostmarkRequests(server_token, base_url=base_url, sleep=sleep)

    def send_email(self, message: DigestEmail) -> str:
        """Submit one multipart digest and return Postmark's message ID."""
        return self._requests.request(
            "/email",
            payload={
                "From": message.from_address,
                "HtmlBody": message.html_body,
                "MessageStream": "outbound",
                "Subject": message.subject,
                "TextBody": message.text_body,
                "To": message.to_address,
            },
            parse=_accepted_message_id,
            error_type=PostmarkDeliveryError,
            failure_prefix="Postmark email send",
        )


def _accepted_message_id(document: dict[str, Any]) -> str:
    if document.get("ErrorCode") != 0:
        raise ValueError("Postmark rejected the message")
    message_id = document.get("MessageID")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Postmark response has no message ID")
    return message_id
