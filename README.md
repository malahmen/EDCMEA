# EDCMEA

**Elite Dangerous Commodity Market Email Alert** - watches Elite Dangerous
commodity markets and emails you when a station pays what you are waiting for.

A command-line tool made to run from cron: each `edcmea check` looks at every
watch rule once, emails what is new, and exits. Market data comes from
[Ardent Insight](https://ardent-insight.com)'s public API (the EDDN feed, no
key needed); mail goes out through Charm's [pop](https://github.com/charmbracelet/pop),
over Resend or any SMTP server. Python 3.9+, standard library only.

> v2 is a rewrite. v1 (tag `v1.0.0`) was a Node script scraping eddb.io, which
> closed in April 2023.

## Quick start

```sh
# pop sends the mail: tell EDCMEA how (values are read from stdin, never argv)
./edcmea.sh pop-env --import            # copy POP_*/RESEND_API_KEY from this shell, or:
echo "alerts@example.com" | ./edcmea.sh pop-env POP_FROM
echo "re_xxxxxxxx"        | ./edcmea.sh pop-env RESEND_API_KEY
./edcmea.sh set email_to you@example.com
./edcmea.sh notify-test

# what to watch
./edcmea.sh commodities diamond
./edcmea.sh rules add ltd lowtemperaturediamond 600000 --pad L --near Sol --max-ly 300
./edcmea.sh check --dry-run

# and from cron, every 10 minutes. cron's PATH is /usr/bin:/bin: name pop's
# full path (check exits 2 at once if it cannot find pop). --quiet logs only
# alerts and errors.
*/10 * * * * EDCMEA_POP=/usr/local/bin/pop /path/to/EDCMEA/edcmea.sh check --quiet >> ~/.local/state/edcmea/check.log 2>&1
```

[scomp-link](https://github.com/malahmen/scomp-link) has a menu for all of it,
schedule included (`scripts/edcmea/edcmea.sh`): it writes the crontab line or
systemd unit with the full paths of python3, pop and EDCMEA, and the
configuration and state folders, as they are when you set it up.

## Commands

| Command | What it does |
| --- | --- |
| `check [--dry-run] [--rule NAME] [--quiet]` | Run every rule once and email what is new. `--dry-run` shows what would be sent and records nothing; `--quiet` prints only alerts and errors. |
| `rules list` | The watch rules. |
| `rules add NAME COMMODITY MIN_PRICE [filters]` | Add a rule, or replace the one with that name. |
| `rules remove NAME` | Delete a rule. |
| `commodities [TEXT]` | Commodity names Ardent knows, with the best sell price there. |
| `status` | What each rule last alerted, and when. |
| `set KEY VALUE` / `config` | Change a setting / show paths and settings as `key=value`. |
| `pop-env KEY` / `--import` / `--list` | pop's mail settings: one from stdin, all from this environment, or which are set. |
| `notify-test` | Send one test email. |

Exit codes: `0` ok, `1` a rule or the email failed (the others still ran), `2`
a configuration or usage error - pop not found included, reported before
anything is fetched (a run with nothing to send would otherwise look healthy
until the first alert).

## Rules

A rule names a commodity and the sell price worth an alert, plus any of:

| Filter | Meaning |
| --- | --- |
| `--min-demand N` | the station wants at least N |
| `--pad S\|M\|L` | the landing pad your ship needs |
| `--max-age-days N` | ignore market data older than N days |
| `--near SYSTEM --max-ly N` | at most N light years from SYSTEM |
| `--max-arrival-ls N` | at most N ls from the arrival star |
| `--carriers` | include fleet carriers (left out by default: one asking 9.6 M cr for a demand of 1 tops every list) |

The commodity is checked against Ardent before the rule is saved: there an
unknown name is an empty market, not an error, so a typo would never alert.

## When it emails

For each rule, the best matching station is emailed the first time, when the
best price on offer has risen by `realert_increase_percent` (5) over the last
alerted one - whichever station pays it - or once `cooldown_hours` (24) have
passed. So a price that stays high mails once a day, not once a run, and a
different station is news only if it pays more: two stations trading the top
spot, or the best one dropping out and a worse one taking its place, mail
nothing until the cooldown. The email lists the best `matches_in_alert` (5)
stations.

What was alerted is saved right after each email, so a failure later in the
run never sends it again; a SIGTERM between sending and saving waits for the
save. One rule failing does not stop the others.

A run that overlaps another (a slow API, a short cron interval) exits at once.
A run stops asking Ardent after `run_deadline_minutes` (4; 0 for none) and
reports the rules it did not reach, so a slow peer cannot hold the lock into
the next runs. The lock file holds the running check's pid and start time; a
run that finds one stuck far past the deadline says so and exits 1.

Ardent is asked three times before a rule fails: a dropped connection, a
truncated answer or a 5xx is retried, a 429 after its `Retry-After` (at most
60 s), any other 4xx not.

## Files

| Path | What |
| --- | --- |
| `~/.config/edcmea/config.json` | settings and rules (0600) |
| `~/.config/edcmea/pop.env` | pop's settings, SMTP password or Resend key included (0600; one others can read, or another user's, is refused) |
| `~/.local/state/edcmea/state.json` | what each rule last alerted; one that cannot be read stops `check` with its name - never treated as empty, which would re-mail everything |

`$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` are honoured; `EDCMEA_CONFIG_DIR` and
`EDCMEA_STATE_DIR` override the folders, `EDCMEA_POP` the pop binary,
`EDCMEA_API_URL` the API.

**pop and secrets.** EDCMEA passes pop its settings in the environment and
nothing secret on its command line. It never shows pop's output as is: `pop
--help`, for one, prints each flag's default from the environment - the SMTP
password included.

## Tests

```sh
python3 -m unittest discover -s tests
```

A fake Ardent (a local HTTP server) and a fake pop (a script that records what
it was given): no network, no mail. CI runs them on every push.

## License

MIT - see [LICENSE](LICENSE).
