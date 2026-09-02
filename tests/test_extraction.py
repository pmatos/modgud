"""Behavioral tests for readable web-page extraction."""

import pytest

from modgud.extraction import ExtractionError, extract_web_page


@pytest.mark.parametrize(
    ("html", "url", "title", "author", "site", "article_text", "boilerplate"),
    [
        pytest.param(
            """
            <!doctype html>
            <html lang="en">
              <head>
                <title>A Calm Database Migration - Practical Python</title>
                <meta property="og:title" content="A Calm Database Migration">
                <meta property="og:site_name" content="Practical Python">
                <meta name="author" content="Ada Rivera">
              </head>
              <body>
                <header><a href="/">Practical Python home</a></header>
                <nav>Courses About Newsletter</nav>
                <main>
                  <article class="post type-post">
                    <h1>A Calm Database Migration</h1>
                    <div class="entry-content">
                      <p>A safe migration starts by making the old and new
                      representations valid at the same time. Writers change
                      only after readers understand both forms.</p>
                      <p>Once every deployed reader accepts the new form, a
                      measured backfill can move historical records without a
                      risky maintenance window.</p>
                    </div>
                    <div class="sharedaddy">Share on every social network</div>
                  </article>
                </main>
                <footer>Privacy Terms Copyright 2026</footer>
              </body>
            </html>
            """,
            "https://practical-python.example/calm-migration",
            "A Calm Database Migration",
            "Ada Rivera",
            "Practical Python",
            "A safe migration starts by making the old and new representations",
            "Share on every social network",
            id="wordpress-style",
        ),
        pytest.param(
            """
            <!doctype html>
            <html lang="en">
              <head>
                <meta property="og:title" content="Queues Are Coordination">
                <meta property="og:site_name" content="Systems Weekly">
                <meta name="author" content="Mina Cho">
                <script type="application/ld+json">
                  {"@context":"https://schema.org","@type":"BlogPosting",
                   "headline":"Queues Are Coordination",
                   "author":{"@type":"Person","name":"Mina Cho"}}
                </script>
              </head>
              <body class="post-template">
                <header class="gh-head">Home Archive Subscribe Sign in</header>
                <main>
                  <article class="gh-article">
                    <header><h1>Queues Are Coordination</h1></header>
                    <section class="gh-content">
                      <p>A queue is more than storage between two processes. It
                      defines who may apply backpressure, which failures are
                      retried, and where ownership changes hands.</p>
                      <p>Those choices should be explicit because an invisible
                      retry policy can turn a small outage into duplicated work
                      across every downstream consumer.</p>
                    </section>
                  </article>
                </main>
                <aside>More from Systems Weekly: buy the complete archive</aside>
                <footer class="gh-foot">Powered by Ghost</footer>
              </body>
            </html>
            """,
            "https://systems-weekly.example/queues",
            "Queues Are Coordination",
            "Mina Cho",
            "Systems Weekly",
            "A queue is more than storage between two processes",
            "Powered by Ghost",
            id="ghost-style",
        ),
        pytest.param(
            """
            <!doctype html>
            <html lang="en">
              <head>
                <title>Notes on Durable Files</title>
                <meta property="og:site_name" content="Small Tools Journal">
                <meta name="author" content="Leo Martins">
              </head>
              <body>
                <div role="navigation">Index Projects Contact RSS</div>
                <article>
                  <h1>Notes on Durable Files</h1>
                  <p>Durability begins before the rename. The temporary file
                  must be flushed completely so a crash cannot publish a name
                  whose bytes exist only in volatile caches.</p>
                  <p>The final directory entry also needs deliberate handling.
                  Atomic visibility and durable visibility are related, but
                  they are not the same promise.</p>
                </article>
                <div class="newsletter">Join 40,000 other readers today</div>
                <footer>Colophon Blogroll Analytics settings</footer>
              </body>
            </html>
            """,
            "https://small-tools.example/durable-files",
            "Notes on Durable Files",
            "Leo Martins",
            "Small Tools Journal",
            "Durability begins before the rename",
            "Join 40,000 other readers today",
            id="independent-blog-style",
        ),
    ],
)
def test_extracts_readable_posts_without_page_boilerplate(
    html: str,
    url: str,
    title: str,
    author: str,
    site: str,
    article_text: str,
    boilerplate: str,
) -> None:
    page = extract_web_page(html.encode(), url=url)

    assert (page.title, page.author, page.site) == (title, author, site)
    assert article_text in " ".join(page.text.split())
    assert boilerplate not in page.text


def test_empty_page_is_an_extraction_failure() -> None:
    html = b"<html><head><title>Empty</title></head><body></body></html>"

    with pytest.raises(ExtractionError, match="readable text"):
        extract_web_page(html, url="https://example.com/empty")


def test_related_post_cards_are_not_part_of_readable_text() -> None:
    html = b"""
        <html>
          <head><title>Operating a Small Service</title></head>
          <body>
            <main id="start-of-content">
              <section class="post__content type-post status-publish">
                <h1>Operating a Small Service</h1>
                <p>A small service still needs an explicit recovery model. The
                useful question is which facts survive when the process exits
                between any two instructions.</p>
                <p>Writing those facts before publishing work gives the next
                process enough context to continue without guessing.</p>
              </section>
              <section class="container-wide more-stories">
                <div class="post-columns post-columns--3-3">
                  <article class="color-muted post-card">
                    <h2>The cost of saying yes has changed</h2>
                    <p>A framework for deciding which unrelated changes are
                    actually cheap in the latest development environment.</p>
                  </article>
                  <article class="color-muted post-card">
                    <h2>Build a deployment dashboard</h2>
                    <p>Learn how another team organized every rollout with a
                    polished dashboard and a collection of integrations.</p>
                  </article>
                  <article class="color-muted post-card">
                    <h2>Join our annual developer conference</h2>
                    <p>Meet other developers, attend workshops, and explore
                    what is next for the wider software community.</p>
                  </article>
                </div>
              </section>
            </main>
          </body>
        </html>
    """

    page = extract_web_page(html, url="https://example.com/small-service")

    assert "A small service still needs an explicit recovery model" in page.text
    assert "The cost of saying yes has changed" not in page.text
