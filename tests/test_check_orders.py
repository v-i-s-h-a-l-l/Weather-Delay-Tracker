"""Tests for the weather delay checker. Run with:  python -m pytest -q

No network or API key is needed: every test swaps in a fake OpenWeatherMap
through httpx.MockTransport, so the real fetching code is what gets tested.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_orders  # noqa: E402
from apology import weather_apology  # noqa: E402
from weather_client import Weather, WeatherError, fetch_all  # noqa: E402

ORDERS = [
    {"order_id": "1001", "customer": "Alice Smith", "city": "New York", "status": "Pending"},
    {"order_id": "1002", "customer": "Bob Jones", "city": "Mumbai", "status": "Pending"},
    {"order_id": "1003", "customer": "Charlie Green", "city": "London", "status": "Pending"},
    {"order_id": "1004", "customer": "InvalidCity123", "city": "InvalidCity123", "status": "Pending"},
]


def owm_ok(main, description="", temp=20.0):
    return httpx.Response(200, json={"weather": [{"main": main, "description": description}], "main": {"temp": temp}})


def fake_api(weather_by_city, delay=0.0):
    """A fake OpenWeatherMap: known cities return their weather, others 404."""

    async def handler(request):
        await asyncio.sleep(delay)
        city = request.url.params["q"]
        if city in weather_by_city:
            return owm_ok(*weather_by_city[city])
        return httpx.Response(404, json={"cod": "404", "message": "city not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def orders_file(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(ORDERS), encoding="utf-8")
    return path


# --- Parallel fetching -------------------------------------------------------

def test_requests_are_in_flight_at_the_same_time():
    in_flight = 0
    peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return owm_ok("Clear", "clear sky")

    cities = ["New York", "Mumbai", "London", "Paris"]
    asyncio.run(fetch_all(cities, "key", transport=httpx.MockTransport(handler)))
    assert peak == len(cities)  # sequential code would never get past 1


def test_total_time_is_close_to_one_request_not_the_sum():
    transport = fake_api({c: ("Clear", "clear sky") for c in ["A", "B", "C", "D"]}, delay=0.2)

    async def timed():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await fetch_all(["A", "B", "C", "D"], "key", transport=transport)
        return loop.time() - start

    assert asyncio.run(timed()) < 0.5  # one-by-one would take at least 0.8s


def test_duplicate_cities_are_requested_once():
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return owm_ok("Clear", "clear sky")

    asyncio.run(fetch_all(["Mumbai", "Mumbai", "London"], "key", transport=httpx.MockTransport(handler)))
    assert sorted(calls) == ["London", "Mumbai"]


# --- Resilience ----------------------------------------------------------------

def test_invalid_city_is_logged_and_the_rest_still_finish(orders_file, caplog):
    transport = fake_api({
        "New York": ("Clear", "clear sky"),
        "Mumbai": ("Rain", "heavy intensity rain"),
        "London": ("Clouds", "overcast clouds"),
    })
    with caplog.at_level("INFO"):
        code = asyncio.run(check_orders.run(orders_file, orders_file, "key", transport=transport))

    assert code == 0
    saved = {o["order_id"]: o for o in json.loads(orders_file.read_text(encoding="utf-8"))}
    assert saved["1001"]["status"] == "Pending"
    assert saved["1002"]["status"] == "Delayed"
    assert saved["1003"]["status"] == "Pending"
    assert saved["1004"]["status"] == "Pending"
    assert saved["1004"]["weather_error"] == "city not found (404)"
    assert any(r.levelname == "ERROR" and "InvalidCity123" in r.getMessage() for r in caplog.records)


def test_one_task_crashing_does_not_take_down_the_others():
    def handler(request):
        if request.url.params["q"] == "Broken":
            return httpx.Response(200, content=b"not json at all")
        return owm_ok("Clear", "clear sky")

    results = asyncio.run(fetch_all(["Broken", "London"], "key", transport=httpx.MockTransport(handler)))
    assert isinstance(results["Broken"], WeatherError)
    assert isinstance(results["London"], Weather)


def test_temporary_server_error_is_retried():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts == 1 else owm_ok("Rain", "light rain")

    results = asyncio.run(fetch_all(["London"], "key", transport=httpx.MockTransport(handler), backoff=0))
    assert isinstance(results["London"], Weather)
    assert attempts == 2


def test_unknown_city_is_not_retried():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"cod": "404", "message": "city not found"})

    asyncio.run(fetch_all(["Nowhere"], "key", transport=httpx.MockTransport(handler), backoff=0))
    assert attempts == 1


def test_bad_api_key_leaves_orders_file_untouched(orders_file):
    before = orders_file.read_text(encoding="utf-8")
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"cod": 401, "message": "Invalid API key"}))
    code = asyncio.run(check_orders.run(orders_file, orders_file, "bad", transport=transport))
    assert code == 1
    assert orders_file.read_text(encoding="utf-8") == before


# --- Delay rules and the apology -------------------------------------------------

@pytest.mark.parametrize("main", ["Rain", "Snow", "Thunderstorm", "Tornado", "Squall", "Ash", "Extreme"])
def test_bad_weather_marks_order_delayed(main):
    orders = [dict(ORDERS[0])]
    check_orders.apply_weather(orders, {"New York": Weather("New York", main, "", None)})
    assert orders[0]["status"] == "Delayed"
    assert orders[0]["apology_message"].startswith("Hi Alice, your order to New York is delayed due to ")


@pytest.mark.parametrize("main", ["Clear", "Clouds", "Drizzle", "Mist", "Fog", "Haze"])
def test_ordinary_weather_keeps_order_pending(main):
    orders = [dict(ORDERS[0])]
    check_orders.apply_weather(orders, {"New York": Weather("New York", main, "", None)})
    assert orders[0]["status"] == "Pending"
    assert "apology_message" not in orders[0]


def test_already_delayed_order_is_not_reset_by_good_weather():
    orders = [dict(ORDERS[0], status="Delayed")]
    check_orders.apply_weather(orders, {"New York": Weather("New York", "Clear", "clear sky", None)})
    assert orders[0]["status"] == "Delayed"


def test_apology_matches_the_assignment_example():
    assert weather_apology("Alice Smith", "New York", "Rain", "heavy intensity rain") == (
        "Hi Alice, your order to New York is delayed due to heavy rain. We appreciate your patience!"
    )


@pytest.mark.parametrize(
    "main, description, phrase",
    [
        ("Snow", "light snow", "light snow"),
        ("Rain", "light intensity shower rain", "light rain showers"),
        ("Thunderstorm", "thunderstorm with heavy rain", "a thunderstorm with heavy rain"),
        ("Tornado", "tornado", "tornado conditions"),
    ],
)
def test_apology_wording_follows_the_actual_weather(main, description, phrase):
    assert f"delayed due to {phrase}." in weather_apology("Bob Jones", "Mumbai", main, description)


# --- Security ---------------------------------------------------------------------

def test_api_key_never_appears_in_output_or_log_file(orders_file, tmp_path, monkeypatch, capsys):
    secret = "SECRET-KEY-12345"
    monkeypatch.setenv("OPENWEATHER_API_KEY", secret)
    monkeypatch.setattr(check_orders, "HERE", tmp_path)  # no real .env gets loaded

    real_client = httpx.AsyncClient

    def client_with_fake_api(*args, **kwargs):
        kwargs["transport"] = fake_api({"London": ("Rain", "moderate rain")})
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_fake_api)
    log_file = tmp_path / "run.log"
    code = check_orders.main(["--orders", str(orders_file), "--log-file", str(log_file)])

    assert code == 0
    assert secret not in capsys.readouterr().out
    assert secret not in log_file.read_text(encoding="utf-8")


def test_missing_api_key_stops_with_a_clear_message(orders_file, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    monkeypatch.setattr(check_orders, "HERE", tmp_path)
    code = check_orders.main(["--orders", str(orders_file), "--log-file", str(tmp_path / "run.log")])
    assert code == 2
    assert "OPENWEATHER_API_KEY is not set" in capsys.readouterr().out
