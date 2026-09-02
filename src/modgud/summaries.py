"""Generate and persist tier-1 artifacts for extracted items."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.models import RoutedModelClient, create_model_client
from modgud.transcripts import chunk_transcript
from modgud.youtube import Chapter

_SYSTEM_PROMPT = """You create compact decision aids for saved items.
Return one JSON object with exactly these fields:
- "one_liner": one line saying what the item is
- "claims": an array of 3 to 5 specific claims the item actually makes

Claims must report the item's substantive assertions, not paraphrase its topic.
Use only the supplied source text. Do not add markdown or commentary."""
_COMBINE_PROMPT = """You create compact decision aids for saved items.
Combine the supplied summaries of consecutive transcript chunks into one JSON
object with exactly these fields:
- "one_liner": one line saying what the complete item is
- "claims": an array of 3 to 5 specific claims the complete item actually makes

Preserve the most substantive claims across the complete item, remove overlap,
and do not mention chunks. Use only the supplied chunk summaries. Do not add
markdown or commentary."""


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


def _request_summary(
    routed: RoutedModelClient,
    source_text: str,
    *,
    system_prompt: str = _SYSTEM_PROMPT,
) -> Tier1Summary | None:
    for _attempt in range(2):
        completion = routed.client.chat.completions.create(
            model=routed.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": source_text},
            ],
            response_format={"type": "json_object"},
        )
        try:
            content = completion.choices[0].message.content
            if not isinstance(content, str):
                raise TypeError("model returned no summary content")
            return _parse_summary(content)
        except (IndexError, TypeError, ValueError):
            continue
    return None


def summarize_item(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
    *,
    settings: Settings,
) -> Tier1Summary | None:
    """Generate and replace the current tier-1 artifact for one extracted item."""
    item = connection.execute(
        "SELECT format, extracted_text_hash, chapters FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ValueError(f"item {item_id} does not exist")
    item_format, extracted_text_hash, chapters_json = item
    if extracted_text_hash is None:
        raise ValueError(f"item {item_id} has no extracted text")
    extracted_content = blob_store.get(str(extracted_text_hash))
    source_texts: tuple[str, ...]
    if item_format == "web":
        source_texts = (extracted_content.decode("utf-8"),)
    elif item_format == "youtube":
        chapters: tuple[Chapter, ...] = ()
        if chapters_json is not None:
            parsed_chapters = json.loads(str(chapters_json))
            if not isinstance(parsed_chapters, list):
                raise ValueError(f"item {item_id} has malformed chapters")
            chapters = tuple(cast("list[Chapter]", parsed_chapters))
        chunks = chunk_transcript(extracted_content, chapters=chapters)
        if not chunks:
            raise ValueError(f"item {item_id} has no transcript cues")
        source_texts = tuple(chunk.text for chunk in chunks)
    else:
        raise ValueError(f"item {item_id} has no supported extracted text")

    routed = create_model_client("tier_1_summary", settings=settings)
    summary = None
    try:
        chunk_summaries = []
        for source_text in source_texts:
            chunk_summary = _request_summary(routed, source_text)
            if chunk_summary is None:
                break
            chunk_summaries.append(chunk_summary)
        if len(chunk_summaries) == 1 and len(source_texts) == 1:
            summary = chunk_summaries[0]
        elif len(chunk_summaries) == len(source_texts):
            combined_input = json.dumps(
                [
                    {
                        "claims": chunk_summary.claims,
                        "one_liner": chunk_summary.one_liner,
                    }
                    for chunk_summary in chunk_summaries
                ],
                ensure_ascii=False,
            )
            summary = _request_summary(
                routed,
                combined_input,
                system_prompt=_COMBINE_PROMPT,
            )
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
