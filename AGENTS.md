# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-18
**Commit:** Not queried (digest-only authoring)
**Branch:** Not queried (digest-only authoring)

## OVERVIEW
Sellbird is an open-source outreach platform for prospect discovery, personalized email sequences and reply handling.
Python >=3.11, setuptools, async SQLite and Pydantic power the runtime; FastAPI serves a local dashboard with plain HTML/CSS/JS and no frontend build.

## STRUCTURE
```text
sellbird/
|-- sellbird/       # Runtime package; nested guidance owns domain details
|   |-- agents/    # Draft/send/reply workflow orchestration
|   |-- collectors/ # Deterministic facts and cost-controlled discovery
|   |-- desktop/   # Optional tray and owned login entries
|   |-- integrations/ # Mail and external-service contracts
|   |-- models/    # Small record layer
|   `-- web/       # Disk-served dashboard assets, classic scripts
|-- tests/         # Offline harness and opt-in integration gates
|-- prompts/       # Model instructions
|-- skills/        # Editable Markdown knowledge
|-- docs/          # Operational reference
|-- scripts/       # Public-mirror diagnostic tooling
`-- harvey/        # Thin compatibility entry points
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Contribution constraints | `CONTRIBUTING.md` | Read before code or PR work |
| Operator setup and usage | `CLAUDE.md` | Product guidance, not a runtime specification |
| CLI entry | `sellbird.cli:main`, `sellbird/__main__.py` | Compatibility entry: `harvey.cli:main` |
| Heartbeat ordering | `sellbird.main:heartbeat` | Reads replies before sending |
| Local dashboard API | `sellbird/dashboard.py` | `build_app` |
| Dashboard UI | `sellbird/web/` | Auth and classic-script ordering matter |
| Agent MCP entry | `mcp_server.run_stdio` | Separate restricted token |
| Model instructions | `prompts/`, `skills/` | Brain skill map reloads Markdown next heartbeat |
| CI and test matrix | `.github/workflows/tests.yml`, `tests/AGENTS.md` | Python 3.11/3.12; Linux/macOS/Windows; Node 20 |

## CODE MAP
Reference centrality was not measured; no reference counts are asserted.

| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `StateManager` | Class | `sellbird/state.py` | Durable SQLite state |
| `Brain` | Class | `sellbird/brain.py` | Model-call boundary and skill loading |
| `CodexClient` | Class | `sellbird/codex.py` | Explicit opt-in isolated provider |
| `build_app` | Function | `sellbird/dashboard.py` | Local FastAPI dashboard |
| `ProfileCollector` | Class | `sellbird/collectors/profile.py` | Company profiling |
| `run_discovery` | Function | `sellbird/collectors/discover.py` | Cost-controlled discovery |

## CONVENTIONS
- Facts live in governed observation rows, not new columns. Durable queries join stages.
- Normalized domains identify companies; provider IDs identify businesses without websites.
- Home/config is task-local. `sellbird.local.yaml` wins; training preserves operator settings and writes atomically.
- Native Gmail/Microsoft/SMTP stages mail in the approval outbox; legacy Instantly owns sequencing externally.
- Claude is default. Codex requires explicit selection, CLI 0.159.3 and macOS/Linux; calls run isolated without tools.

## ANTI-PATTERNS (THIS PROJECT)
- Never publish real contact, business or live-run data; use fictional fixtures and report live results as counts only.
- Do not enable autopilot, confirm signals or accept the entry offer for the human.
- Never read or repeat dashboard unlock tokens; do not substitute them for restricted agent MCP credentials.
- Do not retry uncertain mail delivery as if it were a confirmed failure. Incomplete inbox reads hold sending.
- A genuine reply stops sequences for colleagues at that company, not just the replying address.
- Do not silently fall back between model providers or mutate native Codex configuration.
- Do not describe `sellbird/integrations/calendar.py` as working scheduling; it is a placeholder.

## UNIQUE STYLES
- Dashboard scripts load `auth.js` before `app.js`; tokens stay fragment/tab-local.
- Preserve `escHtml`/`jsq`/`safeHref` escaping and globals referenced by inline HTML handlers.
- Reuse existing themes, responsive layouts and reduced-motion CSS rather than introducing a frontend build.
- Startup playbook loading is conditional, not universal.

## COMMANDS
Documented commands below were not executed while generating this guidance.

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]" "setuptools>=68" wheel
python -m pytest -q
sellbird status --check
sellbird run
sellbird dashboard
sellbird setup
sellbird train https://example.com
sellbird discover --providers
sellbird discover --estimate
```

Non-Windows opt-ins: `SELLBIRD_PACKAGE_TESTS=1 python -m pytest -q tests/test_packaging.py` and `SELLBIRD_CODEX_CLI_TESTS=1 python -m pytest -q tests/test_codex_cli_surface.py` (pinned CLI; localhost mocks).
Claude isolation opt-in: `SELLBIRD_CLAUDE_CLI_TESTS=1 python -m pytest -q tests/test_claude_cli_isolation.py`.

## NOTES
- Runtime validators/prompts win over stale documentation: email word limits, first-email offer advice and signal counts/source advice conflict across older documents.
- `scripts/diagnose_overpass.py` probes public mirrors without changing Sellbird state.
- License is AGPL-3.0-only; retain the historical MIT NOTICE. Requested commits require DCO sign-off.
- Domain-specific guidance lives in `sellbird/AGENTS.md`, its agents/collectors/desktop/integrations children, and `tests/AGENTS.md`; keep implementation detail there.
