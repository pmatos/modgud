# modgud

A personal content triage system. Drop anything in — a URL, a video, a podcast,
a PDF — and get back a summary, the claims it makes, and for audio/video a
timestamped map of where the value actually is.

Named for Móðguðr, who guards the bridge and asks who you are before letting
you across.

## Status

Pre-implementation. See [DESIGN.md](DESIGN.md) for the settled design and
[milestone 1](DESIGN.md#build-order) for what gets built first.

## Development

Install the project and its development tools with uv:

```console
uv sync
```

The installed command exposes its available options through standard CLI help:

```console
uv run modgud --help
```

Adding a web page extracts and summarizes it immediately. Generate the tier-1
artifact for a stored YouTube transcript, or regenerate any supported extracted
item after changing models or prompts, by id:

```console
uv run modgud summarize 42
```

For an audio/video item with a stored transcript, generate or regenerate its
timestamped map through the separately configured span-map route:

```console
uv run modgud span-map 42
```

Retrieve inbound email with the same one-shot command used by the systemd
service:

```console
POSTMARK_SERVER_TOKEN=... uv run modgud poll-inbound
```

The command does not run a loop or expose a listener. A systemd timer starts it
periodically, and the command uses `[inbound].poll_interval_seconds` from the
operator config to skip timer ticks that arrive before the next poll is due.
Configure the timer's wake-up cadence at or below that interval. For an
operator-requested poll regardless of the last successful run, use
`modgud poll-inbound --force`.

Each poll searches processed inbound messages, retrieves the full details for
unseen Postmark message IDs, and places their JSON in the durable SQLite inbound
queue. The message-ID primary key acts as a durable cursor, so restarts and
overlapping searches do not enqueue the same email twice. Search and detail API
calls retry with bounded exponential backoff; the last successful poll time is
only advanced after the complete search succeeds.

Queued mail is resolved deterministically: the first absolute HTTP(S) URL in
the plain-text body wins, with HTML link/text order used only when plain text
contains no usable URL. A forwarded envelope's sender is the capture origin;
otherwise the message's `List-Id` or sender mailbox is used, in that order.
The content's own site remains the item's separate `source`. Messages with no
usable URL stay in the inbound table with nullable URL and origin fields and a
processed timestamp. Usable URLs run through the same item pipeline as
`modgud add`, while their capture events record the origin and Postmark message
ID for provenance.

## Local transcription

The default transcription route is a local whisper.cpp server at
`http://127.0.0.1:8080/v1/audio/transcriptions`. On this Arch Linux machine,
install the compiler, Vulkan, and audio-conversion prerequisites with:

```console
sudo pacman -S --needed cmake ffmpeg gcc git glslang make ninja shaderc \
  spirv-headers vulkan-headers vulkan-icd-loader vulkan-radeon
```

Build the v1.9.3 server with the cross-vendor Vulkan backend and download the
configured production model:

```console
whisper_root="${XDG_DATA_HOME:-$HOME/.local/share}/modgud/whisper.cpp"
mkdir -p "$(dirname "$whisper_root")"
git clone --depth 1 --branch v1.9.3 \
  https://github.com/ggml-org/whisper.cpp.git "$whisper_root"
cmake -S "$whisper_root" -B "$whisper_root/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_VULKAN=ON \
  -DWHISPER_BUILD_SERVER=ON
cmake --build "$whisper_root/build" --target whisper-server -j "$(nproc)"
bash "$whisper_root/models/download-ggml-model.sh" \
  large-v3-turbo "$whisper_root/models"
```

These commands were exercised on the AMD Radeon 890M/RADV host. The first
server line should name that Vulkan device; `use gpu = 1` and `using Vulkan0
backend` during model loading confirm that inference did not fall back to the
CPU backend.

Copy `config.example.toml` as described under [Configuration](#configuration),
then run the server in the foreground from this checkout:

```console
uv run modgud whisper-server
```

`modgud` takes the bind host and port from `[models.transcription].base_url`
and launches whisper.cpp with `--inference-path /v1/audio/transcriptions`,
automatic language detection, and ffmpeg conversion enabled. It takes the
checkout, weights, and compute settings from the same config file:

```toml
[whisper_cpp]
root = "~/.local/share/modgud/whisper.cpp"
model_size = "large-v3-turbo"
threads = 12
```

To select another size, use `download-ggml-model.sh` with that name, update
`model_size`, and restart the service. The launcher resolves it to
`<root>/models/ggml-<model_size>.bin` and reports the missing path instead of
starting if the model was not downloaded. Change `threads` to any positive
integer to tune CPU-side work. The protocol-level model remains `whisper-1` in
`[models.transcription]`; the local weights are selected by `model_size`.

The checkout includes `samples/jfk.wav`, a known 11-second fixture. With the
server running, exercise the exact routed OpenAI client used by modgud:

```console
uv run python - <<'PY'
from modgud.config import get_settings
from modgud.models import create_model_client

settings = get_settings()
routed = create_model_client("transcription", settings=settings)
try:
    with (settings.whisper_cpp.root / "samples/jfk.wav").open("rb") as audio:
        result = routed.client.audio.transcriptions.create(
            model=routed.model,
            file=audio,
        )
    print(result.text)
finally:
    routed.client.close()
PY
```

The result should contain “ask not what your country can do for you”. This
verifies the routed base URL, multipart OpenAI request, configured inference
path, model load, and transcription response end to end.

For a persistent per-user endpoint, install modgud and the committed service:

```console
uv tool install .
mkdir -p ~/.config/systemd/user
cp systemd/modgud-whisper.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modgud-whisper.service
systemctl --user status modgud-whisper.service
```

The service is sandboxed, restarts after failures, reads the same default
config file, and exposes only the address selected by the transcription route.
Stop and disable it before changing that route to a hosted provider.

## Scheduled audio/video processing

YouTube capture attempts captions without downloading media. When YouTube
refuses that request, the item stays queued in `captured`; it does not download
audio or call transcription on the interactive `modgud add` path. Run the
queued fallback by hand with:

```console
uv run modgud batch
```

For podcasts, the same batch first fetches the latest episode's
`<podcast:transcript>`. It accepts WebVTT, SRT, Podcast Namespace JSON, HTML,
and plain text, preferring WebVTT when the feed offers several formats and
normalizing the selected transcript to WebVTT. When no usable creator
transcript is available, it transcribes the episode's audio enclosure instead.

Audio fallback downloads each queued item's audio stream into an isolated
temporary directory, asks the configured `transcription` route for timestamped
WebVTT, persists only that transcript, and removes the audio. A failed item is
recorded without stopping later items in the same batch.

Install the committed user timer to run this work at 03:00 with the lowest CPU
and I/O priorities:

```console
uv tool install .
mkdir -p ~/.config/systemd/user
cp systemd/modgud-batch.service systemd/modgud-batch.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modgud-batch.timer
systemctl --user list-timers modgud-batch.timer
```

Every attempt records an `audio_fallback` event whose `outcome` is
`transcribed` or `failed`. The attempt rate across captured YouTube items is
therefore directly measurable, for example:

```sql
SELECT round(
    100.0 * count(DISTINCT events.item_id) / count(DISTINCT items.id),
    1
) AS fallback_percent
FROM items
LEFT JOIN events
    ON events.item_id = items.id
   AND events.type = 'audio_fallback'
WHERE items.format = 'youtube';
```

Each successfully extracted podcast records a `podcast_transcript` event with
`source` set to `feed` or `audio`; feed events also retain the selected media
type. This keeps the path taken queryable per episode without tying the storage
model to a specific transcription provider.

## Digest delivery

Send the current digest immediately for a demo or manual run:

```console
POSTMARK_SERVER_TOKEN=... uv run modgud digest --now
```

Eligible items are rendered into HTML and plain-text bodies and submitted to
Postmark's outbound message stream. An empty selection submits no request. A
successful request appends one `digest_sent` event whose payload contains the
complete item ID set and Postmark message ID; an exhausted request failure
leaves the selection and event boundary unchanged for the next attempt.

For scheduled delivery, install the committed user units and put the Postmark
server token in the service environment:

```console
uv tool install .
mkdir -p ~/.config/systemd/user ~/.config/modgud
cp systemd/modgud-digest.service systemd/modgud-digest.timer \
  ~/.config/systemd/user/
printf 'POSTMARK_SERVER_TOKEN=replace-me\n' > ~/.config/modgud/environment
chmod 600 ~/.config/modgud/environment
systemctl --user daemon-reload
systemctl --user enable --now modgud-digest.timer
```

The timer starts the one-shot `modgud digest` command once per minute; the
command sends only at or after `[digest].send_time` in the machine's local time
and completes that local day after either one successful send or an empty
selection. This keeps the TOML setting authoritative and lets a failed send be
retried on the next timer tick. `--now` bypasses the clock and daily schedule
gate, but still sends nothing when the selection is empty.

## Configuration

modgud reads one TOML file at process startup. Copy the committed example to
the default per-user location before running a command:

```console
mkdir -p ~/.config/modgud
cp config.example.toml ~/.config/modgud/config.toml
```

The default follows `XDG_CONFIG_HOME` when it is set and otherwise uses
`~/.config/modgud/config.toml`. Pass `--config /path/to/config.toml` to use a
different file. The CLI loads it through `modgud.config.get_settings`, the
shared startup seam for scheduled batch commands and the web app. The function
validates and caches the file once per process, so those entrypoints consume
one settings object rather than maintaining separate configuration paths.

The file controls the four OpenAI-compatible model routes, inbound-mail poll
interval, digest send time and addresses, web bind host and port, and
label-link token lifetime. `digest.from_address` must be a confirmed Postmark
sender signature; `digest.to_address` is the personal inbox that receives the
digest. A hosted model route sets `api_key_env` to an environment-variable
name; it never contains the key itself. For example:

```toml
[models.tier_1_summary]
base_url = "https://provider.example/v1"
model = "provider-model-name"
api_key_env = "HOSTED_PROVIDER_API_KEY"
```

Export `HOSTED_PROVIDER_API_KEY` in the service environment. Postmark features
read `POSTMARK_SERVER_TOKEN` (and `POSTMARK_ACCOUNT_TOKEN` when needed) from
the environment as well. Secret values are held in redacting wrappers in
memory; they are not part of the TOML schema, database schema, or diagnostic
output. If a route names an unset environment variable, startup fails and
names the missing variable without printing a value.

Model callers use the same `modgud.models.create_model_client` factory for all
four tasks. It returns the OpenAI-compatible client and configured model as one
routed value. The factory supplies local endpoints with a non-secret placeholder
credential because the OpenAI client requires one; hosted routes instead use
the secret captured from the row's `api_key_env`. No caller branches on the
provider or endpoint.

Run the same quality gate used by CI:

```console
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Storage

`BlobStore` keeps fetched bytes and UTF-8-encoded extracted text beneath its
root as `sha256/<first-two-hex>/<full-sha256>`. The full digest remains visible
in every filename, while the two-character directory keeps large stores easy
to browse. On each SQLite item, `content_hash` references the raw blob and the
nullable `extracted_text_hash` references extracted text or a timestamped
transcript when available.
`duration_seconds` retains audio/video metadata, while the nullable
`time_to_value_seconds` stores the digest-ready estimate. Text estimates use
the extracted text at 200 words per minute; audio/video estimates use duration,
and missing inputs leave the estimate unknown. The current tier-1 artifact is
stored in `tier_1_summaries` as a separate one-liner and JSON claim array;
long transcripts are summarized through the shared timestamped chunker before
their chunk summaries are combined. Regeneration replaces the artifact row
while append-only events retain generation history.

## Shape

- **In**: CLI, a LAN web drop box, email (Postmark inbound, polled).
- **Through**: format-specific extraction, then transcription and
  summarization against any OpenAI-compatible endpoint — local by default.
- **Out**: a morning digest that is complete on its own, plus a web UI for
  digging deeper.
- **Kept**: SQLite. Raw transcripts, summaries, and your own worth-it labels,
  in open formats that outlive any model choice.
