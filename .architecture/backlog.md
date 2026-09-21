# Architecture deepening backlog

Maintained by the `pm-deepen` skill. Statuses change; rows are never deleted.
`proposed` | `in-flight` | `landed` | `dropped` | `rejected`.

## event-log-writer-seam

- **Status**: landed
- **Score**: 23/25 (leverage 5, locality 5, blast radius 3, heat 5)
- **Files**: ~9 estimated, 8 actual
- **Modules**: `src/modgud/events.py` (new), `src/modgud/cli.py`, `src/modgud/summaries.py`,
  `src/modgud/audio_fallbacks.py`, `src/modgud/podcast_transcripts.py`, `src/modgud/delivery.py`,
  `src/modgud/web.py`
- **Summary**: Sixteen call sites in six modules hand-write the same payload encoding and
  `INSERT INTO events`; one writer module should own the append-only event log.
- **First seen**: 2026-09-20
- **PR**: #78

### Run 2026-09-20 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR opened
- **Branch**: `sym/modgud/routine/refactor-audit/01M2ZCN81D`, adopted (non-default, no unique history, no
  upstream, unpublished on origin). Not renamed, per the adopted-branch rule; the slug is recorded here
  instead.
- **Committed**: the review, this backlog, `src/modgud/events.py`, `tests/test_events.py`, the sixteen
  converted call sites across six modules, and a new `CONTEXT.md`.
- **Evidence**: quality gate green as four separate commands — `ruff check .`, `ruff format --check .`,
  `mypy` (48 source files), `pytest` (314 passed). `grep -rn "INSERT INTO events" src/` returns only
  `events.py`. Diff was 8 files against a scored estimate of 9.
- **Next**: review PR #78. The runner-up candidate, `capture-extraction-outcome-record` (22/25, within one
  point), is the natural next firing and is partly unblocked by this one.

## capture-extraction-outcome-record

- **Status**: proposed
- **Score**: 22/25 (leverage 4, locality 4, blast radius 1, heat 5)
- **Files**: ~3 estimated
- **Modules**: `src/modgud/cli.py`
- **Summary**: `capture_url` threads an unnamed thirteen-field extraction outcome through four disjoint
  branches and two readers 140 lines apart.
- **First seen**: 2026-09-20

## label-token-typed-errors

- **Status**: proposed
- **Score**: 21/25 (leverage 4, locality 5, blast radius 1, heat 3)
- **Files**: ~3 estimated
- **Modules**: `src/modgud/label_tokens.py`, `src/modgud/web.py`
- **Summary**: Label-token failures are discriminated by exception message text and string-compared in the
  web handler; the module has no direct test file.
- **First seen**: 2026-09-20

## workspace-paths-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 3, heat 5)
- **Files**: ~6 estimated
- **Modules**: `src/modgud/cli.py`, `src/modgud/web.py`, `src/modgud/database.py`, `src/modgud/blobs.py`
- **Summary**: Eleven call sites re-derive the data directory's layout by hand, and no module owns the
  connection lifetime.
- **First seen**: 2026-09-20

## web-item-lookup-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/modgud/web.py`
- **Summary**: Six single-item lookups with five column lists; the two label routes are byte-identical for
  eighteen lines and have already drifted.
- **First seen**: 2026-09-20

## named-row-access

- **Status**: proposed
- **Score**: 17/25 (leverage 4, locality 3, blast radius 4, heat 4)
- **Files**: ~14 estimated
- **Modules**: `src/modgud/digests.py`, `src/modgud/web.py`, `src/modgud/origin_reports.py`,
  `src/modgud/cli.py`, `src/modgud/inbound.py`, `src/modgud/time_to_value.py`, `src/modgud/templates/`
- **Summary**: No `row_factory` anywhere; 41 positional row reads plus 7 inside templates, where a
  transposition in `origin_reports.py` silently inverts the report ranking.
- **First seen**: 2026-09-20

## time-to-value-sort-key-removal

- **Status**: dropped
- **Score**: 12/25 (leverage 1, locality 3, blast radius 1, heat 3)
- **Files**: ~2 estimated
- **Modules**: `src/modgud/time_to_value.py`, `src/modgud/digests.py`
- **Summary**: `time_to_value_sort_key` has no production callers; the ordering is re-spelled as SQL in
  `digests.py`.
- **First seen**: 2026-09-20
- **Reason**: Leverage 1 — fails the deletion test. Removing dead code concentrates nothing.

## digest-span-map-query-count

- **Status**: dropped
- **Score**: 16/25 (leverage 2, locality 4, blast radius 2, heat 4)
- **Files**: ~2 estimated
- **Modules**: `src/modgud/digests.py`, `src/modgud/span_maps.py`
- **Summary**: Digest selection issues `1 + 2N` queries inside a `BEGIN IMMEDIATE` write transaction.
- **First seen**: 2026-09-20
- **Reason**: Leverage 2 — a query-count fix inside an existing seam, not a deepening. No interface changes.

## error-response-wrapper-collapse

- **Status**: dropped
- **Score**: 15/25 (leverage 2, locality 3, blast radius 1, heat 4)
- **Files**: ~1 estimated
- **Modules**: `src/modgud/web.py`
- **Summary**: `label_error` and `item_error` are six-line pass-throughs differing by one string literal.
- **First seen**: 2026-09-20
- **Reason**: Leverage 2 — the interface shrinks but callers do the same work; status codes stay ad hoc.

## strftime-timestamp-literal

- **Status**: dropped
- **Score**: 14/25 (leverage 2, locality 3, blast radius 3, heat 3)
- **Files**: ~8 estimated
- **Modules**: `src/modgud/migrations/`, and seven Python modules
- **Summary**: The ISO-8601 `strftime` literal appears 26 times repo-wide.
- **First seen**: 2026-09-20
- **Reason**: Leverage 2 — most occurrences are `DEFAULT` clauses in applied migrations, which are immutable
  history and must not be edited; too few Python-side sites remain to justify a seam.

## capture-url-pipeline-decomposition

- **Status**: dropped
- **Score**: 20/25 (leverage 5, locality 5, blast radius 5, heat 5)
- **Files**: ~4 estimated, but three transaction boundaries and the CLI stdout contract
- **Modules**: `src/modgud/cli.py`, `src/modgud/web.py`, `tests/test_cli.py`
- **Summary**: `capture_url` is 318 lines and 55 branches across three connections with three independent
  commit points, four pre-transaction blob writes, and no direct test.
- **First seen**: 2026-09-20
- **Reason**: Blast radius 5 — too large for one unattended PR. Listed under *Too large to automate*.
  Its automatable sub-problems are tracked as `event-log-writer-seam` and
  `capture-extraction-outcome-record`.

## blob-orphan-reaper

- **Status**: dropped
- **Score**: 18/25 (leverage 4, locality 4, blast radius 5, heat 5)
- **Files**: unknown — a new subsystem
- **Modules**: `src/modgud/blobs.py`, `src/modgud/cli.py`, `src/modgud/audio_fallbacks.py`,
  `src/modgud/podcast_transcripts.py`
- **Summary**: Six sites write a blob before the transaction that records it, and nothing ever collects
  orphaned bytes.
- **First seen**: 2026-09-20
- **Reason**: Blast radius 5, and it is a new feature rather than a deepening. Severity is bounded:
  `BlobStore.put` is content-addressed and idempotent, so the failure mode is wasted disk, never a dangling
  reference.

## Run log

### Run 2026-09-22 — bailed-preflight

- **Outcome**: bailed-preflight
- **Stopped at**: step 2 — an architecture PR is still open: #80 (`summary-source-text-seam`), one
  PR at a time.
- **Branch**: `sym/modgud/routine/refactor-audit/01M33370RK`, adopted (non-default, zero commits ahead
  of `origin/main`, no upstream, unpublished on origin after the preflight fetch). Not renamed.
- **Committed**: this backlog reconciliation and exit report; no code change and no review file.
- **Evidence**: `gh pr view 78` reports MERGED at 2026-09-20T13:42:43Z, so `event-log-writer-seam`
  moves `in-flight` to `landed`. `gh pr list --state open` reports PR #80, head
  `sym/modgud/routine/refactor-audit/01M30GVV0W`, opened by the 2026-09-21 `pm-deepen` firing; that
  branch's backlog carries `summary-source-text-seam` as `in-flight` with `PR: #80`. The entry is not on
  `main` yet only because #80 has not merged; the in-flight rule applies to the open PR, not to where
  its backlog row lives. #80's entries are deliberately not copied here, so they land with #80 and
  keep their slugs. Quality gate discovered but not run, since nothing was implemented:
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.
- **Next**: review and merge (or close) #80, then let the next firing run. #80's backlog ranks
  `capture-extraction-outcome-record` and `capture-reprocess-extraction-dispatch-duplication` tied at
  22/25 as the natural next pick.
