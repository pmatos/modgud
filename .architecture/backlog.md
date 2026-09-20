# Architecture deepening backlog

Maintained by the `pm-deepen` skill. Statuses change; rows are never deleted.
`proposed` | `in-flight` | `landed` | `dropped` | `rejected`.

## event-log-writer-seam

- **Status**: proposed
- **Score**: 23/25 (leverage 5, locality 5, blast radius 3, heat 5)
- **Files**: ~9 estimated
- **Modules**: `src/modgud/events.py` (new), `src/modgud/cli.py`, `src/modgud/summaries.py`,
  `src/modgud/audio_fallbacks.py`, `src/modgud/podcast_transcripts.py`, `src/modgud/delivery.py`,
  `src/modgud/web.py`
- **Summary**: Sixteen call sites in six modules hand-write the same payload encoding and
  `INSERT INTO events`; one writer module should own the append-only event log.
- **First seen**: 2026-09-20

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
