"""Generate and persist tier-1 artifacts for extracted web items."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.models import create_model_client

_SYSTEM_PROMPT = """You create compact decision aids for saved articles.
Return one JSON object with exactly these fields:
- "one_liner": one line saying what the item is
- "claims": an array of 3 to 5 specific claims the item actually makes

Claims must report the item's substantive assertions, not paraphrase its topic.
Use only the supplied extracted text. Do not add markdown or commentary."""


@dataclass(frozen=True, slots=True)
class Tier1Summary:
    """The structured artifact shown inline for one item."""

    one_liner: str
    claims: tuple[str, ...]


def _parse_summary(content: str) -> Tier1Summary:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != {"one_liner", "claims"}:
        raise ValueError("summary must contain exactly one_liner and claims")
    fields = cast("dict[str, Any]", parsed)
    one_liner = fields["one_liner"]
    claims = fields["claims"]
    if (
        not isinstance(one_liner, str)
        or not one_liner.strip()
        or "\n" in one_liner
        or "\r" in one_liner
    ):
        raise ValueError("one_liner must be one non-empty line")
    if (
        not isinstance(claims, list)
        or not 3 <= len(claims) <= 5
        or any(not isinstance(claim, str) or not claim.strip() for claim in claims)
    ):
        raise ValueError("claims must contain 3 to 5 non-empty strings")
    return Tier1Summary(one_liner=one_liner.strip(), claims=tuple(claims))


def summarize_item(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
    *,
    settings: Settings,
) -> Tier1Summary | None:
    """Generate and replace the current tier-1 artifact for one web item."""
    item = connection.execute(
        "SELECT format, extracted_text_hash FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ValueError(f"item {item_id} does not exist")
    item_format, extracted_text_hash = item
    if item_format != "web" or extracted_text_hash is None:
        raise ValueError(f"item {item_id} has no extracted web text")
    extracted_text = blob_store.get(str(extracted_text_hash)).decode("utf-8")

    routed = create_model_client("tier_1_summary", settings=settings)
    summary = None
    try:
        for _attempt in range(2):
            completion = routed.client.chat.completions.create(
                model=routed.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": extracted_text},
                ],
                response_format={"type": "json_object"},
            )
            try:
                content = completion.choices[0].message.content
                if not isinstance(content, str):
                    raise TypeError("model returned no summary content")
                summary = _parse_summary(content)
            except (IndexError, TypeError, ValueError):
                continue
            break
    finally:
        routed.client.close()
    if summary is None:
        failure_payload = json.dumps(
            {
                "attempts": 2,
                "error": "model returned malformed summary output",
                "stage": "summary",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            UPDATE items
            SET state = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM tier_1_summaries
                        WHERE tier_1_summaries.item_id = items.id
                    ) THEN 'summarized'
                    ELSE 'failed'
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (item_id,),
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'failed', ?)",
            (item_id, failure_payload),
        )
        return None
    claims = json.dumps(list(summary.claims), ensure_ascii=False)
    event_payload = json.dumps(
        {
            "claims": summary.claims,
            "model": routed.model,
            "one_liner": summary.one_liner,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO tier_1_summaries (item_id, one_liner, claims)
        VALUES (?, ?, ?)
        ON CONFLICT (item_id) DO UPDATE SET
            one_liner = excluded.one_liner,
            claims = excluded.claims,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (item_id, summary.one_liner, claims),
    )
    connection.execute(
        """
        UPDATE items
        SET state = 'summarized',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (item_id,),
    )
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'summarized', ?)",
        (item_id, event_payload),
    )
    return summary
