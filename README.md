# Termops — Terminal Operations Agent

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

[中文文档](./README_zh.md)

**Termops** is a terminal-native AI agent that hooks into your shell, auto-captures
command errors, and analyzes them with your choice of LLM provider. It runs as a
local daemon with an approval-gated action engine — nothing mutates your system
without your explicit consent.

```text
  ┌──────────────────────────────────────────────────┐
  │  Shell (PowerShell / Bash / Zsh)                 │
  │  ┌────────────────────────────────────────────┐  │
  │  │  $ python -c "import missing_pkg"           │  │
  │  │  ModuleNotFoundError: ...    ← auto-captured │  │
  │  └────────────────────────────────────────────┘  │
  │           │ terminal hook (opt-in)               │
  │           ▼                                      │
  │  ┌────────────────────────────────────────────┐  │
  │  │  termops daemon (127.0.0.1:8923)           │  │
  │  │  ├─ LLM attribution (OpenAI/Anthropic/     │  │
  │  │  │   Ollama/OpenAI-compatible)             │  │
  │  │  ├─ MAPE-K control loop                   │  │
  │  │  ├─ Approval-gated actions                │  │
  │  │  └─ SQLite knowledge store                │  │
  │  └────────────────────────────────────────────┘  │
  │           │                                      │
  │           ▼                                      │
  │  ┌────────────────────────────────────────────┐  │
  │  │  termops CLI  /  Web UI (localhost)        │  │
  │  └────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────┘
```

---

## Features

- **Terminal hook** — auto-captures stderr on non-zero exit codes. Enable it
  with `termops hook install`, disable it anytime with `termops hook uninstall`.
- **Multi-provider LLM** — OpenAI, Anthropic, Ollama, or any OpenAI-compatible
  endpoint (vLLM, LiteLLM, LocalAI, etc.). You supply the API key and model.
- **Approval-gated actions** — every proposed fix must be reviewed and approved
  before execution. Actions expire after 15 minutes and are replay-protected.
- **Structured analysis** — error fingerprinting, evidence extraction, root-cause
  attribution, severity scoring, and actionable next steps.
- **Knowledge store** — successful fixes, false positives, and user feedback
  are persisted in local SQLite, building a searchable repair history.
- **Local Web UI** — dashboard, task timelines, approval panels, and audit logs.
  Binds to loopback only.
- **No cloud dependency** — default profile is deterministic. LLM is opt-in.

---

## Quick Start

### Prerequisites

- Python 3.10+
- (Optional) An LLM API key for AI-powered analysis

### Install

```bash
git clone https://github.com/user/termops.git
cd termops
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

### Start the daemon

```bash
termops-agent --profile demo
```

### Analyze an error

```bash
# Direct text
termops analyze --text "ModuleNotFoundError: No module named 'click'"

# From a log file
termops analyze --file error.log

# Wrap a command and capture its output
termops run -- python -c "import missing_pkg"

# Pipe from stdin
echo "ImportError: cannot import name 'foo'" | termops analyze
```

### View & manage tasks

```bash
termops task list
termops task show <task-id>
termops task watch <task-id>
termops action approve <action-id>
termops action reject <action-id>
```

### Open the Web UI

```bash
termops web login
# Opens http://127.0.0.1:8923
```

---

## Terminal Hook (Auto-Capture)

The terminal hook is a shell plugin that listens for command errors and
automatically sends them to Termops for analysis. **It is opt-in** — nothing
is captured until you explicitly install it.

### Install

```bash
termops hook install
```

This detects your shell (PowerShell, Bash, or Zsh) and tells you which line to
add to your shell profile. Restart your terminal or source the profile to
activate.

### Check status

```bash
termops hook status
```

### Uninstall

```bash
termops hook uninstall
```

---

## LLM Configuration

Termops ships with a deterministic analysis core. To enable AI-powered
attribution, configure at least one LLM provider. API keys are stored in
`~/.termops/config.toml` and can also be set via environment variables.

### Configure via CLI

```bash
# OpenAI
termops config llm --provider openai --model gpt-4o --api-key sk-xxx --enable

# Anthropic
termops config llm --provider anthropic --model claude-sonnet-4-20250514 --api-key sk-ant-xxx --enable

# Ollama (local, no API key needed)
termops config llm --provider ollama --model qwen2.5:7b --enable

# Any OpenAI-compatible endpoint (vLLM, LiteLLM, etc.)
termops config llm --provider openai_compatible --base-url http://localhost:8080/v1 --model my-model --api-key my-key --enable
```

### Configure via environment variables

```bash
export TERMOPS_LLM_PROVIDER=openai
export TERMOPS_LLM_API_KEY=sk-xxx
export TERMOPS_LLM_MODEL=gpt-4o
export TERMOPS_LLM_ENABLED=true
```

### Show current config

```bash
termops config show
```

### Supported providers

| Provider | Key | Default Model | Default URL |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` | `https://api.anthropic.com/v1` |
| `ollama` | — | `qwen2.5:7b` | `http://localhost:11434/v1` |
| `openai_compatible` | `LLM_API_KEY` | (user-defined) | `http://localhost:8080/v1` |

---

## CLI Reference

```text
termops analyze [--text TEXT] [--file PATH] [--source LABEL] [--language LANG]
                [--command CMD] [--cwd DIR] [--exit-code N]
termops run [--language LANG] [--cwd DIR] -- <command...>
termops doctor
termops task list|show|watch|cancel
termops action approve|reject ACTION_ID
termops web login
termops hook install|uninstall|status
termops config llm|show
```

Legacy aliases `erra` / `erra-agent` are still installed for backward compatibility.

---

## How It Works

### MAPE-K Control Loop

Every task flows through a state machine:

```text
queued → running → waiting_approval → executing → verifying
              ↘ succeeded / failed / cancelled
executing | verifying --daemon restart→ reconciling
```

### Task State Machine

```text
queued ──→ running ──→ waiting_approval ──→ executing ──→ verifying ──→ succeeded
  │            │              │                  │              │
  └──→ cancelled            ├──→ rejected       ├──→ failed     └──→ failed
                            └──→ expired
```

### Security

- Actions are approval-gated: only transitions from `waiting_approval` are allowed.
- Every action is hashed (SHA-256 digest); replay, expiry, and tampering are detected.
- The Web UI binds to `127.0.0.1` only and uses HttpOnly + SameSite=Strict cookies.
- No arbitrary shell, file read, or network calls are exposed to the action engine.
- Secrets are never logged, stored in plaintext in the database, or returned via API.

---

## Project Structure

```text
src/termops/
├── __init__.py          Package entry
├── cli.py               CLI commands (analyze, run, hook, config, etc.)
├── daemon.py            Background daemon entry point
├── config.py            Settings loader (file, env, CLI)
├── engine.py            MAPE-K analysis engine
├── graph.py             LangGraph state machine
├── llm.py               LLM provider config models
├── llm_client.py        Multi-provider LLM API client
├── models.py            Core data models (Task, Action, Finding, etc.)
├── store.py             SQLite-backed state store with audit chain
├── api.py               FastAPI REST endpoints
├── web.py               Web UI routes
├── diagnostics.py       Error fingerprinting & classification
├── providers.py         Read-only environment probes
├── security.py          Token generation, hashing, redaction
├── templates/           Jinja2 HTML templates
└── hooks/
    ├── hook.ps1          PowerShell terminal hook
    └── hook.sh           Bash/Zsh terminal hook
tests/
├── conftest.py          Shared fixtures
├── test_cli.py          CLI integration tests
├── test_engine.py       Engine & MAPE-K loop tests
├── test_store.py        State store & audit chain tests
├── test_contracts.py    API contract tests
└── test_api_web.py      Web UI tests
```

---

## Development

```bash
# Run tests
pytest -q

# Lint & type-check
ruff check src tests
mypy src/termops
```

---

## License

MIT