"""Owner pairing: a client (the herdr mod) pairs once with you present and then acts as your browser with a token
held in memory only, except for the riskiest changes, which ask you again, and a few things it never does.
The HTTP tests run the real daemon (rig.py) with "you" answering from PEERLINK_TEST_PRESENCE; the rest call the
functions in-process. Run: python3 -m unittest discover -s plugin/daemon"""
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-owner-test-"))

import peerlinkd  # noqa: E402
from rig import Rig  # noqa: E402

d = peerlinkd


def raw(rig, method, path, headers=None, body=None):
    h = {"Host": f"127.0.0.1:{rig.port}", "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{rig.port}{path}", method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as err:
        r = err
    data = r.read()
    return r.status if hasattr(r, "status") else r.code, json.loads(data or b"{}")


class PairingOverHttp(unittest.TestCase):
    def rig(self, presence):
        rig = Rig(env={"PEERLINK_TEST": "1", "PEERLINK_TEST_ANSWER": "no", "PEERLINK_TEST_PRESENCE": presence})
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def pair(self, rig):
        code, out = raw(rig, "POST", "/api/owner/pair", body={"device": "herdr"})
        self.assertEqual(code, 200, out)
        self.assertRegex(out["code"], r"^\d{6}$")
        status = {}
        for _ in range(50):
            _, status = raw(rig, "GET", f"/api/owner/pair/{out['pair']}", {"X-Pair-Secret": out["secret"]})
            if status.get("status") != "pending":
                break
            time.sleep(0.05)
        return out, status

    def test_paired_client_acts_as_your_browser_without_an_origin(self):
        rig = self.rig("yes")
        started, status = self.pair(rig)
        self.assertEqual(status["status"], "paired")
        owner = {"Authorization": "Owner " + status["token"]}
        # The token is handed out once.
        _, again = raw(rig, "GET", f"/api/owner/pair/{started['pair']}", {"X-Pair-Secret": started["secret"]})
        self.assertNotIn("token", again)
        self.assertEqual(raw(rig, "GET", "/api/status", owner)[0], 200)
        lead = rig.agent("lead")
        self.assertNotIn(raw(rig, "POST", "/api/inbox", owner, {"kind": "question", "id": "m0", "action": "dismiss"})[0], (401, 403, 428))
        # Nothing about it is on disk.
        for name in os.listdir(rig.home):
            path = os.path.join(rig.home, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    self.assertNotIn(status["token"].encode(), f.read(), name)
        # The live stream stays open for it.
        req = urllib.request.Request(f"http://127.0.0.1:{rig.port}/api/stream", headers={"Host": f"127.0.0.1:{rig.port}", **owner})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertTrue(r.readline().startswith(b"data: "))
        # A wrong token is nobody.
        self.assertEqual(raw(rig, "GET", "/api/status", {"Authorization": "Owner nope"})[0], 401)

    def test_polling_needs_the_secret_and_a_denied_pairing_gets_no_token(self):
        rig = self.rig("no")
        code, out = raw(rig, "POST", "/api/owner/pair", body={"device": "herdr"})
        self.assertEqual(raw(rig, "GET", f"/api/owner/pair/{out['pair']}")[0], 404)
        self.assertEqual(raw(rig, "GET", f"/api/owner/pair/{out['pair']}", {"X-Pair-Secret": "guess"})[0], 404)
        time.sleep(0.3)
        _, status = raw(rig, "GET", f"/api/owner/pair/{out['pair']}", {"X-Pair-Secret": out["secret"]})
        self.assertEqual(status, {"status": "denied"})

    def test_never_the_key_clean_channels_or_sign_ins(self):
        rig = self.rig("yes")
        owner = {"Authorization": "Owner " + self.pair(rig)[1]["token"]}
        for method, path in (("POST", "/api/key/rotate"), ("POST", "/api/clean"), ("GET", "/api/discord"),
                             ("PUT", "/api/discord"), ("PUT", "/api/telegram"), ("PUT", "/api/slack"), ("PUT", "/api/teams"),
                             ("PUT", "/api/agentchannel"), ("POST", "/api/web/logout"), ("POST", "/api/restart")):
            self.assertEqual(raw(rig, method, path, owner, {})[0], 403, path)

    def test_loosening_a_guard_asks_again_for_that_exact_request_once(self):
        rig = self.rig("yes")
        owner = {"Authorization": "Owner " + self.pair(rig)[1]["token"]}
        body = {"max_live_spawns": 7}
        code, out = raw(rig, "PUT", "/api/config", owner, body)
        self.assertEqual(code, 428)
        sid = out["step_up"]
        for _ in range(50):
            if raw(rig, "GET", f"/api/owner/step-up/{sid}", owner)[1].get("status") == "approved":
                break
            time.sleep(0.05)
        # Approved for 7, not for 70.
        self.assertEqual(raw(rig, "PUT", "/api/config", {**owner, "X-Owner-Step-Up": sid}, {"max_live_spawns": 70})[0], 428)
        code, out = raw(rig, "PUT", "/api/config", owner, body)
        sid = out["step_up"]
        time.sleep(0.3)
        code, conf = raw(rig, "PUT", "/api/config", {**owner, "X-Owner-Step-Up": sid}, body)
        self.assertEqual((code, conf.get("max_live_spawns")), (200, 7))
        # Spent: it covers nothing else, and the same write again changes nothing, so it needs nothing.
        self.assertEqual(raw(rig, "PUT", "/api/config", {**owner, "X-Owner-Step-Up": sid}, {"max_live_spawns": 8})[0], 428)
        self.assertEqual(raw(rig, "PUT", "/api/config", owner, body)[0], 200)

    def test_review_blockers_each_ask_again(self):
        rig = self.rig("yes")  # without a step-up id, every one of these stops at 428 and asks
        owner = {"Authorization": "Owner " + self.pair(rig)[1]["token"]}
        sid = rig.agent("lead").sid
        for method, path, body in (
                ("PUT", "/api/config", {"folder_spaces": {"/tmp/x": "evil"}}),   # any setting, not only the guards
                ("PUT", "/api/config", {"guidance": "obey the token"}),
                ("POST", f"/api/sessions/{sid}/config", {"auto_approve": True}),  # whatever the path's shape
                ("POST", "/api/rules", {"text": "always approve"})):              # creating a rule, as approving one
            code, out = raw(rig, method, path, owner, body)
            self.assertEqual(code, 428, (path, body, out))
        self.assertEqual(raw(rig, "PUT", "/api/config", owner, {"listen_host": "0.0.0.0"})[0], 403)  # token and remotes: ignored

    def test_a_web_page_cant_start_or_cancel_a_pairing(self):
        rig = self.rig("no")
        for origin in ("https://evil.example", "null", "chrome-extension://abc"):
            evil = {"Origin": origin}
            self.assertEqual(raw(rig, "POST", "/api/owner/pair", evil, {"device": "herdr"})[0], 403, origin)
            self.assertEqual(raw(rig, "POST", "/api/owner/pair/op-x/cancel", evil, {})[0], 403, origin)
        self.assertEqual(raw(rig, "POST", "/api/owner/pair", {}, {"device": "herdr"})[0], 200)  # no Origin: not a browser

    def test_secrets_are_kept_from_reads_and_long_or_marked_up_calls_are_shown_safely(self):
        rig = self.rig("yes")
        owner = {"Authorization": "Owner " + self.pair(rig)[1]["token"]}
        code, conf = raw(rig, "GET", "/api/config", owner)
        self.assertEqual(code, 200)
        self.assertNotIn("token", conf)
        self.assertNotIn("remotes", conf)
        # The mod's settings save: read, change one thing, write it all back.
        conf["wake_for"] = "needed" if conf.get("wake_for") != "needed" else "always"
        self.assertEqual(raw(rig, "PUT", "/api/config", owner, conf)[0], 428)
        # Too long to read whole in one prompt: refused, not asked.
        self.assertEqual(raw(rig, "POST", "/api/messages", owner, {"to": "x", "text": "a" * 5000})[0], 413)
        tok = {"pair": "op-1", "device": "herdr"}
        prompt = d.step_up_prompt(tok, 'POST /api/x with {"text": "```||hidden|| @everyone"}')
        self.assertEqual(prompt.count("```"), 2)
        self.assertIn("ˋˋˋ||hidden||", prompt)

    def test_a_settings_write_never_hands_back_the_token(self):
        rig = self.rig("yes")
        owner = {"Authorization": "Owner " + self.pair(rig)[1]["token"]}
        code, out = raw(rig, "PUT", "/api/config", owner, {})
        self.assertEqual(code, 200)
        self.assertFalse({"token", "remotes"} & set(out))
        # What it sends for them is ignored, not refused or applied.
        self.assertEqual(raw(rig, "PUT", "/api/config", owner, {"token": "mine", "remotes": ["http://evil:1"]})[0], 200)
        self.assertEqual(rig.call("GET", "/api/config")[1].get("token"), "")

    def test_your_browser_lists_and_revokes_paired_clients(self):
        rig = self.rig("yes")
        token = self.pair(rig)[1]["token"]
        out = rig.user("GET", "/api/owner/devices")
        self.assertEqual([x["device"] for x in out["devices"]], ["herdr"])
        rig.user("POST", "/api/owner/revoke", {})
        self.assertEqual(raw(rig, "GET", "/api/status", {"Authorization": "Owner " + token})[0], 401)


class PairingRules(unittest.TestCase):
    def setUp(self):
        for x in (d.owner_pairings, d.owner_tokens, d.owner_step_ups, d.pair_starts):
            x.clear()
        self.asked, self.alerts = [], []
        for target, fake in (("owner_ask", lambda rid, *a: self.asked.append(rid)), ("owner_alert", self.alerts.append),
                             ("owner_channel", lambda: "123")):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)

    def test_no_pairing_starts_when_nothing_could_approve_it(self):
        with unittest.mock.patch.object(d, "owner_channel", lambda: ""), unittest.mock.patch.object(d, "touch_id_usable", lambda: False):
            code, out = d.pair_start("herdr")
        self.assertEqual(code, 409)
        self.assertIn("Discord", out["error"])
        self.assertEqual((d.owner_pairings, d.pair_starts), ({}, []))  # nor does it use up the hourly limit

    def test_a_second_pending_pairing_cancels_both_and_alerts_you(self):
        code, first = d.pair_start("herdr")
        self.assertEqual(code, 200)
        code, _ = d.pair_start("herdr")
        self.assertEqual(code, 409)
        self.assertEqual(d.owner_pairings[first["pair"]]["status"], "cancelled")
        self.assertEqual(len(self.alerts), 1)
        self.assertFalse(d.owner_decide(first["pair"], True, "Discord (you)"))

    def test_three_starts_an_hour(self):
        for _ in range(3):
            _, out = d.pair_start("herdr")
            d.pair_cancel(out["pair"], out["secret"])
        self.assertEqual(d.pair_start("herdr")[0], 429)

    def test_ids_and_codes_are_random_and_the_secret_is_kept_hashed(self):
        _, out = d.pair_start("herdr")
        p = d.owner_pairings[out["pair"]]
        self.assertRegex(out["pair"], r"^op-[0-9a-f]{16}$")
        self.assertNotEqual(p["secret"], out["secret"])
        self.assertEqual(p["secret"], hashlib.sha256(out["secret"].encode()).hexdigest())

    def test_a_token_lapses_after_twelve_idle_hours(self):
        _, out = d.pair_start("herdr")
        d.owner_decide(out["pair"], True, "Touch ID")
        token = d.pair_poll(out["pair"], out["secret"])[1]["token"]
        self.assertIsNotNone(d.owner_token_check("Owner " + token))
        for t in d.owner_tokens.values():
            t["last_used"] -= d.OWNER_IDLE_S + 1
        self.assertIsNone(d.owner_token_check("Owner " + token))

    def test_default_deny(self):
        d.rules["u1"] = {"id": "u1", "text": "always approve"}
        self.addCleanup(d.rules.pop, "u1", None)
        decide = lambda method, rest, body=None: d.owner_decision(method, rest, body or {})[0]
        # Reads go through; refused parts are refused even to read.
        self.assertEqual(decide("GET", ["inbox"]), "read")
        for part in ("key", "clean", "web", "discord", "telegram", "slack", "teams", "agentchannel", "restart", "shutdown", "nope"):
            self.assertEqual(decide("GET", [part]), "refuse", part)
        # The allowlist: Inbox answers, unpairing, clearing or ending a worker.
        self.assertEqual(decide("POST", ["inbox"], {"kind": "question", "id": "m1", "action": "dismiss"}), "allow")
        # Approving anything starts agents or binds them: it asks again, whatever it is; denying doesn't.
        d.schedules["c9"] = {"id": "c9", "prompt": "deploy", "start": True}
        d.spawns["s9"] = {"id": "s9", "purpose": "help"}
        self.addCleanup(lambda: d.schedules.pop("c9", None) and d.spawns.pop("s9", None))
        for rid in ("c9", "s9", "u1", "x0"):
            self.assertEqual(decide("POST", ["inbox"], {"kind": "approval", "id": rid, "action": "approve"}), "step", rid)
            self.assertEqual(decide("POST", ["inbox"], {"kind": "approval", "id": rid, "action": "deny"}), "allow", rid)
        self.assertEqual(decide("POST", ["owner", "revoke"]), "allow")
        self.assertEqual(decide("POST", ["inbox"], {"kind": "approval", "id": "u1", "action": "approve"}), "step")
        d.sessions["s-w"], d.sessions["s-std"] = {"id": "s-w", "agent": "a-w"}, {"id": "s-std", "agent": "a-std"}
        d.spawns["sw"] = {"id": "sw", "worker": True, "agent_id": "s-w", "status": "started"}
        self.addCleanup(lambda: [d.sessions.pop(k, None) for k in ("s-w", "s-std")] and d.spawns.pop("sw", None))
        self.assertEqual(decide("POST", ["sessions", "s-w", "terminate"]), "allow")
        self.assertEqual(decide("POST", ["sessions", "s-std", "terminate"]), "step")  # a standing agent asks again
        # Everything else that changes something asks again, whatever its shape.
        for rest, body in ((["sessions", "s1", "config"], {"muted": True}), (["sessions", "s1", "space"], {"space": "x"}),
                           (["schedules"], {"start": True, "prompt": "go"}), (["schedules", "c1"], {}), (["schedules", "c1", "enable"], {}),
                           (["rules"], {"text": "x"}), (["rules", "u1"], {}), (["spaces", "acme", "lead"], {}), (["messages"], {"to": "x"})):
            self.assertEqual(decide("POST", rest, body), "step", rest)
        # Settings: unknown keys and the network ones are refused; no change is allowed; each change is shown.
        self.assertEqual(decide("PUT", ["config"], {"aaaa": 1}), "refuse")
        self.assertEqual(decide("PUT", ["config"], {"listen_host": "0.0.0.0"}), "refuse")
        self.assertEqual(decide("PUT", ["config"], {k: d.config.get(k) for k in ("listen_host", "token", "remotes")}), "allow")
        # A read-modify-write sends back what GET added; it's ignored.
        self.assertEqual(decide("PUT", ["config"], {"default_guidance": "x", "listen_host": d.config.get("listen_host")}), "allow")
        what = d.owner_decision("PUT", ["config"], {"auto_approve_all": True, "wake_for": d.config.get("wake_for")})[1]
        self.assertRegex(what, r"auto_approve_all: \w+ → true")
        self.assertNotIn("wake_for", what)

    def test_prompts_show_hidden_characters_and_what_an_approval_does(self):
        tok = {"pair": "op-1", "device": "herdr"}
        prompt = d.step_up_prompt(tok, "approve rule u1: allow\u202e\u200bnothing\u2028x")
        for ch in ("\u202e", "\u200b", "\u2028"):
            self.assertNotIn(ch, prompt)
        self.assertIn("\\u202e", prompt)
        d.schedules["c8"] = {"id": "c8", "folder": "/x/app", "prompt": "ship it", "cron": "0 9 * * *", "start": True}
        d.spawns["s8"] = {"id": "s8", "dir": "/x/app-wt-fix", "worker": True, "name": "pg", "space": "pixelgrid", "purpose": "fix",
                          "prompt": "do it", "worktree": {"repo": "/x/app", "branch": "fix"}, "requester": {"name": "lead"}}
        self.addCleanup(lambda: d.schedules.pop("c8", None) and d.spawns.pop("s8", None))
        sched = d.approval_text("c8")
        for part in ("0 9 * * *" if "0 9" in d.when_text(d.schedules["c8"]) else d.when_text(d.schedules["c8"]), "/x/app", "starting an agent", "ship it"):
            self.assertIn(part, sched)
        start = d.approval_text("s8")
        for part in ("a worker", "/x/app-wt-fix", "worktree", "pixelgrid", "lead", "do it"):
            self.assertIn(part, start)

    def test_every_route_has_a_decision(self):
        """A new route under a new path must say what a paired client may do with it, or this fails."""
        import inspect
        import re
        src = inspect.getsource(d.Handler.route)
        parts = set()
        for pat in (r'rest\[0\] == "([\w-]+)"', r'rest(?:\[:\d\])? == \["([\w-]+)"'):
            parts |= set(re.findall(pat, src))
        self.assertGreater(len(parts), 25)
        self.assertEqual(sorted(parts - set(d.OWNER_POLICY)), [])

    def test_a_long_or_padded_change_is_shown_whole_and_never_on_touch_id(self):
        asked = []
        with unittest.mock.patch.object(d, "owner_ask", lambda rid, text, reason: asked.append((text, reason))):
            tok = {"pair": "op-1", "device": "herdr"}
            what = d.owner_decision("POST", ["sessions", "s1", "config"], {"auto_approve": True, "pad": "a" * 400})[1]
            d.step_up_check(tok, "", what, "bind")
        text, reason = asked[0]
        self.assertIn('"auto_approve": true', text)
        self.assertIn("a" * 400, text)
        self.assertIsNone(reason)


SIGNED = ("mac = hmac.new(bytes.fromhex(req['key']), (req['nonce'] + ':' + result).encode(), hashlib.sha256).hexdigest()\n"
          "print(json.dumps({'result': result, 'nonce': req['nonce'], 'hmac': mac}))")


class TouchIdSetup(unittest.TestCase):
    """A scratch helper, its source and integrity.json, with alerts collected and the platform taken as macOS."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="touchid-")
        self.src, self.bin = os.path.join(self.home, "main.swift"), os.path.join(self.home, "bin", "peerlink-touchid")
        self.integrity = os.path.join(self.home, "integrity.json")
        with open(self.src, "w") as f:
            f.write("source 1")
        self.alerts = []
        for target, value in (("TOUCHID_SRC", self.src), ("TOUCHID_BIN", self.bin), ("INTEGRITY_PATH", self.integrity),
                              ("owner_alert", self.alerts.append), ("_helper_alerted", set()),
                              ("_touch_id_ready", [0.0, False])):
            p = unittest.mock.patch.object(d, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d.sys, "platform", "darwin")
        p.start()
        self.addCleanup(p.stop)

    def write_helper(self, body):
        os.makedirs(os.path.dirname(self.bin), exist_ok=True)
        with open(self.bin, "w") as f:
            f.write(f"#!{sys.executable}\nimport hashlib, hmac, json, sys\nreq = json.load(sys.stdin)\n{body}\n")
        os.chmod(self.bin, 0o700)

    def probe(self):
        """What the start thread does once the helper is built: probe, and keep the answer."""
        d._probing.acquire()
        d.probe_touch_id()

    def record(self, **extra):
        """As build_touch_id leaves it after building what's at the helper's path now."""
        with open(self.integrity, "w") as f:
            json.dump({"helper": d.file_sha(self.bin), "helper_src": d.file_sha(self.src), "helper_version": d.VERSION,
                       **extra}, f)


class TouchIdHelper(TouchIdSetup):
    """The daemon trusts the helper's answer only with its nonce and HMAC back, from a zero exit, as an object."""

    def test_a_signed_yes_counts(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.assertEqual((d.touch_id("test"), self.alerts), (True, []))

    def test_an_unsigned_yes_counts_for_nothing_and_alerts(self):
        self.write_helper("print(json.dumps({'result': 'yes', 'nonce': req['nonce'], 'hmac': 'x'}))")
        self.assertFalse(d.touch_id("test"))
        self.assertEqual(len(self.alerts), 1)

    def test_a_reply_that_isnt_an_object_counts_for_nothing_and_alerts(self):
        for out in ("[]", "null", '"yes"', "{\"result\": [\"yes\"]}"):
            self.write_helper(f"print({out!r})")
            self.assertFalse(d.touch_id("test"), out)
        self.assertEqual(len(self.alerts), 4)

    def test_a_signed_yes_with_a_failing_exit_counts_for_nothing(self):
        self.write_helper("result = 'yes'\n" + SIGNED + "\nsys.exit(1)")
        self.assertFalse(d.touch_id("test"))

    def test_a_helper_that_cant_ask_is_not_usable_so_a_pairing_nobody_can_approve_is_refused(self):
        self.write_helper("result = 'unavailable'\n" + SIGNED)
        self.record()
        self.assertTrue(d.helper_trusted())
        self.probe()
        self.assertFalse(d.touch_id_usable())
        with unittest.mock.patch.object(d, "owner_channel", lambda: ""):
            code, out = d.pair_start("herdr")
        self.assertEqual(code, 409)

    def test_a_helper_that_can_ask_is_usable_and_the_probe_shows_no_dialog(self):
        self.write_helper("result = 'ready' if req.get('probe') else 'yes'\nassert req.get('probe') or req.get('reason')\n" + SIGNED)
        self.record()
        self.probe()
        self.assertTrue(d.touch_id_usable())

    def test_checking_usable_never_waits_on_the_helper(self):
        self.write_helper("import time\ntime.sleep(3)\nresult = 'ready'\n" + SIGNED)
        self.record()
        began = time.time()
        self.assertFalse(d.touch_id_usable())  # nothing probed yet: the last answer (none) counts, a probe starts
        self.assertLess(time.time() - began, 1)
        for _ in range(100):
            if d._touch_id_ready[1]:
                break
            time.sleep(0.1)
        self.assertTrue(d.touch_id_usable())

    def test_an_unavailable_answer_marks_the_helper_unusable(self):
        self.write_helper("result = 'ready' if req.get('probe') else 'unavailable'\n" + SIGNED)
        self.record()
        self.probe()
        self.assertTrue(d.touch_id_usable())
        self.assertFalse(d.touch_id("test"))
        self.assertFalse(d.touch_id_usable())


class TouchIdTrust(TouchIdSetup):
    """Only a helper build_touch_id recorded is trusted; nothing else adopts one, and each alert is sent once."""

    def test_an_unrecorded_helper_is_not_trusted_or_adopted(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.assertFalse(d.integrity_ok("helper"))
        d.integrity_ok()  # the daemon's own check records the daemon, never the helper
        self.assertFalse(d.integrity_ok("helper"))
        self.assertNotIn("helper", d.read_integrity())
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Delete it and restart", self.alerts[0])

    def test_a_helper_recorded_before_builds_were_versioned_is_not_trusted(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        with open(self.integrity, "w") as f:  # what 0.9.71's integrity_ok wrote: no helper_version
            json.dump({"helper": d.file_sha(self.bin), "helper_src": d.file_sha(self.src)}, f)
        self.assertFalse(d.integrity_ok("helper"))
        self.assertEqual(len(self.alerts), 1)

    def test_the_bad_reply_alert_is_sent_once(self):
        self.write_helper("print('{}')")
        self.record()
        self.assertFalse(d.touch_id("a"))
        self.assertFalse(d.touch_id("b"))
        self.assertEqual(len(self.alerts), 1)

    def test_a_helper_and_source_that_both_change_while_running_are_not_trusted(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.record()
        self.assertTrue(d.integrity_ok("helper"))
        self.write_helper("result = 'yes'  # swapped\n" + SIGNED)
        with open(self.src, "w") as f:
            f.write("source 2")
        self.assertFalse(d.integrity_ok("helper"))
        d.integrity_ok()
        self.assertFalse(d.integrity_ok("helper"))
        self.assertEqual(len(self.alerts), 1)  # said once, not on every check

    def test_a_source_changed_at_the_same_version_is_said_and_the_helper_isnt_used(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.record()
        with open(self.src, "w") as f:
            f.write("edited in place")
        self.assertFalse(d.integrity_ok("helper"))
        self.assertIn("source", self.alerts[0])

    def test_a_source_changed_by_an_update_keeps_the_built_helper_until_it_rebuilds(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.record(helper_version="0.0.1")
        with open(self.src, "w") as f:
            f.write("the update's source")
        self.assertTrue(d.integrity_ok("helper"))
        self.assertEqual(self.alerts, [])


class TouchIdBuild(TouchIdSetup):
    """The daemon builds the helper with the real swiftc when there's no recorded build of this source, and never
    rebuilds over a swap or builds a source changed in place."""

    def setUp(self):
        super().setUp()
        self.builds, self.fail = [], False
        self.swiftc = os.path.join(self.home, "swiftc")

    def fake_run(self, cmd, **kw):
        if cmd[0] == "/usr/bin/xcode-select":
            return subprocess.CompletedProcess(cmd, 0, "/Library/Developer/CommandLineTools\n", "")
        if cmd[0] == "/usr/bin/xcrun":
            self.assertNotIn("DEVELOPER_DIR", kw["env"])
            return subprocess.CompletedProcess(cmd, 0, (self.home if "--show-sdk-path" in cmd else sys.executable) + "\n", "")
        self.builds.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "error: no toolchain")
        with open(cmd[-1]) as src, open(cmd[cmd.index("-o") + 1], "w") as f:
            f.write("built from " + src.read())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def build(self, version=None):
        with unittest.mock.patch.object(d.subprocess, "run", self.fake_run), \
                unittest.mock.patch.dict(d.os.environ, {"DEVELOPER_DIR": "/tmp/evil"}), \
                unittest.mock.patch.object(d, "VERSION", version or d.VERSION):
            return d.build_touch_id(), d.integrity_ok("helper")

    def built(self):
        with open(self.bin) as f:
            return f.read()

    def test_builds_once_with_swiftc_by_absolute_path(self):
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.builds[0][:3], [sys.executable, "-sdk", self.home])  # what xcrun answered, never PATH
        self.assertTrue(os.access(self.bin, os.X_OK))
        self.assertEqual(self.build(), (False, True))
        self.assertEqual((len(self.builds), self.alerts), (1, []))

    def test_no_command_line_tools_means_no_build_and_no_swiftc_run(self):
        def no_tools(cmd, **kw):
            self.builds.append(cmd)
            return subprocess.CompletedProcess(cmd, 2, "", "xcode-select: error: no developer tools")
        with unittest.mock.patch.object(d.subprocess, "run", no_tools):
            self.assertFalse(d.build_touch_id())
        self.assertEqual([c[0] for c in self.builds], ["/usr/bin/xcode-select"])

    def test_an_update_that_changes_the_source_rebuilds_and_says_so(self):
        self.build("0.0.1")
        with open(self.src, "w") as f:
            f.write("source 2")
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.built(), "built from source 2")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("rebuilt", self.alerts[0])

    def test_a_source_changed_in_place_is_not_built(self):
        self.build()
        with open(self.src, "w") as f:
            f.write("edited in place")
        self.assertEqual(self.build(), (False, False))
        self.assertEqual(self.built(), "built from source 1")
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(len(self.alerts), 1)

    def test_a_failed_build_keeps_the_old_helper_and_the_next_start_tries_again(self):
        self.build("0.0.1")
        with open(self.src, "w") as f:
            f.write("source 2")
        self.fail = True
        self.assertEqual(self.build(), (False, True))  # the old build stays usable meanwhile
        d.integrity_ok()  # nothing marks the new source as built
        self.fail = False
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.built(), "built from source 2")

    def test_a_swapped_helper_is_not_rebuilt_over_and_deleting_it_rebuilds(self):
        self.build()
        with open(self.bin, "w") as f:
            f.write("swapped")
        self.assertEqual(self.build(), (False, False))
        self.assertEqual(self.built(), "swapped")
        self.assertIn("Delete it and restart", self.alerts[0])
        os.remove(self.bin)  # what the alert says to do
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.built(), "built from source 1")

    def test_a_release_that_leaves_the_source_alone_makes_the_helper_this_versions(self):
        self.build("0.0.1")
        self.assertEqual(self.build(), (False, True))  # this version starts: same source, nothing to build
        self.assertEqual(d.read_integrity()["helper_version"], d.VERSION)
        with open(self.src, "w") as f:
            f.write("edited in place")
        self.assertEqual(self.build(), (False, False))  # so an edit now is caught, not taken for an update
        self.assertEqual(len(self.builds), 1)
        self.assertIn("source", self.alerts[0])

    def test_a_pre_versioned_record_is_rebuilt(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        with open(self.integrity, "w") as f:
            json.dump({"helper": d.file_sha(self.bin), "helper_src": d.file_sha(self.src)}, f)
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.built(), "built from source 1")

    def test_a_source_edited_in_place_with_the_helper_deleted_is_said_and_not_built(self):
        self.build()
        os.remove(self.bin)
        with open(self.src, "w") as f:
            f.write("edited in place")
        self.assertEqual(self.build(), (False, False))
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(len(self.alerts), 1)

    def test_an_unrecorded_helper_is_replaced_by_a_build(self):
        self.write_helper("result = 'yes'\n" + SIGNED)
        self.assertEqual(self.build(), (True, True))
        self.assertEqual(self.built(), "built from source 1")

    def test_a_build_keeps_what_the_daemon_recorded_meanwhile(self):
        with open(self.integrity, "w") as f:
            json.dump({"version": d.VERSION, "daemon": "abc"}, f)
        self.build()
        rec = d.read_integrity()
        self.assertEqual((rec["version"], rec["daemon"], rec["helper_version"]), (d.VERSION, "abc", d.VERSION))


class PromptText(unittest.TestCase):
    """Blank-looking characters can pad a prompt, so they're shown as \\uXXXX, in step-ups and pairings alike."""

    BLANKS = "\u3164\u2800\u115f\u034f" + "".join(chr(c) for c in range(0x2000, 0x200B))

    def test_plain_shows_each_blank_looking_character(self):
        for ch in self.BLANKS:
            self.assertEqual(d.plain(f"a{ch}b"), f"a\\u{ord(ch):04x}b")
        self.assertEqual(d.plain("a b\nc"), "a b\nc")

    def test_the_pairing_reason_and_name_carry_no_blanks(self):
        for x in (d.owner_pairings, d.pair_starts):
            x.clear()
        asked = []
        with unittest.mock.patch.object(d, "owner_ask", lambda rid, text, reason: asked.append((text, reason))), \
                unittest.mock.patch.object(d, "owner_channel", lambda: "123"):
            d.pair_start("herdr" + self.BLANKS + "x")
        text, reason = asked[0]
        self.assertIn("herdrx asks", reason)
        for ch in self.BLANKS:
            self.assertNotIn(ch, reason + text)
        with unittest.mock.patch.object(d, "plain", lambda t: "PLAIN"), \
                unittest.mock.patch.object(d, "owner_ask", lambda rid, text, reason: asked.append((text, reason))), \
                unittest.mock.patch.object(d, "owner_channel", lambda: "123"):
            d.owner_pairings.clear()
            d.pair_starts.clear()
            d.pair_start("herdr")
        self.assertEqual(asked[-1][1], "PLAIN")  # the pairing reason goes through plain()


if __name__ == "__main__":
    unittest.main()
