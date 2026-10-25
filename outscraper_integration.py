"""Validation and HTTP client for the Outscraper Google Maps Directions API."""

import csv
import datetime as dt
import io
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


API_BASE_URL = "https://api.outscraper.cloud"
RESULTS_BASE_URL = "https://api.outscraper.com"
DIRECTIONS_ENDPOINT = f"{API_BASE_URL}/google-maps-directions"
REQUEST_TIMEOUT_SECONDS = 30
MAX_ROUTES = 50
MAX_CSV_BYTES = 1_000_000
TRAVEL_MODES = {"best", "car", "transit", "walk", "bike", "flight"}
LANGUAGES = {
    "en", "de", "es", "es-419", "fr", "hr", "it", "nl",
}
REGIONS = {
    "AF", "AL", "DZ", "AS", "AD", "AO", "AI", "AG", "AR", "AM", "AU", "AT",
    "AZ", "BS", "BH", "BD", "BY", "BE", "BZ", "BJ", "BT", "BO", "BA", "BW",
    "BR", "VG", "BN", "BG", "BF", "BI", "KH", "CM", "CA", "CV", "CF", "TD",
    "CL", "CN", "CO", "CG", "CD", "CK", "CR", "CI", "HR", "CU", "CY", "CZ",
    "DK", "DJ", "DM", "DO", "EC", "EG", "SV", "EE", "ET", "FJ", "FI", "FR",
    "GA", "GM", "GE", "DE", "GH", "GI", "GR", "GL", "GT", "GG", "GY", "HT",
    "HN", "HK", "HU", "IS", "IN", "ID", "IQ", "IE", "IM", "IL", "IT", "JM",
    "JP", "JE", "JO", "KZ", "KE", "KI", "KW", "KG", "LA", "LV", "LB", "LS",
    "LY", "LI", "LT", "LU", "MG", "MW", "MY", "MV", "ML", "MT", "MU", "MX",
    "FM", "MD", "MN", "ME", "MS", "MA", "MZ", "MM", "NA", "NR", "NP", "NL",
    "NZ", "NI", "NE", "NG", "NU", "MK", "NO", "OM", "PK", "PS", "PA", "PG",
    "PY", "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RO", "RU", "RW", "WS",
    "SM", "ST", "SA", "SN", "RS", "SC", "SL", "SG", "SK", "SI", "SB", "SO",
    "ZA", "KR", "ES", "LK", "SH", "VC", "SR", "SE", "CH", "TW", "TJ", "TZ",
    "TH", "TL", "TG", "TO", "TT", "TN", "TR", "TM", "VI", "UG", "UA", "AE",
    "GB", "US", "UY", "UZ", "VU", "VE", "VN", "ZM", "ZW",
}


class OutscraperError(Exception):
    """A safe-to-display integration error."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class SubmissionUncertain(OutscraperError):
    """The provider may have accepted a request before the connection failed."""

    def __init__(self, message):
        super().__init__(message, retryable=True)


def _text(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > 500 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} is required and must be at most 500 characters")
    return value


def _timestamp(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 253402300799:
        raise ValueError(f"{field} must be a valid integer timestamp")
    return value


def _route_departure(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return _timestamp(value, field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timestamp or a Kuwait local date and time")
    try:
        local_time = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M")
        return int(local_time.replace(tzinfo=ZoneInfo("Asia/Kuwait")).timestamp())
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"{field} must be a valid Kuwait local date and time") from exc


def validate_configuration(payload):
    if not isinstance(payload, dict):
        raise ValueError("Configuration must be an object")
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes or len(routes) > MAX_ROUTES:
        raise ValueError(f"Provide between 1 and {MAX_ROUTES} routes")

    clean_routes = []
    for index, route in enumerate(routes, 1):
        if not isinstance(route, dict):
            raise ValueError(f"Route {index} must contain an origin and destination")
        clean_route = {
            "origin": _text(route.get("origin"), f"Route {index} origin"),
            "destination": _text(route.get("destination"), f"Route {index} destination"),
        }
        departure = _route_departure(route.get("departure_time"), f"Route {index} departure time")
        if departure is not None:
            clean_route["departure_time"] = departure
        clean_routes.append(clean_route)

    departure_time = _timestamp(payload.get("departure_time"), "Departure time")
    finish_time = _timestamp(payload.get("finish_time"), "Finish time")
    interval = payload.get("interval")
    if interval in (None, ""):
        interval = None
    elif isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("Interval must be a positive integer")
    if finish_time is not None:
        if departure_time is None or interval is None:
            raise ValueError("Finish time requires departure time and interval")
        if finish_time <= departure_time:
            raise ValueError("Finish time must be after departure time")
    elif interval is not None:
        raise ValueError("Interval can only be used with finish time")

    travel_mode = payload.get("travel_mode") or "best"
    if not isinstance(travel_mode, str) or travel_mode not in TRAVEL_MODES:
        raise ValueError("Unsupported travel mode")
    language = payload.get("language") or "en"
    if not isinstance(language, str) or language not in LANGUAGES:
        raise ValueError("Unsupported language")
    region = payload.get("region") or ""
    if not isinstance(region, str) or (region and region not in REGIONS):
        raise ValueError("Unsupported region")

    configuration = {
        "routes": clean_routes,
        "departure_time": departure_time,
        "finish_time": finish_time,
        "interval": interval,
        "travel_mode": travel_mode,
        "language": language,
        "region": region,
    }
    request_context = payload.get("trafficlens_request")
    if request_context is not None:
        if not isinstance(request_context, dict):
            raise ValueError("TrafficLens request context must be an object")
        request_id = request_context.get("source_request_id")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
            raise ValueError("TrafficLens request context has an invalid request ID")
        try:
            road_ab_meters = float(request_context.get("road_ab_meters"))
        except (TypeError, ValueError) as exc:
            raise ValueError("TrafficLens request context has an invalid A-to-B road distance") from exc
        if not (road_ab_meters > 0 and road_ab_meters < float("inf")):
            raise ValueError("TrafficLens request context has an invalid A-to-B road distance")
        origin = _text(request_context.get("point_a"), "Point A")
        destination = _text(request_context.get("point_c"), "Point C")
        if len(clean_routes) != 1 or clean_routes[0]["origin"] != origin or clean_routes[0]["destination"] != destination:
            raise ValueError("The Scraper route must match Point A to Point C")
        interval_minutes = request_context.get("interval_minutes")
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or interval_minutes <= 0:
            raise ValueError("TrafficLens request context has an invalid interval in minutes")
        country_code = request_context.get("country_code")
        if not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code):
            raise ValueError("TrafficLens request context has an invalid country code")
        if country_code not in REGIONS or payload.get("region") != country_code:
            raise ValueError("The API region must match the country detected from route coordinates")
        country_name = _text(request_context.get("country_name"), "Route country")
        timezone_name = request_context.get("timezone")
        if not isinstance(timezone_name, str):
            raise ValueError("TrafficLens request context has an invalid route time zone")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("TrafficLens request context has an invalid route time zone") from exc
        configuration["trafficlens_request"] = {
            "source_request_id": request_id,
            "point_a": origin,
            "point_b": _text(request_context.get("point_b"), "Point B"),
            "point_c": destination,
            "road_ab_meters": road_ab_meters,
            "interval_minutes": interval_minutes,
            "country_code": country_code,
            "country_name": country_name,
            "timezone": timezone_name,
        }
    context = payload.get("trafficlens_context")
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError("TrafficLens reference context must be an object")
        service = context.get("service")
        if service not in {"freeflow_time", "freeflow_speed"}:
            raise ValueError("Reference context requires a free flow service")
        source_request_id = context.get("source_request_id")
        if isinstance(source_request_id, bool) or not isinstance(source_request_id, int) or source_request_id <= 0:
            raise ValueError("Reference context has an invalid source request")
        try:
            study_date = dt.date.fromisoformat(context.get("study_date", ""))
            timezone = ZoneInfo(context.get("timezone", ""))
        except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Reference context must include a valid study date and time zone") from exc
        reference_type = context.get("reference_type")
        if service == "freeflow_speed":
            if reference_type != "weekly_weekend":
                raise ValueError("Free-flow speed requires a weekly weekend reference date")
            date_value = context.get("weekend_date")
            label_value = context.get("weekend_day")
        else:
            if reference_type not in (None, "public_holiday"):
                raise ValueError("Free-flow time requires a public holiday reference date")
            date_value = context.get("holiday_date")
            label_value = context.get("holiday_name")
        try:
            reference_date = dt.date.fromisoformat(date_value or "")
        except (TypeError, ValueError) as exc:
            raise ValueError("Reference context must include a valid reference date") from exc
        country_code = context.get("country_code")
        if not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code):
            raise ValueError("Reference context has an invalid country code")
        country_name = _text(context.get("country_name"), "Route country")
        reference_label = _text(label_value, "Weekly weekend day" if service == "freeflow_speed" else "Public holiday")
        origin = _text(context.get("origin"), "Reference route origin")
        destination = _text(context.get("destination"), "Reference route destination")
        expected_departure = int(dt.datetime.combine(reference_date, dt.time(7), tzinfo=timezone).timestamp())
        if (
            len(clean_routes) != 1
            or clean_routes[0]["origin"] != origin
            or clean_routes[0]["destination"] != destination
            or clean_routes[0].get("departure_time") != expected_departure
            or departure_time is not None
            or finish_time is not None
            or interval is not None
        ):
            raise ValueError("Reference route and 7:00 AM departure must match the configured context")
        normalized_context = {
            "source_request_id": source_request_id,
            "service": service,
            "study_date": study_date.isoformat(),
            "country_code": country_code,
            "country_name": country_name,
            "timezone": timezone.key,
            "reference_type": "weekly_weekend" if service == "freeflow_speed" else "public_holiday",
            "departure_time": expected_departure,
            "origin": origin,
            "destination": destination,
        }
        if service == "freeflow_speed":
            normalized_context.update(
                weekend_date=reference_date.isoformat(),
                weekend_day=reference_label,
            )
        else:
            normalized_context.update(
                holiday_date=reference_date.isoformat(),
                holiday_name=reference_label,
            )
        configuration["trafficlens_context"] = normalized_context
    return configuration

def api_key_configured():
    key = os.environ.get("OUTSCRAPER_API_KEY", "").strip()
    return bool(key and key.lower() not in {"your-outscraper-api-key", "replace-me"})


def configuration_fingerprint(configuration):
    encoded = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_routes_csv(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Choose a non-empty CSV file")
    if len(content.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("CSV file must be no larger than 1 MB")
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
        raw_headers = reader.fieldnames or []
        if any(not isinstance(header, str) or not header.strip() for header in raw_headers):
            raise ValueError("CSV must have non-empty origin and destination column names")
        headers = [header.strip().lower() for header in raw_headers]
        if len(headers) != len(set(headers)) or not {"origin", "destination"}.issubset(headers):
            raise ValueError("CSV must have unique origin and destination columns")
        if set(headers) - {"origin", "destination"}:
            raise ValueError("CSV may only contain origin and destination columns")
        routes = []
        for index, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"CSV row {index} has too many columns")
            route = {str(key).strip().lower(): value for key, value in row.items() if key is not None}
            if not any(str(value or "").strip() for value in route.values()):
                continue
            routes.append({
                "origin": _text(route.get("origin"), f"CSV row {index} origin"),
                "destination": _text(route.get("destination"), f"CSV row {index} destination"),
            })
            if len(routes) > MAX_ROUTES:
                raise ValueError(f"CSV may contain no more than {MAX_ROUTES} routes")
        if not routes:
            raise ValueError("CSV contains no routes")
        return routes
    except (csv.Error, UnicodeError) as exc:
        raise ValueError("Could not read CSV file") from exc


def _query_parameters(route, configuration):
    departure_time = route.get("departure_time", configuration["departure_time"])
    params = [
        ("origin", route["origin"]),
        ("destination", route["destination"]),
        ("async", "true"),
        ("travel_mode", configuration["travel_mode"]),
        ("language", configuration["language"]),
    ]
    if departure_time is not None:
        params.append(("departure_time", str(departure_time)))
    for key in ("finish_time", "interval"):
        if configuration[key] is not None:
            params.append((key, str(configuration[key])))
    if configuration["region"]:
        params.append(("region", configuration["region"]))
    return params


def _request_url(path, params, base_url):
    url = f"{base_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return url


def directions_request_details(route, configuration):
    params = _query_parameters(route, configuration)
    return {
        "method": "GET",
        "url": _request_url("/google-maps-directions", params, API_BASE_URL),
        "headers": {
            "Accept": "application/json",
            "X-API-KEY": "Configured; value hidden",
        },
        "query_parameters": [{"name": name, "value": value} for name, value in params],
    }


def _read_json_response(response):
    body = response.read(10_000_001)
    if len(body) > 10_000_000:
        raise SubmissionUncertain("Outscraper returned an oversized response; submission status is uncertain")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SubmissionUncertain("Outscraper returned an invalid response; submission status is uncertain") from exc
    if not isinstance(decoded, dict):
        raise SubmissionUncertain("Outscraper returned an unexpected response; submission status is uncertain")
    return decoded


def _request(path, api_key, params=None, base_url=API_BASE_URL):
    if not api_key:
        raise OutscraperError("OUTSCRAPER_API_KEY is not configured")
    url = _request_url(path, params, base_url)
    req = urllib.request.Request(url, headers={"X-API-KEY": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return _read_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise OutscraperError("Outscraper rate limit reached (HTTP 429)", retryable=True) from None
        if 500 <= exc.code <= 599:
            raise SubmissionUncertain(f"Outscraper returned HTTP {exc.code}; submission status is uncertain") from None
        raise OutscraperError(f"Outscraper returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        raise SubmissionUncertain("Connection to Outscraper failed; submission status is uncertain") from None


def submit_route(route, configuration, api_key=None):
    key = (api_key if api_key is not None else os.environ.get("OUTSCRAPER_API_KEY", "")).strip()
    return _request("/google-maps-directions", key, _query_parameters(route, configuration))

def valid_request_id(request_id):
    return isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is not None


def fetch_request(provider_request_id, api_key=None):
    if not valid_request_id(provider_request_id):
        raise ValueError("Invalid Outscraper request ID")
    key = (api_key if api_key is not None else os.environ.get("OUTSCRAPER_API_KEY", "")).strip()
    return _request(
        f"/requests/{urllib.parse.quote(provider_request_id, safe='')}", key,
        base_url=RESULTS_BASE_URL,
    )


def provider_state(response):
    if response.get("error"):
        return "failed"
    status = str(response.get("status") or "").strip().lower()
    if status in {"success", "completed", "complete"}:
        return "completed"
    if status in {"pending", "running", "queued", "processing"}:
        return "running"
    if status in {"failed", "failure", "error"}:
        return "failed"
    return "unknown"


def result_count(response):
    data = response.get("data")
    if data is None:
        return 0
    if not isinstance(data, list):
        return 1
    return sum(result_count({"data": item}) if isinstance(item, list) else 1 for item in data)


def redact_provider_data(value, api_key=None):
    key = api_key if api_key is not None else os.environ.get("OUTSCRAPER_API_KEY", "")
    if isinstance(value, dict):
        return {
            name: "[REDACTED]" if re.search(r"api[_-]?key|authorization|secret|password", str(name), re.I)
            else redact_provider_data(item, key)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [redact_provider_data(item, key) for item in value]
    if isinstance(value, str):
        if key:
            value = value.replace(key, "[REDACTED]")
        value = re.sub(r"(?i)(x-api-key\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", value)
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", value)
    return value
