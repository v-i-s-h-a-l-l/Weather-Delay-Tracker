"""Concurrent weather lookups against the OpenWeatherMap Current Weather API.

Every city is requested at the same time with asyncio.gather. A failure for
one city (bad name, timeout, server error) is returned as a WeatherError for
that city only, so the other lookups always finish.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Union

import httpx

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# Worth retrying: rate limiting and temporary server problems.
# Not worth retrying: 401 (bad key) and 404 (unknown city) never fix themselves.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

log = logging.getLogger("weather")


@dataclass(frozen=True)
class Weather:
    city: str
    main: str  # e.g. "Rain"
    description: str  # e.g. "heavy intensity rain"
    temp_c: Optional[float]


class WeatherError(Exception):
    """A lookup that failed for one city, with a readable reason."""

    def __init__(self, city: str, reason: str, status: Optional[int] = None):
        super().__init__(f"{city}: {reason}")
        self.city = city
        self.reason = reason
        self.status = status


WeatherResult = Union[Weather, WeatherError]


async def fetch_weather(
    client: httpx.AsyncClient,
    city: str,
    api_key: str,
    *,
    retries: int = 2,
    backoff: float = 0.5,
) -> Weather:
    """Fetch current weather for one city. Raises WeatherError on failure."""
    params = {"q": city, "appid": api_key, "units": "metric"}
    reason = "unknown error"

    for attempt in range(retries + 1):
        started = time.perf_counter()
        log.info("-> request sent      %s", city)
        try:
            resp = await client.get(OWM_URL, params=params)
        except httpx.TimeoutException:
            reason = "request timed out"
        except httpx.TransportError as exc:
            # Only the exception type is logged: the message can contain the
            # request URL, and the URL contains the API key.
            reason = f"network error ({type(exc).__name__})"
        else:
            elapsed = time.perf_counter() - started
            if resp.status_code == 200:
                weather = _parse(city, resp)
                log.info(
                    "<- response %.2fs    %s: %s (%s)",
                    elapsed, city, weather.main, weather.description,
                )
                return weather
            reason = _error_reason(resp)
            log.warning("<- response %.2fs    %s: %s", elapsed, city, reason)
            if resp.status_code not in RETRYABLE_STATUS:
                raise WeatherError(city, reason, resp.status_code)

        if attempt < retries:
            wait = backoff * (2 ** attempt)
            log.warning("   retrying %s in %.1fs (%s)", city, wait, reason)
            await asyncio.sleep(wait)

    raise WeatherError(city, f"{reason} (gave up after {retries + 1} attempts)")


async def fetch_all(
    cities: Iterable[str],
    api_key: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
) -> Dict[str, WeatherResult]:
    """Look up every distinct city concurrently.

    Returns {city: Weather or WeatherError}. Never raises for a single bad city.
    """
    unique = list(dict.fromkeys(cities))  # one request per distinct city, order kept

    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        # All requests start together; gather waits for all of them.
        # return_exceptions=True means one failing task cannot cancel the rest.
        results = await asyncio.gather(
            *(
                fetch_weather(client, city, api_key, retries=retries, backoff=backoff)
                for city in unique
            ),
            return_exceptions=True,
        )

    out: Dict[str, WeatherResult] = {}
    for city, result in zip(unique, results):
        if isinstance(result, (Weather, WeatherError)):
            out[city] = result
        elif isinstance(result, Exception):
            # A bug or unexpected response in one task: report it for that
            # city instead of crashing the whole run.
            out[city] = WeatherError(city, f"unexpected error: {type(result).__name__}: {result}")
        else:
            # BaseException such as CancelledError or KeyboardInterrupt: let it stop the run.
            raise result
    return out


def _parse(city: str, resp: httpx.Response) -> Weather:
    try:
        data = resp.json()
        condition = data["weather"][0]
        return Weather(
            city=city,
            main=str(condition["main"]),
            description=str(condition.get("description", "")),
            temp_c=(data.get("main") or {}).get("temp"),
        )
    except (ValueError, KeyError, IndexError, TypeError):
        raise WeatherError(city, "unexpected response format from OpenWeatherMap", resp.status_code)


def _error_reason(resp: httpx.Response) -> str:
    try:
        message = str(resp.json().get("message", "")).strip()
    except (ValueError, AttributeError):
        message = ""

    if resp.status_code == 401:
        return (
            "API key rejected (401). Check OPENWEATHER_API_KEY in .env; "
            "a new key can take a couple of hours to activate"
        )
    if resp.status_code == 404:
        return "city not found (404)"
    if resp.status_code == 429:
        return "rate limit hit (429)"
    return f"HTTP {resp.status_code}" + (f": {message}" if message else "")
