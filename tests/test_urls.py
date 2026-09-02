"""Behavioral tests for URL canonicalization."""

import pytest

from modgud.urls import canonicalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://EXAMPLE.com/Case-Sensitive-Path",
            "https://example.com/Case-Sensitive-Path",
        ),
        ("https://example.com/articles/one/", "https://example.com/articles/one"),
        ("http://example.com:80/article", "http://example.com/article"),
        ("https://example.com:443/article", "https://example.com/article"),
        ("https://example.com:8443/article", "https://example.com:8443/article"),
        (
            (
                "https://example.com/article?UTM_Source=newsletter&page=2&fbclid=abc"
                "&gclid=def&dclid=ghi&msclkid=jkl&mc_cid=mno&mc_eid=pqr"
                "&_ga=stu&_gl=vwx&igshid=yz"
            ),
            "https://example.com/article?page=2",
        ),
        (
            "https://youtu.be/AbC_123",
            "https://www.youtube.com/watch?v=AbC_123",
        ),
        (
            "https://youtube.com/watch?v=AbC_123",
            "https://www.youtube.com/watch?v=AbC_123",
        ),
        (
            "https://www.youtube.com/watch?v=AbC_123&t=1m30s&start=90&list=PL42",
            "https://www.youtube.com/watch?v=AbC_123&list=PL42",
        ),
    ],
)
def test_url_is_canonicalized_for_deduplication(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected
