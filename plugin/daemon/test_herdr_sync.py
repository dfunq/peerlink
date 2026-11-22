import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import herdr_sync as hs  # noqa: E402


def ids():
    n = itertools.count(1)
    return lambda prefix: f"{prefix}new{next(n)}"


def agent(addr, name, space, lead=False):
    return {"address": addr, "agent": "a-" + name, "name": name, "space": space, "lead": lead}


def term(space, tab, name=None, pspace=None):
    return {"space": space, "tab": tab, "agent": ("a-" + name) if name else "", "pspace": pspace or ""}


class Joins(unittest.TestCase):
    layout = [{"id": "spops", "name": "ops", "tabs": [{"id": "tb1", "name": "1", "terminals": ["t8"]}]}]

    def new(self, **kw):
        return {**agent("t8", "peerlink", "tools"), "created": 1000, **kw}

    def test_a_new_agent_shown_in_a_mirrored_space_joins_it(self):
        got = hs.joins([self.new()], {"t8": term("spops", "tb1")}, self.layout, {"ops": "spops"}, 1100)
        self.assertEqual(got, ([("t8", "ops")], ["a-peerlink"]))

    def test_who_stays(self):
        cases = {"seen": ([self.new(seen=True)], {}, self.layout, 1100),
                 "explicit": ([self.new(explicit=True)], {}, self.layout, 1100),
                 "old": ([self.new()], {}, self.layout, 1000 + hs.JOIN_FRESH_S + 1),
                 "parent": ([self.new()], {"t8": {"parent": True}}, self.layout, 1100),
                 "spawned": ([self.new()], {"t8": {"spawned": True}}, self.layout, 1100),
                 "unmirrored": ([self.new()], {}, [{**self.layout[0], "id": "spx"}], 1100),
                 "already there": ([self.new(space="ops")], {}, self.layout, 1100)}
        for why, (agents, terms, layout, now) in cases.items():
            with self.subTest(why):
                self.assertEqual(hs.joins(agents, terms, layout, {"ops": "spops"}, now)[0], [])

    def test_a_closed_space_takes_nobody(self):
        got = hs.joins([self.new()], {}, self.layout, {"ops": "spops"}, 1100, closed={"ops"})
        self.assertEqual(got, ([], ["a-peerlink"]))  # judged all the same: it never joins later either

    def test_labels_alone_dont_count(self):
        # Any process can label a terminal: without the app showing it, nobody is judged, so nobody is marked seen.
        self.assertEqual(hs.joins([self.new()], {"t8": term("spops", "tb1")}, None, {"ops": "spops"}, 1100), ([], []))
        self.assertEqual(hs.joins([self.new()], {"t8": term("spops", "tb1")}, [], {"ops": "spops"}, 1100), ([], []))


class TabNames(unittest.TestCase):
    def layout(self, name, terminals=("t8",)):
        return [{"id": "sp", "name": "tools", "tabs": [{"id": "tb1", "name": name, "terminals": list(terminals)}]}]

    def new(self, **kw):
        return {**agent("t8", "peerlink-2", "tools"), "created": 1000, **kw}

    def test_a_new_agent_takes_the_name_you_gave_its_tab(self):
        self.assertEqual(hs.tab_names([self.new()], {}, self.layout("reviewer"), 1100), [("t8", "reviewer")])

    def test_when_it_keeps_its_own(self):
        cases = {"no name": ([self.new()], {}, self.layout(""), 1100),
                 "a number": ([self.new()], {}, self.layout("3"), 1100),
                 "its name already": ([self.new()], {}, self.layout("Peerlink-2"), 1100),
                 "shared tab": ([self.new()], {}, self.layout("reviewer", ("t8", "t9")), 1100),
                 "seen": ([self.new(seen=True)], {}, self.layout("reviewer"), 1100),
                 "old": ([self.new()], {}, self.layout("reviewer"), 1000 + hs.JOIN_FRESH_S + 1),
                 "hired": ([self.new()], {"t8": {"spawned": True}}, self.layout("reviewer"), 1100),
                 "app closed": ([self.new()], {}, None, 1100)}
        for why, (agents, terms, layout, now) in cases.items():
            with self.subTest(why):
                self.assertEqual(hs.tab_names(agents, terms, layout, now), [])


class Plan(unittest.TestCase):
    def test_a_worker_nested_under_its_lead_is_never_moved(self):
        agents = [agent("t8", "peerlink", "tools", lead=True), agent("t9", "worker", "tools")]
        terms = {"t8": term("sptools", "tb8", "peerlink", "tools"), "t9": {**term("spother", "tb9", "worker", "tools"), "parent": True}}
        layout = [{"id": "sptools", "name": "tools", "tabs": [{"id": "tb8", "name": "peerlink", "terminals": ["t8"]}]},
                  {"id": "spother", "name": "other", "tabs": [{"id": "tb9", "name": "worker", "terminals": ["t9"]}]}]
        actions, smap, notes = hs.plan(agents, terms, layout, {"tools": "sptools"}, ids())
        self.assertFalse([a for a in actions if a[0] == "label" and a[1] == "t9"])

    def test_address(self):
        self.assertEqual(hs.hc_address("hc:t7@123"), "t7")
        self.assertEqual(hs.hc_address("hc:box/t1b@9"), "box/t1b")
        self.assertEqual(hs.hc_address("tmux:%3@1"), "")
        self.assertEqual(hs.hc_address(""), "")
        self.assertEqual(hs.key_pid("hc:t7@123"), 123)
        self.assertEqual(hs.key_pid("hc:t7"), 0)

    def test_splits_a_mixed_herdr_space_by_peerlink_space(self):
        # herdr's "mac" holds a tools agent and an ops agent; each lead is first in its peerlink space.
        agents = [agent("t8", "peerlink", "tools", lead=True), agent("t9", "services", "ops", lead=True)]
        terms = {"t8": term("spmac", "tb8", "peerlink", "tools"), "t9": term("spmac", "tb9", "services", "ops")}
        layout = [{"id": "spmac", "name": "mac", "tabs": [{"id": "tb8", "name": "peerlink", "terminals": ["t8"]},
                                                         {"id": "tb9", "name": "services", "terminals": ["t9"]}]}]
        actions, smap, notes = hs.plan(agents, terms, layout, {}, ids())
        # Tie on count and lead: the name order decides, so ops keeps mac and tools gets a new space.
        self.assertEqual(smap, {"ops": "spmac", "tools": "spnew1"})
        self.assertIn(("label", "t8", {"herdr.space": "spnew1", "herdr.tab": "tbnew2"}), actions)
        self.assertIn(("rename_space", "spmac", "ops"), actions)
        self.assertFalse([a for a in actions if a[0] == "label" and a[1] == "t9"])
        self.assertEqual(notes, [])

    def test_reuses_a_herdr_space_named_after_the_peerlink_space(self):
        agents = [agent("t1", "pixelgrid", "pixelgrid", lead=True)]
        terms = {"t1": term("spother", "tb1", "pixelgrid", "pixelgrid")}
        layout = [{"id": "spother", "name": "elsewhere", "tabs": [{"id": "tb1", "name": "pixelgrid", "terminals": ["t1"]}]},
                  {"id": "sppg", "name": "PixelGrid", "tabs": []}]
        actions, smap, _ = hs.plan(agents, terms, layout, {}, ids())
        self.assertEqual(smap["pixelgrid"], "sppg")
        self.assertIn(("label", "t1", {"herdr.space": "sppg", "herdr.tab": "tbnew1"}), actions)
        self.assertIn(("rename_space", "sppg", "pixelgrid"), actions)

    def test_remembered_space_is_kept_and_a_settled_layout_does_nothing(self):
        agents = [agent("t1", "ceo", "skyforge", lead=True), agent("t2", "vault", "skyforge")]
        terms = {"t1": term("sp1", "tb1", "ceo", "skyforge"), "t2": term("sp1", "tb2", "vault", "skyforge")}
        layout = [{"id": "sp1", "name": "skyforge", "tabs": [{"id": "tb1", "name": "ceo", "terminals": ["t1"]},
                                                            {"id": "tb2", "name": "vault", "terminals": ["t2"]}]}]
        actions, smap, notes = hs.plan(agents, terms, layout, {"skyforge": "sp1"}, ids())
        self.assertEqual((actions, smap, notes), ([], {"skyforge": "sp1"}, []))

    def test_names_tabs_and_puts_the_lead_first(self):
        agents = [agent("t1", "ceo", "skyforge", lead=True), agent("t2", "vault", "skyforge")]
        terms = {"t1": term("sp1", "tb1", "ceo", "skyforge"), "t2": term("sp1", "tb2", "vault", "skyforge")}
        layout = [{"id": "sp1", "name": "skyforge", "tabs": [{"id": "tb2", "name": "2", "terminals": ["t2"]},
                                                            {"id": "tb1", "name": "ceo", "terminals": ["t1"]}]}]
        actions, _, _ = hs.plan(agents, terms, layout, {"skyforge": "sp1"}, ids())
        self.assertEqual(actions, [("rename_tab", "tb2", "vault"), ("first_tab", "tb1")])

    def test_a_tab_shared_with_a_shell_stays_put(self):
        agents = [agent("t1", "core", "login", lead=True)]
        terms = {"t1": term("spx", "tb1", "core", "login"), "t5": term("spx", "tb1")}
        actions, _, notes = hs.plan(agents, terms, None, {"login": "splogin"}, ids())
        self.assertFalse([a for a in actions if "herdr.space" in a[2]])
        self.assertEqual(notes, ["core stays put: its tab also holds t5"])

    def test_agents_of_one_space_sharing_a_tab_move_together(self):
        agents = [agent("t1", "a", "s"), agent("t2", "b", "s")]
        terms = {"t1": term("spx", "tbx", "a", "s"), "t2": term("spx", "tbx", "b", "s"), "t3": term("spx", "tby")}
        actions, smap, _ = hs.plan(agents, terms, None, {"s": "sps"}, ids())
        moves = {a[1]: a[2] for a in actions}
        self.assertEqual(moves["t1"]["herdr.tab"], moves["t2"]["herdr.tab"])
        self.assertEqual(moves["t1"]["herdr.space"], "sps")

    def test_without_the_app_only_labels_change(self):
        agents = [agent("t1", "quality", "quality", lead=True)]
        terms = {"t1": term("", "", None, None)}  # an hc terminal herdr doesn't show yet
        actions, smap, _ = hs.plan(agents, terms, None, {}, ids())
        self.assertEqual(actions, [("label", "t1", {"herdr.space": "spnew1", "herdr.tab": "tbnew2",
                                                    "peerlink.agent": "a-quality", "peerlink.space": "quality"})])

    def test_stale_labels_are_refreshed(self):
        agents = [agent("t2", "quality", "quality", lead=True)]
        terms = {"t2": term("spq", "tb2", "skyforge-worker", "skyforge")}
        actions, _, _ = hs.plan(agents, terms, None, {"quality": "spq"}, ids())
        self.assertEqual(actions, [("label", "t2", {"peerlink.agent": "a-quality", "peerlink.space": "quality"})])

    def test_a_space_closed_in_herdr_comes_back_where_its_terminals_are(self):
        agents = [agent("t1", "x", "s")]
        terms = {"t1": term("spgone", "tb1", "x", "s")}
        layout = [{"id": "spother", "name": "other", "tabs": []}]
        _, smap, _ = hs.plan(agents, terms, layout, {"s": "spgone"}, ids())
        # spgone isn't in the app any more, but its terminal still names it: it's kept, the app shows it again.
        self.assertEqual(smap["s"], "spgone")

    def test_a_space_closed_in_herdr_with_no_terminals_left_gets_a_new_one(self):
        agents = [agent("t1", "x", "s")]
        terms = {"t1": term("", "", "x", "s")}
        _, smap, _ = hs.plan(agents, terms, [{"id": "spother", "name": "other", "tabs": []}], {"s": "spgone"}, ids())
        self.assertEqual(smap["s"], "spnew1")

    def test_two_peerlink_spaces_never_share_a_herdr_space(self):
        agents = [agent("t1", "a", "one"), agent("t2", "b", "two")]
        terms = {"t1": term("sp1", "tb1", "a", "one"), "t2": term("sp1", "tb2", "b", "two")}
        _, smap, _ = hs.plan(agents, terms, None, {"one": "sp1", "two": "sp1"}, ids())
        self.assertNotEqual(smap["one"], smap["two"])

    def test_an_offline_spaces_herdr_space_isnt_taken(self):
        # games has nobody live; its remembered herdr space holds a stray agent of another space.
        agents = [agent("t1", "x", "s")]
        terms = {"t1": term("spgames", "tb1", "x", "s")}
        _, smap, _ = hs.plan(agents, terms, None, {"games": "spgames"}, ids())
        self.assertEqual(smap, {"games": "spgames", "s": "spnew1"})

    def test_a_terminal_shown_elsewhere_than_its_labels_say_moves(self):
        # Labelled for loop-control already, but the app still shows it in login.
        agents = [agent("t6", "loop-control", "loop-control", lead=True), agent("t5", "core", "login", lead=True)]
        terms = {"t6": term("splc", "tb6", "loop-control", "loop-control"), "t5": term("splogin", "tb5", "core", "login")}
        layout = [{"id": "splogin", "name": "login", "tabs": [{"id": "tb5", "name": "core", "terminals": ["t5"]},
                                                             {"id": "tb6", "name": "loop-control", "terminals": ["t6"]}]}]
        actions, _, _ = hs.plan(agents, terms, layout, {"login": "splogin", "loop-control": "splc"}, ids())
        self.assertIn(("label", "t6", {"herdr.space": "splc", "herdr.tab": "tbnew1"}), actions)

    def test_describe(self):
        line = hs.describe(("label", "t8", {"herdr.space": "spnew1", "herdr.tab": "tb"}), {"t8": "peerlink"}, {"tools": "spnew1"})
        self.assertEqual(line, "move peerlink (t8) to herdr space tools")


if __name__ == "__main__":
    unittest.main()
