"""Slow down password guessing on the login form.

Every failed sign-in is written to the login_attempts table. Before checking a
password we count recent failures:
  * per email address: 5 failures in 15 minutes locks that address for the rest of the window;
  * per IP address:    30 failures in 15 minutes locks that address.
The table lives in the database (not in memory) so the limit survives restarts and
is shared by every worker process. A successful sign-in clears that email's
failures, but never the IP's, so one valid account cannot be used to reset the IP counter.
"""
import time

from trafficlens.db import db

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 30

_RECENT_BY_EMAIL = "SELECT at FROM login_attempts WHERE email=? AND at>? ORDER BY at"
_RECENT_BY_IP = "SELECT at FROM login_attempts WHERE ip=? AND at>? ORDER BY at"


def _now():
    return time.time()


def _wait_for(sql, value, limit, since):
    if not value:
        return 0
    rows = db().execute(sql, (value, since)).fetchall()
    if len(rows) < limit:
        return 0
    # The block lifts when enough old failures have left the window to drop below the limit.
    unblocked_at = rows[len(rows) - limit]["at"] + WINDOW_SECONDS
    return max(1, int(unblocked_at - _now()) + 1)


def seconds_until_allowed(email, ip):
    """0 when this sign-in attempt may go ahead, otherwise how many seconds to wait."""
    since = _now() - WINDOW_SECONDS
    return max(
        _wait_for(_RECENT_BY_EMAIL, email[:254], MAX_FAILURES_PER_EMAIL, since),
        _wait_for(_RECENT_BY_IP, ip or "", MAX_FAILURES_PER_IP, since),
    )


def record_failure(email, ip):
    now = _now()
    connection = db()
    connection.execute("DELETE FROM login_attempts WHERE at<?", (now - WINDOW_SECONDS,))
    connection.execute("INSERT INTO login_attempts(email,ip,at) VALUES(?,?,?)", (email[:254], ip or "", now))
    connection.commit()


def clear(email):
    connection = db()
    connection.execute("DELETE FROM login_attempts WHERE email=?", (email[:254],))
    connection.commit()
