"""An agent whose Claude Code refuses to start turns shows as stuck, not idle: what it holds unread shows as waiting,
not delivered, its lead is told once, and so is each agent waiting on a reply from it."""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-stuck-test-")

import peerlinkd as d  # noqa: E402


class Stuck(unittest.TestCase):
    def setUp(self):
        d.load_config()
        for value in ("sessions", "handed", "inboxes", "agents", "agent_of"):
            p = unittest.mock.patch.object(d, value, {})
            p.start()
            self.addCleanup(p.stop)
        for value in ("save_config", "save_agents"):
            p = unittest.mock.patch.object(d, value, lambda: None)
            p.start()
            self.addCleanup(p.stop)
        root = tempfile.mkdtemp(prefix="stuck-")
        self.lead_dir, self.dir = os.path.join(root, "crew"), os.path.join(root, "worker")
        for f in (self.lead_dir, self.dir):
            os.makedirs(f)
            d.config.setdefault("folder_spaces", {})[f] = "crew"
        d.config.setdefault("space_leads", {})["crew"] = {"cwd": self.lead_dir, "how": "set"}
        self.lead = self.session("lead", "crew", self.lead_dir)
        self.me = self.session("me", "worker", self.dir)
        self.asker = self.session("asker", "asker", self.lead_dir)

    def session(self, sid, name, cwd):
        rec = {"id": sid, "name": name, "cwd": cwd, "last_seen": time.time(), "started": time.time()}
        d.sessions[sid] = rec
        return rec

    def hand(self, mid, sender, expects_reply=True):
        m = {"id": mid, "ts": time.time(), "from": sender["id"], "from_name": sender["name"], "to": "me", "text": "review?",
             "expects_reply": expects_reply, "status": "queued", "v": 2}
        d.inboxes.setdefault("me", []).append(m)
        d.take_inbox("me", track=True)
        return m

    def told(self, sid):
        return [m["text"] for m in d.inboxes.get(sid, [])]

    def test_a_stuck_agent_shows_its_messages_waiting_and_tells_its_lead_and_senders_once(self):
        m = self.hand("m1", self.asker)
        with d.lock:
            d.note_stuck(self.me, time.time() * 1000)
            d.note_stuck(self.me, time.time() * 1000)
        self.assertEqual(m["status"], "waiting")
        self.assertTrue(d.public_peer(self.me)["stuck"])
        self.assertEqual(len([t for t in self.told("lead") if "worker can't start turns" in t]), 1)
        self.assertEqual(len([t for t in self.told("asker") if "your m1 waits" in t]), 1)
        # A turn takes it: delivered, and the agent isn't stuck any more.
        d.ack("me", ["m1"])
        with d.lock:
            d.note_stuck(self.me, 0)
        self.assertEqual(m["status"], "delivered")
        self.assertIsNone(d.public_peer(self.me)["stuck"])

    def test_a_stuck_lead_is_reported_above_it_or_to_nobody(self):
        d.config["space_leads"]["crew"] = {"cwd": self.dir, "how": "set"}
        with d.lock:
            d.note_stuck(self.me, time.time() * 1000)
        self.assertEqual(self.told("me"), [])


class StuckOverHttp(unittest.TestCase):
    def test_a_heartbeat_saying_stuck_shows_in_the_peer_list_until_one_says_otherwise(self):
        from rig import Rig
        with Rig() as rig:
            folder = tempfile.mkdtemp(prefix="stuck-http-")
            rig.call("POST", "/api/sessions", {"id": "s1", "name": "w", "cwd": folder, "plugin": "rig"})
            peer = lambda: next(p for p in rig.call("GET", "/api/peers?local=1")[1]["peers"] if p["id"] == "s1")
            beat = {"busy": False, "ack": [], "holding": []}
            rig.call("POST", "/api/sessions/s1/heartbeat", {**beat, "stuck": int(time.time() * 1000)})
            self.assertTrue(peer()["stuck"])
            rig.call("POST", "/api/sessions/s1/heartbeat", {**beat, "stuck": 0})
            self.assertIsNone(peer()["stuck"])


if __name__ == "__main__":
    unittest.main()
