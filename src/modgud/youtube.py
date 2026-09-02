"""YouTube metadata and caption extraction through yt-dlp."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, TypedDict, cast

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

type YoutubeDLFactory = Callable[[dict[str, Any]], Any]


class Chapter(TypedDict):
    """A timestamped chapter marker reported by YouTube."""

    start_time: float
    end_time: float
    title: str


@dataclass(frozen=True)
class Caption:
    """One timestamped caption track exactly as yt-dlp retrieved it."""

    language: str
    kind: Literal["manual", "automatic"]
    content: bytes


@dataclass(frozen=True)
class CaptionRefusal:
    """YouTube explicitly refused a caption request."""

    reason: str


@dataclass(frozen=True)
class YoutubeFailure:
    """An ordinary yt-dlp failure, distinct from a YouTube refusal."""

    stage: Literal["metadata", "captions"]
    reason: str


@dataclass(frozen=True)
class ExtractedYouTube:
    """Metadata and optional captions extracted without downloading media."""

    title: str | None
    channel: str | None
    duration_seconds: float | None
    chapters: tuple[Chapter, ...]
    caption: Caption | None = None
    caption_refusal: CaptionRefusal | None = None
    failure: YoutubeFailure | None = None


def _default_youtube_dl_factory(options: dict[str, Any]) -> Any:
    return YoutubeDL(cast(Any, options))


def extract_youtube(
    url: str,
    *,
    youtube_dl_factory: YoutubeDLFactory = _default_youtube_dl_factory,
) -> ExtractedYouTube:
    """Fetch YouTube metadata and one caption track, never the video media."""
    metadata_options: dict[str, Any] = {
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        "skip_download": True,
    }
    try:
        with youtube_dl_factory(metadata_options) as downloader:
            raw_info = downloader.extract_info(url, download=False)
    except DownloadError as error:
        if not _is_caption_refusal(error):
            return ExtractedYouTube(
                title=None,
                channel=None,
                duration_seconds=None,
                chapters=(),
                failure=YoutubeFailure(stage="metadata", reason=str(error)),
            )
        return ExtractedYouTube(
            title=None,
            channel=None,
            duration_seconds=None,
            chapters=(),
            caption_refusal=CaptionRefusal(reason=str(error)),
        )
    info = _as_mapping(raw_info)

    selected = _select_caption(info)
    caption = None
    caption_refusal = None
    failure = None
    if selected is not None:
        language, kind = selected
        with TemporaryDirectory(prefix="modgud-youtube-") as temporary_directory:
            caption_options: dict[str, Any] = {
                **metadata_options,
                "outtmpl": "%(id)s.%(ext)s",
                "paths": {"home": temporary_directory},
                "subtitlesformat": "vtt/best",
                "subtitleslangs": [language],
                "writeautomaticsub": kind == "automatic",
                "writesubtitles": kind == "manual",
            }
            try:
                with youtube_dl_factory(caption_options) as downloader:
                    downloader.extract_info(url, download=True)
            except DownloadError as error:
                if _is_caption_refusal(error):
                    caption_refusal = CaptionRefusal(reason=str(error))
                else:
                    failure = YoutubeFailure(stage="captions", reason=str(error))
            else:
                caption_path = _find_caption(Path(temporary_directory), language)
                caption = Caption(
                    language=language,
                    kind=kind,
                    content=caption_path.read_bytes(),
                )

    return ExtractedYouTube(
        title=_optional_string(info.get("title")),
        channel=_optional_string(info.get("channel") or info.get("uploader")),
        duration_seconds=_optional_float(info.get("duration")),
        chapters=_chapters(info.get("chapters")),
        caption=caption,
        caption_refusal=caption_refusal,
        failure=failure,
    )


@contextmanager
def download_youtube_audio(url: str) -> Iterator[Path]:
    """Yield a temporary best-audio download and remove it on exit."""
    with TemporaryDirectory(prefix="modgud-youtube-audio-") as temporary_directory:
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "no_warnings": True,
            "outtmpl": "%(id)s.%(ext)s",
            "paths": {"home": temporary_directory},
            "quiet": True,
        }
        with _default_youtube_dl_factory(options) as downloader:
            downloader.extract_info(url, download=True)
        audio_files = sorted(
            path
            for path in Path(temporary_directory).iterdir()
            if path.is_file() and path.suffix != ".part"
        )
        if len(audio_files) != 1:
            raise ValueError("yt-dlp did not write exactly one audio stream")
        yield audio_files[0]


def _is_caption_refusal(error: DownloadError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "http error 429",
            "not a bot",
            "po token",
            "proof of origin",
            "sign in to confirm",
        )
    )


def _as_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("yt-dlp returned no video metadata")
    return value


def _select_caption(
    info: Mapping[str, Any],
) -> tuple[str, Literal["manual", "automatic"]] | None:
    candidates: tuple[tuple[str, Literal["manual", "automatic"]], ...] = (
        ("subtitles", "manual"),
        ("automatic_captions", "automatic"),
    )
    for field, kind in candidates:
        tracks = info.get(field)
        if isinstance(tracks, Mapping):
            languages = [
                language
                for language, formats in tracks.items()
                if isinstance(language, str)
                and language != "live_chat"
                and isinstance(formats, list)
                and formats
            ]
            if languages:
                return _preferred_language(languages), kind
    return None


def _preferred_language(languages: list[str]) -> str:
    return min(
        languages,
        key=lambda language: (
            language != "en",
            not language.startswith("en-"),
            language,
        ),
    )


def _find_caption(directory: Path, language: str) -> Path:
    matches = sorted(directory.glob(f"*.{language}.*"))
    if not matches:
        raise ValueError(f"yt-dlp did not write the requested {language} captions")
    return matches[0]


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _chapters(value: object) -> tuple[Chapter, ...]:
    if not isinstance(value, list):
        return ()
    chapters: list[Chapter] = []
    for raw_chapter in value:
        if not isinstance(raw_chapter, Mapping):
            continue
        title = _optional_string(raw_chapter.get("title"))
        start_time = _optional_float(raw_chapter.get("start_time"))
        end_time = _optional_float(raw_chapter.get("end_time"))
        if title is None or start_time is None or end_time is None:
            continue
        chapters.append(
            {"start_time": start_time, "end_time": end_time, "title": title}
        )
    return tuple(chapters)
