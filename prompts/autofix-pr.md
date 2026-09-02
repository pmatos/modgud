# Autofix pull request for issue #{{issue.number}}

You are running autonomously inside the existing issue workspace at
`{{workspace.path}}` on branch `{{branch.name}}`. A pull request was opened
from this branch earlier and is now failing CI or has unresolved reviewer
feedback. Your job is to drive that PR to a clean state, then exit.

## What to do

**Use the pm-autofix-pr skill to fix the failing checks and reviewer feedback
on the open pull request for branch `{{branch.name}}`.**

The skill iterates the standard local-CLI autofix loop: identify the PR, read
failing check logs and unresolved review threads, make targeted edits, run the
local quality gate, commit, push, repeat until clean.

Run the gate as **separate commands**, never `&&`-chained: `uv run ruff check
.`, `uv run ruff format --check .`, the configured type check, and `uv run
pytest`. A flaky early step must not silently skip the ones after it.

Discover the PR yourself with `gh pr list --head {{branch.name}} --state open`
if you need the PR number — do not assume one. Stay on branch
`{{branch.name}}`. Do not open a second PR.

## Constraints

- This run is unattended. No operator will respond to prompts.
- Use the local `gh` CLI for every GitHub mutation. Do **not** call the GitHub
  MCP connector tools — they elicit operator approval and end the run with
  `terminal_reason="provider requested input"`.
- Do not modify operational labels in the `sym:*` namespace. Do not
  self-apply `sym:human-needed`.
- Fix the cause, not the symptom. Deleting or skipping a failing test to get
  green is a failure of this state, not a success.
- `DESIGN.md` is the authority on intended behavior. If a reviewer asks for
  something it contradicts, say so in the thread rather than complying.

## Exit

Exit 0 once the gate passes and you have pushed the fix. The orchestrator
re-enters the wait state, re-checks the PR, and either loops back here,
advances to merge, or terminates on the next signal snapshot.

If you genuinely cannot make progress — the failure needs a product decision,
say — post a `gh pr comment` explaining what blocked you and **exit
non-zero**. That routes the FSM through `provider_success: false` to the
`failed` catch-all and ends the run as blocked.

Exiting 0 without fixing anything would set `provider_success: true`, return
the FSM to `wait_for_pr`, which would observe the same failing signals and
route straight back into this state — an infinite loop.
