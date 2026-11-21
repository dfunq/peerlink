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
        self.assertEqual(raw(rig, "PUT", "/api/config", owner, {"wake_for": "needed"})[0], 200)
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
        # Spent.
        self.assertEqual(raw(rig, "PUT", "/api/config", {**owner, "X-Owner-Step-Up": sid}, body)[0], 428)

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
        for target, fake in (("owner_ask", lambda rid, *a: self.asked.append(rid)), ("owner_alert", self.alerts.append)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)

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

    def test_what_asks_again(self):
        d.rules["u1"] = {"id": "u1"}
        self.addCleanup(d.rules.pop, "u1", None)
        need = d.step_up_needed
        self.assertTrue(need("PUT", ["config"], {"auto_approve_all": True}))
        self.assertFalse(need("PUT", ["config"], {"wake_for": "needed"}))
        self.assertTrue(need("POST", ["sessions", "s1"], {"auto_approve": True}))
        self.assertTrue(need("POST", ["inbox"], {"kind": "approval", "id": "u1", "action": "approve"}))
        self.assertFalse(need("POST", ["inbox"], {"kind": "approval", "id": "s9", "action": "approve"}))
        for rest in (["spaces", "acme", "lead"], ["spaces", "acme", "delete"], ["spaces", "acme", "parent"], ["folders", "space"],
                     ["members"], ["links"], ["agents", "a1", "delete"], ["rules", "u1"]):
            self.assertTrue(need("POST", rest, {}), rest)
        for rest in (["sessions", "s1", "clear"], ["sessions", "s1", "terminate"]):
            self.assertFalse(need("POST", rest, {}), rest)


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
