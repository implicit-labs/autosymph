# autosymph

autosymph is a local agent orchestrator for Linear-backed engineering queues.
It watches Linear issues, creates isolated git worktrees, dispatches coding
agents with state-specific prompts, and moves work through an
implement -> verify -> review -> finalize lifecycle.

It is designed for teams that want repeatable agent workflows instead of
one-off chat sessions: clear states, explicit prompts, durable logs, packaged
skills, and human review gates where they matter.

## What It Does

- Polls Linear for actionable issues.
- Creates isolated worktrees for each issue.
- Dispatches Claude Code, Codex, or Pi runners.
- Uses repo-local prompts for each workflow state.
- Runs multiple projects from one supervisor.
- Shares local resources such as simulator slots and dev ports.
- Records raw agent output, run metadata, summaries, and status.
- Ships reusable skills for verification, finalization, monitoring, and
  evidence handling.

autosymph does not replace your CI, review process, or project-specific test
knowledge. It gives those things a predictable harness.

## How It Works

autosymph uses a layered config model:

```text
~/.autosymph/config/
  devices/
    {hostname}.yaml        # this machine: repos, resources, concurrency
  projects/
    {project-slug}.yaml    # one project: Linear states, prompts, runners
```

At startup, autosymph finds the device config for this host, loads each listed
project config, merges them, and starts one orchestrator per project.

Each workflow state is one of:

| Type | Meaning | Examples |
|---|---|---|
| `agent` | Dispatch a runner with a prompt | `implement`, `verify`, `finalize` |
| `gate` | Wait for a human action in Linear | `review` |
| `terminal` | Stop tracking the issue | `done` |

The default example workflow is:

```text
Ready -> implement -> verify -> review -> finalize -> done
                         |         |
                         v         v
                      rework <-----+
```

Low-risk issues can skip the review gate if your config allows it.

## Requirements

Core autosymph needs only:

| Tool | Why |
|---|---|
| Python 3.11+ | autosymph runtime |
| `uv` | environment and command runner |
| Linear API key | issue polling and state transitions |
| At least one runner | Claude Code, Codex, or Pi |

Install the basics:

```bash
brew install uv
uv sync --extra dev
export LINEAR_API_KEY=lin_api_...
```

Optional platform tools are only needed when a project's verification plan uses
that platform:

| Optional path | Tools |
|---|---|
| iOS verification | macOS, Xcode, `xcrun`, `fb-idb`, `idb_companion` |
| Web verification | Node.js, Playwright browsers |
| Braintrust tracing | `autosymph[tracing]`, `BRAINTRUST_API_KEY` |

Braintrust is BYOK: install the tracing extra and use your own Braintrust
project/API key if you want eval-loop tracing.

## Quick Start

From a fresh checkout:

```bash
uv sync --extra dev

# Install or inspect skills.
scripts/install-skills.sh --target "$HOME/.autosymph/skills"

# Run the interactive onboarding wizard. Validates your Linear API key,
# offers to create any missing workflow states, detects iOS simulators,
# and writes ~/.autosymph/config/{devices,projects}/*.yaml + local.env.
uv run autosymph init

# Run.
uv run autosymph start
```

The wizard handles the common case (fresh device, single project). For
manual setup or air-gapped environments, see [docs/manual-setup.md](docs/manual-setup.md).

For a one-off config file:

```bash
uv run autosymph start -c path/to/project.yaml
```

## Linear Setup

Create a Linear API key and expose it as `LINEAR_API_KEY`.

Optional labels:

| Label | Purpose |
|---|---|
| `runner:codex` | Route an issue through the Codex runner. |
| `runner:pi` | Route an issue through Pi when that runner is configured. |
| `needs-review` | Used by the autoplan review fallback flow when enabled. |
| `risk:low` | Lets configured workflows skip the human review gate. |

Linear status names are configurable in each project YAML. The examples use:
`Ready`, `Implementing`, `Verifying`, `In Review`, `Merging`, `Rework`, and
terminal states `Done`, `Canceled`, `Duplicate`.

## Config Examples

Packaged examples live in `examples/config/`:

```text
examples/config/
  devices/hostname.yaml.example
  projects/ios-project.yaml.example
  projects/web-project.yaml.example
  local.env.example
```

The examples are templates, not required harness setup. Use the web example for
a minimal browser/server project. Use the iOS example only for projects that
need simulator verification.

Key project config fields:

| Field | Meaning |
|---|---|
| `tracker.project` | Exact Linear project name. |
| `workspace.root` | Parent directory for autosymph worktrees. |
| `prompts.root` | Path to this repo's `prompts/` directory. |
| `runners.default` | Runner used unless a state or label overrides it. |
| `states.*.prompt` | Prompt file for an agent state. |
| `states.*.transitions` | Signal-to-next-state routing. |

Device config supplies machine-specific data:

| Field | Meaning |
|---|---|
| `projects.*.repo` | Local git repo path for that project. |
| `resources.ios_simulator` | Optional simulator pool. |
| `resources.dev_port_range` | Optional local web port pool. |
| `agent.max_concurrent_agents` | Global concurrency cap on this machine. |

## Prompts And Skills

Prompts are the behavior of each agent state:

```text
prompts/
  global.md
  implement.md
  verify.md
  verify-review.md
  merge.md
  autoplan.md
  investigating.md
```

Runtime config should point at these prompts with `prompts.root`. Do not copy
prompt files into your runtime config directory unless you intentionally want a
fork.

Skills are packaged in `skills/` and can be installed into agent discovery
directories:

```bash
scripts/install-skills.sh --dry-run
scripts/install-skills.sh
scripts/check-skills.sh
```

The installer symlinks by default. Use `--copy` where symlinks are undesirable.
Use `--target DIR` for custom discovery paths.

Important bundled skills:

| Skill | Purpose |
|---|---|
| `verify-preflight` | Fast-fail missing auth fixtures or broken iOS/web tools. |
| `verify-finalize` | Atomically upload verify evidence and post the summary. |
| `verify-completion-audit` | Mechanically audit a verify run's completion contract. |
| `autosymph-monitor` | Observe running autosymph sessions and diagnose loops. |
| `context-management` | File-based handoff rules for large payloads. |
| `screenshot-to-linear` | Upload visual evidence without passing large base64 through the model. |
| `record-demo-video` | Build demo videos from screenshots/clips. |

## Project Verification Files

Project-specific verification knowledge belongs in the application repo, under:

```text
.autosymph/verify/
  auth.md
  ios.md
  web.md
  fixtures.md
```

Templates live in `examples/project/.autosymph/verify/`.

Use these files for things autosymph cannot know generically:

- how to sign in,
- which fixture modes are safe,
- how to build and launch the app,
- which routes or accessibility labels matter,
- which tests are allowed to use live backend/realtime systems.

The worktree manager copies `.autosymph/` into each agent worktree so verify
agents see the same project instructions.

## Monitor

The `autosymph-monitor` skill is the operator view for a running autosymph
session.

It discovers the status API in this order:

1. explicit URL/port argument,
2. `AUTOSYMPH_STATUS_URL`,
3. `AUTOSYMPH_STATUS_PORT`,
4. `~/.autosymph/status-api.json`,
5. configured `server.port`,
6. localhost fallback probes.

Run the deterministic tick script directly:

```bash
${AUTOSYMPH_SKILLS_DIR:-$HOME/.autosymph/skills}/autosymph-monitor/scripts/tick.sh
```

Or invoke the skill from an agent that supports skills:

```text
/autosymph-monitor
```

autosymph writes `~/.autosymph/status-api.json` when the local status server is
available.

## CLI

```bash
uv run autosymph start                    # start the TUI supervisor
uv run autosymph start --daemon           # run in the background
uv run autosymph start --max-agents 4     # cap global concurrency
uv run autosymph config check config.yaml # validate one project config
uv run autosymph status                   # query the local status API
uv run autosymph logs ISSUE-123           # show logs for an issue
uv run autosymph ledger check             # verify the durable local ledger
uv run autosymph ledger export --output ledger.jsonl
uv run autosymph models check             # detect stale pinned model ids
uv run autosymph models refresh           # inspect model registry changes
```

TUI controls:

| Key | Action |
|---|---|
| `q` | Quit all orchestrators |
| `r` | Force refresh |
| `j` / `k` | Scroll |

## Models

Use model aliases by default:

```yaml
claude:
  model: sonnet

states:
  verify:
    model: opus
```

Aliases float with the runner and avoid routine config churn. Pin full model
ids only when reproducibility matters. If you pin ids, use:

```bash
uv run autosymph models check
uv run autosymph models refresh
```

## Logs And State

autosymph writes local state under `~/.autosymph/` by default:

```text
~/.autosymph/
  logs/
    factory.sqlite3
    {project-slug}/{issue-slug}/{state}-run{N}.ndjson
    {project-slug}/{issue-slug}/{state}-run{N}.meta.json
  workspaces/
    {project-slug}/{issue-slug}/
  status-api.json
```

Raw `.ndjson` logs are intentionally kept because monitor and audit skills use
them to reconstruct what an agent actually did.

`factory.sqlite3` is the crash-safe source of truth for run allocation,
terminal outcomes, transition decisions, and later repair receipts. Raw log
contents stay in NDJSON; the ledger stores only their path, byte size, and
SHA-256 digest. Remote tracing is optional and cannot determine whether a run
was durably recorded.

The reliability rollout is configured per project:

```yaml
reliability:
  mode: observe # observe | enforce_verify | enforce_all
  # database_path: ~/.autosymph/state/autosymph.db
  backup_before_migrate: true
  recover_read_only: true
```

Use `autosymph ledger check` for an integrity check. Use `autosymph ledger
export --output ledger.jsonl` for a manifest-prefixed, redacted audit export;
add `--format parquet` when the optional `parquet` dependency is installed.

## Optional Verification Tooling

### iOS

Install iOS tools only for projects that need simulator verification:

```bash
brew install python@3.12 idb-companion
uv tool install --python 3.12 fb-idb
```

Keep `fb-idb` on Python 3.12 or 3.13. Some Python 3.14 Homebrew builds can
break `pyexpat`, which breaks `idb` startup.

### Web

Install browser tooling only for projects that need web verification:

```bash
brew install node
npx playwright install chromium
```

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run autosymph config check examples/config/projects/web-project.yaml.example
scripts/check-skills.sh
```

Useful Phase 7 smoke test:

```bash
uv sync --extra dev
scripts/install-skills.sh --dry-run
uv run pytest -q
```

## Prior Art

autosymph was inspired by [symphony](https://github.com/odysseus0/symphony)
(itself a fork of [openai/symphony](https://github.com/openai/symphony)),
which pioneered the Linear-backed, state-machine-driven agent orchestration
pattern. autosymph is an independent Python reimplementation with a
different runtime model, skill set, and verification pipeline; it shares no
source code with either upstream project.

## License

autosymph is released under the [MIT License](LICENSE).
