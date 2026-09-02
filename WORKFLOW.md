# Implementing issue #{{issue.number}}: {{issue.title}}

## Issue

{{issue.body}}

## Workspace

Work in {{workspace.path}} on branch {{branch.name}}.

## Project context

modgud is a personal content triage system: drop a URL in, get back a summary,
the claims it makes, and for audio/video a timestamped map of where the value
is. Read `DESIGN.md` before editing — it records not just the design but the
decisions that were reversed and why, so it is the authority when the issue and
your instincts disagree.

Stack: Python managed with `uv`, FastAPI with server-rendered templates, SQLite,
no JS build step. Every model call goes through the OpenAI-compatible protocol
via the routing config; never hardcode a provider or a base URL.

## What to do

1. Read the issue, then read `DESIGN.md` and the code the issue touches before
   editing.
2. Implement the issue test-first. Tests should assert behavior, not
   implementation shape.
3. Honour the issue's `Blocked by` list: if it names work that is not in `main`
   yet, that is a blocked run, not a reason to implement the dependency too.
4. Run the project's quality gate. Once issue #1 has landed that is
   `uv run ruff check .`, `uv run ruff format --check .`, the configured type
   check, and `uv run pytest`. Run each as a **separate command** — never
   `&&`-chained, because a failing early step would silently skip the rest.
5. Commit, push {{branch.name}}, and open a non-draft pull request with the
   local `gh` CLI.
6. Remove the issue's `agent-ready` label after the PR is open.
7. If the work cannot proceed, leave a `gh issue comment` describing what
   blocked it and exit cleanly.

## Scope

Implement the issue and nothing else. These tickets were deliberately sized to
one reviewable change each, and their dependency edges assume that. If you find
a real problem with the issue as written, say so in a PR comment and implement
what it asks under a stated assumption — do not silently widen or narrow it.

## Constraints

- **You are running unattended.** No operator will respond to prompts, approve
  tool calls, or read intermediate output during this run.
- **Use the local `gh` CLI for every GitHub mutation** (`gh issue ...`,
  `gh pr ...`, `gh issue comment ...`, `gh issue edit ...`). Do not call GitHub
  MCP connector tools (for example `add_issue_labels`, `create_pull_request`);
  they elicit operator approval through the provider transport and end the run
  as `input_required`.
- **Do not modify operational labels in the `sym:*` namespace**, and do not
  self-apply `needs-human` or any other handoff label as an exit strategy. Use
  the comment-and-exit path in step 7; the operator owns label triage.
- Do not commit secrets. Postmark tokens and any provider API keys come from
  the environment, never from config files or the database.
