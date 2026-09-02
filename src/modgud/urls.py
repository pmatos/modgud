"""Canonical identities for captured URLs."""

from urllib.parse import unquote_plus, urlsplit, urlunsplit

_TRACKING_PARAMETERS = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
    }
)


def canonicalize_url(url: str) -> str:
    """Return the stable identity used to deduplicate a URL."""
    parts = urlsplit(url)
    if parts.hostname is None:
        return url

    userinfo = parts.netloc.rpartition("@")[0]
    host = parts.hostname.lower()
    is_youtube = host in {
        "youtu.be",
        "youtube.com",
        "www.youtu.be",
        "www.youtube.com",
    }
    path = parts.path.rstrip("/")
    query_fields = parts.query.split("&") if parts.query else []

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.removeprefix("/").partition("/")[0]
        host = "www.youtube.com"
        path = "/watch"
        query_fields.insert(0, f"v={video_id}")
    elif host == "youtube.com":
        host = "www.youtube.com"

    if ":" in host:
        host = f"[{host}]"
    netloc = f"{userinfo}@{host}" if userinfo else host
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    if parts.port is not None and parts.port != default_port:
        netloc = f"{netloc}:{parts.port}"

    retained_query_fields = []
    for field in query_fields:
        name = unquote_plus(field.partition("=")[0]).lower()
        is_tracking = name.startswith("utm_") or name in _TRACKING_PARAMETERS
        if not is_tracking and not (is_youtube and name in {"t", "start"}):
            retained_query_fields.append(field)

    query = "&".join(retained_query_fields)
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))
