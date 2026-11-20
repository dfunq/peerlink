"""What only you may do in peerlink stays yours. An agent holding the key can't get a browser sign-in, so it
can't pass the browser-only checks; restart, stop and a new key are asked on screen. Each test runs the real daemon
(rig.py) the way it runs at ~/.peerlink: no sign-in link comes back to a key holder, and "the screen" answers as told.
Run: python3 -m unittest discover -s plugin/daemon"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rig import Rig  # noqa: E402


class AdminTest(unittest.TestCase):
    def rig(self, answer):
        self.opened = os.path.join(tempfile.mkdtemp(prefix="peerlink-opened-"), "urls")
        rig = Rig(env={"PEERLINK_TEST": "0", "PEERLINK_TEST_ANSWER": answer, "PEERLINK_TEST_OPEN": self.opened})
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def opened_urls(self, wait=3.0):
        end = time.time() + wait
        while time.time() < end:
            if os.path.exists(self.opened):
                with open(self.opened) as f:
                    return f.read().split()
            time.sleep(0.05)
        return []

    def raw(self, rig, method, path, headers=None, body=None):
        h = {"Host": f"127.0.0.1:{rig.port}", "Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{rig.port}{path}", method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode())

        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            r = urllib.request.build_opener(Stay).open(req, timeout=10)
        except urllib.error.HTTPError as err:
            r = err
        raw = r.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return r.status if hasattr(r, "status") else r.code, data, r.headers

    def sign_in(self, rig, url):
        path = url.split(f":{rig.port}", 1)[1]
        code, _, headers = self.raw(rig, "GET", path)
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        return code, cookie

    def test_a_key_holder_never_gets_a_sign_in_link_and_you_are_asked_on_screen(self):
        rig = self.rig("yes")
        code, out = rig.call("POST", "/api/web/link", {})
        self.assertEqual(code, 202)
        self.assertNotIn("url", out)
        self.assertNotIn("login", json.dumps(out))
        urls = self.opened_urls()
        self.assertEqual(len(urls), 1)  # opened in your browser, said yes on screen
        code, cookie = self.sign_in(rig, urls[0])
        self.assertEqual(code, 302)
        self.assertTrue(cookie.startswith("peerlink_session="))

    def test_turned_down_on_screen_nothing_opens(self):
        rig = self.rig("no")
        code, out = rig.call("POST", "/api/web/link", {})
        self.assertEqual(code, 202)
        self.assertEqual(self.opened_urls(wait=1.0), [])

    def test_a_link_signed_with_the_agents_key_signs_nobody_in(self):
        rig = self.rig("no")
        nonce, exp = "a" * 32, int(time.time()) + 60
        payload = f"{nonce}.{exp}"
        sig = hmac.new(rig.key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        code, cookie = self.sign_in(rig, f"http://127.0.0.1:{rig.port}/login?t={payload}.{sig}")
        self.assertEqual((code, cookie), (403, ""))

    def test_a_browser_write_needs_its_origin_and_the_sessions_file_signs_nobody_in(self):
        rig = self.rig("yes")
        rig.call("POST", "/api/web/link", {})
        _, cookie = self.sign_in(rig, self.opened_urls()[0])
        body = {"wake_for": "needed"}
        for headers, want in (({}, 403), ({"Origin": "http://evil.example"}, 403), ({"Origin": f"http://127.0.0.1:{rig.port}"}, 200)):
            code, _, _ = self.raw(rig, "PUT", "/api/config", {"Cookie": cookie, **headers}, body)
            self.assertEqual(code, want, headers)
        with open(os.path.join(rig.home, "web_sessions.json")) as f:
            kept = json.load(f)
        value = cookie.partition("=")[2]
        self.assertNotIn(value, json.dumps(kept))
        self.assertEqual(list(kept), [hashlib.sha256(value.encode()).hexdigest()])
        # What the file holds, used as a cookie, is no sign-in.
        code, _, _ = self.raw(rig, "GET", "/api/status", {"Cookie": f"peerlink_session={list(kept)[0]}"})
        self.assertEqual(code, 401)

    def test_cookies_kept_before_0_9_44_are_dropped_from_the_file_and_sign_nobody_in(self):
        rig = Rig(env={"PEERLINK_TEST": "0", "PEERLINK_TEST_ANSWER": "no"})
        old = "a-cookie-kept-whole"
        with open(os.path.join(rig.home, "web_sessions.json"), "w") as f:
            json.dump({old: time.time() + 3600}, f)
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        with open(os.path.join(rig.home, "web_sessions.json")) as f:
            self.assertEqual(json.load(f), {})
        code, _, _ = self.raw(rig, "GET", "/api/status", {"Cookie": f"peerlink_session={old}"})
        self.assertEqual(code, 401)

    def test_discord_settings_are_yours_from_the_browser_only(self):
        rig = self.rig("no")
        code, out = rig.call("PUT", "/api/discord", {"allowed_users": ["123456789012345678"]})
        self.assertEqual((code, out["error"]), (403, "Discord is set up from the web UI"))
        code, _ = rig.call("DELETE", "/api/discord")
        self.assertEqual(code, 403)
        self.assertEqual(rig.call("GET", "/api/discord")[0], 200)

    def test_starting_clean_and_deleting_agents_or_spaces_are_yours_from_the_browser_only(self):
        rig = self.rig("no")
        for path, body in (("/api/clean", {"parts": ["history"]}), ("/api/agents/a1/delete", {}), ("/api/spaces/team/delete", {})):
            code, out = rig.call("POST", path, body)
            self.assertEqual(code, 403, path)
            self.assertIn("web UI", out["error"])

    def test_restart_stop_and_a_new_key_are_asked_on_screen_and_a_no_leaves_them(self):
        rig = self.rig("no")
        pid = rig.call("GET", "/api/health")[1]["pid"]
        for path in ("/api/restart", "/api/shutdown", "/api/key/rotate"):
            code, out = rig.call("POST", path, {})
            self.assertEqual((code, out.get("asked")), (202, True), path)
            time.sleep(0.3)
        self.assertEqual(rig.call("GET", "/api/health")[1]["pid"], pid)
        with open(os.path.join(rig.home, "key")) as f:
            self.assertEqual(f.read().strip(), rig.key)
        # Saying the same build is no reason to stop it.
        code, _ = rig.call("POST", "/api/shutdown", {"version": rig.call("GET", "/api/health")[1]["version"]})
        self.assertEqual(code, 202)

    def test_a_newer_plugin_replaces_the_daemon_without_asking(self):
        rig = self.rig("no")
        code, out = rig.call("POST", "/api/shutdown", {"version": "99.0.0"})
        self.assertEqual((code, out), (200, {"ok": True}))
        rig.proc.wait(5)
        self.assertIsNotNone(rig.proc.poll())


if __name__ == "__main__":
    unittest.main()
