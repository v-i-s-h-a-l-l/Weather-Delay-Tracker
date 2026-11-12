"""Weather-aware apology messages for delayed orders.

Example:
    >>> weather_apology("Alice Smith", "New York", "Rain", "heavy intensity rain")
    'Hi Alice, your order to New York is delayed due to heavy rain. We appreciate your patience!'
"""
from __future__ import annotations


def weather_apology(customer_name: str, city: str, weather_main: str, description: str) -> str:
    """Build a personalised delay message from the customer and the actual weather."""
    parts = customer_name.split()
    first_name = parts[0] if parts else "there"
    reason = describe_weather(weather_main, description)
    return (
        f"Hi {first_name}, your order to {city} is delayed due to {reason}. "
        f"We appreciate your patience!"
    )


def describe_weather(weather_main: str, description: str) -> str:
    """Turn OpenWeatherMap's wording into a phrase that reads naturally.

    OpenWeatherMap descriptions look like "heavy intensity rain" or
    "thunderstorm with light rain". These become "heavy rain" and
    "a thunderstorm with light rain".
    """
    main = weather_main.strip().lower()
    desc = " ".join(description.lower().replace("intensity", "").split())

    if main == "rain":
        return desc.replace("shower rain", "rain showers") or "rain"
    if main == "snow":
        return desc or "snow"
    if main == "thunderstorm":
        return f"a {desc}" if desc else "a thunderstorm"
    if main == "tornado":
        return "tornado conditions"
    if main == "squall":
        return "strong wind squalls"
    if main == "ash":
        return "volcanic ash in the area"
    if main == "extreme":
        return desc or "extreme weather"
    return desc or "severe weather"
