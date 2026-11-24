"""The daemon's code fingerprint covers every module beside it that it imports, so a change to any of them replaces
a running daemon of the same version. Run: python3 -m unittest discover -s plugin/daemon"""
import ast
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-fingerprint-test-"))

import peerlinkd  # noqa: E402


def local_imports(path):
    """Names of the modules beside the daemon that this file imports."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return {n for n in names if os.path.isfile(os.path.join(HERE, n + ".py"))}


class FingerprintTest(unittest.TestCase):
    def test_covers_every_module_the_daemon_loads(self):
        seen, todo = set(), ["peerlinkd"]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            todo.extend(local_imports(os.path.join(HERE, name + ".py")))
        missing = sorted(n + ".py" for n in seen if n + ".py" not in peerlinkd.CODE_FILES)
        self.assertEqual(missing, [], "add these to CODE_FILES in peerlinkd.py")

    def test_agentslink_is_in_it(self):
        self.assertIn("agentslink.py", peerlinkd.CODE_FILES)


if __name__ == "__main__":
    unittest.main()
