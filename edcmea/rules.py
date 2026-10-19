"""Watch rules: what to look for, and the best station that matches.

A rule, as stored in config.json:

    {"name": "ltd", "commodity": "lowtemperaturediamond", "min_price": 600000,
     "min_demand": 100, "pad": "L", "max_age_days": 1,
     "near": "Sol", "max_ly": 200, "max_arrival_ls": 5000, "carriers": false}

Only name, commodity and min_price are required. Ardent applies min_price,
min_demand, max_age_days and carriers itself; pad, near/max_ly and
max_arrival_ls are applied here, from the coordinates and fields of each row.
"""
import math
import re

from . import ardent

PADS = {"S": 1, "M": 2, "L": 3}      # Ardent's maxLandingPadSize
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
FIELDS = ("name", "commodity", "min_price", "min_demand", "pad", "max_age_days",
          "near", "max_ly", "max_arrival_ls", "carriers")


def validate(rule: dict) -> dict:
    """The rule with its values typed and checked; ValueError naming what is wrong."""
    unknown = set(rule) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown rule field(s): {', '.join(sorted(unknown))}")
    out = {}
    name = str(rule.get("name", ""))
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"rule name '{name}': 1-40 of a-z 0-9 _ -, starting with a letter or digit")
    out["name"] = name
    if not rule.get("commodity"):
        raise ValueError("a rule needs a commodity")
    out["commodity"] = ardent.commodity_key(str(rule["commodity"]))
    for key in ("min_price", "min_demand"):
        if rule.get(key) not in (None, ""):
            value = int(rule[key])
            if value < 0:
                raise ValueError(f"{key} cannot be negative")
            out[key] = value
    if "min_price" not in out or out["min_price"] <= 0:
        raise ValueError("a rule needs a min_price above 0 - it is what makes a price worth an alert")
    for key in ("max_age_days", "max_ly", "max_arrival_ls"):
        if rule.get(key) not in (None, ""):
            value = float(rule[key])
            if value <= 0:
                raise ValueError(f"{key} must be above 0")
            out[key] = value
    if rule.get("pad") not in (None, ""):
        pad = str(rule["pad"]).upper()
        if pad not in PADS:
            raise ValueError(f"pad must be S, M or L, got '{rule['pad']}'")
        out["pad"] = pad
    if rule.get("near") not in (None, ""):
        out["near"] = str(rule["near"])
    if ("near" in out) != ("max_ly" in out):
        raise ValueError("near and max_ly go together: a distance needs a system to measure from")
    out["carriers"] = bool(rule.get("carriers", False))
    return out


def distance(a, b) -> float:
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def matches(rule: dict, origin=None) -> list:
    """Rows for `rule`, best sell price first. `origin` is the `near` system's
    coordinates (resolved once by the caller); each row gets `distance_ly`."""
    rows = ardent.imports(rule["commodity"], min_price=rule["min_price"],
                          min_demand=rule.get("min_demand", 0),
                          max_days_ago=rule.get("max_age_days"), fleet_carriers=rule["carriers"])
    out = []
    for row in rows:
        try:
            price = int(row["sellPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        # Ardent applies these too; checked again so a change on its side
        # cannot turn into alerts nobody asked for.
        if price < rule["min_price"]:
            continue
        if int(row.get("demand") or 0) < rule.get("min_demand", 0):
            continue
        if not rule["carriers"] and row.get("stationType") == "FleetCarrier":
            continue
        if "pad" in rule and int(row.get("maxLandingPadSize") or 0) < PADS[rule["pad"]]:
            continue
        if "max_arrival_ls" in rule and float(row.get("distanceToArrival") or 0) > rule["max_arrival_ls"]:
            continue
        if origin is not None:
            here = (float(row["systemX"]), float(row["systemY"]), float(row["systemZ"]))
            row = dict(row, distance_ly=distance(origin, here))
            if row["distance_ly"] > rule["max_ly"]:
                continue
        out.append(row)
    out.sort(key=lambda r: int(r["sellPrice"]), reverse=True)
    return out


def describe(rule: dict) -> str:
    """One line: what the rule watches."""
    parts = [f"{rule['commodity']} >= {rule['min_price']:,} cr"]
    if rule.get("min_demand"):
        parts.append(f"demand >= {rule['min_demand']:,}")
    if rule.get("pad"):
        parts.append(f"pad {rule['pad']}")
    if rule.get("max_age_days"):
        parts.append(f"data <= {rule['max_age_days']:g} days old")
    if rule.get("near"):
        parts.append(f"within {rule['max_ly']:g} ly of {rule['near']}")
    if rule.get("max_arrival_ls"):
        parts.append(f"<= {rule['max_arrival_ls']:g} ls from arrival")
    parts.append("fleet carriers too" if rule.get("carriers") else "no fleet carriers")
    return ", ".join(parts)
