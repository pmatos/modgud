# Existing-item lifecycle transition seam

Date: 2026-09-24
Outcome: implemented
Selected candidate: `transactional-item-transition`

## Constraints consulted

- `CONTEXT.md`: `modgud.events` remains the sole event-table writer; `ItemLog` uses a caller-owned connection and never opens or commits; analytic event reads remain decentralized.
- `DESIGN.md`: items have one lifecycle state; state transitions are explained by append-only events; storage remains open and greppable.
- Prior run PR #80: source-text resolution already landed and was not reconsidered.
- Historical `.architecture/backlog.md` before its deletion on `main`: the blast-radius-5 capture pipeline and other dropped candidates were not reconsidered.
- No `CLAUDE.md`, `AGENTS.md`, or ADR directory exists.

## Ranked candidates

The score is `(leverage × 2) + locality + heat + (6 - blast radius)`.

| Rank | Candidate | Score | Leverage | Locality | Blast radius | Heat | Evidence |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | `transactional-item-transition` | 23/25 | 5 | 5 | 2 (~8 files) | 4 | Four processing paths repeated item mutation plus the matching `ItemLog` event; reprocess and summaries carried additional concurrency/artifact rules. |
| 2 | `document-extraction-seam` | 21/25 | 4 | 5 | 2 (~5 files) | 4 | Capture and reprocess independently dispatched web/PDF extraction and normalized two result shapes. |
| 3 | `long-form-summary-lifecycle` | 21/25 | 4 | 5 | 2 (~4 files) | 4 | Web GET, POST, and worker split eligibility, request-state, and unexpected-failure persistence with the tier-2 module. |
| 4 | `label-capability-seam` | 20/25 | 4 | 4 | 2 (~8 files) | 4 | Digest, delivery, CLI, and web separately assembled signing secret, lifetime, expiry, and URL policy. |
| 5 | `model-session-lifecycle` | 20/25 | 4 | 4 | 2 (~7 files) | 4 | Five model callers repeated ownership and closure of the routed OpenAI client. |
| 6 | `workspace-paths-seam` | 20/25 | 4 | 4 | 3 (~6 files) | 5 | CLI and web repeatedly derived database/blob paths; larger cross-entrypoint migration than higher candidates. |
| 7 | `web-item-lookup-seam` | 20/25 | 4 | 4 | 2 (~4 files) | 4 | Web routes repeated item lookup projections; limited to one entrypoint module. |
| 8 | `chunked-model-artifact-runner` | 18/25 | 3 | 4 | 2 (~5 files) | 4 | Tier-1 and tier-2 repeated retry/client/chunk execution but retained substantial tier-specific assembly. |
| 9 | `transcript-persistence-seam` | 18/25 | 3 | 4 | 2 (~4 files) | 4 | Audio and podcast paths repeated transcript state/event persistence; it is a subset of the selected lifecycle seam. |
| 10 | `web-label-interaction-seam` | 18/25 | 3 | 4 | 1 (~3 files) | 4 | Two web routes repeated authorization and lookup; useful but web-local. |

Tie-breaking placed `document-extraction-seam` above `long-form-summary-lifecycle`: blast radius and heat were equal, and reprocess/document extraction was touched more recently. The selected candidate led both by two points.

## Hard filters and prior decisions

- `capture-url-pipeline-decomposition`: blast radius 5 in the historical backlog; still too large for one unattended PR.
- `capture-extraction-outcome-record`: leverage 1 when implemented as a large data carrier; its interface would be nearly as complex as its implementation and a useful test would assert field forwarding.
- Centralized event-log reads: contradicts `CONTEXT.md`.
- Generic transaction wrapper, retry helper, timestamp literal, and error-wrapper collapse: leverage 1–2; complexity moves rather than concentrates.
- Ports over lifecycle persistence: only one complete adapter exists, so an abstract port would be hypothetical.
- Source-text resolution and event-log writing: already landed in PRs #80 and #78.

## Selected friction

`audio_fallbacks.py`, `podcast_transcripts.py`, `reprocess.py`, and `summaries.py` each knew item columns, timestamp SQL, state selection, and matching event calls. The copies had materially different extra rules:

- audio and podcast transitions require provenance before `extracted` or `failed`;
- reprocess requires `BEGIN IMMEDIATE`, expected-state comparison, missing-text comparison, metadata coalescing, and a stable refusal;
- failed summary regeneration must retain `summarized` when a prior artifact remains valid;
- successful summary generation must commit the artifact, item state, and event together.

Deleting a lifecycle module would redistribute all of this policy across the four callers. The candidate passes the deletion test.

## Interface designs

### A. Minimal explicit functions — selected

A concrete `modgud.item_lifecycle` module exposes `mark_extracted`, `mark_failed`, `mark_summary_failed`, `mark_unsummarizable`, and `mark_summarized`. Each accepts the caller's connection and item id, hides transition SQL and the matching `ItemLog` call, and never opens or commits. Route-specific provenance remains in the route caller immediately before the lifecycle function.

### B. Typed transition command union — strongest loser

One `apply_item_transition` executor accepts a closed union of route-specific command dataclasses. It statically fixes provenance and event order, but expands the interface by one command type for every route outcome and requires a dispatcher. It lost on depth: callers learn more types for the same persistence behavior.

### C. Deepen `ItemLog`

Change `ItemLog.extracted`, `failed`, `unsummarizable`, and `summarized` to mutate item state. This shortens callers but contradicts the recorded `ItemLog` meaning as the event-append interface and changes capture call sites outside the candidate.

### D. Ports and adapters

Introduce an `ItemLifecyclePort` plus a SQLite adapter. The operation is coherent, but only one complete adapter exists. The protocol and fake would be a hypothetical seam; its concrete version collapses to design B.

Adjudication order: depth, locality, seam placement, test surface, blast radius. A typed design adjudicator selected design A.

## Implementation

`modgud.item_lifecycle` now owns the existing-item writes and delegates every lifecycle event to `ItemLog`. All four parallel implementations migrated together. `CONTEXT.md` records **Item lifecycle**. The implementation diff touched seven files against an estimate of eight; this report and backlog are audit artifacts outside that implementation estimate.

## Test-first evidence

The new interface test was added before the module and failed during collection:

```text
ModuleNotFoundError: No module named 'modgud.item_lifecycle'
```

After implementation:

- lifecycle/audio/podcast/summary focus: 23 passed;
- reprocess focus: 15 passed;
- `uv run ruff check .`: passed;
- `uv run ruff format --check .`: 63 files formatted;
- `uv run mypy`: no issues in 53 source files;
- `uv run pytest`: 348 passed.

`INSERT INTO events` remains present only in `modgud.events`.

## Proposed ADR

**Title:** Existing-item lifecycle transitions are transaction-owned operations.

**Decision:** Every transition of an existing item to `extracted`, `failed`, `unsummarizable`, or `summarized` uses `modgud.item_lifecycle` on a caller-owned SQLite connection. The module owns the relevant item mutation, owns the tier-1 artifact write for a summarized outcome, and invokes `ItemLog` for the matching event on that connection. Source-specific provenance may be appended immediately before the lifecycle event in the same transaction. `modgud.events` remains the sole event-table writer. Initial capture remains separate because it creates the item.

No ADR file was written; this is a proposal for review.
