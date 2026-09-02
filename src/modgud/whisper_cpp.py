"""Launch the configured whisper.cpp OpenAI-compatible server."""

import os
from typing import NoReturn
from urllib.parse import urlsplit

from modgud.config import Settings


class WhisperCppError(RuntimeError):
    """The configured local whisper.cpp server cannot be started."""


def launch_server(settings: Settings) -> NoReturn:
    """Replace this process with the configured whisper.cpp server."""
    whisper_cpp = settings.whisper_cpp
    route = urlsplit(settings.models["transcription"].base_url)
    host = route.hostname
    if host is None:
        raise AssertionError("The transcription route was validated at startup")
    port = route.port or (443 if route.scheme == "https" else 80)
    inference_path = f"{route.path.rstrip('/')}/audio/transcriptions"
    executable = whisper_cpp.root / "build/bin/whisper-server"
    model = whisper_cpp.root / "models" / f"ggml-{whisper_cpp.model_size}.bin"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WhisperCppError(f"whisper.cpp server not executable: {executable}")
    if not model.is_file():
        raise WhisperCppError(f"whisper.cpp model not found: {model}")
    arguments = [
        str(executable),
        "--host",
        host,
        "--port",
        str(port),
        "--model",
        str(model),
        "--threads",
        str(whisper_cpp.threads),
        "--language",
        "auto",
        "--inference-path",
        inference_path,
        "--convert",
    ]
    os.execv(executable, arguments)
