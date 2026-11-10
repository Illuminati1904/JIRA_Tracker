# JIRA Tracker: Scope Change Sentinel accelerator

Turns a Jira dump into an interactive scope-change report, and **remembers every story** between runs: what changed,
when, how sure we are, and which decisions from earlier reports were acted on.

It is the *Scope Change Sentinel* skill rebuilt as a Python accelerator, with the snapshot files replaced by an
event-sourced memory and a Claude-maintained knowledge layer on top. Read [`docs/DESIGN.md`](docs/DESIGN.md) for the
architecture, trade-offs and open questions.

```
Jira dump (CSV or REST JSON) ──► memory (SQLite event ledger) ──► analysis (any time window)
                                      │                               │
                                      ▼                               ▼
                       story dossiers + project memory      Claude: judge · summary · actions
                       (Managed Agents memory store)                  │
                                                                      ▼
                                                        single-file interactive HTML report
```

## Quick start

Python 3.11+.

```bash
pip install -e .            # installs `sentinel` (needs the `anthropic` and `pydantic` packages)
sentinel demo               # synthetic project, deterministic report, nothing leaves your machine
```

`sentinel demo` writes `demo_data/` and a report under `demo_data/home/reports/`. Add `--llm` to also call Claude (set
`ANTHROPIC_API_KEY` first; use it on synthetic data only).

## Daily use

```bash
# first time: agree the baseline date and ingest the dump
sentinel run export_2026-10-26.csv --since 2026-10-26

# every day after: just the new dump. Nothing to keep, nothing to re-upload.
sentinel run export_2026-11-04.csv
```

* **Rolling** (default): compares today's dump with the previous one.
* **Fixed baseline**: `sentinel report --since 2026-10-26` shows cumulative drift from that date. Because history is a
  log, you can ask for any window at any time.
* **Offline**: add `--no-llm` for deterministic analysis and narratives; nothing is sent anywhere.
* `sentinel history ABC-123` prints a story's dossier: status, sign-off, and every scope change with its timestamp and
  confidence.

### Which input gives which quality

| You give it | Result |
|---|---|
| **REST JSON with `expand=changelog,names`** | Exact history from the day each story was created, on the very first run |
| CSV, first dump only | Inferred from comments and timestamps (the skill's old "audit mode"); never fabricates before/after text |
| CSV, second dump onwards | Observed: exact text diffs, change times bounded by the two dump times |
| CSV first, JSON later | Snapshot guesses are superseded by the changelog automatically |

Run `sentinel doctor <file>` before ingesting: it lists columns that degrade the analysis and statuses missing from the
workflow.

### Getting a REST JSON dump

Jira Cloud: `GET /rest/api/3/search/jql?jql=<your filter>&fields=*all&expand=changelog,names&maxResults=100`, following
`nextPageToken` until done, and saving the pages or the issues to one `.json` (or `.jsonl`). Jira Data Center:
`/rest/api/2/search` with `startAt`. Search responses cap the changelog at 100 histories per issue; the tool detects
truncation and warns. *(Written from Jira's documented schema; not yet run against your tenant.)*

## Commands

| Command | Purpose |
|---|---|
| `sentinel ingest FILE` | Load a dump into memory (`--label`, `--taken-at`, `--partial` for filtered exports) |
| `sentinel report` | Analyse a window and build the HTML (`--since DATE`, `--since-run LABEL`, `--no-llm`, `--budget-usd`) |
| `sentinel run FILE` | Ingest and report in one step |
| `sentinel history KEY` | A story's dossier |
| `sentinel actions [close ID]` | Decisions raised in reports and whether they are still open |
| `sentinel memory init \| pull \| rebuild \| status` | Create the managed store, refresh the local mirror, rebuild dossiers from the ledger, show state |
| `sentinel doctor FILE` | Check a dump |
| `sentinel demo` | Synthetic end-to-end run |

Global option: `--home DIR` (workspace; default `./sentinel_home` or `$SENTINEL_HOME`). Configuration:
[`sentinel.toml.example`](sentinel.toml.example).

## Memory

* **Ledger** (`<home>/sentinel.sqlite`): append-only events, text blobs, runs, judgements, decisions, LLM spend. Source of
  truth.
* **Dossiers** (`/stories/<KEY>.md`): facts written by code, understanding written by Claude.
* **Project memory** (`/project/*.md`): patterns, glossary, decisions; curated by Claude through the memory tool,
  write-restricted to that folder.
* Backends: `local` (default, files under `<home>/memory`) or `managed` (a Claude Managed Agents memory store with a
  local mirror). Switch with `sentinel memory init` then `memory_backend = "managed"`.

## Privacy defaults

Raw Jira text stays in the local SQLite file. Before anything reaches the Claude API or a hosted memory store,
account ids and @mentions become pseudonyms, e-mails/URLs/secrets are masked, display names are never sent, and
`redact_terms` masks client names. `.gitignore` excludes the workspace, dumps, SQLite files and reports. **Never commit
client Jira data to a personal GitHub repository.**

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests          # 55 tests, no network, no API key
node scripts/ui_check.js <report.html> <out-prefix>          # headless Chromium: no JS errors in a US time zone
```

The Claude layer is tested against a scripted fake SDK and has also been run live on synthetic data (judge, summary, memory
tool). Creating a Managed Agents memory store must be done from your own machine: the API refuses memory-store writes from
Anthropic-hosted sandboxes with restricted network access. See "What is verified" in the design doc.
