# modgud — design

Settled through a design interview. This records decisions and the reasoning
that produced them, including the ones that were reversed.

## The core reframing

The original premise was that summarization is a commodity and a *calibrated
verdict* — learned from past accept/reject decisions — is the product.

That premise does not survive contact with how content actually arrives here:

- Items enter the system **only because they were deliberately dropped in**.
  There is no subscription firehose.
- The failure being solved is a **backlog** — things sent to self and never
  read — not misjudgment about what to read.

If every item was already chosen, a model that predicts "is this worth it" is
second-guessing a decision just made with more context than the model has, and
its training sample is selected on the outcome variable. So:

**The summary and the span map are the entire product.** The difficulty is in
capture and delivery, not scoring.

Dropped as a result: the stage-1 classifier, embeddings over synthetic
abstracts, per-source priors, score thresholds, active learning, shadow mode,
and any exploration budget. These return only if subscription feeds are ever
ingested — a change that would reintroduce a base rate worth filtering.

## Inlets

| Inlet | Purpose |
|---|---|
| CLI | Desktop capture, scripting, backfill |
| Web drop box | LAN only; one field, one button |
| Email | Postmark inbound, retrieved via the Messages API |

Email is polled rather than pushed. Postmark's Messages API can search and
retrieve inbound messages, so **no public endpoint, webhook, tunnel or exposed
port is required** — the workstation only reaches out. Poll every 2 minutes.

Email is also the away-from-home capture path, which is why remote access to
the web UI is not needed.

Ingestion is idempotent. Canonical URL is the primary identity (strip tracking
params, resolve `youtu.be`, drop YouTube `t=`, normalize trailing slashes);
content hash is a secondary key, catching the same PDF arriving from two URLs.

Some things have no canonical URL. A podcast episode is identified by its feed
and its GUID, not by a page. Rather than add a second identity column, these get
a **synthetic canonical form** — `podcast:<feed-hash>/<guid>` — so one unique
constraint covers everything and dedup logic stays uniform.

Dropping a **feed** URL captures its latest episode. It does not subscribe: a
feed that keeps producing items would be the subscription firehose this system
explicitly does not ingest, and would reintroduce the base rate that made the
classifier pointless.
Re-dropping a known item **logs a second capture event** — that is a signal,
not a no-op — and returns the existing summary immediately.

## Extraction

Every format is accepted and stored from day one. Nothing is ever rejected: a
rejected inlet teaches you not to use the system.

Optimization is tiered by build effort, not by cost:

- **Web** — readability extraction.
- **YouTube** — `yt-dlp` captions first, falling back to transcribing the audio
  stream.
- **Papers** — title + abstract; they already ship the thing this system
  synthesizes.
- **Podcasts, PDFs, decks** — generic path initially.

### YouTube captions are not free

`yt-dlp` caption extraction now runs into YouTube's proof-of-origin token
requirement; unauthenticated clients increasingly get "Sign in to confirm
you're not a bot." The workaround is `--cookies-from-browser`, and the browser
whose cookie store is reliably readable is Firefox — which is not installed
here.

So the design **inverts the original assumption**: transcription is the
reliable path and captions are a cheap optimization attempted first. When
captions fail, pull audio and transcribe. This converts an ongoing maintenance
treadmill into a compute cost.

## Models: one config, four tasks

Every model call — transcription, tier-1 summary, span map, and title/format
cleanup — goes through the **OpenAI-compatible protocol**. A config table maps
each task to `{base_url, model}`.

Local is the default:

- **Transcription**: whisper.cpp server, built with the Vulkan backend (this
  machine is AMD Strix Halo — no NVIDIA). Run with
  `--inference-path /v1/audio/transcriptions` so it speaks the same protocol as
  everything else.
- **Text**: `gemma4:26b-a4b` via ollama. A MoE with ~4B active parameters, so
  fast on 124 GiB of unified memory even without strong GPU acceleration.

Hosted endpoints (Fireworks and any other OpenAI-compatible provider) are
additional rows in the same table, used where local proves inadequate.

This is deliberately **not** a primary/fallback arrangement. There is no
branching code, no second quality bar to maintain, and no vendor dependency to
escape — moving a task between endpoints is a config edit. The durability
guarantee is not provider choice; it is that raw transcripts are persisted, so
re-summarizing under any future model never requires re-fetching or
re-transcribing.

### Long inputs

A transcript can exceed the context window of whatever model a task is routed
to. Tier-1 summarization of long inputs reuses the same timestamped chunking
built for span maps: summarize per chunk, then combine. There is one chunking
mechanism in the system, not two.

### Span maps must not ask the model for timestamps

Asking a model to read a 35k-token transcript and emit accurate timestamps
fails, and fails *silently* — you jump to 23:40 and it is the wrong moment.

Instead: chunk the transcript with timestamps attached structurally, and have
the model **select and describe chunks**. Timestamps are then carried by code
and cannot be hallucinated. This is what makes a small local model adequate for
the highest-value output in the system.

## Output

**Tier 1** (always, inline in the digest):

- Source and a one-line what-it-is
- 3–5 bullets stating the claims the item actually makes
- For audio/video: a timestamped span map, one line per span

**Tier 2** (on demand): full transcript and long-form summary, via a per-item
link into the web UI.

The five-verdict taxonomy (`skip` / `summary-only` / `skim` / `partial` /
`full`) is dropped. Those are judgments that are no longer needed — the item
was already chosen, and the span map already says where the value is. Only the
`partial` idea survives, reframed as a map rather than a verdict.

### The digest

- Delivered each morning.
- **Complete on its own.** Every tier-1 artifact renders inline. Reading the
  digest and never clicking through is a legitimate, complete use of the
  system. A digest that is only a list of links rebuilds the graveyard with
  better typography.
- Ordered shortest-time-to-value first, computed from extracted text (not raw
  markup) for text items and from duration for audio and video.
- The boundary is the **last successful send**. Selection is not persisted
  separately: a digest that fails to send leaves no `digest_sent` event, so the
  next run selects the same items again. This is deliberate — a separate
  "selected" marker would permanently skip items whenever a send failed.
- ~10 items inline; the remainder as one-liners.
- Overflow is **discarded, not carried forward** — otherwise a busy week
  compounds into a backlog inside the tool meant to cure backlogs.
- Nothing is sent on an empty day. A digest that sometimes does not arrive
  stays meaningful; one that always arrives becomes wallpaper.

## Feedback

One-click 👍/👎 links per item in the digest, collecting the label at the moment
the opinion forms. Must be POST-backed behind a confirmation page — email
clients prefetch URLs and would otherwise forge labels.

The label is **retrospective worth-it**, on items actually engaged with.
Engagement is logged as a covariate but never treated as ground truth: once
output is acted on, engagement is caused by the output, and a model trained on
it confirms its own bias.

Skip-regret is deliberately **not** collected. It is structurally
unobservable — you cannot know you regretted skipping something unless an
outside channel later tells you it was good, which means the signal only
arrives for items with high external salience. That is a biased subsample, not
the hard negatives it appears to be.

Origin — where an item *reached* you, as distinct from its content source — is
recorded where the inlet reveals it (email headers). CLI and web drops record
`manual`. The eventual payoff is a report on which inbound sources generate
items never valued. **No report is built until the data earns it**; if capture
turns out to be mostly CLI and web, the field should be cut.

Unlabeled items are their own category and are never imputed. Labels are not
missing at random — labeling is skipped exactly when busy or when the item was
forgettable, which correlates with the target.

## Storage

SQLite. An `items` table plus an **append-only `events` log** for captures,
verdicts, and labels.

Karakeep was considered and rejected. It is an active, well-built project with
a full REST API, but adopting it means running and upgrading a Next.js +
Drizzle + Meilisearch + Puppeteer application, its schema has no place for span
maps or worth-it labels (they would become sidecar tables keyed by its IDs),
and its reading UI would sit unused beside the one being built here. The
browser extension it would have provided is replaceable with a bookmarklet.

Storage formats stay open and greppable. The labels must outlive any model,
embedding, or implementation choice.

## Item lifecycle

Every item carries exactly one state. Downstream surfaces (the digest, the item
list, the CLI) branch on it, so it is defined once here rather than invented per
ticket:

| State | Meaning |
|---|---|
| `captured` | Raw content stored; nothing processed yet |
| `extracted` | Readable text or a transcript is available |
| `summarized` | A tier-1 artifact exists; eligible for the digest |
| `unsummarizable` | Accepted and stored, but no summarization path exists for this format yet |
| `failed` | Extraction or summarization errored; the error is recorded |

State transitions are recorded as events. `failed` and `unsummarizable` are both
digest-visible: an item that could not be processed still appears as a
capture-only line, because silently dropping it reproduces the graveyard.

## Configuration and secrets

One config file holds everything an operator sets: the model routing table, the
inbound poll interval, the digest send time, the web app's bind address, and the
label-link token lifetime. Secrets — Postmark tokens, any hosted-provider API
keys — come from the environment and are never written to the config file or the
database.

Malformed or missing configuration fails loudly at startup rather than part-way
through a batch.

## Runtime

- Python throughout, managed with `uv`. `yt-dlp` is callable in-process, and
  the extraction ecosystem (readability, PDF, feeds) is strongest here.
- FastAPI with server-rendered templates. No build step.
- **Web pages** are processed on arrival — seconds of work, and the case where
  the headline is most often wanted immediately.
- **Audio/video** runs in a 03:00 batch under `nice -n 19` and `ionice`, so it
  never competes with the working day. Load-triggered scheduling was rejected:
  it produces behaviour that cannot be predicted or debugged.
- Scheduling is external: **systemd timers** invoking CLI subcommands, not an
  in-process scheduler. One timer for the 03:00 batch, one for the inbound poll,
  one for the digest at 07:00. The process stays crash-safe and restartable, and
  every scheduled action is runnable by hand.

## Build order

1. **Email inlet + CLI → web pages only → summary → morning digest.**
   Deliberately minimal. It answers the only question that matters: *does the
   digest actually get read?* If it does not, extraction pipelines, span maps
   and label logs are all effort spent on a system that will not be opened.
2. **YouTube.** Top format by volume, and where summaries save the most time.
   Build whisper.cpp, implement span maps. Compare local against a hosted
   endpoint on ten real long items — judged on timestamp accuracy and claim
   faithfulness — and set the routing table from the result.
3. **Web UI** for tier 2 and history. Third, because until the digest works it
   has nothing worth displaying.
4. **Labels**, and the origin report only if the data justifies it.

## Non-goals

- Discovery or recommending new sources. This triages things already found.
- Note-taking, highlighting, spaced repetition.
- Multi-user, sharing, collaboration.
- A mobile app. Email is the mobile capture path.
- Replacing a podcast player.

## Known risks, accepted

- **The digest could become the new graveyard.** This is the same failure being
  cured, one layer up. Milestone 1 exists to detect it within a week rather
  than after everything is built.
- **Local long-context span mapping may disappoint.** Hedged by the chunking
  design and the routing table.
- **The label log has no v1 consumer** beyond a report that may never be built.
  Justified only by its one-keystroke cost.
