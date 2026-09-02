# Resolve merge conflicts for issue #{{issue.number}}

You are running autonomously inside the existing issue workspace at
`{{workspace.path}}` on branch `{{branch.name}}`. The open pull request from
this branch has merge conflicts against `main` and cannot be merged. Your job
is to resolve them, push, and exit.

## What to do

**Use the pm-autofix-pr skill** — its trigger set includes "fix the build" and
"iterate on pr", which covers conflict resolution.

1. `git fetch origin main`.
2. Rebase or merge `origin/main` into `{{branch.name}}`. Prefer rebase unless
   rewriting the history would be destructive to reviewers.
3. Resolve each conflict by preserving the intent of both changes. Where
   resolution needs a judgment call, choose the most defensible option and
   document it in a `gh pr comment` after pushing.
4. Run `git status` and confirm nothing is left unstaged before running the
   gate or pushing.
5. Run the gate as **separate commands**, never `&&`-chained: `uv run ruff
   check .`, `uv run ruff format --check .`, the configured type check, and
   `uv run pytest`. A green gate is the proof the resolution did not silently
   break behavior.
6. If you rebased, `git fetch origin {{branch.name}}` first, then push with
   `--force-with-lease`. If it still rejects, another writer is active — stop
   and comment rather than overwriting. If you merged, push normally.

Discover the PR with `gh pr list --head {{branch.name}} --state open` if you
need the number. Stay on branch `{{branch.name}}`. Do not open a second PR.

## Constraints

- Unattended run; no operator will respond mid-run.
- Use the local `gh` CLI for every GitHub mutation. Do **not** call the GitHub
  MCP connector tools.
- Do not modify operational labels in the `sym:*` namespace. Do not
  self-apply `sym:human-needed`.
- Never run `git checkout ORIG_HEAD -- .` or any bulk checkout during an
  in-progress merge or rebase — it clobbers conflict resolutions.

## Exit

Exit 0 once the rebase or merge is clean and pushed. The orchestrator
re-checks `mergeable` on the next tick and routes accordingly.

If the conflicts genuinely cannot be resolved without a product decision, post
a `gh pr comment` describing what blocked you and **exit non-zero** to end the
run as blocked. Exiting 0 without resolving would return the FSM to
`wait_for_pr`, which would observe the same `mergeable: false` signal and route
straight back here — an infinite loop.
