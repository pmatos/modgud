"""Behavioral tests for YouTube metadata and caption extraction."""

from pathlib import Path
from typing import Any, ClassVar, Self

from yt_dlp.utils import DownloadError

from modgud.youtube import extract_youtube

_CAPTIONS = b"""WEBVTT

00:00:01.000 --> 00:00:03.500
The first useful claim.

00:01:02.250 --> 00:01:05.000
The supporting evidence.
"""


class _SuccessfulYoutubeDL:
    options_seen: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        type(self).options_seen.append(options)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        assert url == "https://www.youtube.com/watch?v=video123"
        if download:
            assert self.options["skip_download"] is True
            caption_dir = Path(self.options["paths"]["home"])
            (caption_dir / "video123.en.vtt").write_bytes(_CAPTIONS)

        return {
            "id": "video123",
            "title": "How Durable Queues Work",
            "channel": "Systems Workshop",
            "duration": 125.75,
            "chapters": [
                {"start_time": 0.0, "end_time": 62.0, "title": "The problem"},
                {
                    "start_time": 62.0,
                    "end_time": 125.75,
                    "title": "A durable design",
                },
            ],
            "subtitles": {"en": [{"ext": "vtt"}]},
            "automatic_captions": {},
        }


class _CaptionRefusingYoutubeDL(_SuccessfulYoutubeDL):
    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        if download:
            raise DownloadError("Sign in to confirm you're not a bot")
        return super().extract_info(url, download=download)


class _ProbeRefusingYoutubeDL(_SuccessfulYoutubeDL):
    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        raise DownloadError("This content requires a proof of origin PO token")


class _CaptionFailingYoutubeDL(_SuccessfulYoutubeDL):
    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        if download:
            raise DownloadError("Unable to download subtitles: HTTP Error 500")
        return super().extract_info(url, download=download)


class _AutomaticCaptionYoutubeDL(_SuccessfulYoutubeDL):
    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        info = super().extract_info(url, download=download)
        info["subtitles"] = {}
        info["automatic_captions"] = {"en": [{"ext": "vtt"}]}
        return info


def test_available_captions_keep_timing_and_video_media_is_not_downloaded() -> None:
    _SuccessfulYoutubeDL.options_seen = []

    extracted = extract_youtube(
        "https://www.youtube.com/watch?v=video123",
        youtube_dl_factory=_SuccessfulYoutubeDL,
    )

    assert (
        extracted.title,
        extracted.channel,
        extracted.duration_seconds,
        extracted.chapters,
    ) == (
        "How Durable Queues Work",
        "Systems Workshop",
        125.75,
        (
            {
                "start_time": 0.0,
                "end_time": 62.0,
                "title": "The problem",
            },
            {
                "start_time": 62.0,
                "end_time": 125.75,
                "title": "A durable design",
            },
        ),
    )
    assert extracted.caption is not None
    assert (
        extracted.caption.language,
        extracted.caption.kind,
        extracted.caption.content,
    ) == ("en", "manual", _CAPTIONS)
    assert all(
        options["skip_download"] is True
        for options in _SuccessfulYoutubeDL.options_seen
    )


def test_youtube_caption_denial_is_a_recognizable_refusal() -> None:
    extracted = extract_youtube(
        "https://www.youtube.com/watch?v=video123",
        youtube_dl_factory=_CaptionRefusingYoutubeDL,
    )

    assert extracted.title == "How Durable Queues Work"
    assert extracted.caption is None
    assert extracted.caption_refusal is not None
    assert "not a bot" in extracted.caption_refusal.reason


def test_youtube_probe_denial_is_also_a_recognizable_caption_refusal() -> None:
    extracted = extract_youtube(
        "https://www.youtube.com/watch?v=video123",
        youtube_dl_factory=_ProbeRefusingYoutubeDL,
    )

    assert extracted.caption is None
    assert extracted.caption_refusal is not None
    assert "proof of origin" in extracted.caption_refusal.reason


def test_non_refusal_caption_errors_keep_metadata_in_a_generic_failure() -> None:
    extracted = extract_youtube(
        "https://www.youtube.com/watch?v=video123",
        youtube_dl_factory=_CaptionFailingYoutubeDL,
    )

    assert extracted.title == "How Durable Queues Work"
    assert extracted.caption_refusal is None
    assert extracted.failure is not None
    assert (extracted.failure.stage, extracted.failure.reason) == (
        "captions",
        "Unable to download subtitles: HTTP Error 500",
    )


def test_automatic_captions_are_used_when_manual_captions_are_absent() -> None:
    _AutomaticCaptionYoutubeDL.options_seen = []

    extracted = extract_youtube(
        "https://www.youtube.com/watch?v=video123",
        youtube_dl_factory=_AutomaticCaptionYoutubeDL,
    )

    assert extracted.caption is not None
    assert extracted.caption.kind == "automatic"
    caption_options = _AutomaticCaptionYoutubeDL.options_seen[-1]
    assert caption_options["writeautomaticsub"] is True
    assert caption_options["writesubtitles"] is False
