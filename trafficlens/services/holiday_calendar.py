"""Public-holiday and weekend reference dates for free-flow studies."""
import datetime as dt
import json
import outscraper_integration as outscraper
import time
import urllib.error
from functools import lru_cache
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
try:
    from timezonefinder import TimezoneFinder
except ModuleNotFoundError:
    TimezoneFinder = None
try:
    import holidays
except ModuleNotFoundError:
    holidays = None
from trafficlens.db import db
from trafficlens.services import catalog as catalog_svc
from trafficlens.services import request_groups as request_groups_svc


_timezone_finder = TimezoneFinder() if TimezoneFinder is not None else None


def _nearest_public_holiday(country_code, target_date):
    if holidays is None:
        raise ValueError("Install the project requirements to enable free-flow holiday dates.")
    calendar = holidays.country_holidays(
        country_code, years=range(target_date.year - 1, target_date.year + 2),
        observed=True, language="en",
    )
    candidates = sorted(calendar.items(), key=lambda item: (abs((item[0] - target_date).days), item[0]))
    if not candidates:
        raise ValueError(f"No public holidays are available for country code {country_code}.")
    holiday_date, holiday_name = candidates[0]
    return holiday_date, str(holiday_name)


def _latest_previous_weekly_weekend(country_code, timezone_name, now=None):
    if holidays is None:
        raise ValueError("Install the project requirements to identify weekly weekend dates.")
    try:
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("The route time zone is not available for weekend date selection.") from exc
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)
    try:
        calendar = holidays.country_holidays(
            country_code,
            years=(current.year - 1, current.year),
            observed=True,
            language="en",
        )
    except (NotImplementedError, ValueError) as exc:
        raise ValueError(f"No weekly weekend calendar is available for country code {country_code}.") from exc
    weekend_days = calendar.weekend
    if not weekend_days or any(
        isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
        for day in weekend_days
    ):
        raise ValueError(f"No valid weekly weekend days are available for country code {country_code}.")
    candidate = current.date() - dt.timedelta(days=1)
    while candidate.weekday() not in weekend_days:
        candidate -= dt.timedelta(days=1)
    weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    return candidate, weekday_names[candidate.weekday()]


def _nearest_previous_weekly_weekend(country_code, timezone_name, study_date, now=None):
    try:
        timezone = ZoneInfo(timezone_name)
        study_day = dt.date.fromisoformat(study_date) if isinstance(study_date, str) else study_date
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("The study date or route time zone is not valid.") from exc
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)
    try:
        calendar = holidays.country_holidays(
            country_code,
            years=(study_day.year - 1, study_day.year, current.year),
            observed=True,
            language="en",
        )
    except (NotImplementedError, ValueError) as exc:
        raise ValueError(f"No weekly weekend calendar is available for country code {country_code}.") from exc
    weekend_days = set(calendar.weekend or ())
    if not weekend_days:
        raise ValueError(f"No weekly weekend calendar is available for country code {country_code}.")
    candidates = [
        study_day + dt.timedelta(days=offset)
        for offset in range(-7, 8)
        if (study_day + dt.timedelta(days=offset)).weekday() in weekend_days
        and (study_day + dt.timedelta(days=offset)) < current.date()
    ]
    if not candidates:
        return _latest_previous_weekly_weekend(country_code, timezone_name, now=current)
    selected = min(candidates, key=lambda day: (abs((day - study_day).days), day))
    weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    return selected, weekday_names[selected.weekday()]


def _validate_holiday_source_request(configuration):
    context = configuration.get("trafficlens_context")
    if not context:
        return
    row = db().execute("SELECT * FROM requests WHERE id=?", (context["source_request_id"],)).fetchone()
    if not row:
        raise ValueError("The TrafficLens request associated with this reference preview no longer exists.")
    owner = request_groups_svc._shared_flow_leader(row, context.get("service"))
    if owner:
        raise ValueError(f"This free-flow service is shared by every period of the link. Use the Directions result of REQ-{owner}.")
    source = catalog_svc.to_dict(row)
    try:
        expected_origin = ", ".join(f"{float(value):.6f}" for value in source["coords"][0])
        expected_destination = ", ".join(
            f"{float(value):.6f}" for value in (
                source["point_c"] if context.get("volume_input") else source["coords"][1]
            )
        )
        study_date = dt.date.fromisoformat(source["d1"])
        if context["service"] == "freeflow_speed":
            if context.get("volume_input"):
                configured_date = dt.date.fromisoformat(context.get("weekend_date", ""))
                today = dt.datetime.now(ZoneInfo(context["timezone"])).date()
                try:
                    calendar = holidays.country_holidays(
                        context["country_code"],
                        years=(configured_date.year, today.year),
                        observed=True,
                        language="en",
                    )
                except (NotImplementedError, ValueError) as exc:
                    raise ValueError("The route's weekly weekend calendar is unavailable.") from exc
                if configured_date >= today or configured_date.weekday() not in set(calendar.weekend or ()):
                    raise ValueError("Choose a weekly weekend date strictly before today.")
                reference_date = configured_date
                reference_label = configured_date.strftime("%A")
            else:
                reference_date, reference_label = _latest_previous_weekly_weekend(
                    context["country_code"], context["timezone"]
                )
            configured_date = context.get("weekend_date")
            configured_label = context.get("weekend_day")
        else:
            reference_date, reference_label = _nearest_public_holiday(
                context["country_code"], study_date
            )
            configured_date = context.get("holiday_date")
            configured_label = context.get("holiday_name")
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("The source request no longer has a valid route or reference date.") from exc
    expected_region = context["country_code"] if context["country_code"] in outscraper.REGIONS else ""
    service_is_requested = context["service"] in source["services"]
    service_is_volume_input = context["service"] == "freeflow_speed" and "volumes" in source["services"]
    if (
        not (service_is_requested or service_is_volume_input)
        or context["study_date"] != study_date.isoformat()
        or configured_date != reference_date.isoformat()
        or configured_label != reference_label
        or configuration["region"] != expected_region
        or configuration["routes"][0]["origin"] != expected_origin
        or configuration["routes"][0]["destination"] != expected_destination
    ):
        reference_name = "weekly weekend date" if context["service"] == "freeflow_speed" else "public holiday"
        raise ValueError(f"The source route, study date, {reference_name}, or region changed. Prepare a new preview.")
    if context.get("volume_input"):
        try:
            timezone = ZoneInfo(context["timezone"])
            departure = dt.datetime.fromtimestamp(configuration["departure_time"], timezone)
            finish = dt.datetime.fromtimestamp(configuration["finish_time"], timezone)
            if (
                departure.date() != reference_date
                or finish.date() != reference_date
                or departure >= finish
                or configuration["routes"][0].get("departure_time") != configuration["departure_time"]
                or configuration["interval"] != context.get("interval_minutes")
                or context.get("departure_time") != configuration["departure_time"]
                or context.get("finish_time") != configuration["finish_time"]
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError, OSError, ZoneInfoNotFoundError) as exc:
            raise ValueError("The free-flow A→C date, time range, or interval no longer matches the reviewed request.") from exc


def _route_holiday_location(coordinate):
    latitude, longitude = coordinate
    return _route_holiday_location_cached(float(latitude), float(longitude))


@lru_cache(maxsize=256)
def _route_holiday_location_cached(latitude, longitude):
    if _timezone_finder is None:
        raise ValueError("Install the project requirements to identify route countries and time zones.")
    query = urlencode({
        "format": "jsonv2", "lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}",
        "zoom": "10", "addressdetails": "1",
    })
    url = f"https://nominatim.openstreetmap.org/reverse?{query}"
    last_error = None
    for attempt in range(3):
        try:
            outbound = UrlRequest(url, headers={
                "User-Agent": "TrafficLens/1.0 (support@trafficlens.example)",
                "Accept": "application/json",
                "Accept-Language": "en",
            })
            with urlopen(outbound, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            address = result.get("address") or {}
            country_code = str(address.get("country_code") or result.get("country_code") or "").upper()
            country_name = str(address.get("country") or result.get("country") or "").strip()
            if not country_name:
                display_name = str(result.get("display_name") or "").strip()
                if display_name:
                    country_name = display_name.split(",")[-1].strip()
            timezone_name = _timezone_finder.timezone_at(lat=latitude, lng=longitude)
            if len(country_code) != 2 or not country_name or not (timezone_name and isinstance(timezone_name, str)):
                raise ValueError("Incomplete route country or time zone metadata")
            ZoneInfo(timezone_name)
            return country_code, country_name, timezone_name
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError, ZoneInfoNotFoundError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise ValueError("Could not identify the route country and local time zone from the route coordinates. Try again later or verify the route location.") from last_error
