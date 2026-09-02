# Simplify pass for issue #{{issue.number}}

You are running autonomously inside the existing issue workspace at
`{{workspace.path}}` on branch `{{branch.name}}`. The code review pass on
this pull request just completed. Your job is to run a simplification pass
against it, apply any fixes it finds, and exit.

## What to do

**Run `/simplify` against the changed code for the currently open pull
request on branch `{{branch.name}}`.** Let it apply its own fixes to the
working tree. It looks for reuse, simplification, efficiency and altitude
issues; it does not hunt for bugs, which the previous state already did.

Run the project quality gate as **separate commands**, never `&&`-chained:
`uv run ruff check .`, `uv run ruff format --check .`, the configured type
check, and `uv run pytest`. A flaky early step must not silently skip the
ones after it.

If it made changes, commit and push them to `{{branch.name}}`.

Discover the PR yourself with `gh pr list --head {{branch.name}} --state open`
if you need the PR number — do not assume one. Stay on branch
`{{branch.name}}`. Do not open a second PR.

## Constraints

- This run is unattended. No operator will respond to prompts.
- Use the local `gh` CLI for every GitHub mutation. Do **not** call the GitHub
  MCP connector tools.
- Do not modify operational labels in the `sym:*` namespace. Do not
  self-apply `sym:human-needed`.
- Simplifying must not change behavior. If a simplification would alter what
  the code does, leave it and note it in a PR comment.

## Exit

Exit 0 once `/simplify` has run and any fixes are pushed, or it found nothing
to simplify. The orchestrator then enters the wait state and starts polling
CI and merge signals.

If it cannot proceed, post a `gh pr comment` and **exit non-zero** to
terminate the run as blocked.
