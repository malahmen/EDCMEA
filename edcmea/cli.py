"""edcmea - Elite Dangerous Commodity Market Email Alert.

Watches Elite Dangerous commodity markets (Ardent Insight's data, from EDDN)
and emails, through Charm's pop, when a station pays what a rule asks.

Commands:
  check [--dry-run] [--rule NAME] [--quiet]   run every rule once; email what is new (for cron)
  rules list                        the watch rules
  rules add NAME COMMODITY MIN_PRICE [filters]   add or replace a rule
  rules remove NAME                 delete a rule
  commodities [TEXT]                commodity names Ardent knows (filtered by TEXT)
  status                            what each rule last alerted, and when
  set KEY VALUE                     change a setting (see: config)
  config                            paths, settings and pop's readiness, as key=value
  pop-env KEY                       set one of pop's settings, value read from stdin
  pop-env --import                  copy the POP_*/RESEND_API_KEY of this environment
  pop-env --list                    which pop settings are set (names only)
  notify-test                       send one test email

Exit codes: 0 ok; 1 a rule or the email failed; 2 bad configuration or usage
(pop not found included: `check` says so before fetching anything).

Every rule is checked against Ardent before it is saved: an unknown commodity
there is an empty market, not an error, so a typo would never alert.
"""
import argparse
import os
import sys
import time

from . import ardent, check, config, notify, rules

EX_OK, EX_FAILED, EX_CONFIG = check.EX_OK, check.EX_FAILED, check.EX_CONFIG


def _err(msg: str) -> int:
    print(f"[error] {msg}", file=sys.stderr)
    return EX_CONFIG


def cmd_rules(args) -> int:
    cfg = config.load()
    if args.action == "list":
        if not cfg["rules"]:
            print("no rules yet - edcmea rules add NAME COMMODITY MIN_PRICE")
        for rule in cfg["rules"]:
            try:
                print(f"{rule['name']}\t{rules.describe(rules.validate(rule))}")
            except ValueError as exc:
                print(f"{rule.get('name', '?')}\tINVALID: {exc}")
        return EX_OK
    if args.action == "remove":
        kept = [r for r in cfg["rules"] if r.get("name") != args.name]
        if len(kept) == len(cfg["rules"]):
            return _err(f"no rule named '{args.name}'")
        cfg["rules"] = kept
        config.save(cfg)
        print(f"[ok] removed {args.name}")
        return EX_OK
    # add
    raw = {"name": args.name, "commodity": args.commodity, "min_price": args.min_price,
           "min_demand": args.min_demand, "pad": args.pad, "max_age_days": args.max_age_days,
           "near": args.near, "max_ly": args.max_ly, "max_arrival_ls": args.max_arrival_ls,
           "carriers": args.carriers}
    try:
        rule = rules.validate({k: v for k, v in raw.items() if v is not None})
        rule["commodity"] = ardent.resolve_commodity(rule["commodity"])
        if "near" in rule and ardent.system(rule["near"]) is None:
            raise ValueError(f"Ardent knows no system '{rule['near']}'")
    except ValueError as exc:
        return _err(str(exc))
    except ardent.ApiError as exc:
        print(f"[error] could not check the rule against Ardent: {exc}", file=sys.stderr)
        return EX_FAILED
    replaced = any(r.get("name") == rule["name"] for r in cfg["rules"])
    cfg["rules"] = [r for r in cfg["rules"] if r.get("name") != rule["name"]] + [rule]
    config.save(cfg)
    print(f"[ok] {'replaced' if replaced else 'added'} {rule['name']}: {rules.describe(rule)}")
    return EX_OK


def cmd_commodities(args) -> int:
    try:
        rows = ardent.commodities()
    except ardent.ApiError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EX_FAILED
    needle = ardent.commodity_key(args.text or "")
    for row in sorted(rows, key=lambda r: ardent.commodity_key(r["commodityName"])):
        key = ardent.commodity_key(row["commodityName"])
        if needle in key:
            print(f"{key}\tbest sell {int(row.get('maxSellPrice') or 0):,} cr")
    return EX_OK


def cmd_status(_args) -> int:
    state = config.load_state()
    names = [r.get("name") for r in config.load()["rules"]]
    for name in names:
        last = state.get(name)
        if not last:
            print(f"{name}\tnever alerted")
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last.get("at", 0))))
        print(f"{name}\t{int(last.get('price', 0)):,} cr at {last.get('station')} ({last.get('system')}), {when}")
    return EX_OK


def cmd_set(args) -> int:
    cfg = config.load()
    try:
        config.set_setting(cfg, args.key, args.value)
    except ValueError as exc:
        return _err(str(exc))
    config.save(cfg)
    print(f"[ok] {args.key} = {cfg['settings'][args.key]}")
    return EX_OK


def cmd_config(_args) -> int:
    cfg = config.load()
    try:
        pop_env = config.load_pop_env()
        pop_env_set = ",".join(k for k in config.POP_KEYS if k in pop_env) or "none"
    except ValueError as exc:            # a pop.env it refuses: the rest still shows
        pop_env_set = f"refused - {exc}"
    lines = {"config_file": config.config_file(), "pop_env_file": config.pop_env_file(),
             "state_file": config.state_file(), "rules": len(cfg["rules"]),
             "pop": notify.pop_binary() or "missing",
             "pop_env": pop_env_set,
             "api_url": ardent.API_URL}
    lines.update(cfg["settings"])
    for key, value in lines.items():
        print(f"{key}={value}")
    return EX_OK


def cmd_pop_env(args) -> int:
    if args.list:
        env = config.load_pop_env()
        for key in config.POP_KEYS:
            print(f"{key}\t{'set' if key in env else '-'}")
        return EX_OK
    if args.import_env:
        found = [k for k in config.POP_KEYS if os.environ.get(k)]
        if not found:
            return _err("none of pop's settings is set in this environment")
        for key in found:
            config.set_pop_env(key, os.environ[key])
        print(f"[ok] copied {', '.join(found)} into {config.pop_env_file()}")
        return EX_OK
    if not args.key:
        return _err("pop-env KEY (value on stdin), --import or --list")
    value = sys.stdin.readline().rstrip("\n")
    try:
        config.set_pop_env(args.key, value)
    except ValueError as exc:
        return _err(str(exc))
    print(f"[ok] {args.key} {'set' if value else 'removed'}")
    return EX_OK


def cmd_notify_test(_args) -> int:
    to = config.load()["settings"]["email_to"]
    try:
        notify.send("EDCMEA: test alert", "This is a test from EDCMEA. Alerts will look like this one's sender "
                    "and arrive here.", to)
    except notify.NotifyError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EX_FAILED
    print(f"[ok] test email sent to {to}")
    return EX_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edcmea", description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="run every rule once; email what is new")
    c.add_argument("--dry-run", action="store_true", help="show what would be sent; send and record nothing")
    c.add_argument("--rule", help="only this rule")
    c.add_argument("--quiet", action="store_true",
                   help="print only alerts and errors (for a cron log: it grows by what happened)")

    r = sub.add_parser("rules", help="list, add or remove watch rules")
    rs = r.add_subparsers(dest="action", required=True)
    rs.add_parser("list")
    rm = rs.add_parser("remove")
    rm.add_argument("name")
    ra = rs.add_parser("add", help="add a rule, or replace the one with that name")
    ra.add_argument("name", help="a short name: a-z 0-9 _ -")
    ra.add_argument("commodity", help="as 'edcmea commodities' lists it, e.g. lowtemperaturediamond")
    ra.add_argument("min_price", type=int, help="alert at this sell price (credits) or more")
    ra.add_argument("--min-demand", type=int, help="the station must want at least this many")
    ra.add_argument("--pad", choices=["S", "M", "L"], help="the landing pad your ship needs")
    ra.add_argument("--max-age-days", type=float, help="ignore market data older than this")
    ra.add_argument("--near", help="a system to measure distance from (with --max-ly)")
    ra.add_argument("--max-ly", type=float, help="at most this many light years from --near")
    ra.add_argument("--max-arrival-ls", type=float, help="at most this far from the arrival star")
    ra.add_argument("--carriers", action="store_true", default=None, help="include fleet carriers")

    cm = sub.add_parser("commodities", help="commodity names Ardent knows")
    cm.add_argument("text", nargs="?", help="only names containing this")
    sub.add_parser("status", help="what each rule last alerted")
    s = sub.add_parser("set", help="change a setting")
    s.add_argument("key")
    s.add_argument("value")
    sub.add_parser("config", help="paths, settings and pop's readiness")
    pe = sub.add_parser("pop-env", help="pop's mail settings (value on stdin)")
    pe.add_argument("key", nargs="?")
    pe.add_argument("--list", action="store_true")
    pe.add_argument("--import", dest="import_env", action="store_true")
    sub.add_parser("notify-test", help="send one test email")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "check":
            return check.run(dry_run=args.dry_run, only=args.rule, quiet=args.quiet)
        return {"rules": cmd_rules, "commodities": cmd_commodities, "status": cmd_status,
                "set": cmd_set, "config": cmd_config, "pop-env": cmd_pop_env,
                "notify-test": cmd_notify_test}[args.command](args)
    except ValueError as exc:            # a config file that is not JSON, and the like
        return _err(str(exc))
