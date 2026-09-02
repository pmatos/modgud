import subprocess


def test_help_describes_the_command() -> None:
    result = subprocess.run(
        ["modgud", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: modgud" in result.stdout
