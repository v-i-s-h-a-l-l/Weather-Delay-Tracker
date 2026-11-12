"""Offline stand-in for OpenWeatherMap, used by --mock and the tests.

It plugs into httpx as a transport, so the real fetching code runs unchanged:
the same concurrent requests, the same error handling. Only the network is
replaced. Each city answers after a different delay, so the log shows
responses arriving out of order, which only happens when requests overlap.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Tuple

import httpx

# city (lower case) -> (seconds to wait, main, description, temperature in C)
MOCK_CITIES: Dict[str, Tuple[float, str, str, float]] = {
    "new york": (0.60, "Clouds", "broken clouds", 8.4),
    "mumbai": (0.35, "Rain", "heavy intensity rain", 27.1),
    "london": (0.45, "Snow", "light snow", 0.6),
}
NOT_FOUND_DELAY = 0.20


def mock_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        city = request.url.params.get("q", "").strip().lower()
        if city in MOCK_CITIES:
            delay, main, description, temp = MOCK_CITIES[city]
            await asyncio.sleep(delay)
            return httpx.Response(
                200,
                json={
                    "weather": [{"main": main, "description": description}],
                    "main": {"temp": temp},
                    "name": city.title(),
                    "cod": 200,
                },
            )
        await asyncio.sleep(NOT_FOUND_DELAY)
        # Same body the real API sends for an unknown city.
        return httpx.Response(404, json={"cod": "404", "message": "city not found"})

    return httpx.MockTransport(handler)
