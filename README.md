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

# and from cron, every 10 minutes
*/10 * * * * /path/to/EDCMEA/edcmea.sh check >> ~/.local/state/edcmea/check.log 2>&1
```

[scomp-link](https://github.com/malahmen/scomp-link) has a menu for all of it,
schedule included (`scripts/edcmea/edcmea.sh`).

## Commands

| Command | What it does |
| --- | --- |
| `check [--dry-run] [--rule NAME]` | Run every rule once and email what is new. `--dry-run` shows what would be sent and records nothing. |
| `rules list` | The watch rules. |
| `rules add NAME COMMODITY MIN_PRICE [filters]` | Add a rule, or replace the one with that name. |
| `rules remove NAME` | Delete a rule. |
| `commodities [TEXT]` | Commodity names Ardent knows, with the best sell price there. |
| `status` | What each rule last alerted, and when. |
| `set KEY VALUE` / `config` | Change a setting / show paths and settings as `key=value`. |
| `pop-env KEY` / `--import` / `--list` | pop's mail settings: one from stdin, all from this environment, or which are set. |
| `notify-test` | Send one test email. |

Exit codes: `0` ok, `1` a rule or the email failed (the others still ran), `2`
a configuration or usage error.

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

For each rule, the best matching station is emailed when it is new to that
rule, when its price has risen by `realert_increase_percent` (5) since the last
alert, or once `cooldown_hours` (24) have passed. So a price that stays high
mails once a day, not once a run. The email lists the best `matches_in_alert`
(5) stations. A run that overlaps another (a slow API, a short cron interval)
exits at once.

## Files

| Path | What |
| --- | --- |
| `~/.config/edcmea/config.json` | settings and rules (0600) |
| `~/.config/edcmea/pop.env` | pop's settings, SMTP password or Resend key included (0600) |
| `~/.local/state/edcmea/state.json` | what each rule last alerted |

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
