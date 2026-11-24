"""Install herdr mods: copies your mods folder into herdr-app and your home, never sign-ins or secrets, backs up
what it replaces outside herdr-app's mods, leaves peerlink's own panel alone, and works only from your browser."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-herdr-mods-test-")

import peerlinkd as d  # noqa: E402
from rig import Rig  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class InstallHerdrMods(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="herdr-mods-")
        self.src, self.app, self.home = (os.path.join(base, x) for x in ("src", "app", "home"))
        os.makedirs(os.path.join(self.app, "mods"))
        os.makedirs(self.home)
        for target, value in ((os.environ, {"HERDR_APP_HOME": self.app, "HOME": self.home, "USERPROFILE": self.home}),
                              (d.config, {"herdr_mods_source": ""})):
            p = unittest.mock.patch.dict(target, value)
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d, "save_config", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        write(os.path.join(self.src, "mods", "jira", "main.js"), "jira 1")
        write(os.path.join(self.src, "mods", "jira", "icons", "bug.svg"), "<svg/>")
        write(os.path.join(self.src, "mods", "jira", "jsconfig.json"), "{}")
        write(os.path.join(self.src, "mods", "peerlink", "main.js"), "not ours")
        write(os.path.join(self.src, "layouts", "herdr-jira.json"), "{}")
        write(os.path.join(self.src, "_home", ".herdr-jira", "oauth.py"), "print(1)")
        write(os.path.join(self.src, "_home", ".herdr-jira", "tokens.json"), "SECRET")
        write(os.path.join(self.src, "_home", ".herdr-jira", "app.json"), "SECRET")
        write(os.path.join(self.src, "_home", ".herdr-jira", "send.log"), "log")

    def test_copies_mods_layouts_and_home_files_and_saves_the_source(self):
        code, out = d.install_herdr_mods(self.src)
        self.assertEqual(code, 200)
        self.assertEqual(d.config["herdr_mods_source"], self.src)
        self.assertEqual(out["mods"], ["jira"])
        self.assertEqual(read(os.path.join(self.app, "mods", "jira", "main.js")), "jira 1")
        self.assertTrue(os.path.exists(os.path.join(self.app, "mods", "jira", "icons", "bug.svg")))
        self.assertTrue(os.path.exists(os.path.join(self.app, "layouts", "herdr-jira.json")))
        self.assertEqual(read(os.path.join(self.home, ".herdr-jira", "oauth.py")), "print(1)")

    def test_home_takes_only_herdr_folders_never_a_dotfile(self):
        write(os.path.join(self.home, ".zshrc"), "mine")
        write(os.path.join(self.src, "_home", ".zshrc"), "theirs")
        write(os.path.join(self.src, "_home", ".config", "x.json"), "theirs")
        write(os.path.join(self.src, "_home", ".herdr-", "y.py"), "theirs")
        code, out = d.install_herdr_mods(self.src)
        self.assertEqual(code, 200)
        self.assertEqual(read(os.path.join(self.home, ".zshrc")), "mine")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".herdr-")))
        self.assertEqual(sorted(out["home_skipped"]), sorted([".zshrc", os.path.join(".config", "x.json"), os.path.join(".herdr-", "y.py")]))
        self.assertEqual(read(os.path.join(self.home, ".herdr-jira", "oauth.py")), "print(1)")

    def test_a_herdr_folder_that_is_a_symlink_carries_nothing_elsewhere(self):
        elsewhere = os.path.join(os.path.dirname(self.home), "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.home, ".herdr-x"))
        write(os.path.join(self.src, "_home", ".herdr-x", "authorized_keys"), "theirs")
        os.makedirs(os.path.join(self.home, ".herdr-y"))
        os.symlink(elsewhere, os.path.join(self.home, ".herdr-y", "sub"))
        write(os.path.join(self.src, "_home", ".herdr-y", "sub", "f"), "theirs")
        code, out = d.install_herdr_mods(self.src)
        self.assertEqual(code, 200)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertIn(os.path.join(".herdr-x", "authorized_keys"), out["home_skipped"])
        self.assertIn(os.path.join(".herdr-y", "sub", "f"), out["home_skipped"])
        self.assertEqual(read(os.path.join(self.home, ".herdr-jira", "oauth.py")), "print(1)")

    def test_never_copies_secrets_logs_herdrs_own_files_or_over_peerlinks_panel(self):
        write(os.path.join(self.app, "mods", "peerlink", "main.js"), "the real panel")
        d.install_herdr_mods(self.src)
        for p in ("tokens.json", "app.json", "send.log"):
            self.assertFalse(os.path.exists(os.path.join(self.home, ".herdr-jira", p)), p)
        self.assertFalse(os.path.exists(os.path.join(self.app, "mods", "jira", "jsconfig.json")))
        self.assertEqual(read(os.path.join(self.app, "mods", "peerlink", "main.js")), "the real panel")

    def test_again_changes_nothing_and_an_update_backs_up_outside_herdr_apps_mods(self):
        d.install_herdr_mods(self.src)
        code, out = d.install_herdr_mods()
        self.assertEqual((out["copied"], out["backup"]), ([], ""))
        write(os.path.join(self.src, "mods", "jira", "main.js"), "jira 2")
        code, out = d.install_herdr_mods()
        self.assertEqual(len(out["copied"]), 1)
        self.assertEqual(read(os.path.join(self.app, "mods", "jira", "main.js")), "jira 2")
        backup = os.path.join(d.HOME, "backups", os.path.basename(out["backup"]))
        self.assertEqual(read(os.path.join(backup, "herdr-app", "mods", "jira", "main.js")), "jira 1")
        self.assertEqual(sorted(os.listdir(os.path.join(self.app, "mods"))), ["jira"])  # no .bak folder herdr would load

    def test_a_layout_you_have_is_kept(self):
        write(os.path.join(self.app, "layouts", "herdr-jira.json"), "mine, arranged")
        code, out = d.install_herdr_mods(self.src)
        self.assertEqual(read(os.path.join(self.app, "layouts", "herdr-jira.json")), "mine, arranged")
        self.assertEqual(out["layouts_kept"], ["herdr-jira.json"])

    def test_refuses_without_a_source_or_herdr_app(self):
        self.assertEqual(d.install_herdr_mods()[0], 400)
        self.assertEqual(d.install_herdr_mods(os.path.join(self.src, "nope"))[0], 400)
        os.rmdir(os.path.join(self.app, "mods"))
        self.assertEqual(d.install_herdr_mods(self.src)[0], 409)


class OnlyFromYourBrowser(unittest.TestCase):
    def test_the_key_can_neither_install_nor_set_the_source(self):
        with Rig(env={"PEERLINK_TEST": "1"}) as rig:
            code, out = rig.call("POST", "/api/herdr/install-mods", {"source": "/tmp"})
            self.assertEqual(code, 403)
            rig.call("POST", "/api/config", {"herdr_mods_source": "/tmp"})
            self.assertEqual(rig.user("GET", "/api/config")["herdr_mods_source"], "")


if __name__ == "__main__":
    unittest.main()
