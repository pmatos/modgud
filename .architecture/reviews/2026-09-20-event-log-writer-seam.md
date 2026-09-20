# Architecture review — modgud — 2026-09-20

**Scope**: Whole tree, weighted by hot spots from the last 60 commits (`src/modgud/cli.py` 21 touches,
`src/modgud/database.py` 11, `src/modgud/web.py` 7, `src/modgud/config.py` 6, `src/modgud/digests.py` 6).
Two sub-agents walked the capture/CLI side and the web/digest/report side; findings below carry `path:line`
anchors back to the evidence.

**Picked**: `event-log-writer-seam` — see `.architecture/backlog.md`

**Degradations**: none. `gh` authenticated, sub-agents available, quality gate discovered from
`.github/workflows/ci.yml`.

**Vocabulary note**: the repo has no `CONTEXT.md` and no ADRs, so domain terms below are taken from the
code and `DESIGN.md`: an **item** is a captured piece of content, an **event** is an append-only row in
`events` recording something that happened to an item, and a **capture** is one run of `capture_url`.
Architecture terms are `codebase-design`'s: module, interface, depth, seam, adapter, leverage, locality.

**Diagram legend**: solid edges are the interface a caller must know; dashed edges are inside the
implementation.

## Candidates

### event-log-writer-seam — one writer for the append-only event log · Strong · score 23/25

- **Files**: `src/modgud/cli.py:71-238` (five `_record_*` helpers), `src/modgud/cli.py:100,160,196,214,234`,
  `src/modgud/summaries.py:209,245`, `src/modgud/audio_fallbacks.py:89,96,131,138`,
  `src/modgud/podcast_transcripts.py:228,235,312`, `src/modgud/delivery.py:190`, `src/modgud/web.py:484`,
  `src/modgud/migrations/001_initial.sql:22-34` (the table and its append-only trigger).
  New module `src/modgud/events.py` plus `tests/test_events.py`.
  **File-count estimate: 9.**
- **Score**: **23/25** (leverage 5, locality 5, blast radius 3, heat 5)
  - *Leverage 5* — 16 hand-written `INSERT INTO events` statements across 6 modules, each preceded by its own
    hand-rolled `json.dumps(..., separators=(",", ":"), sort_keys=True)`. One seam pays back at every one of
    them and removes the entire class of "spell the payload encoding again" setup.
  - *Locality 5* — today, changing how an event payload is encoded, or adding an event type, means editing a
    module and re-deriving the encoding from a neighbour. Afterwards it is a one-file edit in `events.py`.
  - *Blast radius 3* — several modules (6 source files, 1 new module, 1 new test, plus the call-site edits);
    no published interface changes. The CLI stdout contract, the HTTP routes, and the stored payload bytes
    are all preserved.
  - *Heat 5* — every one of the six call-site modules appears in the last 60 commits; `cli.py` is the single
    hottest file in the repo.
- **Problem**: the event log is the repo's central fact — `origin_reports.py:19-53` and `digests.py:260-271`
  both read it, `web.py:212-228` joins against it — but it has **no writer module**. Sixteen call sites each
  re-implement the same three-step prologue: build a `dict`, `json.dumps` it with a specific separator/sort
  configuration, and `INSERT INTO events (item_id, type, payload)` with the type as a SQL string literal.
  The five `_record_*` helpers in `cli.py` are the shallowest instance: `_record_caption_refusal`
  (`cli.py:201-218`) and `_record_unsummarizable` (`cli.py:221-238`) are two 18-line functions whose entire
  semantic content is the pair `('caption_refused', 'captions')` versus `('unsummarizable', 'extraction')`.
  Their interface — a connection, a keyword `item_id`, a keyword `reason` — is as complex as everything they
  do. That is shallowness by definition.

  The duplication has already drifted. `summaries.py:214-220` encodes its `summarized` payload with
  `ensure_ascii=False`; the other fifteen sites take the default and escape non-ASCII. `web.py:485` omits
  `sort_keys` entirely. Nothing in the tree states which is correct, because nothing owns the decision.

  There is a second, quieter cost: none of the sixteen sites commits, and none says it does not.
  `summarize_item` (`summaries.py:119-250`) and every `_record_*` helper are fragments that are only correct
  inside a caller's `with connect(...)` block, and neither their signatures nor their docstrings say so.
  A writer seam is where that contract can finally be written down.
- **Deletion test**: delete the five `_record_*` helpers and complexity **concentrates** — the payload shapes
  and event-type literals move into one module that owns the `events` table, rather than dispersing back to
  their callers. Delete the seam afterwards and it disperses again to sixteen places. Concentrates. Passes.
- **Solution**: add `src/modgud/events.py` owning the append-only event log: an `EventType` enumeration of
  the eleven literals currently spelled as bare SQL strings, and one `record_event(connection, item_id,
  event_type, fields)` writer that performs the canonical encoding and the insert. Replace the five `cli.py`
  helpers and the eleven other call sites with calls to it. Payload bytes are preserved exactly at every
  site; the refactor is a pure re-seating of where the encoding lives.
- **Benefits**:
  - *Leverage*: a caller that wants to record something learns one function and one enumeration instead of a
    SQL string, a JSON configuration, and the table's column order.
  - *Locality*: the answer to "how is an event payload encoded?" and "what event types exist?" becomes one
    file. Today those questions are answered by reading six.
  - *Test surface*: the event log gets a `tests/test_events.py` that exercises encoding and the append-only
    trigger directly through the interface. Today the only way to assert on event writing is to stand up a
    capture — 22 of `tests/test_cli.py`'s capture tests do it by `subprocess.run(["modgud", ...])` against a
    live `ThreadingHTTPServer` (`tests/test_cli.py:140-193`), because `capture_url` has no injectable seams.
    The encoding is currently pinned only incidentally, by 39 assertions that read `FROM events` across 8
    test files.

**Before**

```mermaid
graph LR
  CLI[cli.py] --> D1[json.dumps + INSERT]
  SUM[summaries.py] --> D2[json.dumps + INSERT]
  AUD[audio_fallbacks.py] --> D3[json.dumps + INSERT]
  POD[podcast_transcripts.py] --> D4[json.dumps + INSERT]
  DEL[delivery.py] --> D5[json.dumps + INSERT]
  WEB[web.py] --> D6[json.dumps + INSERT]
  D1 --> T[(events)]
  D2 --> T
  D3 --> T
  D4 --> T
  D5 --> T
  D6 --> T
```

**After**

```mermaid
graph LR
  CLI[cli.py] --> E[events.record_event]
  SUM[summaries.py] --> E
  AUD[audio_fallbacks.py] --> E
  POD[podcast_transcripts.py] --> E
  DEL[delivery.py] --> E
  WEB[web.py] --> E
  E -.-> ENC[canonical payload encoding]
  E -.-> INS[INSERT INTO events]
  INS -.-> T[(events)]
```

---

### capture-extraction-outcome-record — name the 13-field record `capture_url` threads · Worth exploring · score 22/25

- **Files**: `src/modgud/cli.py:333-345` (the sentinel block), `:346-391` (four disjoint writers),
  `:393-402` and `:488-537` (two readers, 140 lines downstream), `tests/test_cli.py`.
  **File-count estimate: 3.**
- **Score**: **22/25** (leverage 4, locality 4, blast radius 1, heat 5)
  - *Leverage 4* — one deeply-nested caller stops threading an anonymous record; it does not pay back at
    other call sites, so it cannot reach 5.
  - *Locality 4* — adding an extraction format becomes an edit in one place instead of four disjoint branches
    plus two downstream readers.
  - *Blast radius 1* — contained to `cli.py` and its test; no published interface changes.
  - *Heat 5* — `cli.py` is the hottest file in the repo.
- **Problem**: `capture_url` declares thirteen `None` sentinels in one block at `cli.py:333-345`
  (`extracted_text_hash`, `title`, `author`, `channel`, `chapters`, `extracted_site`, `extraction_error`,
  `extraction_error_stage`, `unsummarizable_reason`, `extracted_text`, `caption_language`, `caption_kind`,
  `duration_seconds`), fills a different subset of them in each of four mutually exclusive extraction
  branches, then reads them twice — at `:393-402` to derive `item_state`, and again at `:488-537` to choose
  which recorder to call. Three of the thirteen are set only on the YouTube branch and consumed 140 lines
  later. This is an extraction-outcome type with no name and no type.
- **Deletion test**: concentrates. Naming the record moves the "which fields does a YouTube extraction
  produce?" question out of the reader's head and into one declaration.
- **Solution**: a frozen dataclass per extraction outcome (or one with an explicit discriminant), returned by
  each branch and consumed by the two readers.
- **Benefits**: *locality* — the four branches become four constructors; *test surface* — the state-derivation
  logic at `:393-402` becomes testable without running a capture.

**Before**

```mermaid
graph TD
  S[13 None sentinels] --> B1[youtube branch]
  S --> B2[podcast branch]
  S --> B3[web branch]
  S --> B4[pdf branch]
  B1 --> R1[derive item_state]
  B2 --> R1
  B3 --> R1
  B4 --> R1
  R1 --> R2[choose recorder]
```

**After**

```mermaid
graph TD
  B1[youtube branch] --> O[ExtractionOutcome]
  B2[podcast branch] --> O
  B3[web branch] --> O
  B4[pdf branch] --> O
  O --> R1[derive item_state]
  O --> R2[choose recorder]
  O -.-> F[fields per outcome kind]
```

---

### label-token-typed-errors — stop discriminating failures by message text · Worth exploring · score 21/25

- **Files**: `src/modgud/label_tokens.py:17-97`, `src/modgud/web.py:167-189`, new `tests/test_label_tokens.py`.
  **File-count estimate: 3.**
- **Score**: **21/25** (leverage 4, locality 5, blast radius 1, heat 3)
  - *Leverage 4* — only two call sites, but it removes a whole class of test setup: the module's error
    contract currently costs 28-37 lines of scaffolding per assertion.
  - *Locality 5* — the failure taxonomy becomes one declaration instead of a message string in one module and
    a string comparison in another.
  - *Blast radius 1* — three files, no published interface changes.
  - *Heat 3* — `label_tokens.py` landed in #65 and has not moved since; `web.py` is hot.
- **Problem**: `validate_label_token` (`label_tokens.py:56-97`) signals five distinct outcomes, only one of
  which has a type (`ExpiredLabelToken`, `:17`). The other four are all `InvalidLabelToken`, discriminated
  **only by their message text** — `"malformed token"`, `"invalid signature"`, `"malformed payload"`,
  `"wrong item"`, `"wrong label"`. `web.py:180-188` then rebuilds user-facing copy by string-comparing the
  exception: `if str(error) == "wrong item"`. Renaming that literal at `label_tokens.py:92` silently
  downgrades the page served at `web.py:182` to a generic fallback — no type error, no import to follow.
  There is no `tests/test_label_tokens.py`: the 96-line module's error contract is pinned by exactly three
  assertions in web tests (`tests/test_web.py:360,403,445`), each of which first builds a nine-field
  `DigestItem`, renders a full HTML email, and **regex-scrapes an `href` out of it**
  (`tests/test_web.py:97-131`) — because the URL assembly lives in the private `digests._label_link`.
- **Deletion test**: concentrates. One exception hierarchy replaces a set of strings crossing a module seam.
- **Solution**: one exception class per failure mode (or a typed discriminant on `InvalidLabelToken`), and a
  direct unit test file for the module.
- **Benefits**: *leverage* — the caller matches on types the type-checker enforces; *test surface* — five
  outcomes become five cheap unit tests instead of three expensive integration ones.

**Before**

```mermaid
graph LR
  W[web.py handler] --> V[validate_label_token]
  V -.-> E1[InvalidLabelToken 'wrong item']
  V -.-> E2[InvalidLabelToken 'wrong label']
  V -.-> E3[InvalidLabelToken 'malformed payload']
  E1 --> C[str-compare in web.py]
  E2 --> C
  E3 --> C
```

**After**

```mermaid
graph LR
  W[web.py handler] --> V[validate_label_token]
  V --> T[typed failure]
  T -.-> E1[WrongItem]
  T -.-> E2[WrongLabel]
  T -.-> E3[MalformedToken]
  W --> M[match on type]
```

---

### workspace-paths-seam — one module owns where the data lives · Worth exploring · score 20/25

- **Files**: the literal `"modgud.sqlite3"` at `src/modgud/cli.py:253,562,580,586,600,727,736,750,756,782` and
  `src/modgud/web.py:110`; `BlobStore(data_dir / "blobs")` at `src/modgud/cli.py:326,589,603,725` and
  `src/modgud/web.py:111`. **File-count estimate: 6.**
- **Score**: **20/25** (leverage 4, locality 4, blast radius 3, heat 5)
- **Problem**: there is no module that knows the shape of a data directory. `data_dir` is a `Path` that
  eleven call sites unpack by hand, re-deriving the database filename and the blob root each time.
  The same absence shows up in connection lifetime: `grep -rn '\.commit()' src/` returns **zero** hits, so
  every one of the 25 `with connect(...)` sites relies on `sqlite3.Connection.__exit__` committing —
  which it does — while none of them relies on it closing, which it does not. Closure is left to refcounting.
- **Deletion test**: concentrates, weakly. A `Workspace` that hands out a connection and a blob store puts
  the layout and the connection contract in one place.
- **Solution**: a module owning the data-directory layout and the connection context manager.
- **Benefits**: *locality* — moving the database file, adding a busy timeout, or closing deterministically
  becomes a one-file edit.

**Before**

```mermaid
graph LR
  C1[cli commands] --> P1[data_dir / 'modgud.sqlite3']
  C1 --> P2[BlobStore data_dir / 'blobs']
  W1[web app] --> P3[data_dir / 'modgud.sqlite3']
  W1 --> P4[BlobStore data_dir / 'blobs']
  P1 --> DB[(sqlite)]
  P3 --> DB
```

**After**

```mermaid
graph LR
  C1[cli commands] --> WS[Workspace]
  W1[web app] --> WS
  WS -.-> P1[database path]
  WS -.-> P2[blob root]
  WS -.-> P3[connection lifetime]
  P1 -.-> DB[(sqlite)]
```

---

### web-item-lookup-seam — one way to look an item up for display · Worth exploring · score 20/25

- **Files**: `src/modgud/web.py:208,212-228,293-299,374-380,405,445-449,474-478`; the duplicated route bodies
  at `:435-462` and `:464-494`. **File-count estimate: 4.**
- **Score**: **20/25** (leverage 4, locality 4, blast radius 2, heat 4)
- **Problem**: six single-item lookups with five distinct column lists, and `coalesce(title, canonical_url)`
  written out five times (`:214,294,375,446,475`). `confirm_label` and `record_label` are byte-identical for
  eighteen lines and have **already drifted**: the `if item is None` check sits outside the `with` block at
  `:452` and inside it at `:481`. `SELECT format, extracted_text_hash, chapters FROM items WHERE id = ?`
  appears verbatim in three further modules (`summaries.py:128`, `span_maps.py:196`,
  `long_form_summaries.py:38`), each unpacking into the same three names.
- **Deletion test**: concentrates.
- **Solution**: one lookup returning a named item projection, shared by the routes that display an item.
- **Benefits**: *locality* — the display projection stops being re-derived per route; the drift between the
  twin routes becomes impossible to express.

**Before**

```mermaid
graph LR
  R1[home] --> Q1[SELECT list A]
  R2[item_detail] --> Q2[SELECT list B]
  R3[summary GET] --> Q3[SELECT list C]
  R4[confirm_label] --> Q4[SELECT list D]
  R5[record_label] --> Q5[SELECT list D copy]
  Q1 --> T[(items)]
  Q2 --> T
  Q3 --> T
  Q4 --> T
  Q5 --> T
```

**After**

```mermaid
graph LR
  R1[home] --> L[item lookup]
  R2[item_detail] --> L
  R3[summary GET] --> L
  R4[confirm_label] --> L
  R5[record_label] --> L
  L -.-> Q[projection + SQL]
  Q -.-> T[(items)]
```

---

### named-row-access — give database rows names instead of ordinals · Speculative · score 17/25

- **Files**: 34 lines carrying 41 positional reads across `digests.py` (15), `web.py` (10),
  `origin_reports.py` (6), `cli.py` (4), `inbound.py` (4), `time_to_value.py` (2); plus 7 more inside Jinja
  templates (`templates/confirm_label.html:8`, `templates/label_recorded.html:8`, `templates/index.html:20-21`).
  **File-count estimate: 14.**
- **Score**: **17/25** (leverage 4, locality 3, blast radius 4, heat 4)
- **Problem**: no `row_factory` is set anywhere in `src/` or `tests/`, so every database read is by ordinal.
  `origin_reports.py:72-76` reads `row[1]`..`row[4]` as four `int(...)`s — `item_count`, `worth_it`,
  `not_worth_it`, `unlabelled`. Transposing indices 2 and 3 inverts the report's entire ranking
  (`origin_reports.py:135`) and every "X% not worth it" string (`:144`) while raising nothing. `web.py:233-242`
  reads eight indices of which five are interchangeable strings. Three templates index raw sqlite tuples
  from inside HTML, beyond the type-checker's reach entirely.
- **Deletion test**: this one is closer to *moves* than *concentrates* — `sqlite3.Row` names the columns but
  the projection knowledge stays spread across the same modules. Hence locality 3.
- **Solution**: `row_factory = sqlite3.Row` plus named access; templates receive mapped objects.
- **Benefits**: *leverage* — a class of silent transposition bugs becomes impossible; but the blast radius
  crosses into templates, which the type-checker does not see, so a single unattended PR cannot verify it all.

**Before**

```mermaid
graph LR
  Q[SELECT a,b,c,d] --> R[tuple]
  R --> P1[row 1 in origin_reports]
  R --> P2[row 2 in origin_reports]
  R --> P3[item 0 in template]
```

**After**

```mermaid
graph LR
  Q[SELECT a,b,c,d] --> R[named row]
  R --> P1[row.worth_it]
  R --> P2[row.not_worth_it]
  R --> P3[item.title]
```

## Dropped

| Candidate | Dropped because |
|---|---|
| `time-to-value-sort-key-removal` | Leverage 1 — `time_to_value_sort_key` (`time_to_value.py:12-14`) has zero production callers and is pinned only by `tests/test_time_to_value.py:12,63`; the ordering it encodes is independently re-spelled as SQL at `digests.py:288-290`. Deleting it removes dead code but concentrates nothing, so it fails the deletion test as a *deepening*. Worth a tidy-up commit by a human; not a candidate here. |
| `digest-span-map-query-count` | Leverage 2 — `digests.py:308` calls `get_span_map`, which issues two queries per row (`span_maps.py:163-184`), making digest selection `1 + 2N` queries inside `delivery.py:139`'s `BEGIN IMMEDIATE`. Real, but it is a query-count problem *inside* an existing seam; fixing it does not change any interface, so it is a performance fix rather than a deepening. |
| `error-response-wrapper-collapse` | Leverage 2 — `label_error` (`web.py:137-142`) and `item_error` (`:144-149`) are 6-line pass-throughs to `_error_response` (`:127-135`) differing by one string literal. Collapsing them shrinks the interface but callers do exactly the same work, and the status codes stay ad hoc at each of the 13 call sites. Cosmetic. |
| `strftime-timestamp-literal` | Leverage 2 — `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` appears 26 times across `src/modgud/`, but the great majority are `DEFAULT` clauses in the eleven migration files, which are immutable history and must not be edited. The handful of Python-side occurrences are not enough to justify a seam. |

## Too large to automate

| Candidate | Why |
|---|---|
| `capture-url-pipeline-decomposition` | Blast radius 5. `capture_url` (`cli.py:241-558`) is 318 lines, 55 branches, 42 distinct locals and 5 return paths — 40% of `cli.py` in one function. It opens **three** connections (`:254`, `:417`, `:548`) with three independent commit points, so a failure in the summarize phase cannot undo the item row committed at `:543`. It performs four `blob_store.put` calls (`:327,358,376,389`) *before* the write transaction opens, and two return paths (`:419-422`, `:433-453`) abandon those bytes on disk. Its pre-fetch dedupe read at `:259-262` runs under no explicit transaction and is re-checked under `BEGIN IMMEDIATE` at `:423-432` one or two network fetches later. No test imports it: 22 of its tests shell out via `subprocess.run(["modgud", ...])`. Decomposing it means re-deciding three transaction boundaries and the CLI's stdout contract in one diff — that is a human-scheduled program of work, not one unattended PR. Two of its sub-problems *are* separately automatable and are scored above as `capture-extraction-outcome-record` and `event-log-writer-seam`. |
| `blob-orphan-reaper` | Blast radius 5 and, more decisively, it is a new subsystem rather than a deepening. Six call sites pair a `blob_store.put` with a later row write across a transaction boundary (`cli.py:327,358,376,389`, `audio_fallbacks.py:104`, `podcast_transcripts.py:197`), and no garbage collection of `blobs/` exists anywhere in `src/`. Bounded severity — `BlobStore.put` is content-addressed and idempotent (`blobs.py:15-40`), and `items.content_hash` is `NOT NULL UNIQUE`, so the failure mode is orphaned bytes, never a dangling reference — but designing a reaper is a feature, and features are not this skill's remit. |

## Pick

**`event-log-writer-seam`, 23/25.**

The runner-up **candidate** is `capture-extraction-outcome-record` at 22/25 — **within one point**, so the
pick was close and the runner-up is the natural next firing. The two differ almost entirely on reach: both
name something the code currently leaves anonymous, but the outcome record pays back inside one function
(leverage 4, blast radius 1) while the event writer pays back at sixteen call sites in six modules
(leverage 5, blast radius 3). The rubric doubles leverage precisely to prefer the wider payback, and the
inverted blast-radius term is not enough to close a two-point leverage gap.

Two further points favoured the pick on the evidence rather than the arithmetic. First, the event log is
already read by three modules (`origin_reports.py`, `digests.py`, `web.py`) and written by six, so it is the
one concept in this tree whose absence of an owning module is felt from both sides. Second, its duplication
has **measurably drifted** — `summaries.py:214` encodes with `ensure_ascii=False` and the other fifteen sites
do not, `web.py:485` drops `sort_keys` — which is the signal that a seam is overdue rather than hypothetical.
The outcome record is, by contrast, still internally consistent; it is friction, not yet drift.

`capture-extraction-outcome-record` is also partly *unblocked* by this pick: with the recorder helpers behind
one writer, the second reader of the thirteen sentinels (`cli.py:488-537`) shrinks to a dispatch over event
types, which makes naming the record a smaller and safer next change.

## Design

Written at step 4; see below.
