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
