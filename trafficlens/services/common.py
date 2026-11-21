"""Small helpers and the background-worker registry shared by several parts of the app."""
import datetime as dt
import hashlib
import threading
from trafficlens.config import DATA_INTERVALS, DEFAULT_DATA_INTERVAL


def _clean_data_interval(raw):
    """Returns (minutes, error). Missing value -> the 15-minute default."""
    if raw in (None, ""):
        return DEFAULT_DATA_INTERVAL, None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)) or not str(raw).strip().isdigit():
        return None, "Choose how often you want the data"
    minutes = int(str(raw).strip())
    if minutes not in DATA_INTERVALS:
        return None, "Choose how often you want the data: " + ", ".join(str(x) for x in DATA_INTERVALS) + " minutes"
    return minutes, None


_outscraper_workers_lock = threading.Lock()


_outscraper_workers = set()


def _timestamp_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fmt_hours(hs):
    hs, out, i = sorted(set(hs)), [], 0
    while i < len(hs):
        j = i
        while j + 1 < len(hs) and hs[j + 1] == hs[j] + 1: j += 1
        out.append(f"{hs[i]:02d}:00-{hs[j] + 1:02d}:00"); i = j + 1
    return ", ".join(out) or "-"


def _clean_request_title(value):
    """Client-chosen request name: optional, single line, at most 80 characters."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())[:80]
    return cleaned or None


def _parse_local_time(value):
    t = dt.time.fromisoformat(str(value or ""))
    if t.tzinfo or t.microsecond:
        raise ValueError
    return t
