"""Market data from Ardent Insight's public API (no key needed).

eddb.io, which the first EDCMEA scraped, closed in April 2023. Ardent serves the
same EDDN market feed as JSON:

    /v2/commodity/name/<name>/imports   stations buying it, best price first
    /v2/system/name/<name>              a system's coordinates
    /v2/commodities                     every commodity, with price ranges

Ardent filters server-side on fleetCarriers, maxDaysAgo, minVolume and minPrice,
and returns at most 100 rows; it ignores `limit`. Everything else (pad size,
distance) is filtered here, in rules.py.

An unknown commodity is not an error there: its imports are an empty list, the
same answer as "nobody pays that much". So names are resolved against
/v2/commodities when a rule is written (resolve_commodity), never trusted.
"""
import difflib
import email.utils
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = os.environ.get("EDCMEA_API_URL", "https://api.ardent-insight.com/v2")
USER_AGENT = "EDCMEA/2 (+https://github.com/malahmen/EDCMEA)"
TIMEOUT = 30            # seconds, per socket operation (connect, one read)
RETRIES = 3
RETRY_AFTER_MAX = 60    # the longest a 429's Retry-After is waited for
CHUNK = 64 * 1024

# A monotonic time after which no request starts and no body is read further
# (set_deadline). TIMEOUT alone bounds one socket operation, not a request: a
# peer dripping a byte every 29 s would hold a run - and its lock - for hours.
_deadline = None


class ApiError(Exception):
    """The market data could not be fetched."""


def set_deadline(seconds) -> None:
    """Every request from now on must finish within `seconds` (None: no limit)."""
    global _deadline
    _deadline = None if seconds is None else time.monotonic() + float(seconds)


def _remaining() -> float:
    return float("inf") if _deadline is None else _deadline - time.monotonic()


def _read(response) -> bytes:
    """The body, chunk by chunk, so the deadline is checked between reads (a
    plain read() loops inside the socket until the whole body is in)."""
    parts = []
    while True:
        if _remaining() <= 0:
            raise ApiError("the run's deadline passed while reading an answer")
        chunk = response.read1(CHUNK)
        if not chunk:
            if getattr(response, "length", None):    # the peer closed before Content-Length
                raise http.client.IncompleteRead(b"".join(parts), response.length)
            return b"".join(parts)
        parts.append(chunk)


def _retry_after(exc) -> float:
    """A 429's Retry-After (seconds or an HTTP date), capped; 0 if absent."""
    value = (exc.headers.get("Retry-After") if exc.headers else None) or ""
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = when.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return 0.0
    if seconds != seconds:      # nan
        return 0.0
    return max(0.0, min(seconds, RETRY_AFTER_MAX))


def _get(path: str, params: dict = None):
    url = API_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(RETRIES):
        wait = 2 ** attempt
        left = _remaining()
        if left <= 0:
            raise ApiError(f"{url}: the run's deadline passed")
        try:
            with urllib.request.urlopen(request, timeout=min(TIMEOUT, left)) as response:
                return json.loads(_read(response).decode("utf-8"))
        except urllib.error.HTTPError as exc:      # before OSError: it is one
            exc.close()
            if exc.code == 404:
                return None
            last = exc
            if exc.code == 429:
                wait = max(wait, _retry_after(exc))
            elif exc.code < 500:
                break                   # a client error does not get better by asking again
        # Anything the network or the answer can throw: a reset, a timeout
        # (socket.timeout is not TimeoutError before 3.10), a truncated body
        # (http.client.IncompleteRead), bytes that are not UTF-8 JSON.
        except (OSError, http.client.HTTPException, ValueError) as exc:
            last = exc
        if attempt + 1 < RETRIES:
            if wait >= _remaining():
                break
            time.sleep(wait)
    raise ApiError(f"{url}: {last}")


def commodity_key(name: str) -> str:
    """Ardent's path form of a commodity: lower case, no spaces
    ("Advanced Catalysers" -> "advancedcatalysers")."""
    return "".join(name.lower().split())


def resolve_commodity(name: str) -> str:
    """The commodity's key if Ardent knows it; else ValueError naming the
    closest known ones ("lowtemperaturediamonds" -> lowtemperaturediamond?)."""
    known = {commodity_key(row["commodityName"]) for row in commodities()}
    key = commodity_key(name)
    if key in known:
        return key
    close = difflib.get_close_matches(key, sorted(known), n=3, cutoff=0.6)
    hint = f" - did you mean {', '.join(close)}?" if close else " (see: edcmea commodities)"
    raise ValueError(f"no commodity '{name}' on Ardent{hint}")


def imports(commodity: str, *, min_price: int = 0, min_demand: int = 0,
            max_days_ago: float = None, fleet_carriers: bool = False) -> list:
    """Stations buying `commodity`, best sell price first (at most 100)."""
    params = {"fleetCarriers": "true" if fleet_carriers else "false"}
    if min_price:
        params["minPrice"] = int(min_price)
    if min_demand:
        params["minVolume"] = int(min_demand)
    if max_days_ago is not None:
        params["maxDaysAgo"] = max_days_ago
    rows = _get(f"/commodity/name/{urllib.parse.quote(commodity_key(commodity), safe='')}/imports", params)
    if not isinstance(rows, list):
        raise ApiError(f"unexpected answer for {commodity}: {str(rows)[:200]}")
    return rows


def system(name: str):
    """(x, y, z) of a star system, or None if Ardent does not know it."""
    data = _get(f"/system/name/{urllib.parse.quote(name, safe='')}")
    if not data:
        return None
    try:
        return (float(data["systemX"]), float(data["systemY"]), float(data["systemZ"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(f"no usable coordinates for system '{name}': {str(data)[:200]}") from exc


def commodities() -> list:
    """Every commodity Ardent has a market for: [{commodityName, maxSellPrice, ...}]."""
    rows = _get("/commodities")
    if not isinstance(rows, list):
        raise ApiError("unexpected answer for the commodity list")
    return rows
