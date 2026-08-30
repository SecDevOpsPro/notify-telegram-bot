# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A private Telegram bot (`notify_bot`) that checks Bulgarian government
services (MVR obligations, e-vignette, Sofia parking sticker, wheel clamps)
and Cuban exchange rates, and sends a daily scheduled report to approved
users. Python 3.12, `python-telegram-bot`, async SQLite via `aiosqlite`.

See `README.md` for features/commands and the architecture diagram, and
`docs/plan.md` / `docs/api-sofiatraffic.md` for deeper reference.

## Setup & commands

Dependency and task management goes through `uv` and `mise`.

```sh
uv sync                              # install deps
uv run python -m notify_bot.run_bot  # run the bot (needs TOKEN, ADMIN_TELEGRAM_ID)
uv run pytest tests/                 # run tests
uv run ruff format .                 # format
uv run ruff check .                  # lint
```

Equivalent `mise` tasks exist: `mise run tests`, `mise run format`.

Required env vars for local runs: `TOKEN`, `ADMIN_TELEGRAM_ID`. See
`notify_bot/config.py` for the full list (all config is env-var driven,
no config files read at runtime — `config.json.tpl` is only a template for
Docker Compose's env file).

## Code layout

```
notify_bot/
  run_bot.py       entry point, PTB Application wiring
  config.py        all env-var config in one place
  db.py            async SQLite layer, single shared connection
  errors.py        format_error() — verbose detail for admin/debug users only
  middlewares.py   @require_approved decorator
  handlers/        one module per command group
  services/        one module per external API (mvr, bgtoll, sofiatraffic, cambiocuba, boleron)
  scheduler/       daily report job
```

New external integrations go in `services/`, new commands in `handlers/`,
wired up in `run_bot.py`.

## Conventions to preserve

- **Single shared SQLite connection.** `db.py` intentionally uses one
  long-lived `aiosqlite` connection guarded by `_connection_lock`, not a
  connection-per-call or a pool. Read the module docstring in `db.py`
  before touching connection handling — this was a deliberate fix for a
  concurrency bug, not an oversight.
- **Async everywhere.** HTTP calls use `httpx` (async), never `requests`,
  to avoid blocking the event loop.
- **Error visibility split.** `errors.format_error()` gives full exception
  detail only to the admin / users in `config.DEBUG_USER_IDS`; everyone
  else gets a terse message. Keep new error paths going through it rather
  than leaking tracebacks to regular users.
- **Access control.** Commands that touch user data go through
  `@require_approved` (`middlewares.py`). Public commands (`/start`,
  `/help`, `/request`, `/change`) intentionally skip it.
- **Config is env-var only**, centralized in `config.py` — don't read
  `os.environ` directly from handlers/services.
- Secrets (`.sops.yaml` present) are managed with `sops`/`age`; never
  commit decrypted secrets or real tokens.

## Testing

- Tests live in `tests/`, mirroring the module they cover
  (`test_db.py`, `test_mvr.py`, etc.), using `pytest` + `pytest-asyncio`
  (`asyncio_mode = "auto"`) and `pytest-mock`.
- `DATABASE_PATH` can be overridden via env var for test isolation — see
  `conftest.py`.
- Run the targeted test file while iterating, then the full suite
  (`uv run pytest tests/`) before finishing.

## PR/CI expectations

- CI (`.github/workflows/pr-tests.yaml`) builds the package with `uv build`
  and runs `pytest tests/` on every PR to `main`. Keep changes passing
  `ruff check .` and the test suite.
- Dependencies are pinned in `pyproject.toml`/`uv.lock` and kept current by
  Renovate (`renovate.json5`) — avoid hand-editing version pins unless
  necessary.
