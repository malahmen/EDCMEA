"""EDCMEA against a fake Ardent (a local HTTP server) and a fake pop (a script
that records what it was given). No network, no mail.

Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from edcmea import ardent, check, cli, config, notify, rules  # noqa: E402

LTD = "lowtemperaturediamond"


def row(station, price, *, carrier=False, pad=3, demand=500, x=0.0, ls=100.0, market=None, system=None):
    return {"commodityName": LTD, "marketId": market or abs(hash(station)) % 10 ** 9,
            "stationName": station, "stationType": "FleetCarrier" if carrier else "Coriolis",
            "distanceToArrival": ls, "maxLandingPadSize": pad, "systemName": system or f"{station} System",
            "systemX": x, "systemY": 0.0, "systemZ": 0.0, "sellPrice": price, "demand": demand,
            "updatedAt": "2026-10-19T10:00:00.000Z"}


class FakeArdent:
    """A tiny Ardent: /commodities, /system/name/X, /commodity/name/X/imports,
    with the server-side filters Ardent has (fleetCarriers, minPrice, minVolume)."""

    def __init__(self):
        self.imports = {LTD: [row("Carrier K0F", 9_643_800, carrier=True, demand=1),
                              row("Arc City", 642_763, x=60.0),
                              row("Small Port", 640_000, pad=1),
                              row("Far Away", 630_000, x=900.0),
                              row("Deep Station", 620_000, ls=200_000.0),
                              row("Low Demand", 610_000, demand=5),
                              row("Cheap", 300_000)]}
        self.systems = {"sol": {"systemX": 0, "systemY": 0, "systemZ": 0, "systemName": "Sol"}}
        self.down = False
        self.server_filters = True     # False: Ardent stops honouring its filters
        self.requests = []
        # What the next requests get instead of an answer, one each:
        # (code, headers) an HTTP error; "truncate" the first bytes of a
        # longer body (they parse: "[]"), then the connection closes; "drop" the connection closes with no answer.
        self.script = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, q))
                if fake.script:
                    action = fake.script.pop(0)
                    if action == "drop":
                        self.close_connection = True
                        return None
                    if action == "truncate":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"[]")    # valid JSON, yet only part of the answer
                        self.close_connection = True
                        return None
                    code, headers = action
                    return self.reply(code, {"error": "scripted"}, headers)
                if fake.down:
                    return self.reply(503, {"error": "down"})
                parts = url.path.strip("/").split("/")
                if url.path == "/v2/commodities":
                    return self.reply(200, [{"commodityName": "low temp. diamonds", "maxSellPrice": 1},
                                            {"commodityName": LTD, "maxSellPrice": 523200},
                                            {"commodityName": "painite", "maxSellPrice": 1}])
                if parts[:3] == ["v2", "system", "name"]:
                    s = fake.systems.get(parts[3].lower())
                    return self.reply(200 if s else 404, s or {"error": "not found"})
                if parts[:3] == ["v2", "commodity", "name"] and parts[4:] == ["imports"]:
                    rows = list(fake.imports.get(parts[3].lower(), []))
                    if not fake.server_filters:
                        return self.reply(200, rows)
                    if q.get("fleetCarriers") == "false":
                        rows = [r for r in rows if r["stationType"] != "FleetCarrier"]
                    rows = [r for r in rows if r["sellPrice"] >= int(q.get("minPrice", 0))]
                    rows = [r for r in rows if r["demand"] >= int(q.get("minVolume", 0))]
                    return self.reply(200, rows)
                return self.reply(404, {"error": "no route"})

            def reply(self, code, data, headers=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v2"


class RawPeer:
    """A peer that is not an HTTP server: "silent" accepts and never answers;
    "drip" sends headers for a long body, then a byte every 50 ms."""

    def __init__(self, mode):
        self.mode, self.stop = mode, threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/v2"
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conns = []
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            conns.append(conn)
            if self.mode == "drip":
                threading.Thread(target=self.drip, args=(conn,), daemon=True).start()
        for conn in conns:
            conn.close()
        self.sock.close()

    def drip(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n[")
            while not self.stop.wait(0.05):
                conn.sendall(b" ")
        except OSError:
            pass


def in_thread(fn, seconds=10):
    """fn() in a thread; (finished, result or exception). A hang is a failure,
    not a test run that never ends."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["result"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive(), box.get("result")


POP_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ["STUB_POP_LOG"]
keys = ["POP_FROM", "POP_SMTP_HOST", "POP_SMTP_PASSWORD", "RESEND_API_KEY"]
st, null = os.fstat(0), os.stat(os.devnull)
stdin_null = (st.st_dev, st.st_ino) == (null.st_dev, null.st_ino)
if os.environ.get("STUB_POP_SLEEP"):
    time.sleep(float(os.environ["STUB_POP_SLEEP"]))
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in keys},
                         "stdin_null": stdin_null}) + "\n")
if os.environ.get("STUB_POP_FAIL"):
    print("smtp auth failed for password " + os.environ.get("POP_SMTP_PASSWORD", ""), file=sys.stderr)
    sys.exit(1)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeArdent()
        self.addCleanup(self.fake.server.shutdown)
        pop = os.path.join(self.tmp, "pop")
        with open(pop, "w") as fh:
            fh.write(POP_STUB)
        os.chmod(pop, 0o755)
        self.pop_log = os.path.join(self.tmp, "pop.log")
        env = {"EDCMEA_CONFIG_DIR": os.path.join(self.tmp, "config"),
               "EDCMEA_STATE_DIR": os.path.join(self.tmp, "state"),
               "EDCMEA_POP": pop, "STUB_POP_LOG": self.pop_log}
        patches = [mock.patch.dict(os.environ, env),
                   mock.patch.object(ardent, "API_URL", self.fake.url),
                   mock.patch.object(ardent.time, "sleep", lambda s: None)]
        for k in config.POP_KEYS:          # the developer's own pop settings stay out
            patches.append(mock.patch.dict(os.environ, {k: ""}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in config.POP_KEYS:
            os.environ.pop(k, None)

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            rc = cli.main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def pops(self):
        try:
            with open(self.pop_log) as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []

    def ready(self, *rule_args):
        """A recipient, pop's settings, and one rule."""
        self.assertEqual(self.cli("set", "email_to", "cmdr@example.com")[0], 0)
        self.cli("pop-env", "POP_FROM", stdin="alerts@example.com\n")
        self.cli("pop-env", "POP_SMTP_HOST", stdin="smtp.example.com\n")
        self.cli("pop-env", "POP_SMTP_PASSWORD", stdin="s3cret-app-pw\n")
        rc, out = self.cli("rules", "add", "ltd", LTD, "600000", *rule_args)
        self.assertEqual(rc, 0, out)


class Rules(Base):
    def test_a_commodity_ardent_does_not_know_is_refused_with_suggestions(self):
        rc, out = self.cli("rules", "add", "ltd", "lowtemperaturediamonds", "600000")
        self.assertEqual(rc, 2)
        self.assertIn("did you mean lowtemperaturediamond", out)
        self.assertEqual(config.load()["rules"], [])

    def test_a_display_name_resolves_to_ardents_key(self):
        rc, out = self.cli("rules", "add", "ltd", "Low Temperature Diamond", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(config.load()["rules"][0]["commodity"], LTD)

    def test_validation(self):
        bad = [({"name": "Bad Name", "commodity": LTD, "min_price": 1}, "rule name"),
               ({"name": "x", "commodity": LTD}, "min_price"),
               ({"name": "x", "commodity": LTD, "min_price": 1, "pad": "XL"}, "pad"),
               ({"name": "x", "commodity": LTD, "min_price": 1, "near": "Sol"}, "go together"),
               ({"name": "x", "commodity": LTD, "min_price": 1, "max_ly": 5}, "go together"),
               ({"name": "x", "commodity": LTD, "min_price": 1, "colour": "red"}, "unknown")]
        for rule, why in bad:
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, why):
                rules.validate(rule)

    def test_an_unknown_system_is_refused(self):
        rc, out = self.cli("rules", "add", "ltd", LTD, "1", "--near", "Nowhere", "--max-ly", "10")
        self.assertEqual(rc, 2)
        self.assertIn("no system 'Nowhere'", out)

    def test_add_replaces_by_name_and_remove_removes(self):
        self.cli("rules", "add", "ltd", LTD, "1")
        self.cli("rules", "add", "ltd", LTD, "2")
        self.assertEqual([r["min_price"] for r in config.load()["rules"]], [2])
        self.assertEqual(self.cli("rules", "remove", "ltd")[0], 0)
        self.assertEqual(self.cli("rules", "remove", "ltd")[0], 2)

    def test_the_config_file_is_owner_only(self):
        self.cli("rules", "add", "ltd", LTD, "1")
        self.assertEqual(stat.S_IMODE(os.stat(config.config_file()).st_mode), 0o600)


class Filters(Base):
    def best(self, **extra):
        rule = rules.validate(dict({"name": "t", "commodity": LTD, "min_price": 600000}, **extra))
        origin = ardent.system(rule["near"]) if "near" in rule else None
        return [r["stationName"] for r in rules.matches(rule, origin)]

    def test_no_fleet_carriers_unless_asked(self):
        self.assertNotIn("Carrier K0F", self.best())
        self.assertEqual(self.best(carriers=True)[0], "Carrier K0F")

    def test_pad_distance_arrival_and_demand(self):
        self.assertNotIn("Small Port", self.best(pad="L"))
        self.assertIn("Small Port", self.best(pad="S"))
        self.assertNotIn("Far Away", self.best(near="Sol", max_ly=300))
        self.assertNotIn("Deep Station", self.best(max_arrival_ls=5000))
        self.assertNotIn("Low Demand", self.best(min_demand=100))
        self.assertNotIn("Cheap", self.best())

    def test_the_filters_hold_if_ardent_stops_applying_its_own(self):
        # Carriers, price and demand are filtered by Ardent too; a change on its
        # side must not turn into alerts nobody asked for.
        self.fake.server_filters = False
        self.assertNotIn("Carrier K0F", self.best())      # demand 1: checked on its own
        self.assertNotIn("Cheap", self.best())
        self.assertNotIn("Low Demand", self.best(min_demand=100))

    def test_best_price_first(self):
        self.assertEqual(self.best()[0], "Arc City")


class Check(Base):
    def test_first_run_alerts_through_pop(self):
        self.ready()
        rc, out = self.cli("check")
        self.assertEqual(rc, 0, out)
        self.assertIn("[alert] ltd: 642,763 cr - Arc City", out)
        (call,) = self.pops()
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--to") + 1], "cmdr@example.com")
        self.assertIn("642,763 cr at Arc City", argv[argv.index("--subject") + 1])
        self.assertIn("Arc City", argv[argv.index("--body") + 1])

    def test_pop_gets_its_settings_from_pop_env_and_no_secret_on_its_command_line(self):
        self.ready()
        self.cli("check")
        (call,) = self.pops()
        self.assertEqual(call["env"]["POP_SMTP_PASSWORD"], "s3cret-app-pw")
        self.assertEqual(call["env"]["POP_FROM"], "alerts@example.com")
        self.assertNotIn("s3cret-app-pw", " ".join(call["argv"]))

    def test_the_same_best_station_is_not_alerted_again(self):
        self.ready()
        self.cli("check")
        rc, out = self.cli("check")
        self.assertIn("already alerted", out)
        self.assertEqual(len(self.pops()), 1)

    def test_a_price_rise_of_the_threshold_alerts_again(self):
        self.ready()
        self.cli("check")
        self.fake.imports[LTD][1]["sellPrice"] = 660_000          # +2.7%: below 5%
        self.cli("check")
        self.assertEqual(len(self.pops()), 1)
        self.fake.imports[LTD][1]["sellPrice"] = 680_000          # +5.8% on the alerted price
        self.cli("check")
        self.assertEqual(len(self.pops()), 2)

    def test_a_new_best_station_alerts(self):
        self.ready()
        self.cli("check")
        self.fake.imports[LTD].append(row("New Best", 700_000))
        self.cli("check")
        self.assertEqual(len(self.pops()), 2)
        self.assertIn("New Best", self.pops()[1]["argv"][self.pops()[1]["argv"].index("--subject") + 1])

    def test_after_the_cooldown_it_alerts_again(self):
        self.ready()
        self.cli("check")
        later = time.time() + 25 * 3600
        with mock.patch.object(check.time, "time", lambda: later):
            self.cli("check")
        self.assertEqual(len(self.pops()), 2)

    def test_dry_run_sends_and_records_nothing(self):
        self.ready()
        rc, out = self.cli("check", "--dry-run")
        self.assertIn("[would alert]", out)
        self.assertEqual(self.pops(), [])
        self.assertEqual(config.load_state(), {})

    def test_one_rule_failing_does_not_stop_the_others(self):
        self.ready()
        self.cli("rules", "add", "near", LTD, "600000", "--near", "Sol", "--max-ly", "100")
        self.fake.systems.clear()                                   # Sol vanished
        rc, out = self.cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("[error] near:", out)
        self.assertIn("[alert] ltd:", out)

    def test_ardent_down_fails_the_run(self):
        self.ready()
        self.fake.down = True
        rc, out = self.cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("[error] ltd:", out)
        self.assertEqual(self.pops(), [])

    def test_a_failed_email_is_not_recorded_and_its_secret_not_shown(self):
        self.ready()
        with mock.patch.dict(os.environ, {"STUB_POP_FAIL": "1"}):
            rc, out = self.cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("pop failed", out)
        self.assertNotIn("s3cret-app-pw", out)
        self.assertEqual(config.load_state(), {})
        self.cli("check")                                           # mail back: it is sent now
        self.assertEqual(len(self.pops()), 2)

    def test_checking_one_rule_keeps_the_others_state(self):
        self.ready()
        self.cli("rules", "add", "other", LTD, "600000", "--pad", "L")
        self.cli("check")
        self.cli("check", "--rule", "ltd")
        self.assertEqual(sorted(config.load_state()), ["ltd", "other"])

    def test_a_removed_rule_is_forgotten(self):
        self.ready()
        self.cli("check")
        self.cli("rules", "remove", "ltd")
        self.cli("rules", "add", "ltd2", LTD, "600000")
        self.cli("check")
        self.assertEqual(sorted(config.load_state()), ["ltd2"])

    def test_runs_never_overlap(self):
        self.ready()
        import fcntl
        os.makedirs(config.state_dir(), exist_ok=True)
        with open(config.lock_file(), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            rc, out = self.cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("another check is running", out)
        self.assertEqual(self.pops(), [])

    def test_no_rules_or_no_recipient_is_a_configuration_error(self):
        self.assertEqual(self.cli("check")[0], 2)
        self.cli("rules", "add", "ltd", LTD, "600000")
        rc, out = self.cli("check")
        self.assertEqual(rc, 2)
        self.assertIn("email_to", out)


class PopSettings(Base):
    def test_values_come_from_stdin_and_the_file_is_owner_only(self):
        rc, _ = self.cli("pop-env", "POP_SMTP_PASSWORD", stdin="pw-from-stdin\n")
        self.assertEqual(rc, 0)
        self.assertEqual(config.load_pop_env()["POP_SMTP_PASSWORD"], "pw-from-stdin")
        self.assertEqual(stat.S_IMODE(os.stat(config.pop_env_file()).st_mode), 0o600)

    def test_list_names_only(self):
        self.cli("pop-env", "POP_SMTP_PASSWORD", stdin="pw-from-stdin\n")
        rc, out = self.cli("pop-env", "--list")
        self.assertIn("POP_SMTP_PASSWORD\tset", out)
        self.assertNotIn("pw-from-stdin", out)
        rc, out = self.cli("config")
        self.assertIn("pop_env=POP_SMTP_PASSWORD", out)
        self.assertNotIn("pw-from-stdin", out)

    def test_import_copies_this_environment(self):
        with mock.patch.dict(os.environ, {"POP_FROM": "me@example.com", "RESEND_API_KEY": "re_123"}):
            rc, out = self.cli("pop-env", "--import")
        self.assertEqual(rc, 0, out)
        self.assertEqual(config.load_pop_env(), {"POP_FROM": "me@example.com", "RESEND_API_KEY": "re_123"})

    def test_unknown_keys_and_line_breaks_are_refused(self):
        self.assertEqual(self.cli("pop-env", "PATH", stdin="x\n")[0], 2)
        with self.assertRaises(ValueError):
            config.set_pop_env("POP_FROM", "a\nb")

    def test_an_empty_value_removes_the_setting(self):
        self.cli("pop-env", "POP_FROM", stdin="me@example.com\n")
        self.cli("pop-env", "POP_FROM", stdin="\n")
        self.assertNotIn("POP_FROM", config.load_pop_env())

    def test_without_a_way_to_send_it_says_so(self):
        self.cli("set", "email_to", "cmdr@example.com")
        self.cli("pop-env", "POP_FROM", stdin="me@example.com\n")
        rc, out = self.cli("notify-test")
        self.assertEqual(rc, 1)
        self.assertIn("RESEND_API_KEY, or POP_SMTP_HOST", out)


class Settings(Base):
    def test_typed_like_their_defaults(self):
        self.assertEqual(self.cli("set", "cooldown_hours", "6")[0], 0)
        self.assertEqual(config.load()["settings"]["cooldown_hours"], 6.0)
        self.assertEqual(self.cli("set", "cooldown_hours", "soon")[0], 2)
        self.assertEqual(self.cli("set", "colour", "red")[0], 2)
        self.assertEqual(self.cli("set", "email_to", "not-an-address")[0], 2)


class Crash(BaseException):
    """What a killed run looks like from inside: nothing after it runs."""


class Resilience(Base):
    """Kamino 168: whatever fails after a rule mailed, it is not mailed again."""

    def test_network_failures_are_retried(self):
        self.ready()
        for failure in ("drop", "truncate", (503, {})):
            with self.subTest(failure=failure):
                self.fake.requests.clear()
                self.fake.script = [failure]
                self.assertIsInstance(ardent.commodities(), list)
                self.assertEqual(len(self.fake.requests), 2)


class Deadline(Base):
    """Kamino 179 (2) and the urlopen-timeout survivor."""

    def peer(self, mode):
        p = RawPeer(mode)
        self.addCleanup(p.stop.set)
        return p

    def test_a_silent_peer_times_out(self):
        p = self.peer("silent")
        with mock.patch.object(ardent, "API_URL", p.url), mock.patch.object(ardent, "TIMEOUT", 0.3):
            finished, result = in_thread(ardent.commodities)
        self.assertTrue(finished, "no timeout: urlopen waited on a peer that never answers")
        self.assertIsInstance(result, ardent.ApiError)


class Survivors(Base):
    """Kamino 179 (4): the mutants the first review saw survive."""

    def test_a_5xx_is_retried_until_it_answers(self):
        self.fake.script = [(503, {}), (502, {})]
        self.assertIsInstance(ardent.commodities(), list)
        self.assertEqual(len(self.fake.requests), 3)

    def test_a_4xx_is_not_retried(self):
        self.fake.script = [(400, {})]
        with self.assertRaises(ardent.ApiError):
            ardent.commodities()
        self.assertEqual(len(self.fake.requests), 1)


class Edges(Base):
    """Kamino 197."""

    def test_a_429_is_retried_after_retry_after(self):
        waits = []
        with mock.patch.object(ardent.time, "sleep", waits.append):
            self.fake.script = [(429, {"Retry-After": "7"})]
            self.assertIsInstance(ardent.commodities(), list)
            self.fake.script = [(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})]   # past: the backoff
            self.assertIsInstance(ardent.commodities(), list)
            self.fake.script = [(429, {"Retry-After": "86400"})]                           # capped
            self.assertIsInstance(ardent.commodities(), list)
        self.assertEqual(waits, [7.0, 1, ardent.RETRY_AFTER_MAX])

    def test_a_slash_in_a_name_stays_in_its_path_segment(self):
        ardent.system("Col 285 Sector AB/CD")
        self.assertTrue(self.fake.requests[-1][0].endswith("/system/name/Col%20285%20Sector%20AB%2FCD"),
                        self.fake.requests[-1][0])


class Launcher(unittest.TestCase):
    def test_edcmea_sh_runs_the_engine(self):
        res = subprocess.run(["bash", os.path.join(ROOT, "edcmea.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("check", res.stdout)


if __name__ == "__main__":
    unittest.main()
