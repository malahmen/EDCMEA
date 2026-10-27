"""One run of every watch rule - what cron calls (`edcmea check`).

For each rule: the best station that matches. It is alerted the first time,
when the best price on offer has risen by realert_increase_percent over the
last alerted one - whichever station pays it - or once cooldown_hours have
passed. A different station at the same or a lower price is not news: two
stations trading the top spot, or the best one dropping out, mail nothing
until the cooldown.

One rule failing - Ardent down, an unknown system, an answer of an unexpected
shape - is reported and the others still run; the exit code says so. What was
alerted is saved right after each email, so a later failure, or a SIGTERM
(held off between sending and saving), never sends it twice.

Runs never overlap: a second `check` while one is running exits at once, and
a run stops asking Ardent after run_deadline_minutes so it cannot hold the
lock for hours. The lock file says who holds it and since when; a holder far
past the deadline is reported as stuck (exit 1), not waited for in silence.
"""
import contextlib
import fcntl
import os
import signal
import threading
import time
from datetime import datetime, timezone

from . import ardent, config, notify, rules

EX_OK, EX_FAILED, EX_CONFIG = 0, 1, 2

# A lock held this long past the run deadline is a stuck run (pop's own
# timeout, once per alert, is the only other wait).
STUCK_AFTER_DEADLINE = 30 * 60


class Terminated(BaseException):
    """A stop signal (BaseException: no `except Exception` may swallow it)."""


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
    """New to the rule, a real improvement on the last alert, or the cooldown."""
    if not last:
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


class _Signals:
    """SIGTERM, SIGHUP and SIGINT stop the run (Terminated) - except between
    sending an email and saving that it was sent, where the stop waits for
    the save: a run killed there would send it again next time."""

    SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def __init__(self):
        self.depth = 0
        self.pending = None
        self.old = {}

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for sig in self.SIGNALS:
                self.old[sig] = signal.signal(sig, self._handler)
        return self

    def __exit__(self, *exc):
        for sig, old in self.old.items():
            signal.signal(sig, old)
        return False

    def _handler(self, signum, _frame):
        name = signal.Signals(signum).name
        if self.depth:
            self.pending = name
            return
        raise Terminated(name)

    @contextlib.contextmanager
    def critical(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if self.pending and not self.depth:
                raise Terminated(self.pending)


def _busy(lock, settings: dict, out, say) -> int:
    """Another check holds the lock: say who, and whether it is stuck."""
    lock.seek(0)
    try:
        pid, started = (int(float(v)) for v in lock.read().split()[:2])
    except ValueError:
        say("[info] another check is running - this one stops here")
        return EX_OK
    age = time.time() - started
    limit = float(settings.get("run_deadline_minutes") or 0) * 60 + STUCK_AFTER_DEADLINE
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    if age > limit:
        out(f"[error] check pid {pid} has held {config.lock_file()} since {when} ({age / 60:.0f} min) - "
            f"it is stuck; stop it (kill {pid}) and the next run goes ahead")
        return EX_FAILED
    say(f"[info] another check is running (pid {pid}, since {when}) - this one stops here")
    return EX_OK


def run(dry_run: bool = False, only: str = None, out=print, quiet: bool = False) -> int:
    """`quiet`: only alerts and errors are printed (a cron log grows by what
    happened, not by one line per rule per run)."""
    say = (lambda _msg: None) if quiet else out
    cfg = config.load()
    settings, all_rules = cfg["settings"], cfg["rules"]
    known = {r.get("name") for r in all_rules if isinstance(r, dict)}
    if only:
        all_rules = [r for r in all_rules if isinstance(r, dict) and r.get("name") == only]
        if not all_rules:
            out(f"[error] no rule named '{only}'")
            return EX_CONFIG
    if not all_rules:
        out("[error] no watch rules yet - add one: edcmea rules add --help")
        return EX_CONFIG
    if not dry_run:
        if not settings.get("email_to"):
            out("[error] no recipient - edcmea set email_to you@example.com")
            return EX_CONFIG
        # Before anything is fetched: without pop every alert would fail, and
        # a run with nothing to send would look healthy until the first one.
        if not notify.pop_binary():
            out(f"[error] {notify.missing_pop_message()}")
            return EX_CONFIG
        config.load_pop_env()           # a pop.env that would be refused: ValueError, exit 2

    os.makedirs(config.state_dir(), mode=0o700, exist_ok=True)
    # Opened without truncating: the holder's pid and start time are in it.
    fd = os.open(config.lock_file(), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _busy(lock, settings, out, say)
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()} {time.time():.0f}\n")
        lock.flush()
        deadline = float(settings.get("run_deadline_minutes") or 0) * 60
        ardent.set_deadline(deadline or None)
        try:
            with _Signals() as signals:
                return _run_locked(settings, all_rules, known, dry_run, out, say, signals)
        except Terminated as exc:
            out(f"[error] stopped by {exc} - every alert sent so far is recorded")
            return EX_FAILED
        finally:
            ardent.set_deadline(None)


def _kept(state: dict, known: set) -> dict:
    # Forget rules no longer configured, so a re-added one alerts afresh.
    # `known` is every configured rule, not just this run's (--rule).
    return {k: v for k, v in state.items() if k in known}


def _one_rule(raw, state: dict, settings: dict, now: float, dry_run: bool, out, say, signals, known) -> None:
    rule = rules.validate(raw)
    name = rule["name"]
    origin = None
    if "near" in rule:
        origin = ardent.system(rule["near"])
        if origin is None:
            raise ValueError(f"Ardent knows no system '{rule['near']}'")
    found = rules.matches(rule, origin)
    if not found:
        say(f"[ok]    {name}: nothing at {rule['min_price']:,} cr or more")
        return
    best = found[0]
    if not should_alert(best, state.get(name) or {}, settings, now):
        say(f"[ok]    {name}: {line_for(best)} - already alerted, nothing better since")
        return
    subject, body = compose(rule, found, settings)
    if dry_run:
        out(f"[would alert] {name}: {line_for(best)}")
        return
    with signals.critical():
        notify.send(subject, body, settings["email_to"])
        state[name] = {"market_id": best.get("marketId"), "price": int(best["sellPrice"]),
                       "station": best.get("stationName"), "system": best.get("systemName"), "at": now}
        config.save_state(_kept(state, known))
    out(f"[alert] {name}: {line_for(best)}")


def _run_locked(settings: dict, all_rules: list, known: set, dry_run: bool, out, say, signals) -> int:
    state = config.load_state()          # unreadable: ValueError naming it, nothing sent
    failed = 0
    now = time.time()
    for raw in all_rules:
        name = raw.get("name", "?") if isinstance(raw, dict) else "?"
        try:
            _one_rule(raw, state, settings, now, dry_run, out, say, signals, known)
        except (ardent.ApiError, notify.NotifyError, ValueError) as exc:
            out(f"[error] {name}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001 - one rule's surprise must not stop the others
            out(f"[error] {name}: unexpected {type(exc).__name__}: {exc}")
            failed += 1
    if not dry_run:
        config.save_state(_kept(state, known))
    return EX_FAILED if failed else EX_OK
