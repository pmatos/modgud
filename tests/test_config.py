"""Behavioral tests for operator configuration and runtime secrets."""

from datetime import time, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from modgud.config import ConfigError, get_settings


def _write_config(path: Path, *, transcription_extra: str = "") -> None:
    path.write_text(
        f"""
[models.transcription]
base_url = "http://127.0.0.1:8080/v1"
model = "whisper-1"
{transcription_extra}
[models.tier_1_summary]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b"

[models.tier_2_summary]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b"

[models.span_map]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b"

[models.cleanup]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b"

[whisper_cpp]
root = "/opt/whisper.cpp"
model_size = "large-v3-turbo"
threads = 12

[inbound]
poll_interval_seconds = 120

[digest]
send_time = "07:00"
from_address = "modgud@example.com"
to_address = "reader@example.com"

[web]
bind = "127.0.0.1:8000"

[labels]
token_lifetime_days = 90
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_operator_settings_are_loaded_from_one_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)

    settings = get_settings(config_path)

    assert (
        settings.models["transcription"].base_url,
        settings.models["transcription"].model,
        settings.models["tier_1_summary"].model,
        settings.whisper_cpp.root,
        settings.whisper_cpp.model_size,
        settings.whisper_cpp.threads,
        settings.inbound_poll_interval,
        settings.digest_send_time,
        settings.digest_from_address,
        settings.digest_to_address,
        settings.web_bind.host,
        settings.web_bind.port,
        settings.label_token_lifetime,
    ) == (
        "http://127.0.0.1:8080/v1",
        "whisper-1",
        "gemma4:26b-a4b",
        Path("/opt/whisper.cpp"),
        "large-v3-turbo",
        12,
        timedelta(minutes=2),
        time(7),
        "modgud@example.com",
        "reader@example.com",
        "127.0.0.1",
        8000,
        timedelta(days=90),
    )


def test_label_token_lifetime_defaults_to_ninety_days(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "token_lifetime_days = 90\n", ""
        ),
        encoding="utf-8",
    )

    settings = get_settings(config_path)

    assert settings.label_token_lifetime == timedelta(days=90)


def test_ipv6_web_bind_produces_a_valid_label_link_base_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'bind = "127.0.0.1:8000"', 'bind = "[fd00::20]:8000"'
        ),
        encoding="utf-8",
    )

    settings = get_settings(config_path)

    assert settings.web_bind.base_url == "http://[fd00::20]:8000"


def test_missing_config_names_the_expected_file(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    with pytest.raises(
        ConfigError, match=r"Configuration file not found: .*missing.toml"
    ):
        get_settings(config_path)


def test_malformed_toml_names_the_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "broken.toml"
    config_path.write_text('[web\nbind = "127.0.0.1:8000"\n', encoding="utf-8")

    with pytest.raises(
        ConfigError, match=r"Invalid config .*broken.toml: malformed TOML"
    ):
        get_settings(config_path)


def test_missing_required_setting_names_the_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'bind = "127.0.0.1:8000"\n', ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"web\.bind is required"):
        get_settings(config_path)


def test_malformed_setting_names_the_field_and_constraint(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "poll_interval_seconds = 120",
            "poll_interval_seconds = -1",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError,
        match=r"inbound\.poll_interval_seconds must be a positive integer",
    ):
        get_settings(config_path)


def test_hosted_provider_secret_is_loaded_from_its_named_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        transcription_extra='api_key_env = "TRANSCRIPTION_API_KEY"',
    )
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "environment-only-secret")

    settings = get_settings(config_path)

    assert settings.models["transcription"].api_key_env == "TRANSCRIPTION_API_KEY"
    assert (
        settings.secrets.model_api_keys["transcription"].reveal()
        == "environment-only-secret"
    )


def test_missing_hosted_provider_secret_names_its_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        transcription_extra='api_key_env = "MISSING_PROVIDER_API_KEY"',
    )
    monkeypatch.delenv("MISSING_PROVIDER_API_KEY", raising=False)

    with pytest.raises(
        ConfigError,
        match=r"models\.transcription\.api_key_env requires environment variable "
        r"MISSING_PROVIDER_API_KEY",
    ):
        get_settings(config_path)


def test_literal_secrets_are_rejected_without_echoing_the_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        transcription_extra='api_key = "must-never-appear"',
    )

    with pytest.raises(ConfigError) as failure:
        get_settings(config_path)

    assert "models.transcription.api_key is not a recognized setting" in str(
        failure.value
    )
    assert "must-never-appear" not in str(failure.value)


@pytest.mark.parametrize(
    ("existing", "replacement", "problem"),
    [
        (
            'base_url = "http://127.0.0.1:8080/v1"',
            'base_url = "file:///tmp/model"',
            r"models\.transcription\.base_url must be an HTTP\(S\) URL",
        ),
        (
            'model = "whisper-1"',
            'model = ""',
            r"models\.transcription\.model must be a non-empty string",
        ),
        (
            'model = "whisper-1"',
            'model = "whisper-1"\napi_key_env = "not-a-variable"',
            r"models\.transcription\.api_key_env must be an environment variable name",
        ),
        (
            'model_size = "large-v3-turbo"',
            'model_size = "../outside"',
            r"whisper_cpp\.model_size must be a whisper\.cpp model name",
        ),
        (
            "poll_interval_seconds = 120",
            "poll_interval_seconds = true",
            r"inbound\.poll_interval_seconds must be a positive integer",
        ),
        (
            'send_time = "07:00"',
            'send_time = "24:00"',
            r"digest\.send_time must use 24-hour HH:MM format",
        ),
        (
            'bind = "127.0.0.1:8000"',
            'bind = "127.0.0.1"',
            r"web\.bind must be a host and port",
        ),
        (
            "token_lifetime_days = 90",
            "token_lifetime_days = 0",
            r"labels\.token_lifetime_days must be a positive integer",
        ),
    ],
)
def test_each_malformed_setting_names_its_constraint(
    tmp_path: Path,
    existing: str,
    replacement: str,
    problem: str,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            existing,
            replacement,
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=problem):
        get_settings(config_path)


def test_every_model_task_must_have_a_route(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    cleanup_route = """
[models.cleanup]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b"
"""
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(cleanup_route, ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"models\.cleanup is required"):
        get_settings(config_path)


def test_settings_are_loaded_only_once_per_process(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    first = get_settings(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "poll_interval_seconds = 120",
            "poll_interval_seconds = 30",
        ),
        encoding="utf-8",
    )

    second = get_settings(config_path)

    assert second is first
    assert second.inbound_poll_interval == timedelta(minutes=2)


def test_postmark_secrets_are_captured_from_the_environment_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token-secret")
    monkeypatch.setenv("POSTMARK_ACCOUNT_TOKEN", "account-token-secret")

    settings = get_settings(config_path)

    assert settings.secrets.postmark_server_token is not None
    assert settings.secrets.postmark_account_token is not None
    assert settings.secrets.postmark_server_token.reveal() == "server-token-secret"
    assert settings.secrets.postmark_account_token.reveal() == "account-token-secret"
    assert "server-token-secret" not in repr(settings)
    assert "account-token-secret" not in repr(settings)


def test_label_signing_secret_is_captured_from_the_environment_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    secret = "a-dedicated-label-signing-secret-with-32-bytes"
    monkeypatch.setenv("LABEL_TOKEN_SECRET", secret)

    settings = get_settings(config_path)

    assert settings.secrets.label_token_secret is not None
    assert settings.secrets.label_token_secret.reveal() == secret
    assert secret not in repr(settings)


def test_label_signing_secret_must_have_cryptographic_strength(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    monkeypatch.setenv("LABEL_TOKEN_SECRET", "too-short")

    with pytest.raises(
        ConfigError,
        match=r"LABEL_TOKEN_SECRET must contain at least 32 bytes",
    ):
        get_settings(config_path)


def test_committed_example_is_a_valid_complete_config() -> None:
    config_path = Path(__file__).parents[1] / "config.example.toml"

    settings = get_settings(config_path)

    assert set(settings.models) == {
        "cleanup",
        "span_map",
        "tier_1_summary",
        "tier_2_summary",
        "transcription",
    }


def test_committed_transcription_route_is_local_whisper_cpp() -> None:
    config_path = Path(__file__).parents[1] / "config.example.toml"

    settings = get_settings(config_path)
    route = settings.models["transcription"]

    assert urlsplit(route.base_url).hostname == "127.0.0.1"
    assert route.model == "whisper-1"
    assert route.api_key_env is None
    assert settings.whisper_cpp.model_size == "large-v3-turbo"
