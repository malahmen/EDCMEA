"""Where EDCMEA keeps things, and what it keeps.

    $XDG_CONFIG_HOME/edcmea/config.json   settings and watch rules (yours to edit)
    $XDG_CONFIG_HOME/edcmea/pop.env       pop's settings: SMTP or Resend (0600)
    $XDG_STATE_HOME/edcmea/state.json     what was last alerted, per rule
    $XDG_STATE_HOME/edcmea/check.lock     one `check` at a time

EDCMEA_CONFIG_DIR / EDCMEA_STATE_DIR override the two folders (the tests use
them). Every file is written whole to a temporary file and renamed, so a run
killed half-way never leaves a truncated config or state behind.
"""
import json
import math
import os
import stat
import tempfile

SETTINGS = {
    # who gets the alerts; pop's own POP_FROM (in pop.env) says who sends them
    "email_to": "",
    # the same best station is alerted again only when its price has risen by
    # this much, or after cooldown_hours - otherwise every run would mail
    "realert_increase_percent": 5.0,
    "cooldown_hours": 24.0,
    # how many of the best matches an alert lists
    "matches_in_alert": 5,
    # a run stops asking Ardent after this long (the rules left are reported
    # as failed), so a slow peer cannot hold the lock past the next run
    "run_deadline_minutes": 4.0,
}

# The pop settings EDCMEA passes on (pop reads them from its environment, and
# cron has none of the user's): https://github.com/charmbracelet/pop
POP_KEYS = ("POP_FROM", "POP_SIGNATURE", "RESEND_API_KEY", "POP_SMTP_HOST", "POP_SMTP_PORT",
            "POP_SMTP_USERNAME", "POP_SMTP_PASSWORD", "POP_SMTP_ENCRYPTION",
            "POP_SMTP_INSECURE_SKIP_VERIFY")
POP_SECRETS = ("RESEND_API_KEY", "POP_SMTP_PASSWORD")


def config_dir() -> str:
    return os.environ.get("EDCMEA_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "edcmea")


def state_dir() -> str:
    return os.environ.get("EDCMEA_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "edcmea")


def config_file() -> str:
    return os.path.join(config_dir(), "config.json")


def pop_env_file() -> str:
    return os.path.join(config_dir(), "pop.env")


def state_file() -> str:
    return os.path.join(state_dir(), "state.json")


def lock_file() -> str:
    return os.path.join(state_dir(), "check.lock")


def write_private(path: str, text: str) -> None:
    """`text` into `path`, owner-only, atomically."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as exc:            # not JSON, or not UTF-8 at all
        raise ValueError(f"{path} is not valid JSON ({exc}) - fix it or remove it") from exc
    if not isinstance(data, type(default)):
        raise ValueError(f"{path} holds a {type(data).__name__}, not a JSON "
                         f"{'object' if isinstance(default, dict) else type(default).__name__}"
                         " - fix it or remove it")
    return data


def load() -> dict:
    """{'settings': {...}, 'rules': [...]}, defaults filled in."""
    data = _read_json(config_file(), {})
    settings = dict(SETTINGS)
    settings.update(data.get("settings") or {})
    return {"settings": settings, "rules": list(data.get("rules") or [])}


def save(cfg: dict) -> None:
    write_private(config_file(), json.dumps(cfg, indent=2, sort_keys=True) + "\n")


def set_setting(cfg: dict, key: str, value: str) -> None:
    """Set one setting from text, typed like its default; ValueError if unknown or bad."""
    if key not in SETTINGS:
        raise ValueError(f"no setting '{key}' (there are: {', '.join(SETTINGS)})")
    default = SETTINGS[key]
    try:
        if isinstance(default, bool):
            typed = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            typed = int(value)
        elif isinstance(default, float):
            typed = float(value)
            if not math.isfinite(typed):
                raise ValueError(value)
        else:
            typed = value
    except ValueError as exc:
        raise ValueError(f"{key} must be a {type(default).__name__}, got '{value}'") from exc
    if isinstance(typed, (int, float)) and not isinstance(typed, bool) and typed < 0:
        raise ValueError(f"{key} cannot be negative")
    if key == "email_to" and typed and "@" not in typed:
        raise ValueError(f"email_to must be an address, got '{value}'")
    cfg["settings"][key] = typed


def load_state() -> dict:
    """{rule name: {...last alert...}}. A state file that cannot be read is an
    error naming it, never an empty state: that would re-mail every alert."""
    path = state_file()
    state = _read_json(path, {})
    for name, last in state.items():
        if not isinstance(last, dict):
            raise ValueError(f"{path}: the entry for '{name}' is not a JSON object - fix it or remove it")
    return state


def save_state(state: dict) -> None:
    write_private(state_file(), json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_pop_env() -> dict:
    """pop.env as a dict (KEY=value lines; # comments and blanks ignored).

    EDCMEA writes it 0600; one made by hand may not be. A file others can read,
    or someone else's, is refused (ValueError naming it): it holds a password.
    A value is everything after the first '=' up to the line break - spaces
    included, a password may end in one."""
    path = pop_env_file()
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            info = os.fstat(fh.fileno())      # the file opened, not the name
            if info.st_uid != os.getuid():
                raise ValueError(f"{path} belongs to another user (uid {info.st_uid}) - "
                                 "it is not used; recreate it with: edcmea pop-env")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(f"{path} can be read by others ({stat.S_IMODE(info.st_mode):04o}) - "
                                 f"it is not used; chmod 600 {path}")
            for line in fh:
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in POP_KEYS:
                    env[key.strip()] = value
    except FileNotFoundError:
        pass
    return env


def set_pop_env(key: str, value: str) -> None:
    """One pop setting into pop.env (empty value: removed). Never echoed anywhere."""
    if key not in POP_KEYS:
        raise ValueError(f"not a pop setting: {key} (there are: {', '.join(POP_KEYS)})")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot hold a line break")
    env = load_pop_env()
    if value:
        env[key] = value
    else:
        env.pop(key, None)
    lines = ["# pop's settings, passed to it by EDCMEA (edcmea pop-env). Owner-only."]
    lines += [f"{k}={env[k]}" for k in POP_KEYS if k in env]
    write_private(pop_env_file(), "\n".join(lines) + "\n")
