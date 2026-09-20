# AGENTS.md

Guidance for AI coding agents working in this repository.

> **Keep this file current.** If your change adds/removes/moves a module,
> changes a core convention (e.g. the DB connection model, error handling,
> access control), or alters setup/test/lint commands, update the relevant
> section of this file in the same change — don't leave it to a separate ask.

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
uv run mypy notify_bot               # type-check
```

Equivalent `mise` tasks exist: `mise run tests`, `mise run format`, `mise run typecheck`.

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
  formatting.py    align_fields() — label/value rows that line up in Telegram's proportional font
  payment_buttons.py  build_copy_keyboard() — tap-to-copy (copy_text) buttons for one fine's IBAN/BIC/reason/amount;
                   build_fines_keyboard() — the daily report's /driver and /plate shortcut buttons
  middlewares.py   @require_approved decorator
  updates.py       require() to narrow PTB's optional Update fields; HandlerCallback type
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
- **Typed handlers.** `mypy notify_bot` must stay clean. PTB types
  `update.message`, `update.effective_user`, `update.callback_query` and
  `context.user_data` as optional — narrow them once at the top of a handler
  with `require(update.message, "message")` (`updates.py`) rather than
  dereferencing them directly or adding `# type: ignore`. DB rows are
  `TypedDict`s (`db.UserRow`, `db.ProfileRow`, `db.ReportTarget`), not bare
  `dict`s; use `HandlerCallback[T]` to annotate decorators that wrap handlers.
- **Service API shape.** A `services/` function taking two same-typed
  credential strings (EGN vs licence number, plate vs talon) takes them
  keyword-only (`def check_fines(*, driver_licence_no, egn)`) — a swap is
  invisible to mypy otherwise, and the argument order differs between APIs.
  Result dataclasses are `frozen=True, slots=True`; keep raw API entries
  (`mvr.RawObligation`) and display strings (`mvr._RenderedGroup`) as
  separate types.
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
  `ruff check .` and the test suite (and, ideally, `mypy notify_bot`).
- Dependencies are pinned in `pyproject.toml`/`uv.lock` and kept current by
  Renovate (`renovate.json5`) — avoid hand-editing version pins unless
  necessary.
