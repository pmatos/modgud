"""Signed capabilities for label confirmation links."""

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, cast

from modgud.config import SecretValue


class InvalidLabelToken(ValueError):
    """A label capability was malformed, modified, or used out of scope."""


class ExpiredLabelToken(InvalidLabelToken):
    """A once-valid label capability is past its signed expiry."""


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise InvalidLabelToken("malformed token") from error


def create_label_token(
    item_id: int,
    label: str,
    *,
    signing_secret: SecretValue,
    expires_at: datetime,
) -> str:
    """Return a URL-safe token binding an item, label, and expiry."""
    payload = json.dumps(
        {
            "expires_at": int(expires_at.timestamp()),
            "item_id": item_id,
            "label": label,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.digest(
        signing_secret.reveal().encode(),
        encoded_payload,
        hashlib.sha256,
    )
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded_payload.decode()}.{encoded_signature.decode()}"


def validate_label_token(
    token: str,
    item_id: int,
    label: str,
    *,
    signing_secret: SecretValue,
    now: datetime,
) -> None:
    """Reject a token that is modified or bound to a different route."""
    try:
        encoded_payload, encoded_signature = token.split(".")
    except ValueError as error:
        raise InvalidLabelToken("malformed token") from error
    expected_signature = hmac.digest(
        signing_secret.reveal().encode(),
        encoded_payload.encode(),
        hashlib.sha256,
    )
    if not hmac.compare_digest(_decode(encoded_signature), expected_signature):
        raise InvalidLabelToken("invalid signature")
    try:
        document = json.loads(_decode(encoded_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidLabelToken("malformed payload") from error
    if not isinstance(document, dict):
        raise InvalidLabelToken("malformed payload")
    payload = cast("dict[str, Any]", document)
    if set(payload) != {"expires_at", "item_id", "label"}:
        raise InvalidLabelToken("malformed payload")
    if (
        type(payload["expires_at"]) is not int
        or type(payload["item_id"]) is not int
        or not isinstance(payload["label"], str)
    ):
        raise InvalidLabelToken("malformed payload")
    if payload["item_id"] != item_id:
        raise InvalidLabelToken("wrong item")
    if payload["label"] != label:
        raise InvalidLabelToken("wrong label")
    if now.timestamp() >= payload["expires_at"]:
        raise ExpiredLabelToken("expired token")
