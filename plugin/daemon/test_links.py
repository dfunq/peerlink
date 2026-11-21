"""Tests for links two space leads broker between their agents. Run: python3 -m unittest discover -s plugin/daemon"""
import copy
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-links-test-"))

import peerlinkd  # noqa: E402

d = peerlinkd
NAMES = ("sky", "pg", "stripe", "pgs", "subl", "subm")


class BrokeredLinksTest(unittest.TestCase):
    """sky leads "sky" (pg works there, and "sub" sits inside it); stripe leads "sec", where pgs works."""

    def setUp(self):
        d.load_config()
        d.load_discord()
        self.kept = copy.deepcopy(({k: d.config.get(k) for k in d.SPACE_KEYS + ("agent_links", "link_info")}, d.tasks, d.messages))
        self.addCleanup(self.restore)
        d.config.update({"rate_limit_per_min": 100, "wake_for": "needed", "agent_links": [], "link_info": {},
                         "folder_spaces": {"/x/sky": "sky", "/x/pg": "sky", "/x/stripe": "sec", "/x/pgs": "sec", "/x/subl": "sub", "/x/subm": "sub"},
                         "space_parents": {"sub": "sky"},
                         "space_leads": {"sky": {"cwd": "/x/sky", "how": "set"}, "sec": {"cwd": "/x/stripe", "how": "set"}, "sub": {"cwd": "/x/subl", "how": "set"}}})
        now = time.time()
        for n in NAMES:
            d.sessions["s-" + n] = {"id": "s-" + n, "name": n, "cwd": "/x/" + n, "agent": "a-" + n, "last_seen": now}
        d.forget_spaces()
        d.inboxes.clear()
        d.link_asks.clear()

    def restore(self):
        conf, tasks, messages = self.kept
        d.config.update(conf)
        for n in NAMES:
            d.sessions.pop("s-" + n, None)
        d.tasks.clear()
        d.tasks.update(tasks)
        d.messages[:] = messages
        d.inboxes.clear()
        d.link_asks.clear()
        d.forget_spaces()

    def link(self, by, **body):
        return d.broker_link("s-" + by, body)

    def inbox(self, name):
        return [m for m in d.inboxes.get("s-" + name, [])]

    def linked(self, a, b):
        return "a-" + b in d.links_of(d.sessions["s-" + a])

    def test_two_leads_link_their_agents_and_the_link_ends_with_its_task(self):
        status, out = d.send({"from": "s-sky", "to": "pg", "kind": "task", "text": "Wire the shared SDK"})
        self.assertEqual(status, 200, out)
        tid = out["task"]
        # Only a lead brokers, and only for its own agents.
        self.assertEqual(self.link("pg", mine="pg", theirs="pgs")[0], 403)
        self.assertEqual(self.link("sky", mine="pgs", theirs="pg")[0], 403)
        # sky asks; stripe hears whom it would link, and how to agree.
        self.assertEqual(self.link("sky", mine="pg", theirs="pgs", why="share the SDK types", task=tid), (200, {"status": "asked", "lead": "stripe"}))
        ask = self.inbox("stripe")[-1]
        self.assertTrue(ask["expects_reply"])
        self.assertIn('call link with mine "pgs" and theirs "pg"', ask["text"])
        self.assertFalse(self.linked("pg", "pgs"))
        # stripe agrees from its side: both agents are told, and see each other.
        self.assertEqual(self.link("stripe", mine="pgs", theirs="pg"), (200, {"status": "linked"}))
        self.assertTrue(self.linked("pg", "pgs") and self.linked("pgs", "pg"))
        self.assertIn("sky and stripe linked you with pgs (space sec) for task " + tid, self.inbox("pg")[-1]["text"])
        self.assertIn("sky and stripe linked you with pg (space sky)", self.inbox("pgs")[-1]["text"])
        self.assertEqual(self.link("sky", mine="pg", theirs="pgs")[0], 409)
        # The task closes, and the link with it.
        self.assertEqual(d.update_task(tid, {"by": "s-pg", "status": "done", "note": "Wired."})[0], 200)
        self.assertFalse(self.linked("pg", "pgs"))
        self.assertIn(f"Your link with pg has ended: task {tid} closed.", self.inbox("pgs")[-1]["text"])
        self.assertEqual(d.config["link_info"], {})

    def test_a_lead_over_both_links_them_at_once_and_either_lead_removes_only_its_own(self):
        self.assertEqual(self.link("sky", mine="pg", theirs="subm"), (200, {"status": "linked"}))
        self.assertTrue(self.linked("subm", "pg"))
        self.assertEqual(self.link("sky", mine="pg", theirs="subm", remove=True), (200, {"status": "removed"}))
        self.assertFalse(self.linked("subm", "pg"))
        # One your user made in the web UI stays theirs.
        d.config["agent_links"] = [["a-pg", "a-pgs"]]
        status, out = self.link("sky", mine="pg", theirs="pgs", remove=True)
        self.assertEqual(status, 404)
        self.assertIn("your user made that one", out["error"])
        self.assertTrue(self.linked("pg", "pgs"))


if __name__ == "__main__":
    unittest.main()
