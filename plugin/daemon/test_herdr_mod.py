"""peerlink's copy of the herdr app's spaces mod: the patch, the tree file it reads, and the daemon keeping both."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import herdr_mod as hm  # noqa: E402

# Stands in for herdr's spaces mod: every place the patch touches, once.
def fake(patches):
    return "\n".join('import { spaces, run } from "herdr";' if old == hm.IMPORT else old for old, _ in patches) + "\n"


FAKE = fake(hm.PATCHES)


class Patch(unittest.TestCase):
    def test_every_piece_goes_in(self):
        out, missing = hm.patch(FAKE, '/x/tree.json')
        self.assertIsNone(missing)
        for _, new in hm.PATCHES:
            self.assertIn(new.split("readTextFile(PEERLINK_TREE)")[0], out)
        self.assertIn('readTextFile("/x/tree.json")', out)  # the daemon's own home, not a guess at ~/.peerlink
        self.assertNotIn("PEERLINK_TREE", out)

    def test_the_agents_mod_takes_peerlinks_parent_first(self):
        out, missing = hm.patch(fake(hm.AGENTS_PATCHES), "/x/tree.json", hm.AGENTS_PATCHES)
        self.assertIsNone(missing)
        self.assertIn("peerlinkParent(k) === addr", out)
        self.assertIn("if (lead.length > 6) return [];", out)
        self.assertIn("(peerlinkTree.names || {})[k.address] || k.name", out)
        self.assertIn(".pl(15)", out)  # a sub-agent's line drops from the lead's icon
        self.assertIn("when(j > 0, el => el.ml(7))", out)  # and a deeper one's from the icon above it  # a nested row goes by its agent's name  # a loop in the tree can't recurse forever
        self.assertIn('readTextFile("/x/tree.json")', out)
        # A right-click on a lead's row (wide or narrow) or a sub-agent's opens its menu; the menu popup skips the list.
        self.assertEqual(out.count('.on_mouse_down("right", (_e, cx) => { cx.stop_propagation(); peerlinkAgentMenuOpen('), 3)
        self.assertIn("if (props()?.sortMenu || props()?.agentMenu) return;", out)
        self.assertIn("if (props()?.agentMenu) return peerlinkAgentMenu(t, props());", out)
        self.assertEqual(hm.point_layout('{"mod": "agents"}', "peerlink-agents", "agents"), '{"mod": "peerlink-agents"}')
        self.assertEqual(hm.point_layout('{"mod": "agents"}', "peerlink-spaces", "spaces"), '{"mod": "agents"}')

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_an_agents_menu_goes_to_it_or_ends_its_terminal(self):
        # The menu's two items, clicked, with herdr's calls recorded: Go focuses a pane (or peeks a sub-agent that has
        # none), Close kills the terminal, and each closes the popup first.
        stub = r'''
const calls = [];
const run = (c, a) => calls.push([c, a]);
const closeOverlay = (o) => calls.push(["close", o]);
const openOverlay = (o) => calls.push(["open", o.mod, o.props]);
const el = () => new Proxy({}, { get: (_, k) => k === "on_click" ? (f) => { clicks.push(f); return el(); } : () => el() });
let clicks = [];
const h_flex = el, v_flex = el, div = el, svg = el;
'''
        script = stub + hm.AGENTS_JS.replace("readTextFile(PEERLINK_TREE)", "(async()=>null)()") + r'''
for (const pane of ["p1", null]) {
  clicks = [];
  peerlinkAgentMenu({}, { overlay: "o1", agentMenu: { addr: "t4", pane, name: "web" } });
  clicks.forEach((f) => f());
}
peerlinkAgentMenuOpen("t4", null, "");
console.log(JSON.stringify(calls));
'''
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [
            ["close", "o1"], ["pane.focus", {"terminal": "t4"}], ["close", "o1"], ["terminal.kill", {"terminal": "t4"}],
            ["close", "o1"], ["terminal.peek", {"terminal": "t4"}], ["close", "o1"], ["terminal.kill", {"terminal": "t4"}],
            ["open", "peerlink-agents", {"agentMenu": {"addr": "t4", "pane": None, "name": "t4"}}]])

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_peerlinks_parent_wins_else_hcs(self):
        script = hm.AGENTS_JS.replace("readTextFile(PEERLINK_TREE)", "(async()=>null)()") + r'''
peerlinkRead = Date.now();
peerlinkTree = { agents: { t9: "t8", t7: "t7" } };
console.log(JSON.stringify([peerlinkParent({ address: "t9", parent: null }), peerlinkParent({ address: "t6", parent: "t5" }),
                            peerlinkParent({ address: "t7", parent: null })]));
'''
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["t8", "t5", None])

    def test_a_layout_points_its_spaces_block(self):
        layout = '{"content": {"mod": "spaces"}, "x": {"mod": "agents"}, "y": {"mod":"spaces-extra"}}'
        self.assertEqual(hm.point_layout(layout, "peerlink-spaces"),
                         '{"content": {"mod": "peerlink-spaces"}, "x": {"mod": "agents"}, "y": {"mod":"spaces-extra"}}')
        self.assertEqual(hm.point_layout(hm.point_layout(layout, "peerlink-spaces"), "spaces"), layout)

    def test_a_changed_mod_is_left_alone(self):
        out, missing = hm.patch(FAKE.replace("    const out = [];", "    const rows = [];"), "/x/tree.json")
        self.assertIsNone(out)
        self.assertIn("const out", missing)

    def test_the_tree_names_each_herdr_spaces_parent(self):
        smap = {"hq": "sp1", "ops": "sp2", "login": "sp3", "games": "sp4", "deep": "sp5"}
        parents = {"ops": "hq", "login": "hq", "deep": "ops", "games": "gone"}
        self.assertEqual(hm.tree(smap, parents), {"parents": {"sp2": "sp1", "sp3": "sp1", "sp5": "sp2"}, "agents": {}, "names": {}, "models": {}})

    def test_a_model_tag_is_short(self):
        self.assertEqual(hm.model_tag("claude-sonnet-5-5", "medium"), ["Sonnet 5.5", "med"])
        self.assertEqual(hm.model_tag("claude-opus-5-5[1m]", "xhigh"), ["Opus 5.5", "xhigh"])
        self.assertEqual(hm.model_tag("Haiku 5.5", None), ["Haiku 5.5", ""])
        self.assertEqual(hm.model_tag("opus", "turbo"), ["Opus", ""])
        self.assertIsNone(hm.model_tag("gpt-9", "high"))
        self.assertEqual(hm.model_tag("claude-3-5-sonnet-20241022", "low"), ["Sonnet", "low"])
        self.assertIsNone(hm.model_tag(None, "high"))

    def test_the_tree_carries_each_terminals_model_tag(self):
        got = hm.tree({}, {}, models={"t1": ["Opus 5.5", "med"], "t2": None})
        self.assertEqual(got["models"], {"t1": ["Opus 5.5", "med"]})

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_the_nesting_orders_depth_first_and_keeps_worktrees_with_their_repo(self):
        script = hm.NEST_JS.replace("readTextFile(PEERLINK_TREE)", "(async()=>null)()") + r'''
peerlinkRead = Date.now();
peerlinkTree = { parents: { sp2: "sp1", sp3: "sp1", sp5: "sp2" } };
const row = (id, tree) => ({ w: { id }, tree: tree || null });
const out = peerlinkNest([row("sp4"), row("sp4wt", "└─"), row("sp2"), row("sp1"), row("sp5"), row("sp3")]);
console.log(JSON.stringify(out.map((r) => [r.w.id, r.depth, r.lines || null])));
'''
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [["sp4", 0, []], ["sp4wt", 0, None], ["sp1", 0, []], ["sp2", 1, ["├"]],
                                                ["sp5", 2, ["│", "└"]], ["sp3", 1, ["└"]]])


class Daemon(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="hmod-")
        os.environ["PEERLINK_HOME"] = os.path.join(self.home, "peerlink")
        os.makedirs(os.environ["PEERLINK_HOME"], exist_ok=True)
        import peerlinkd
        self.d = peerlinkd
        self.app = os.path.join(self.home, "app")
        os.makedirs(os.path.join(self.app, "mods", "spaces"))
        with open(os.path.join(self.app, "mods", "spaces", "main.js"), "w") as f:
            f.write(FAKE)
        with open(os.path.join(self.app, "mods", "spaces", "mod.json"), "w") as f:
            f.write('{"title": "Spaces"}')
        os.makedirs(os.path.join(self.app, "layouts"))
        with open(os.path.join(self.app, "layouts", "mine.json"), "w") as f:
            f.write('{"id": "spaces", "content": {"mod": "spaces"}}')
        p = unittest.mock.patch.dict(os.environ, {"HERDR_APP_HOME": self.app})
        p.start()
        self.addCleanup(p.stop)
        p = unittest.mock.patch.object(self.d, "HOME", os.environ["PEERLINK_HOME"])
        p.start()
        self.addCleanup(p.stop)

    def test_it_writes_the_tree_and_makes_the_mod_once(self):
        self.d.herdr_nest({"hq": "sp1", "ops": "sp2"}, {"ops": "hq"})
        with open(os.path.join(self.d.HOME, "herdr-tree.json")) as f:
            self.assertEqual(json.load(f), {"parents": {"sp2": "sp1"}, "agents": {}, "names": {}, "models": {}})
        mod = os.path.join(self.app, "mods", "peerlink-spaces")
        with open(os.path.join(mod, "main.js")) as f:
            self.assertIn(json.dumps(os.path.join(self.d.HOME, "herdr-tree.json")), f.read())
        with open(os.path.join(self.app, "layouts", "mine.json")) as f:  # the sidebar shows the copy now
            self.assertEqual(f.read(), '{"id": "spaces", "content": {"mod": "peerlink-spaces"}}')
        self.assertTrue(os.path.exists(os.path.join(mod, "mod.json")))
        with open(os.path.join(self.app, "layouts", "mine.json"), "w") as f:  # you switch it back by hand
            f.write('{"content": {"mod": "spaces"}}')
        before = os.path.getmtime(os.path.join(mod, "main.js"))
        with unittest.mock.patch.object(self.d.herdr_mod, "patch", side_effect=AssertionError("made again")):
            self.d.herdr_nest({"hq": "sp1", "ops": "sp2"}, {"ops": "hq"})
        self.assertEqual(os.path.getmtime(os.path.join(mod, "main.js")), before)
        with open(os.path.join(self.app, "layouts", "mine.json")) as f:
            self.assertEqual(f.read(), '{"content": {"mod": "spaces"}}')  # and it stays that way

    def test_without_a_copy_the_layouts_show_herdrs_own(self):
        with open(os.path.join(self.app, "layouts", "mine.json"), "w") as f:
            f.write('{"content": {"mod": "peerlink-spaces"}}')
        with open(os.path.join(self.app, "mods", "spaces", "main.js"), "w") as f:
            f.write("a spaces mod the patch doesn't fit")
        self.d.herdr_nest({}, {})
        with open(os.path.join(self.app, "layouts", "mine.json")) as f:
            self.assertEqual(f.read(), '{"content": {"mod": "spaces"}}')

    def test_a_new_herdr_spaces_mod_is_copied_again_or_left_flat(self):
        self.d.herdr_nest({}, {})
        with open(os.path.join(self.app, "mods", "spaces", "main.js"), "w") as f:
            f.write("something else entirely")
        self.d.herdr_nest({}, {})  # the patch no longer fits: herdr's own mod shows, not a copy of its old one
        with open(os.path.join(self.app, "layouts", "mine.json")) as f:
            self.assertEqual(f.read(), '{"id": "spaces", "content": {"mod": "spaces"}}')
        with open(os.path.join(self.app, "mods", "spaces", "main.js"), "w") as f:
            f.write(FAKE)
        self.d.herdr_nest({}, {})  # it fits again: the copy is made from the new mod and shown
        with open(os.path.join(self.app, "layouts", "mine.json")) as f:
            self.assertEqual(f.read(), '{"id": "spaces", "content": {"mod": "peerlink-spaces"}}')


if __name__ == "__main__":
    unittest.main()
