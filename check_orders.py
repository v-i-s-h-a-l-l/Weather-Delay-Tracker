"""Check the weather for every pending order and flag likely delivery delays.

Usage:
    python check_orders.py            # real API; updates orders.json in place
    python check_orders.py --mock     # offline demo; writes orders.mock.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv

from apology import weather_apology
from weather_client import Weather, WeatherError, WeatherResult, fetch_all

HERE = Path(__file__).resolve().parent

# OpenWeatherMap's "main" field never says "Extreme" any more; severe weather
# comes back as Thunderstorm, Tornado, Squall or Ash. Those count as the
# assignment's "Extreme", and "Extreme" itself stays in case it ever appears.
# Drizzle and Fog are left out on purpose: they rarely stop a delivery.
DELAY_CONDITIONS = {"rain", "snow", "extreme", "thunderstorm", "tornado", "squall", "ash"}

PENDING = "Pending"
DELAYED = "Delayed"

log = logging.getLogger("orders")


def is_delay_weather(main: str) -> bool:
    return main.strip().lower() in DELAY_CONDITIONS


def load_orders(path: Path) -> List[dict]:
    with path.open(encoding="utf-8") as f:
        orders = json.load(f)
    if not isinstance(orders, list):
        raise ValueError(f"{path} should contain a JSON list of orders")
    return orders


def save_orders(path: Path, orders: List[dict]) -> None:
    # Write to a temporary file first, then swap it in, so a crash halfway
    # through can never leave a half-written orders.json behind.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def apply_weather(orders: List[dict], results: Dict[str, WeatherResult]) -> Dict[str, list]:
    """Update pending orders from the weather results. Returns order ids by outcome."""
    summary: Dict[str, list] = {"delayed": [], "on_time": [], "failed": []}

    for order in orders:
        if order.get("status") != PENDING:
            continue  # only pending orders are checked; a Delayed order is never flipped back
        order_id = order.get("order_id", "?")
        city = str(order.get("city") or "").strip()
        result = results.get(city)

        if not isinstance(result, Weather):
            reason = result.reason if isinstance(result, WeatherError) else "order has no city"
            log.error(
                "Order %s (%s): weather lookup failed: %s. Status left as %s.",
                order_id, city or "no city", reason, order["status"],
            )
            order.pop("weather", None)
            order["weather_error"] = reason
            summary["failed"].append(order_id)
            continue

        order.pop("weather_error", None)
        order["weather"] = f"{result.main} ({result.description})"

        if is_delay_weather(result.main):
            message = weather_apology(order.get("customer", ""), city, result.main, result.description)
            order["status"] = DELAYED
            order["apology_message"] = message
            log.info("Order %s -> Delayed | %s", order_id, message)
            summary["delayed"].append(order_id)
        else:
            log.info("Order %s -> on time (%s in %s)", order_id, result.main, city)
            summary["on_time"].append(order_id)

    return summary


async def run(
    orders_path: Path,
    output_path: Path,
    api_key: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    retries: int = 2,
    backoff: float = 0.5,
) -> int:
    orders = load_orders(orders_path)
    pending = [o for o in orders if o.get("status") == PENDING]
    cities = [str(o.get("city") or "").strip() for o in pending]
    cities = [c for c in cities if c]
    log.info(
        "Loaded %d orders from %s: %d pending, %d distinct cities",
        len(orders), orders_path.name, len(pending), len(set(cities)),
    )
    if not pending:
        log.info("No pending orders, nothing to check.")
        return 0

    started = time.perf_counter()
    results = await fetch_all(cities, api_key, transport=transport, retries=retries, backoff=backoff)
    log.info("All %d lookups finished in %.2fs (run concurrently)", len(results), time.perf_counter() - started)

    summary = apply_weather(orders, results)

    if not any(isinstance(r, Weather) for r in results.values()):
        log.error("Every weather lookup failed, so %s was left unchanged.", output_path.name)
        return 1

    save_orders(output_path, orders)
    log.info(
        "Done: %d delayed, %d on time, %d failed. Wrote %s",
        len(summary["delayed"]), len(summary["on_time"]), len(summary["failed"]), output_path.name,
    )
    return 0


def setup_logging(log_file: Optional[Path]) -> List[logging.Handler]:
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)
    # httpx logs every request URL at INFO level, and the URL contains the API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return handlers


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Flag orders whose delivery city has bad weather.")
    parser.add_argument("--orders", type=Path, default=HERE / "orders.json", help="orders file (default: orders.json)")
    parser.add_argument(
        "--output", type=Path,
        help="where to write the updated orders (default: overwrite --orders; with --mock: orders.mock.json)",
    )
    parser.add_argument("--mock", action="store_true", help="use built-in fake weather instead of the real API")
    parser.add_argument("--log-file", type=Path, default=HERE / "weather_check.log", help="log file path")
    args = parser.parse_args(argv)

    handlers = setup_logging(args.log_file)
    try:
        if args.mock:
            from mock_weather import mock_transport

            log.warning("MOCK MODE: using built-in fake weather, not the real API")
            transport: Optional[httpx.AsyncBaseTransport] = mock_transport()
            api_key = "mock-key"
            output = args.output or args.orders.with_name("orders.mock.json")
        else:
            load_dotenv(HERE / ".env")
            api_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
            if not api_key or api_key == "your_api_key_here":
                log.error("OPENWEATHER_API_KEY is not set. Copy .env.example to .env and put your key in it.")
                return 2
            transport = None
            output = args.output or args.orders

        try:
            return asyncio.run(run(args.orders, output, api_key, transport=transport))
        except (OSError, ValueError) as exc:  # missing or malformed orders file
            log.error("Could not process orders: %s", exc)
            return 2
    finally:
        root = logging.getLogger()
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    sys.exit(main())
