"""One run of every watch rule - what cron calls (`edcmea check`).

For each rule: the best station that matches. It is alerted when it is new to
that rule, when its price has risen by realert_increase_percent since the last
alert, or once cooldown_hours have passed - so a price that stays high mails
once a day, not once a run. One rule failing (Ardent down, an unknown system)
is reported and the others still run; the exit code says so.

Runs never overlap: a second `check` while one is running exits at once (a
slow Ardent and a short cron interval would otherwise send the same alert
twice).
"""
import fcntl
import os
import time
from datetime import datetime, timezone

from . import ardent, config, notify, rules

EX_OK, EX_FAILED, EX_CONFIG = 0, 1, 2


def _age(updated_at: str) -> str:
    try:
        then = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return "age unknown"
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def line_for(row: dict) -> str:
    pad = {1: "S", 2: "M", 3: "L"}.get(row.get("maxLandingPadSize"), "?")
    where = f"{row.get('stationName')} ({row.get('stationType')}, pad {pad}, " \
            f"{float(row.get('distanceToArrival') or 0):,.0f} ls) - {row.get('systemName')}"
    extra = f", {row['distance_ly']:,.1f} ly away" if "distance_ly" in row else ""
    return (f"{int(row['sellPrice']):,} cr - {where} - demand {int(row.get('demand') or 0):,}"
            f"{extra} - updated {_age(row.get('updatedAt'))}")


def should_alert(best: dict, last: dict, settings: dict, now: float) -> bool:
    if not last:
        return True
    if best.get("marketId") != last.get("market_id"):
        return True
    price, before = int(best["sellPrice"]), int(last.get("price") or 0)
    if price > before and price >= before * (1 + float(settings["realert_increase_percent"]) / 100):
        return True
    return now - float(last.get("at", 0)) >= float(settings["cooldown_hours"]) * 3600


def compose(rule: dict, found: list, settings: dict):
    best = found[0]
    subject = (f"EDCMEA: {rule['commodity']} {int(best['sellPrice']):,} cr at "
               f"{best.get('stationName')} ({best.get('systemName')})")
    shown = found[:max(1, int(settings["matches_in_alert"]))]
    body = [f"Rule '{rule['name']}': {rules.describe(rule)}", "",
            f"The best {len(shown)} of {len(found)} matching stations:", ""]
    body += [f"- {line_for(row)}" for row in shown]
    body += ["", "Market data: Ardent Insight (EDDN). Sent by EDCMEA."]
    return subject, "\n".join(body)


def run(dry_run: bool = False, only: str = None, out=print) -> int:
    cfg = config.load()
    settings, all_rules = cfg["settings"], cfg["rules"]
    known = {r.get("name") for r in all_rules}
    if only:
        all_rules = [r for r in all_rules if r.get("name") == only]
        if not all_rules:
            out(f"[error] no rule named '{only}'")
            return EX_CONFIG
    if not all_rules:
        out("[error] no watch rules yet - add one: edcmea rules add --help")
        return EX_CONFIG
    if not dry_run and not settings.get("email_to"):
        out("[error] no recipient - edcmea set email_to you@example.com")
        return EX_CONFIG

    os.makedirs(config.state_dir(), mode=0o700, exist_ok=True)
    with open(config.lock_file(), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            out("[info] another check is running - this one stops here")
            return EX_OK
        return _run_locked(settings, all_rules, known, dry_run, out)


def _run_locked(settings: dict, all_rules: list, known: set, dry_run: bool, out) -> int:
    state = config.load_state()
    failed = 0
    now = time.time()
    for raw in all_rules:
        name = raw.get("name", "?")
        try:
            rule = rules.validate(raw)
            origin = None
            if "near" in rule:
                origin = ardent.system(rule["near"])
                if origin is None:
                    raise ValueError(f"Ardent knows no system '{rule['near']}'")
            found = rules.matches(rule, origin)
        except (ardent.ApiError, ValueError) as exc:
            out(f"[error] {name}: {exc}")
            failed += 1
            continue
        if not found:
            out(f"[ok]    {name}: nothing at {rule['min_price']:,} cr or more")
            continue
        best = found[0]
        if not should_alert(best, state.get(name) or {}, settings, now):
            out(f"[ok]    {name}: still {line_for(best)} - already alerted")
            continue
        subject, body = compose(rule, found, settings)
        if dry_run:
            out(f"[would alert] {name}: {line_for(best)}")
            continue
        try:
            notify.send(subject, body, settings["email_to"])
        except notify.NotifyError as exc:
            out(f"[error] {name}: {exc}")
            failed += 1
            continue
        state[name] = {"market_id": best.get("marketId"), "price": int(best["sellPrice"]),
                       "station": best.get("stationName"), "system": best.get("systemName"), "at": now}
        out(f"[alert] {name}: {line_for(best)}")
    if not dry_run:
        # Forget rules no longer configured, so a re-added one alerts afresh.
        # `known` is every configured rule, not just this run's (--rule).
        config.save_state({k: v for k, v in state.items() if k in known})
    return EX_FAILED if failed else EX_OK
