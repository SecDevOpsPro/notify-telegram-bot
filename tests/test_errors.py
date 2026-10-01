"""Tests for notify_bot/errors.py — verbose-for-admin, terse-for-everyone-else."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from notify_bot.errors import ServiceError, format_error


def test_regular_user_only_sees_base_message():
    with patch("notify_bot.errors.config.is_debug_user", return_value=False):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("secret detail"))

    assert text == "⚠️ Something went wrong."
    assert "secret detail" not in text


def test_admin_sees_exception_type_and_message():
    with patch("notify_bot.errors.config.is_debug_user", return_value=True):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("secret detail"))

    assert text.startswith("⚠️ Something went wrong.")
    assert "ValueError" in text
    assert "secret detail" in text


def test_exception_detail_is_html_escaped_for_admin():
    with patch("notify_bot.errors.config.is_debug_user", return_value=True):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("<script>steal()</script>"))

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# ── HTTP context and cause chain ─────────────────────────────────────────────


def _response(status: int, body: str, url: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("GET", url))


def _admin_text(exc: Exception) -> str:
    with patch("notify_bot.errors.config.is_debug_user", return_value=True):
        return format_error(1, "⚠️ Check failed.", exc)


def test_admin_sees_request_status_and_body_of_service_error():
    resp = _response(400, '{"error": "bad carNo"}', "https://api.example/gtp?carNo=CB1234XX")
    text = _admin_text(ServiceError("HTTP 400 from gtp", response=resp))

    assert "ServiceError: HTTP 400 from gtp" in text
    assert "GET https://api.example/gtp?carNo=CB1234XX" in text
    assert "HTTP 400 Bad Request" in text
    assert (
        'Body:\n<pre><code class="language-json">{\n  &quot;error&quot;: &quot;bad carNo&quot;\n}'
        "</code></pre>"
    ) in text


def test_sensitive_query_params_are_redacted():
    resp = _response(500, "", "https://api.example/fines?driverLicenseNo=123&egn=8001011234&x=1")
    text = _admin_text(ServiceError("boom", response=resp))

    assert "8001011234" not in text
    assert "123&" not in text
    assert "egn=%2A%2A%2A" in text or "egn=***" in text
    assert "x=1" in text


def test_long_body_is_truncated():
    resp = _response(502, "x" * 2000, "https://api.example/")
    text = _admin_text(ServiceError("boom", response=resp))

    assert "<pre>" + "x" * 800 + "…</pre>" in text
    assert "x" * 801 not in text


def test_cause_chain_includes_underlying_http_error():
    resp = _response(503, "maintenance", "https://mvr.example/api")
    try:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as inner:
            raise ServiceError("MVR API returned HTTP 503") from inner
    except ServiceError as exc:
        text = _admin_text(exc)

    assert "ServiceError: MVR API returned HTTP 503" in text
    assert "↳ caused by HTTPStatusError" in text
    assert "GET https://mvr.example/api" in text
    assert "Body:\n<pre>maintenance</pre>" in text


def test_regular_user_never_sees_http_details():
    resp = _response(400, "secret body", "https://api.example/gtp?carNo=CB1234XX")
    with patch("notify_bot.errors.config.is_debug_user", return_value=False):
        text = format_error(1, "⚠️ Check failed.", ServiceError("HTTP 400", response=resp))

    assert text == "⚠️ Check failed."


def test_response_shared_along_the_chain_is_shown_once():
    resp = _response(400, '{"a": 1}', "https://api.example/x")
    try:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as inner:
            raise ServiceError("HTTP 400", response=resp) from inner
    except ServiceError as exc:
        text = _admin_text(exc)

    assert text.count("Body:") == 1
    assert "↳ caused by HTTPStatusError" in text


def test_non_json_body_is_plain_pre_block():
    resp = _response(502, "<html>  Bad   gateway </html>", "https://api.example/")
    text = _admin_text(ServiceError("boom", response=resp))

    assert "<pre>&lt;html&gt; Bad gateway &lt;/html&gt;</pre>" in text
    assert "language-json" not in text
