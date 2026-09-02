"""Command-line interface for modgud."""

import argparse


def main() -> None:
    """Run the modgud command-line interface."""
    parser = argparse.ArgumentParser(
        prog="modgud",
        description="Triage personal content.",
    )
    parser.parse_args()
