"""Behavioral tests for task-routed model clients."""

from pathlib import Path

import pytest

from modgud.config import get_settings
from modgud.models import ModelTask, create_model_client


@pytest.fixture
def example_config_path() -> Path:
    return Path(__file__).parents[1] / "config.example.toml"


@pytest.mark.parametrize(
    ("task", "expected_base_url", "expected_model"),
    [
        ("transcription", "http://127.0.0.1:8080/v1/", "whisper-1"),
        ("tier_1_summary", "http://127.0.0.1:11434/v1/", "gemma4:26b-a4b"),
        ("tier_2_summary", "http://127.0.0.1:11434/v1/", "gemma4:26b-a4b"),
        ("span_map", "http://127.0.0.1:11434/v1/", "gemma4:26b-a4b"),
        ("cleanup", "http://127.0.0.1:11434/v1/", "gemma4:26b-a4b"),
    ],
)
def test_every_task_client_uses_its_shipped_local_route(
    example_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task: ModelTask,
    expected_base_url: str,
    expected_model: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-ambient-key")
    settings = get_settings(example_config_path)

    routed = create_model_client(task, settings=settings)

    try:
        assert str(routed.client.base_url) == expected_base_url
        assert routed.model == expected_model
        assert routed.client.api_key != "unrelated-ambient-key"
    finally:
        routed.client.close()


def test_config_edit_routes_a_task_to_a_hosted_endpoint(
    example_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example_config_path.read_text(encoding="utf-8").replace(
            """[models.cleanup]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b""",
            """[models.cleanup]
base_url = "https://models.example/v1"
model = "hosted-cleanup"
api_key_env = "CLEANUP_API_KEY""",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLEANUP_API_KEY", "hosted-secret")
    settings = get_settings(config_path)

    routed = create_model_client("cleanup", settings=settings)

    try:
        assert str(routed.client.base_url) == "https://models.example/v1/"
        assert routed.model == "hosted-cleanup"
        assert routed.client.api_key == "hosted-secret"
    finally:
        routed.client.close()
