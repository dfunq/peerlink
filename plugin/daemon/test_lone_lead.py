"""A space's default lead goes by its own agent, not its folder: another agent sharing that folder, or agents checking
back in one by one after a restart, never hand the space to someone else, and the notice reaches the agent in the space."""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-lone-lead-test-")

import peerlinkd as d  # noqa: E402


class LoneLead(unittest.TestCase):
    def setUp(self):
        d.load_config(save=False)
        self.spaces = {}
        now = time.time()
        patches = [unittest.mock.patch.dict(d.config, {"space_leads": {"apps": {"cwd": "/x/hub", "how": "only", "agent": "a-apps"}}}),
                   unittest.mock.patch.object(d, "space_of", lambda r: (self.spaces.get(r.get("id"), "default"), "agent", r.get("cwd", ""))),
                   unittest.mock.patch.object(d, "save_config", lambda: None),
                   unittest.mock.patch.object(d, "DAEMON_START", now),
                   unittest.mock.patch.object(d, "messages", []),
                   unittest.mock.patch.object(d, "folder_seen", {})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.kept = (dict(d.sessions), dict(d.agents), dict(d.inboxes))
        self.addCleanup(self.restore)
        d.sessions.clear()
        d.inboxes.clear()
        d.agents["a-apps"] = {"id": "a-apps", "name": "apps", "cwd": "/x/hub", "seen": now - 10000}

    def restore(self):
        for store, was in zip((d.sessions, d.agents, d.inboxes), self.kept):
            store.clear()
            store.update(was)

    def live(self, sid, name, cwd, space):
        d.sessions[sid] = {"id": sid, "name": name, "cwd": cwd, "agent": "a-" + sid, "last_seen": time.time()}
        self.spaces[sid] = space

    def told(self, sid):
        """The apps lead notices this agent got (the other spaces here have no lead, so their lone agents get theirs)."""
        return [m["text"] for m in d.inboxes.get(sid, []) if "lead the apps space" in m["text"]]

    def test_another_agent_in_the_leads_folder_doesnt_hand_the_space_away_after_a_restart(self):
        self.live("hq", "hq", "/x/hub", "hq")  # shares the lead's folder, in its own space
        self.live("web", "web", "/x/acme", "apps")  # checked back in first, briefly counted in apps
        with d.lock:
            d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["apps"]["cwd"], "/x/hub")
        self.assertFalse(self.told("web") + self.told("hq"))
        # Past the grace, a lead that never came back gives way.
        with unittest.mock.patch.object(d, "DAEMON_START", time.time() - d.LEAD_GRACE_S - 5), d.lock:
            d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["apps"]["cwd"], "/x/acme")

    def test_a_lead_that_moved_away_hands_over_and_the_notice_reaches_the_agent_in_the_space(self):
        d.sessions["apps"] = {"id": "apps", "name": "apps", "cwd": "/x/hub", "agent": "a-apps", "last_seen": time.time()}
        self.spaces["apps"] = "elsewhere"
        self.live("shop", "shop", "/x/shop", "zzz")  # goes by its folder's name, in another space
        self.live("w", "worker", "/x/shop", "apps")
        with d.lock:
            d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["apps"]["cwd"], "/x/shop")
        self.assertEqual(len(self.told("w")), 1)
        self.assertFalse(self.told("shop"))

    def test_while_the_lead_is_away_another_space_sharing_its_folder_gets_none_of_its_traffic(self):
        self.live("hq", "hub", "/x/hub", "hq")  # goes by the lead's folder's name, in its own space
        self.live("web", "web", "/x/acme", "apps")
        with d.lock:
            d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["apps"]["cwd"], "/x/hub")
        self.assertIsNone(d.lead_rec("apps"))

    def test_a_lead_back_under_another_spelling_of_its_folder_keeps_the_space(self):
        d.sessions["apps"] = {"id": "apps", "name": "apps", "cwd": "/x/hub/", "agent": "a-apps", "last_seen": time.time()}
        self.spaces["apps"] = "apps"
        with d.lock:
            d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["apps"], {"cwd": "/x/hub/", "how": "only", "agent": "a-apps"})
        self.assertEqual(d.lead_rec("apps")["id"], "apps")
        self.assertFalse(self.told("apps"))


if __name__ == "__main__":
    unittest.main()
