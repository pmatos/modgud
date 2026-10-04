from pathlib import Path

import pytest

from modgud.capture import CaptureResult, capture_url


def test_capture_returns_item_outcome_without_cli_presentation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = capture_url(tmp_path, "not a URL", None)

    assert result == CaptureResult(
        item_id=1,
        canonical_url="not a URL",
        created=True,
    )
    assert capsys.readouterr().out == ""
