#!/usr/bin/env python3
"""Project each issue's `Blocked by` section into GitHub native dependencies.

The issue body is the authored source of truth; this makes GitHub agree with
it, so tooling that reads native blocked-by relationships (Symphonika's
dispatch eligibility included) sees the same graph a reader does.

Idempotent: existing edges are left alone. --dry-run to preview.
"""
import argparse, json, re, subprocess, sys

REPO = "pmatos/modgud"
BLOCKED_BY = re.compile(r"##\s*Blocked by\s*(.*?)(?:\n##|\Z)", re.S | re.I)
REF = re.compile(r"#(\d+)")


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def paged(path: str) -> list[dict]:
    out = gh("api", "--paginate", path).stdout
    return json.loads(out) if out.strip().startswith("[") else []


def blockers(body: str) -> list[int]:
    m = BLOCKED_BY.search(body or "")
    if not m:
        return []
    seen, order = set(), []
    for n in REF.findall(m.group(1)):
        n = int(n)
        if n not in seen:
            seen.add(n)
            order.append(n)
    return order


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    issues = [i for i in paged(f"repos/{REPO}/issues?state=all&per_page=100")
              if "pull_request" not in i]
    by_num = {i["number"]: i for i in issues}

    added = skipped = failed = 0
    for num in sorted(by_num):
        want = blockers(by_num[num]["body"])
        if not want:
            continue
        have = {d["number"] for d in json.loads(
            gh("api", f"repos/{REPO}/issues/{num}/dependencies/blocked_by").stdout or "[]")}
        for dep in want:
            if dep in have:
                skipped += 1
                continue
            if dep not in by_num:
                print(f"#{num}: blocker #{dep} does not exist", file=sys.stderr)
                failed += 1
                continue
            print(f"#{num} blocked by #{dep}  ({by_num[dep]['title']})")
            if args.dry_run:
                added += 1
                continue
            r = gh("api", "--method", "POST",
                   f"repos/{REPO}/issues/{num}/dependencies/blocked_by",
                   "-F", f"issue_id={by_num[dep]['id']}", check=False)
            if r.returncode == 0:
                added += 1
            else:
                print(f"  FAILED: {r.stderr.strip()[:200]}", file=sys.stderr)
                failed += 1

    print(f"\nadded {added}, already present {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
