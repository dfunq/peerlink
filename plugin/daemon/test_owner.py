"""Owner pairing: a client (the herdr mod) pairs once with you present and then acts as your browser with a token
held in memory only, except for the riskiest changes, which ask you again, and a few things it never does.
The HTTP tests run the real daemon (rig.py) with "you" answering from PEERLINK_TEST_PRESENCE; the rest call the
functions in-process. Run: python3 -m unittest discover -s plugin/daemon"""
import hashlib
import hmac
import json
import os
import stat
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


class TouchIdHelper(unittest.TestCase):
    """The daemon trusts the helper's answer only with its nonce and HMAC back, and stops using a helper that changed
    while its source didn't."""

    def helper(self, body):
        path = os.path.join(tempfile.mkdtemp(prefix="touchid-"), "peerlink-touchid")
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\nimport hashlib, hmac, json, sys\nreq = json.load(sys.stdin)\n{body}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def run_helper(self, body):
        alerts = []
        with unittest.mock.patch.object(d, "TOUCHID_BIN", self.helper(body)), unittest.mock.patch.object(d, "owner_alert", alerts.append):
            return d.touch_id("test"), alerts

    def test_a_signed_yes_counts(self):
        yes, alerts = self.run_helper(
            "mac = hmac.new(bytes.fromhex(req['key']), (req['nonce'] + ':yes').encode(), hashlib.sha256).hexdigest()\n"
            "print(json.dumps({'result': 'yes', 'nonce': req['nonce'], 'hmac': mac}))")
        self.assertEqual((yes, alerts), (True, []))

    def test_an_unsigned_yes_counts_for_nothing_and_alerts(self):
        yes, alerts = self.run_helper("print(json.dumps({'result': 'yes', 'nonce': req['nonce'], 'hmac': 'x'}))")
        self.assertFalse(yes)
        self.assertEqual(len(alerts), 1)

    def test_a_helper_changed_behind_its_source_is_not_used(self):
        home = tempfile.mkdtemp(prefix="integrity-")
        src, binary = os.path.join(home, "main.swift"), os.path.join(home, "peerlink-touchid")
        for p, text in ((src, "source"), (binary, "built")):
            with open(p, "w") as f:
                f.write(text)
        alerts = []
        with unittest.mock.patch.multiple(d, TOUCHID_SRC=src, TOUCHID_BIN=binary, INTEGRITY_PATH=os.path.join(home, "i.json"),
                                          owner_alert=alerts.append):
            self.assertTrue(d.integrity_ok("helper"))
            with open(binary, "w") as f:
                f.write("swapped")
            self.assertFalse(d.integrity_ok("helper"))
            self.assertFalse(d.integrity_ok("helper"))  # still suspect: the swapped file isn't taken as normal
            with open(src, "w") as f:
                f.write("new source")
            self.assertTrue(d.integrity_ok("helper"))  # an update rebuilt it
        self.assertEqual(len(alerts), 2)


if __name__ == "__main__":
    unittest.main()
