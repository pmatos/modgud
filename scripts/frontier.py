#!/usr/bin/env python3
"""Sync the `agent-ready` eligibility label to the dependency frontier.

An issue is eligible when every issue named in its `Blocked by` section is
closed. Symphonika's issue_filters select on `agent-ready`, so this is what
decides which tickets the daemon may pick up.

Run after merges to advance the frontier. Idempotent; --dry-run to preview.
"""

import argparse
import json
import re
import subprocess
import sys

REPO = "pmatos/modgud"
LABEL = "agent-ready"
BLOCKED_BY = re.compile(
    r"##\s*Blocked by\s*(.*?)(?:\n##|\Z)", re.DOTALL | re.IGNORECASE
)
REF = re.compile(r"#(\d+)")


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    ).stdout


def blockers(body: str) -> set[int]:
    m = BLOCKED_BY.search(body or "")
    return {int(n) for n in REF.findall(m.group(1))} if m else set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    issues = json.loads(
        gh(
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,title,body,state,labels",
        )
    )
    state = {i["number"]: i["state"].upper() for i in issues}

    changes = []
    for i in issues:
        if state[i["number"]] != "OPEN":
            continue
        deps = blockers(i["body"])
        unknown = deps - state.keys()
        if unknown:
            print(
                f"#{i['number']}: unknown blockers {sorted(unknown)}", file=sys.stderr
            )
        open_deps = sorted(d for d in deps if state.get(d, "OPEN") == "OPEN")
        eligible = not open_deps
        labelled = LABEL in {l["name"] for l in i["labels"]}
        if eligible != labelled:
            changes.append((i["number"], i["title"], eligible, open_deps))

    if not changes:
        print("frontier already in sync")
        return 0

    for num, title, eligible, open_deps in changes:
        verb = "add" if eligible else "remove"
        why = (
            "unblocked"
            if eligible
            else "blocked by " + ", ".join(f"#{d}" for d in open_deps)
        )
        print(f"{verb:>6} {LABEL}  #{num} {title}  ({why})")
        if not args.dry_run:
            gh(
                "issue",
                "edit",
                str(num),
                "--repo",
                REPO,
                f"--{'add' if eligible else 'remove'}-label",
                LABEL,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
