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
FAKE = "\n".join(old for old, _ in hm.PATCHES) + "\n"


class Patch(unittest.TestCase):
    def test_every_piece_goes_in(self):
        out, missing = hm.patch(FAKE, '/x/tree.json')
        self.assertIsNone(missing)
        for _, new in hm.PATCHES:
            self.assertIn(new.split("readTextFile(PEERLINK_TREE)")[0], out)
        self.assertIn('readTextFile("/x/tree.json")', out)  # the daemon's own home, not a guess at ~/.peerlink
        self.assertNotIn("PEERLINK_TREE", out)

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
        smap = {"skyforge": "sp1", "ops": "sp2", "login": "sp3", "games": "sp4", "deep": "sp5"}
        parents = {"ops": "skyforge", "login": "skyforge", "deep": "ops", "games": "gone"}
        self.assertEqual(hm.tree(smap, parents), {"parents": {"sp2": "sp1", "sp3": "sp1", "sp5": "sp2"}})

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_the_nesting_orders_depth_first_and_keeps_worktrees_with_their_repo(self):
        script = hm.NEST_JS.replace("readTextFile(PEERLINK_TREE)", "(async()=>null)()") + r'''
peerlinkRead = Date.now();
peerlinkTree = { parents: { sp2: "sp1", sp3: "sp1", sp5: "sp2" } };
const row = (id, tree) => ({ w: { id }, tree: tree || null });
const out = peerlinkNest([row("sp4"), row("sp4wt", "└─"), row("sp2"), row("sp1"), row("sp5"), row("sp3")]);
console.log(JSON.stringify(out.map((r) => [r.w.id, r.depth])));
'''
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [["sp4", 0], ["sp4wt", 0], ["sp1", 0], ["sp2", 1], ["sp5", 2], ["sp3", 1]])


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
        self.d.herdr_nest({"skyforge": "sp1", "ops": "sp2"}, {"ops": "skyforge"})
        with open(os.path.join(self.d.HOME, "herdr-tree.json")) as f:
            self.assertEqual(json.load(f), {"parents": {"sp2": "sp1"}})
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
            self.d.herdr_nest({"skyforge": "sp1", "ops": "sp2"}, {"ops": "skyforge"})
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
        self.d.herdr_nest({}, {})  # the patch no longer fits: the old copy stays, nothing breaks
        with open(os.path.join(self.app, "mods", "peerlink-spaces", "main.js")) as f:
            self.assertIn("peerlinkNest", f.read())


if __name__ == "__main__":
    unittest.main()
