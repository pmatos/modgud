"""Contract tests for the committed digest scheduler units."""

from pathlib import Path


def test_systemd_timer_invokes_the_config_gated_digest_command() -> None:
    repository = Path(__file__).parents[1]
    service = (repository / "systemd/modgud-digest.service").read_text(encoding="utf-8")
    timer = (repository / "systemd/modgud-digest.timer").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "EnvironmentFile=-%h/.config/modgud/environment" in service
    assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin" in service
    assert "ExecStart=/usr/bin/env modgud digest" in service
    assert "--now" not in service
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=modgud-digest.service" in timer
    assert "WantedBy=timers.target" in timer


def test_systemd_service_runs_the_configured_transcription_endpoint() -> None:
    repository = Path(__file__).parents[1]
    service = (repository / "systemd/modgud-whisper.service").read_text(
        encoding="utf-8"
    )

    assert "Type=simple" in service
    assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin" in service
    assert "ExecStart=/usr/bin/env modgud whisper-server" in service
    assert "WorkingDirectory=/tmp" in service
    assert "Restart=on-failure" in service
    assert "WantedBy=default.target" in service


def test_media_batch_runs_at_0300_with_background_priorities() -> None:
    repository = Path(__file__).parents[1]
    service = (repository / "systemd/modgud-batch.service").read_text(encoding="utf-8")
    timer = (repository / "systemd/modgud-batch.timer").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "EnvironmentFile=-%h/.config/modgud/environment" in service
    assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin" in service
    assert "ExecStart=/usr/bin/env modgud batch" in service
    assert "Nice=19" in service
    assert "IOSchedulingClass=idle" in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=modgud-batch.service" in timer
    assert "WantedBy=timers.target" in timer
