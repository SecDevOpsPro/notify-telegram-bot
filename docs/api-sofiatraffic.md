# Sofia Traffic Parking API

Reference for the two parking endpoints served by [www.sofiatraffic.bg](https://www.sofiatraffic.bg/en/parking).

> **Note — CSRF protection.**
> The site runs Laravel, which sets a `XSRF-TOKEN` cookie that must be
> reflected back as the `X-XSRF-TOKEN` request header on every XHR call.
> The service module (`services/sofiatraffic.py`) handles this automatically
> by visiting the parking page first to obtain a fresh session.

> **Note — Cloudflare protection.**
> Server-side requests may be challenged (403/503) by Cloudflare.
> The bot surfaces a friendly message with a direct link when this happens.

---

## 1. Parking e-Vignette Sticker

Check whether a vehicle has a registered **Sofia city parking zone sticker**.

### Request

```
GET https://www.sofiatraffic.bg/bg/parking/sticker/{plate}
```

| Header | Value |
|---|---|
| `Accept` | `application/json, text/plain, */*` |
| `X-Requested-With` | `XMLHttpRequest` |
| `X-XSRF-TOKEN` | URL-decoded value of the `XSRF-TOKEN` session cookie |
| `Referer` | `https://www.sofiatraffic.bg/en/parking` |

**Path parameter:** `{plate}` — vehicle registration plate, e.g. `AA1234BB`.

### Response examples

**Sticker found:**

```json
{
  "sticker": {
    "plateNumber": "AA1234BB",
    "validFrom": "2026-01-01",
    "validTo": "2026-12-31",
    "zone": "blue",
    "type": "annual",
    "status": "VALID"
  }
}
```

**No sticker:**

```json
{
  "sticker": null
}
```

### HTTP status codes

| Code | Meaning |
| --- | --- |
| `200` | Request processed; check payload for sticker presence |
| `404` | Plate not found / no sticker |
| `403` / `503` | Cloudflare bot-detection challenge |

---

## 2. Wheel Clamp Check

Check whether a vehicle is currently **wheel-clamped** in Sofia.

### Request

```text
GET https://www.sofiatraffic.bg/bg/parking/clamp/{plate}
```

Headers are identical to the sticker endpoint above.

### Response examples

**Vehicle clamped:**

```json
{
  "clamp": {
    "plateNumber": "AA1234BB",
    "clamped": true,
    "clampedAt": "2026-02-28 10:30",
    "location": "ul. Vitosha 1, Sofia",
    "instructions": "Call 0700 17 100 or visit the nearest municipality office."
  }
}
```

**Not clamped:**

```json
{
  "clamp": null
}
```

### HTTP status codes

| Code | Meaning |
| --- | --- |
| `200` | Request processed; check `clamped` field |
| `404` | Plate not found (treat as not clamped) |
| `403` / `503` | Cloudflare bot-detection challenge |

---

## 3. Parking Fines (⚠️ currently broken — not implemented)

The `/bg/parking/fines?plate=...&egn=...` endpoint requires both the plate
and the Bulgarian national ID (EGN) and was observed to be unreliable during
development.  It is **not implemented** in the bot until a stable response
shape is confirmed.

---

## CSRF Flow

```mermaid
sequenceDiagram
    participant B as Bot (httpx)
    participant S as sofiatraffic.bg

    B->>S: GET /bg/parking (browser headers)
    S-->>B: 200 + Set-Cookie: XSRF-TOKEN=...; sofia_traffic_session=...
    B->>B: URL-decode XSRF-TOKEN cookie value
    B->>S: GET /bg/parking/sticker/{plate}<br/>X-XSRF-TOKEN: {decoded}
    S-->>B: 200 {"sticker": {...}}
```

---

## Bot Commands

| Command | Description |
| --- | --- |
| `/sticker` | Check parking sticker using enrolled plate |
| `/sticker CB1234AB` | Check parking sticker for a specific plate |
| `/clamp` | Check wheel-clamp status using enrolled plate |
| `/clamp CB1234AB` | Check wheel-clamp status for a specific plate |

Both commands are available to **approved users only**.
The plate argument takes precedence over the enrolled plate.

---

## Service Module

**`notify_bot/services/sofiatraffic.py`**

| Symbol | Type | Description |
| --- | --- | --- |
| `SofiaTrafficError` | Exception | Base exception |
| `CloudflareError` | Exception | Cloudflare 403/503 challenge |
| `CsrfFetchError` | Exception | Could not obtain XSRF-TOKEN |
| `StickerInfo` | Dataclass | Parsed sticker result |
| `ClampInfo` | Dataclass | Parsed clamp result |
| `check_sticker(plate)` | `async` function | Calls sticker endpoint |
| `check_clamp(plate)` | `async` function | Calls clamp endpoint |
