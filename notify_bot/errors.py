"""Shared helper for surfacing errors to users — verbose for the admin, terse for everyone else."""

from __future__ import annotations

import html
import json
import re

import httpx

from notify_bot import config

#: Query parameters never echoed back, even to debug users (API keys, national IDs, licences).
_REDACTED_PARAMS = frozenset(
    {"key", "token", "egn", "obligedpersonident", "drivinglicencenumber", "driverlicenseno"}
)

#: Characters of the response body included in the debug detail (Telegram caps messages at 4096).
_BODY_SNIPPET_CHARS = 800

#: Maximum depth followed along an exception's ``__cause__`` / ``__context__`` chain.
_MAX_CHAIN = 4


class ServiceError(Exception):
    """
    Base for ``services/`` exceptions.

    Pass the offending ``response`` when there is one so :func:`format_error`
    can show debug users what the upstream API actually returned.
    """

    def __init__(self, message: str, *, response: httpx.Response | None = None) -> None:
        super().__init__(message)
        self.response = response


def _redact_url(url: httpx.URL) -> str:
    params = [
        (k, "***" if k.lower() in _REDACTED_PARAMS else v) for k, v in url.params.multi_items()
    ]
    return str(url.copy_with(params=params)) if params else str(url)


def _code(text: str) -> str:
    return f"<code>{html.escape(text)}</code>"


def _body_block(response: httpx.Response) -> str | None:
    """
    The response body as a Telegram ``<pre>`` block, or ``None`` if empty.

    JSON is pretty-printed and tagged ``language-json`` so Telegram
    syntax-highlights it; anything else is whitespace-collapsed plain text.
    """
    try:
        text = response.text
    except Exception:
        return None
    try:
        body = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        language = "json"
    except ValueError:
        body = re.sub(r"\s+", " ", text).strip()
        language = None
    if not body:
        return None
    if len(body) > _BODY_SNIPPET_CHARS:
        body = body[:_BODY_SNIPPET_CHARS] + "…"
    if language:
        return f'<pre><code class="language-{language}">{html.escape(body)}</code></pre>'
    return f"<pre>{html.escape(body)}</pre>"


def _http_details(exc: BaseException, seen_responses: set[int]) -> list[str]:
    """HTML parts — request line, status, body — for an exception carrying an httpx response."""
    response = getattr(exc, "response", None)
    if not isinstance(response, httpx.Response) or id(response) in seen_responses:
        response = None  # absent, or already shown further up the chain
    else:
        seen_responses.add(id(response))
    try:
        # httpx raises RuntimeError when no request is attached (e.g. hand-built responses).
        request = response.request if response is not None else getattr(exc, "request", None)
    except RuntimeError:
        request = None

    parts: list[str] = []
    if isinstance(request, httpx.Request):
        parts.append(_code(f"{request.method} {_redact_url(request.url)}"))
    if response is not None:
        parts.append(_code(f"HTTP {response.status_code} {response.reason_phrase}".rstrip()))
        if block := _body_block(response):
            parts.append(f"Body:\n{block}")
    return parts


def _describe(exc: BaseException) -> str:
    """HTML: exception type and message, its HTTP context, then each underlying cause."""
    parts: list[str] = []
    seen: set[int] = set()
    seen_responses: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and id(current) not in seen and depth < _MAX_CHAIN:
        seen.add(id(current))
        prefix = "" if depth == 0 else "↳ caused by "
        parts.append(_code(f"{prefix}{type(current).__name__}: {current}"))
        parts.extend(_http_details(current, seen_responses))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
        depth += 1
    return "\n\n".join(parts)  # blank line between parts so they're easy to tell apart


def format_error(user_id: int, base_message: str, exc: Exception) -> str:
    """
    Build an HTML-safe error message.

    The admin and any configured debug user (see ``config.DEBUG_USER_IDS``)
    get the base message plus the exception type/details, the HTTP request and
    response behind it (sensitive query parameters redacted) and its cause
    chain; regular users only ever see the base message.
    """
    if config.is_debug_user(user_id):
        return f"{base_message}\n\n🛠 {_describe(exc)}"
    return base_message
