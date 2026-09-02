"""Create OpenAI-compatible clients from validated task routes."""

from dataclasses import dataclass
from typing import Literal

from openai import OpenAI

from modgud.config import Settings, get_settings

type ModelTask = Literal[
    "transcription",
    "tier_1_summary",
    "span_map",
    "cleanup",
]

_UNAUTHENTICATED_API_KEY = "not-required"


@dataclass(frozen=True, slots=True)
class RoutedModelClient:
    """An OpenAI-compatible client paired with its configured model."""

    client: OpenAI
    model: str


def create_model_client(
    task: ModelTask,
    *,
    settings: Settings | None = None,
) -> RoutedModelClient:
    """Create the client and model configured for ``task``."""
    active_settings = get_settings() if settings is None else settings
    route = active_settings.models[task]
    secret = active_settings.secrets.model_api_keys.get(task)
    api_key = _UNAUTHENTICATED_API_KEY if secret is None else secret.reveal()
    return RoutedModelClient(
        client=OpenAI(base_url=route.base_url, api_key=api_key),
        model=route.model,
    )
