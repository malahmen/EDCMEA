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
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = os.environ.get("EDCMEA_API_URL", "https://api.ardent-insight.com/v2")
USER_AGENT = "EDCMEA/2 (+https://github.com/malahmen/EDCMEA)"
TIMEOUT = 30
RETRIES = 3


class ApiError(Exception):
    """The market data could not be fetched."""


def _get(path: str, params: dict = None):
    url = API_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
            if exc.code < 500:
                break                   # a client error does not get better by asking again
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < RETRIES:
            time.sleep(2 ** attempt)
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
    rows = _get(f"/commodity/name/{urllib.parse.quote(commodity_key(commodity))}/imports", params)
    if not isinstance(rows, list):
        raise ApiError(f"unexpected answer for {commodity}: {str(rows)[:200]}")
    return rows


def system(name: str):
    """(x, y, z) of a star system, or None if Ardent does not know it."""
    data = _get(f"/system/name/{urllib.parse.quote(name)}")
    if not data:
        return None
    return (float(data["systemX"]), float(data["systemY"]), float(data["systemZ"]))


def commodities() -> list:
    """Every commodity Ardent has a market for: [{commodityName, maxSellPrice, ...}]."""
    rows = _get("/commodities")
    if not isinstance(rows, list):
        raise ApiError("unexpected answer for the commodity list")
    return rows
