# Code review pass for issue #{{issue.number}}

You are running autonomously inside the existing issue workspace at
`{{workspace.path}}` on branch `{{branch.name}}`. A pull request was just
opened from this branch. Your job is to run an automated code review pass
against it, apply any fixes it finds, and exit.

## What to do

**Run `/code-review --fix` against the diff for the currently open pull
request on branch `{{branch.name}}`.** Let it apply its own findings to the
working tree.

Run the project quality gate as **separate commands**, never `&&`-chained:
`uv run ruff check .`, `uv run ruff format --check .`, the configured type
check, and `uv run pytest`. A flaky early step must not silently skip the
ones after it.

If it made changes, commit and push them to `{{branch.name}}`.

Discover the PR yourself with `gh pr list --head {{branch.name}} --state open`
if you need the PR number — do not assume one. Stay on branch
`{{branch.name}}`. Do not open a second PR.

## Constraints

- This run is unattended. No operator will respond to prompts. Behavior that
  depends on a human answering mid-run is a failure mode.
- Use the local `gh` CLI for every GitHub mutation. Do **not** call the GitHub
  MCP connector tools — they elicit operator approval and end the run with
  `terminal_reason="provider requested input"`.
- Do not modify operational labels in the `sym:*` namespace. Do not
  self-apply `sym:human-needed` — the orchestrator applies that automatically
  when a run ends up blocked.
- `DESIGN.md` is the authority on intended behavior. A review finding that
  contradicts it is wrong about the design, not the other way round; say so in
  a PR comment rather than changing the design to match the code.

## Exit

Exit 0 once `/code-review --fix` has run and any fixes it made are pushed, or
it found nothing to fix.

If it genuinely cannot proceed (no open PR for this branch, for example), post
a `gh pr comment` explaining what blocked you and **exit non-zero**. A
non-zero exit routes the FSM through `provider_success: false` to the
`failed` catch-all and terminates the run as blocked.
