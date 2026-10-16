import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=True)
API_KEY = (os.getenv("API_KEY") or "").strip() or None

GEOCODING_URL = "https://api.openweathermap.org/geo/1.0/direct"
CURRENT_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
CACHE_PATH = Path(__file__).with_name("weather_cache.json")
MAX_RETRIES = 3
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class WeatherAppError(Exception):
    """An expected error that can be shown directly to the CLI user."""


class WeatherNetworkError(WeatherAppError):
    """A temporary network/service failure for which cached data may help."""


def _request_json(url: str, params: dict) -> dict | list:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=10)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise WeatherNetworkError(
                "Не удалось подключиться к сервису погоды. Проверьте интернет-соединение."
            ) from exc

        if response.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
            time.sleep(2**attempt)
            continue
        if response.status_code != 200:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            detail = (
                error_payload.get("message")
                if isinstance(error_payload, dict)
                else None
            )
            detail_text = f" Детали OpenWeather: {detail}" if detail else ""
            if response.status_code in (401, 403):
                raise WeatherAppError(
                    "OpenWeather отклонил API-ключ (HTTP "
                    f"{response.status_code}). Проверьте API_KEY в файле .env и "
                    f"статус активации ключа.{detail_text}"
                )
            if response.status_code == 404:
                raise WeatherAppError(
                    f"Сервис не нашёл запрошенные данные (HTTP 404).{detail_text}"
                )
            if response.status_code == 429:
                raise WeatherNetworkError(
                    f"Слишком много запросов к сервису погоды (HTTP 429).{detail_text}"
                )
            if response.status_code in RETRYABLE_STATUSES:
                raise WeatherNetworkError(
                    f"Сервис временно недоступен (HTTP {response.status_code}).{detail_text}"
                )
            raise WeatherAppError(
                f"Сервис погоды вернул ошибку (HTTP {response.status_code}).{detail_text}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise WeatherAppError("Сервис погоды вернул некорректный ответ.") from exc

    raise WeatherAppError("Не удалось получить ответ от сервиса погоды.")


def get_coordinates(city: str) -> tuple[float, float]:
    """Return latitude and longitude for the first geocoding result."""
    if not API_KEY or API_KEY == "your_openweathermap_api_key":
        raise WeatherAppError(
            "Укажите действительный API-ключ OpenWeather в файле .env (API_KEY=...)."
        )
    if not city.strip():
        raise WeatherAppError("Название города не должно быть пустым.")

    results = _request_json(
        GEOCODING_URL,
        {"q": city.strip(), "limit": 1, "lang": "ru", "appid": API_KEY},
    )
    if not isinstance(results, list) or not results:
        raise WeatherAppError(f"Город «{city}» не найден.")
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherAppError("Сервис геокодинга вернул некорректные координаты.") from exc


def get_weather_by_coordinates(lat: float, lon: float) -> dict:
    """Return current weather for the supplied coordinates."""
    if not API_KEY or API_KEY == "your_openweathermap_api_key":
        raise WeatherAppError(
            "Укажите действительный API-ключ OpenWeather в файле .env (API_KEY=...)."
        )
    if not math.isfinite(lat) or not -90 <= lat <= 90:
        raise WeatherAppError("Широта должна быть числом от -90 до 90.")
    if not math.isfinite(lon) or not -180 <= lon <= 180:
        raise WeatherAppError("Долгота должна быть числом от -180 до 180.")

    result = _request_json(
        CURRENT_WEATHER_URL,
        {
            "lat": lat,
            "lon": lon,
            "units": "metric",
            "lang": "ru",
            "appid": API_KEY,
        },
    )
    if not isinstance(result, dict):
        raise WeatherAppError("Сервис погоды вернул некорректный ответ.")
    return result


def _save_cache(city: str, lat: float, lon: float, weather: dict) -> None:
    cache = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "weather": weather,
    }
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Не удалось сохранить кэш погоды: {exc}")


def _load_fresh_cache() -> dict | None:
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched_at >= timedelta(hours=3):
            return None
        if (
            not isinstance(cache, dict)
            or not isinstance(cache.get("city"), str)
            or not isinstance(cache.get("weather"), dict)
            or not isinstance(cache.get("lat"), (int, float))
            or not isinstance(cache.get("lon"), (int, float))
        ):
            return None
        return cache
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        print("Файл кэша погоды повреждён или имеет неверный формат.")
        return None


def _print_weather(city: str, weather: dict) -> None:
    try:
        temperature = weather["main"]["temp"]
        description = weather["weather"][0]["description"]
    except (KeyError, IndexError, TypeError) as exc:
        raise WeatherAppError("В ответе сервиса отсутствуют данные о погоде.") from exc
    print(f"Погода в {city}: {temperature}°C, {description}")


def _show_cached_weather() -> bool:
    cache = _load_fresh_cache()
    if cache is None:
        print("Свежих данных в кэше нет (кэш действует менее 3 часов).")
        return False
    answer = input(f"Показать кэшированные данные для «{cache['city']}»? (д/н): ").strip().lower()
    if answer not in ("д", "да", "y", "yes"):
        return False
    _print_weather(cache["city"], cache["weather"])
    return True


def _fetch_and_print(city: str | None, lat: float, lon: float) -> None:
    weather = get_weather_by_coordinates(lat, lon)
    resolved_city = city or weather.get("name") or f"{lat}, {lon}"
    _save_cache(resolved_city, lat, lon, weather)
    _print_weather(resolved_city, weather)


def main() -> None:
    while True:
        print("\n1 — по городу")
        print("2 — по координатам")
        print("0 — выход")
        choice = input("Выберите режим: ").strip()

        if choice == "0":
            return
        if choice == "1":
            city = input("Введите город: ").strip()
            try:
                lat, lon = get_coordinates(city)
                _fetch_and_print(city, lat, lon)
            except WeatherNetworkError as exc:
                print(exc)
                _show_cached_weather()
            except WeatherAppError as exc:
                print(exc)
        elif choice == "2":
            try:
                lat = float(input("Введите широту: ").strip())
                lon = float(input("Введите долготу: ").strip())
                _fetch_and_print(None, lat, lon)
            except WeatherNetworkError as exc:
                print(exc)
                _show_cached_weather()
            except ValueError:
                print("Координаты должны быть числами.")
            except WeatherAppError as exc:
                print(exc)
        else:
            print("Неизвестный режим. Выберите 1, 2 или 0.")


if __name__ == "__main__":
    main()
