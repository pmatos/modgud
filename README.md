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

Adding a web page extracts and summarizes it immediately. Regenerate the
current tier-1 artifact for an extracted web item by id when changing models or
prompts:

```console
uv run modgud summarize 42
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
interval, digest send time, web bind host and port, and label-link token
lifetime. A hosted model route sets `api_key_env` to an environment-variable
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
nullable `extracted_text_hash` references extracted text when available.
`duration_seconds` retains audio/video metadata, while the nullable
`time_to_value_seconds` stores the digest-ready estimate. Text estimates use
the extracted text at 200 words per minute; audio/video estimates use duration,
and missing inputs leave the estimate unknown. The current tier-1 artifact is
stored in `tier_1_summaries` as a separate one-liner and JSON claim array;
regeneration replaces that row while append-only events retain generation
history.

## Shape

- **In**: CLI, a LAN web drop box, email (Postmark inbound, polled).
- **Through**: format-specific extraction, then transcription and
  summarization against any OpenAI-compatible endpoint — local by default.
- **Out**: a morning digest that is complete on its own, plus a web UI for
  digging deeper.
- **Kept**: SQLite. Raw transcripts, summaries, and your own worth-it labels,
  in open formats that outlive any model choice.
