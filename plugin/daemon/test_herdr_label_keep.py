"""SKY-689 follow-up: a terminal gives up peerlink's labels only once it's clear its agent is gone, never on the first
passes after the daemon starts or the machine wakes, when every heartbeat is late."""
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-label-keep-test-")

import herdr_sync as hs  # noqa: E402
import peerlinkd as d  # noqa: E402


class LabelKeep(unittest.TestCase):
    def setUp(self):
        d.load_config()
        d.config["peer_ttl_s"] = 30  # as live
        self.agents = {"a-vault": {"id": "a-vault", "seen": 0}, "a-gone": {"id": "a-gone", "seen": 0}}
        for name, value in (("agents", self.agents), ("herdr_steady", {"since": 1000.0, "last": 0.0})):
            p = unittest.mock.patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.placed = [{"address": "t2g", "agent": "a-vault"}]
        self.terms = {"t2g": {"agent": "a-vault"}, "t3": {"agent": "a-vault"}, "t9": {"agent": "a-gone"},
                      "t4": {"agent": "vault"}, "t5": {}}

    def test_right_after_start_every_label_stays(self):
        self.assertEqual(d.herdr_label_keep(self.placed, self.terms, now=1010), {"t3", "t9", "t4"})

    def test_once_steady_an_agent_running_elsewhere_or_long_gone_lets_go(self):
        self.agents["a-gone"]["seen"] = 1100
        d.herdr_label_keep(self.placed, self.terms, now=1100)
        # t3: vault runs in t2g. t4: no agent goes by "vault". t9: seen 30s ago, maybe just late.
        self.assertEqual(d.herdr_label_keep(self.placed, self.terms, now=1130), {"t9"})
        d.herdr_label_keep(self.placed, self.terms, now=1180)
        self.assertEqual(d.herdr_label_keep(self.placed, self.terms, now=1230), set())

    def test_a_gap_in_the_passes_starts_the_wait_again(self):
        d.herdr_label_keep(self.placed, self.terms, now=1100)
        self.assertEqual(d.herdr_label_keep(self.placed, self.terms, now=1130), set())
        # The machine slept an hour: everything waits again.
        self.assertEqual(d.herdr_label_keep(self.placed, self.terms, now=4731), {"t3", "t9", "t4"})

    def test_passes_a_sync_interval_apart_reach_steady(self):
        # The syncer sleeps HERDR_SYNC_EVERY_S and then takes a few seconds: every gap is a little over 30 s.
        now = 1000.0
        for gap in (31, 37, 40, 33, 39):
            now += gap
            kept = d.herdr_label_keep(self.placed, self.terms, now=now)
        self.assertGreaterEqual(now - 1000, 2 * d.config["peer_ttl_s"])
        self.assertEqual(kept, set())

    def test_a_preview_doesnt_move_the_clock(self):
        d.herdr_label_keep(self.placed, self.terms, apply=False, now=1500)
        self.assertEqual(d.herdr_steady, {"since": 1000.0, "last": 0.0})

    def test_plan_leaves_kept_terminals_alone(self):
        terms = {"t3": {"space": "sp1", "tab": "tb3", "agent": "a-vault", "pspace": "s"}}
        layout = [{"id": "sp1", "name": "s", "tabs": [{"id": "tb3", "name": "vault", "terminals": ["t3"]}]}]
        self.assertEqual(hs.plan([], terms, layout, {}, lambda p: p + "x", keep={"t3"})[0], [])


if __name__ == "__main__":
    unittest.main()
