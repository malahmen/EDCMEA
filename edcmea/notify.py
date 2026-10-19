"""Alerts go out as email through Charm's pop (https://github.com/charmbracelet/pop),
which sends through Resend or any SMTP server. EDCMEA never holds the mail
settings itself in its config: they are pop's, kept in pop.env (0600) and
handed to pop in its environment - cron gives a job none of the user's own.

pop is run with its output captured and never echoed: `pop --help`, for one,
prints every flag's default from the environment, the SMTP password included.
Nothing secret is ever on pop's command line either.
"""
import os
import shutil
import subprocess

from . import config


class NotifyError(Exception):
    """The alert could not be sent."""


def pop_binary() -> str:
    return os.environ.get("EDCMEA_POP") or shutil.which("pop") or ""


def send(subject: str, body: str, to: str) -> None:
    """Send one email through pop; NotifyError with a reason (never a secret)."""
    pop = pop_binary()
    if not pop:
        raise NotifyError("pop is not installed - https://github.com/charmbracelet/pop (brew install pop)")
    if not to:
        raise NotifyError("no recipient: set one with 'edcmea set email_to you@example.com'")
    env = dict(os.environ)
    env.update(config.load_pop_env())
    if not env.get("POP_FROM"):
        raise NotifyError("no sender: set POP_FROM (edcmea pop-env POP_FROM)")
    if not (env.get("RESEND_API_KEY") or env.get("POP_SMTP_HOST")):
        raise NotifyError("pop has no way to send: set RESEND_API_KEY, or POP_SMTP_HOST and its "
                          "POP_SMTP_* settings (edcmea pop-env)")
    try:
        result = subprocess.run([pop, "--to", to, "--subject", subject, "--body", body],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise NotifyError("pop did not finish within 120 s") from exc
    if result.returncode != 0:
        # pop's own message, with every configured secret value blanked out.
        text = (result.stderr or result.stdout or "").strip()
        for key in config.POP_SECRETS:
            if env.get(key):
                text = text.replace(env[key], "****")
        raise NotifyError(f"pop failed (exit {result.returncode}): {text[:500] or 'no message'}")
