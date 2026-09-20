import pytest

from modgud.podcasts import PodcastFeedError, parse_podcast_feed

_FEED_URL = "https://example.com/feed.xml"
_EPISODE_URL = "https://example.com/posts/one"
_AUDIO_ENCLOSURE = '<enclosure url="/one.mp3" type="audio/mpeg" />'


def _feed(item_body: str, *, link: str = _EPISODE_URL) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:podcast="https://podcastindex.org/namespace/1.0">
          <channel>
            <title>Example</title>
            <item>
              <guid>one</guid>
              <link>{link}</link>
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
    assert episode.audio_url is None
    assert episode.transcripts == ()


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("http://[bad", id="not-a-valid-url"),
        pytest.param("http://", id="no-hostname"),
        pytest.param("https://example.com:abc/x", id="invalid-port"),
    ],
)
def test_a_malformed_entry_link_does_not_become_the_page_url(link: str) -> None:
    episode = parse_podcast_feed(
        _feed(_AUDIO_ENCLOSURE, link=link),
        feed_url=_FEED_URL,
    )

    assert episode.page_url is None


def test_an_entry_links_tracking_parameters_are_stripped_from_the_page_url() -> None:
    episode = parse_podcast_feed(
        _feed(
            _AUDIO_ENCLOSURE,
            link="https://example.com/posts/one?utm_source=newsletter&amp;fbclid=abc123",
        ),
        feed_url=_FEED_URL,
    )

    assert episode.page_url == "https://example.com/posts/one"


def _two_entry_feed(*, bad_link: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Example</title>
            <item>
              <guid>bad</guid>
              <link>{bad_link}</link>
              <title>Bad</title>
              {_AUDIO_ENCLOSURE}
            </item>
            <item>
              <guid>good</guid>
              <link>{_EPISODE_URL}</link>
              <title>Good</title>
              {_AUDIO_ENCLOSURE}
            </item>
          </channel>
        </rss>
    """.encode()


def test_a_malformed_link_on_an_unselected_entry_does_not_crash_discovery() -> None:
    episode = parse_podcast_feed(
        _two_entry_feed(bad_link="http://[bad"),
        feed_url=_FEED_URL,
        episode_url=_EPISODE_URL,
    )

    assert episode.guid == "good"
