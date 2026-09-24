"""Scheduled transcription fallback for refused YouTube captions."""

from dataclasses import dataclass
from pathlib import Path

from openai import OpenAIError
from yt_dlp.utils import DownloadError

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.database import connect
from modgud.events import ItemLog
from modgud.item_lifecycle import mark_extracted, mark_failed
from modgud.models import create_model_client
from modgud.youtube import download_youtube_audio


@dataclass(frozen=True, slots=True)
class AudioFallbackBatchResult:
    """Counts produced by one audio-fallback batch."""

    attempted: int
    transcribed: int
    failed: int


def run_audio_fallback_batch(
    database: str | Path,
    blob_store: BlobStore,
    *,
    settings: Settings,
) -> AudioFallbackBatchResult:
    """Transcribe every captured YouTube item with refused captions."""
    with connect(database) as connection:
        pending = connection.execute(
            """
            SELECT items.id, items.canonical_url
            FROM items
            WHERE items.format = 'youtube'
              AND items.state = 'captured'
              AND items.extracted_text_hash IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM events
                  WHERE events.item_id = items.id
                    AND events.type = 'caption_refused'
              )
            ORDER BY items.id
            """
        ).fetchall()

    transcribed = 0
    failed = 0
    routed = create_model_client("transcription", settings=settings)
    try:
        for item_id, canonical_url in pending:
            try:
                with (
                    download_youtube_audio(str(canonical_url)) as audio_path,
                    audio_path.open("rb") as audio,
                ):
                    transcript = routed.client.audio.transcriptions.create(
                        file=audio,
                        model=routed.model,
                        response_format="vtt",
                    )
            except (DownloadError, OpenAIError, OSError, ValueError) as error:
                with connect(database) as connection:
                    ItemLog(connection, int(item_id)).audio_fallback("failed")
                    mark_failed(
                        connection,
                        int(item_id),
                        error=str(error),
                        stage="audio_fallback",
                    )
                failed += 1
                continue
            transcript_content = transcript.encode("utf-8")
            transcript_hash = blob_store.put(transcript_content)
            with connect(database) as connection:
                ItemLog(connection, int(item_id)).audio_fallback("transcribed")
                mark_extracted(
                    connection,
                    int(item_id),
                    extracted_text_hash=transcript_hash,
                    event_source="audio_fallback",
                )
            transcribed += 1
    finally:
        routed.client.close()

    return AudioFallbackBatchResult(
        attempted=len(pending),
        transcribed=transcribed,
        failed=failed,
    )
