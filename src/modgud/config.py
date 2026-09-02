"""Load modgud's operator configuration once for each process."""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urlsplit

_MODEL_TASKS = frozenset({"transcription", "tier_1_summary", "span_map", "cleanup"})
_ENVIRONMENT_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CLOCK_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")


class ConfigError(RuntimeError):
    """Configuration cannot be loaded safely."""


def _require(
    table: dict[str, Any],
    key: str,
    *,
    field: str,
    path: Path,
) -> Any:
    try:
        return table[key]
    except KeyError as error:
        raise ConfigError(f"Invalid config {path}: {field} is required") from error


def _table(value: Any, *, field: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid config {path}: {field} must be a table")
    return cast("dict[str, Any]", value)


def _required_table(
    document: dict[str, Any],
    key: str,
    *,
    field: str,
    path: Path,
) -> dict[str, Any]:
    return _table(
        _require(document, key, field=field, path=path),
        field=field,
        path=path,
    )


def _non_empty_string(value: Any, *, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Invalid config {path}: {field} must be a non-empty string")
    return value


def _positive_integer(value: Any, *, field: str, path: Path) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError(f"Invalid config {path}: {field} must be a positive integer")
    return value


def _reject_unknown(
    table: dict[str, Any],
    allowed: set[str],
    *,
    field: str,
    path: Path,
) -> None:
    unknown = sorted(table.keys() - allowed)
    if unknown:
        unknown_field = f"{field}.{unknown[0]}" if field else unknown[0]
        raise ConfigError(
            f"Invalid config {path}: {unknown_field} is not a recognized setting"
        )


def _http_url(value: Any, *, field: str, path: Path) -> str:
    url = _non_empty_string(value, field=field, path=path)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ConfigError(
            f"Invalid config {path}: {field} must be an HTTP(S) URL"
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ConfigError(f"Invalid config {path}: {field} must be an HTTP(S) URL")
    return url


def _environment_variable(value: Any, *, field: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ENVIRONMENT_VARIABLE.fullmatch(value) is None:
        raise ConfigError(
            f"Invalid config {path}: {field} must be an environment variable name"
        )
    return value


def _clock_time(value: Any, *, field: str, path: Path) -> time:
    if not isinstance(value, str):
        match = None
    else:
        match = _CLOCK_TIME.fullmatch(value)
    if match is None:
        raise ConfigError(
            f"Invalid config {path}: {field} must use 24-hour HH:MM format"
        )
    return time(hour=int(match.group(1)), minute=int(match.group(2)))


def _web_bind(value: Any, *, field: str, path: Path) -> "WebBind":
    if not isinstance(value, str):
        parsed = None
    else:
        try:
            parsed = urlsplit(f"//{value}")
            port = parsed.port
        except ValueError:
            parsed = None
    if (
        parsed is None
        or parsed.hostname is None
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"Invalid config {path}: {field} must be a host and port")
    return WebBind(host=parsed.hostname, port=port)


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """An OpenAI-compatible endpoint selected for one model task."""

    base_url: str
    model: str
    api_key_env: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """A secret whose representation never reveals its value."""

    _value: str

    def reveal(self) -> str:
        """Return the secret for use at an external service boundary."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSecrets:
    """Secrets captured from the process environment at startup."""

    model_api_keys: Mapping[str, SecretValue]
    postmark_server_token: SecretValue | None
    postmark_account_token: SecretValue | None

    def __repr__(self) -> str:
        return "RuntimeSecrets(<redacted>)"


@dataclass(frozen=True, slots=True)
class WebBind:
    """The interface and TCP port used by the web application."""

    host: str
    port: int


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated operator settings shared by every entrypoint."""

    models: Mapping[str, ModelRoute]
    inbound_poll_interval: timedelta
    digest_send_time: time
    web_bind: WebBind
    label_token_lifetime: timedelta
    secrets: RuntimeSecrets


def default_config_path() -> Path:
    """Return the conventional per-user configuration path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home is None:
        return Path.home() / ".config" / "modgud" / "config.toml"
    return Path(config_home) / "modgud" / "config.toml"


def get_settings(config_path: str | Path | None = None) -> Settings:
    """Return settings loaded once from ``config_path``."""
    path = default_config_path() if config_path is None else Path(config_path)
    return _load_settings(path.expanduser().resolve())


@cache
def _load_settings(path: Path) -> Settings:
    try:
        with path.open("rb") as config_file:
            document: dict[str, Any] = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(f"Configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid config {path}: malformed TOML ({error})") from error
    except OSError as error:
        raise ConfigError(f"Cannot read configuration file {path}: {error}") from error

    _reject_unknown(
        document,
        {"digest", "inbound", "labels", "models", "web"},
        field="",
        path=path,
    )
    model_document = _required_table(
        document,
        "models",
        field="models",
        path=path,
    )
    missing_tasks = sorted(_MODEL_TASKS - model_document.keys())
    if missing_tasks:
        raise ConfigError(
            f"Invalid config {path}: models.{missing_tasks[0]} is required"
        )
    unexpected_tasks = sorted(model_document.keys() - _MODEL_TASKS)
    if unexpected_tasks:
        raise ConfigError(
            f"Invalid config {path}: models.{unexpected_tasks[0]} is not a "
            "recognized setting"
        )

    models: dict[str, ModelRoute] = {}
    for task in sorted(_MODEL_TASKS):
        route_config = _table(
            model_document[task],
            field=f"models.{task}",
            path=path,
        )
        _reject_unknown(
            route_config,
            {"api_key_env", "base_url", "model"},
            field=f"models.{task}",
            path=path,
        )
        models[task] = ModelRoute(
            base_url=_http_url(
                _require(
                    route_config,
                    "base_url",
                    field=f"models.{task}.base_url",
                    path=path,
                ),
                field=f"models.{task}.base_url",
                path=path,
            ),
            model=_non_empty_string(
                _require(
                    route_config,
                    "model",
                    field=f"models.{task}.model",
                    path=path,
                ),
                field=f"models.{task}.model",
                path=path,
            ),
            api_key_env=_environment_variable(
                route_config.get("api_key_env"),
                field=f"models.{task}.api_key_env",
                path=path,
            ),
        )

    inbound = _required_table(
        document,
        "inbound",
        field="inbound",
        path=path,
    )
    _reject_unknown(
        inbound,
        {"poll_interval_seconds"},
        field="inbound",
        path=path,
    )
    poll_interval_seconds = _positive_integer(
        _require(
            inbound,
            "poll_interval_seconds",
            field="inbound.poll_interval_seconds",
            path=path,
        ),
        field="inbound.poll_interval_seconds",
        path=path,
    )

    digest = _required_table(
        document,
        "digest",
        field="digest",
        path=path,
    )
    _reject_unknown(digest, {"send_time"}, field="digest", path=path)
    digest_send_time = _clock_time(
        _require(
            digest,
            "send_time",
            field="digest.send_time",
            path=path,
        ),
        field="digest.send_time",
        path=path,
    )

    web = _required_table(document, "web", field="web", path=path)
    _reject_unknown(web, {"bind"}, field="web", path=path)
    web_bind = _web_bind(
        _require(web, "bind", field="web.bind", path=path),
        field="web.bind",
        path=path,
    )

    labels = _required_table(
        document,
        "labels",
        field="labels",
        path=path,
    )
    _reject_unknown(
        labels,
        {"token_lifetime_days"},
        field="labels",
        path=path,
    )
    token_lifetime_days = _positive_integer(
        _require(
            labels,
            "token_lifetime_days",
            field="labels.token_lifetime_days",
            path=path,
        ),
        field="labels.token_lifetime_days",
        path=path,
    )

    model_api_keys: dict[str, SecretValue] = {}
    for task, model_route in models.items():
        if model_route.api_key_env is None:
            continue
        api_key = os.environ.get(model_route.api_key_env)
        if not api_key:
            raise ConfigError(
                f"Invalid config {path}: models.{task}.api_key_env requires "
                f"environment variable {model_route.api_key_env}"
            )
        model_api_keys[task] = SecretValue(api_key)
    return Settings(
        models=MappingProxyType(models),
        inbound_poll_interval=timedelta(seconds=poll_interval_seconds),
        digest_send_time=digest_send_time,
        web_bind=web_bind,
        label_token_lifetime=timedelta(days=token_lifetime_days),
        secrets=RuntimeSecrets(
            model_api_keys=MappingProxyType(model_api_keys),
            postmark_server_token=_secret_from_environment("POSTMARK_SERVER_TOKEN"),
            postmark_account_token=_secret_from_environment("POSTMARK_ACCOUNT_TOKEN"),
        ),
    )


def _secret_from_environment(variable: str) -> SecretValue | None:
    value = os.environ.get(variable)
    if value is None:
        return None
    return SecretValue(value)
