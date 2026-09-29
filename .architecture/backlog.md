# Architecture deepening backlog

Maintained by the `pm-deepen` skill. Statuses change; rows are never deleted.
`proposed` | `in-flight` | `landed` | `dropped` | `rejected`.

## transactional-item-transition

- **Status**: in-flight
- **Score**: 23/25 (leverage 5, locality 5, blast radius 2, heat 4)
- **Files**: ~8 estimated, 7 implementation files actual
- **Modules**: `src/modgud/item_lifecycle.py`, `audio_fallbacks.py`, `podcast_transcripts.py`, `reprocess.py`, `summaries.py`
- **Summary**: Four processing paths repeated item-state mutation and the matching `ItemLog` event; one lifecycle module now owns the transactional pairing.
- **First seen**: 2026-09-24
- **Report**: `.architecture/reviews/2026-09-24-item-lifecycle-transitions.md`
- **PR**: #81

## summary-source-text-seam

- **Status**: landed
- **Score**: 23/25 (leverage 5, locality 5, blast radius 2, heat 4)
- **Files**: 6 actual
- **Modules**: `src/modgud/source_material.py`, `summaries.py`, `long_form_summaries.py`, `span_maps.py`, `web.py`
- **Summary**: Source-text lookup, document/transcript dispatch, and chunking are centralized while format eligibility remains caller policy.
- **First seen**: 2026-09-21
- **PR**: #80

## event-log-writer-seam

- **Status**: landed
- **Score**: 23/25 (leverage 5, locality 5, blast radius 3, heat 5)
- **Files**: ~9 estimated, 8 actual
- **Modules**: `src/modgud/events.py` and six event-writing callers
- **Summary**: Sixteen call sites formerly hand-wrote payload encoding and `INSERT INTO events`; `ItemLog` is now the sole writer.
- **First seen**: 2026-09-20
- **PR**: #78

## document-extraction-seam

- **Status**: proposed
- **Score**: 21/25 (leverage 4, locality 5, blast radius 2, heat 4)
- **Files**: ~5 estimated
- **Modules**: `src/modgud/extraction.py`, `cli.py`, `reprocess.py`
- **Summary**: Capture and reprocess independently dispatch web/PDF extraction and normalize two nearly identical result shapes.
- **First seen**: 2026-09-24

## long-form-summary-lifecycle

- **Status**: proposed
- **Score**: 21/25 (leverage 4, locality 5, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/modgud/long_form_summaries.py`, `web.py`
- **Summary**: Web GET, POST, and the worker split tier-2 eligibility, request state, and unexpected-failure persistence.
- **First seen**: 2026-09-24

## label-token-typed-errors

- **Status**: proposed
- **Score**: 21/25 (leverage 4, locality 5, blast radius 1, heat 3)
- **Files**: ~3 estimated
- **Modules**: `src/modgud/label_tokens.py`, `web.py`
- **Summary**: Web presentation distinguishes label-token scope failures by exception message text instead of typed outcomes.
- **First seen**: 2026-09-20

## label-capability-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~8 estimated
- **Modules**: `label_tokens.py`, `digests.py`, `delivery.py`, `cli.py`, `web.py`
- **Summary**: Digest, delivery, CLI, and web separately assemble signing secret, lifetime, expiry, and label-link URL policy.
- **First seen**: 2026-09-24

## model-session-lifecycle

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~7 estimated
- **Modules**: `models.py` and five model callers
- **Summary**: Model routing is centralized, but five callers repeat ownership and closure of the routed client.
- **First seen**: 2026-09-24

## workspace-paths-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 3, heat 5)
- **Files**: ~6 estimated
- **Modules**: `src/modgud/cli.py`, `web.py`, `database.py`, `blobs.py`
- **Summary**: Entry points repeatedly derive the data directory's database/blob layout.
- **First seen**: 2026-09-20

## web-item-lookup-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/modgud/web.py`
- **Summary**: Six single-item lookups use five column projections; label routes duplicate lookup and authorization flow.
- **First seen**: 2026-09-20

## capture-extraction-outcome-record

- **Status**: dropped
- **Score**: 5/25 (leverage 1, locality 1, blast radius 1, heat 5)
- **Files**: ~3 estimated
- **Modules**: `src/modgud/cli.py`
- **Summary**: Replacing capture locals with a large record would create an interface nearly as complex as its implementation.
- **First seen**: 2026-09-20
- **Reason**: Leverage 1 after re-verification; one caller and field-forwarding tests fail the deletion/test-surface bar.

## named-row-access

- **Status**: proposed
- **Score**: 17/25 (leverage 4, locality 3, blast radius 4, heat 4)
- **Files**: ~14 estimated
- **Modules**: `digests.py`, `web.py`, `origin_reports.py`, `cli.py`, `inbound.py`, `time_to_value.py`, templates
- **Summary**: Positional SQLite row access is broad and transposition-prone.
- **First seen**: 2026-09-20

## capture-url-pipeline-decomposition

- **Status**: dropped
- **Score**: 20/25 (leverage 5, locality 5, blast radius 5, heat 5)
- **Files**: three transaction seams and cross-entrypoint behavior
- **Modules**: `src/modgud/cli.py`, `web.py`, `tests/test_cli.py`
- **Summary**: `capture_url` is a large cross-entrypoint pipeline, but a safe useful decomposition exceeds one unattended PR.
- **First seen**: 2026-09-20
- **Reason**: Blast radius 5; do not re-pick under a renamed capture-seam candidate.

## time-to-value-sort-key-removal

- **Status**: dropped
- **Score**: 12/25 (leverage 1, locality 3, blast radius 1, heat 3)
- **Summary**: Dead helper removal concentrates no behavior.
- **First seen**: 2026-09-20
- **Reason**: Leverage 1; fails the deletion test.

## digest-span-map-query-count

- **Status**: dropped
- **Score**: 16/25 (leverage 2, locality 4, blast radius 2, heat 4)
- **Summary**: Query-count optimization inside an existing seam, not a module deepening.
- **First seen**: 2026-09-20

## error-response-wrapper-collapse

- **Status**: dropped
- **Score**: 15/25 (leverage 2, locality 3, blast radius 1, heat 4)
- **Summary**: Collapsing small HTML wrappers moves presentation syntax without hiding policy.
- **First seen**: 2026-09-20

## strftime-timestamp-literal

- **Status**: dropped
- **Score**: 14/25 (leverage 2, locality 3, blast radius 3, heat 3)
- **Summary**: Most repetitions live in immutable migrations; remaining Python literals do not justify a seam.
- **First seen**: 2026-09-20

## blob-orphan-reaper

- **Status**: dropped
- **Score**: 18/25 (leverage 4, locality 4, blast radius 5, heat 5)
- **Summary**: New garbage-collection subsystem rather than a contained deepening; content addressing bounds the failure to wasted disk.
- **First seen**: 2026-09-20
- **Reason**: Blast radius 5.
