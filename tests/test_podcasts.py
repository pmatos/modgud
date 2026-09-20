import pytest

from modgud.podcasts import PodcastFeedError, parse_podcast_feed

_FEED_URL = "https://example.com/feed.xml"
_EPISODE_URL = "https://example.com/posts/one"


def _feed(item_body: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:podcast="https://podcastindex.org/namespace/1.0">
          <channel>
            <title>Example</title>
            <item>
              <guid>one</guid>
              <link>{_EPISODE_URL}</link>
              <title>One</title>
              {item_body}
            </item>
          </channel>
        </rss>
    """.encode()


def test_discovered_entry_without_audio_or_transcript_is_not_an_episode() -> None:
    with pytest.raises(PodcastFeedError):
        parse_podcast_feed(
            _feed(""),
            feed_url=_FEED_URL,
            episode_url=_EPISODE_URL,
        )


def test_discovered_entry_with_only_a_transcript_is_an_episode() -> None:
    episode = parse_podcast_feed(
        _feed('<podcast:transcript url="/one.vtt" type="text/vtt" />'),
        feed_url=_FEED_URL,
        episode_url=_EPISODE_URL,
    )

    assert episode.audio_url is None
    assert [transcript.url for transcript in episode.transcripts] == [
        "https://example.com/one.vtt"
    ]


def test_feed_submitted_directly_is_an_episode_without_audio() -> None:
    episode = parse_podcast_feed(_feed(""), feed_url=_FEED_URL)

    assert episode.guid == "one"
