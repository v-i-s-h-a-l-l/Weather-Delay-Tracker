# AI Log

**Tool:** Claude (Anthropic), in the claude.ai app.

## Prompts (verbatim)

1. *(both assignment PDFs attached)*: "iss these two tasks easy to do"
   Used to scope the work. The answer flagged the points that shaped the build: OpenWeatherMap no longer returns "Extreme" as a `main` value; `requests` inside `asyncio.gather` would not actually run concurrently; InvalidCity123 comes back as a 404; new API keys take time to activate; real weather may not produce a delay during the demo, so an offline mock mode is needed.

2. "lets first finish the weather + orders assignment properly."
   Produced the full implementation, tests and docs.

## What came out for each required part

### Parallel fetching
- `httpx.AsyncClient` shared across all requests, one coroutine per distinct city, launched together with `asyncio.gather` (`weather_client.fetch_all`).
- An async HTTP client was chosen deliberately: blocking `requests` calls inside `gather` run one after another.
- Every request and response is logged with millisecond timestamps, so the overlap is visible in the run output.
- Verified by a test that counts how many requests are in flight at the same moment (it must equal the number of cities) and a timing test (four 0.2s requests must finish in under 0.5s).

### Error handling
- `asyncio.gather(..., return_exceptions=True)` plus a per-city `WeatherError`, so one failure never cancels the other lookups.
- 404 (InvalidCity123) is logged as an error, the order stays `Pending`, and a `weather_error` field is added to it.
- Timeouts, 429 and 5xx are retried twice with exponential backoff. 401 and 404 are not retried.
- A malformed response or unexpected exception becomes an error for that city only.
- If every lookup fails (for example a bad key), `orders.json` is not modified and the exit code is 1.
- Verified by tests for each of these cases.

### Security
- The API key is loaded from `.env` (python-dotenv), with `.env` gitignored.
- httpx's request logging is turned down because OpenWeatherMap puts the key in the URL. A test checks the key never reaches the console or the log file. That test was confirmed to fail when the logging is switched back on.
