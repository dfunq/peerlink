"""Tests for the edges a long-running daemon meets: a clock that goes back, a herdr release that changes a line,
a damaged record, a closed task marked twice. Run: python3 -m unittest discover -s plugin/daemon"""
import datetime
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-hardening-test-"))

import agentslink  # noqa: E402
import cronlib  # noqa: E402
import herdr_mod  # noqa: E402
import peerlink_mcp  # noqa: E402
import peerlinkd  # noqa: E402
import slacklink  # noqa: E402
import teamslink  # noqa: E402

d = peerlinkd


class CronTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(time, "tzset"), "needs tzset")
    def test_the_hour_that_comes_twice_never_gives_a_time_already_past(self):
        was = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

        def back():
            os.environ.pop("TZ", None) if was is None else os.environ.update(TZ=was)
            time.tzset()
        self.addCleanup(back)
        # 2026-11-01 06:10 UTC is 01:10 for the second time that night.
        after = datetime.datetime(2026, 11, 1, 6, 10, tzinfo=datetime.timezone.utc).timestamp()
        nxt = cronlib.cron_next("*/30 * * * *", after)
        self.assertGreater(nxt, after)
        self.assertEqual(nxt - after, 20 * 60)

    def test_a_stepped_star_day_field_is_still_any_day(self):
        # "*/2" in day-of-month with a weekday: both must match, as cron reads a field that starts with a star.
        monday = datetime.datetime(2026, 11, 2, 12, 0).timestamp()  # a Monday, an even day
        nxt = datetime.datetime.fromtimestamp(cronlib.cron_next("0 9 */2 * 1", monday))
        self.assertEqual((nxt.isoweekday(), nxt.day % 2), (1, 1))  # */2 from 1: odd days only, and a Monday


class HerdrPatchTest(unittest.TestCase):
    def test_the_patch_fits_whatever_herdr_imports(self):
        body = "\n".join(old for old, _ in herdr_mod.PATCHES[1:])
        for names in ("spaces, run", "spaces, run, readTextFile", "spaces,\n  run"):
            out, missing = herdr_mod.patch('import { ' + names + ' } from "herdr";\n' + body, "/x/tree.json")
            self.assertIsNone(missing)
            self.assertEqual(out.count("readTextFile } from \"herdr\";"), 1)
            self.assertIn('readTextFile("/x/tree.json")', out)

    def test_no_import_line_is_reported(self):
        out, missing = herdr_mod.patch("const x = 1;", "/x/tree.json")
        self.assertIsNone(out)
        self.assertIn("import", missing)


class BridgeTest(unittest.TestCase):
    def test_slack_text_over_one_block_is_split_not_cut(self):
        text = "word " * 1400
        blocks = slacklink.sections(text.strip())
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(b["text"]["text"]) <= 3000 for b in blocks))
        self.assertEqual(" ".join(b["text"]["text"] for b in blocks).split(), text.split())

    def test_the_teams_token_goes_only_to_graph(self):
        auth = unittest.mock.Mock()
        with self.assertRaises(teamslink.TeamsError):
            teamslink.graph(auth, "GET", "https://example.com/hostedContents/1/$value", raw=True)
        auth.token.assert_not_called()

    def test_a_refresh_that_fails_for_now_is_not_a_sign_out(self):
        auth = teamslink.Auth("c", "t", "r")
        with unittest.mock.patch.object(teamslink, "_form", return_value={"error": "temporarily_unavailable"}):
            with self.assertRaises(teamslink.TeamsError) as busy:
                auth.token()
        self.assertNotEqual(busy.exception.code, 401)
        with unittest.mock.patch.object(teamslink, "_form", return_value={"error": "invalid_grant"}):
            with self.assertRaises(teamslink.TeamsError) as out:
                auth.token()
        self.assertEqual(out.exception.code, 401)

    def test_one_client_over_its_limit_leaves_everyones_allowance_alone(self):
        limiter = agentslink.Limiter()
        mine, everyone = [(1, 60)], [(2, 60)]

        def allow(client):
            return limiter.allow(f"{client}:post", mine) and limiter.allow("all:post", everyone)
        self.assertTrue(allow("a"))
        self.assertFalse(allow("a"))
        self.assertFalse(allow("a"))
        self.assertTrue(allow("b"))  # a's refused calls didn't count against everyone

    def test_the_mcp_server_answers_a_bad_call_instead_of_dying(self):
        daemon = unittest.mock.Mock(wrong="")
        daemon.list_peers.side_effect = KeyError("boom")
        res = peerlink_mcp.handle(daemon, {"id": 1, "method": "tools/call", "params": {"name": "list_peers"}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("error", peerlink_mcp.handle(daemon, {"id": 2, "method": "tools/call", "params": {"name": "list_peers", "arguments": [1]}}))
        self.assertIn("error", peerlink_mcp.handle(daemon, {"id": 3, "method": "tools/call", "params": [1]}))


class DaemonTest(unittest.TestCase):
    def setUp(self):
        d.load_config()
        d.load_discord()
        d.config.update({"rate_limit_per_min": 100, "wake_for": "needed"})
        now = time.time()
        for name in ("lead", "core"):
            d.sessions["s-" + name] = {"id": "s-" + name, "name": name, "cwd": "/x/" + name, "last_seen": now}
        keep = (dict(d.tasks), list(d.messages), dict(d.sent_times))
        d.messages.clear()
        d.inboxes.clear()

        def back():
            for name in ("lead", "core"):
                d.sessions.pop("s-" + name, None)
            d.inboxes.clear()
            d.tasks.clear()
            d.tasks.update(keep[0])
            d.messages[:] = keep[1]
            d.sent_times.clear()
            d.sent_times.update(keep[2])
        self.addCleanup(back)

    def test_a_closed_task_stays_closed_for_agents(self):
        status, out = d.send({"from": "s-lead", "to": "core", "kind": "task", "text": "Ship it"})
        self.assertEqual(status, 200, out)
        tid = out["task"]
        self.assertEqual(d.update_task(tid, {"by": "s-core", "status": "done", "note": "shipped"})[0], 200)
        status, out = d.update_task(tid, {"by": "s-core", "status": "failed", "note": "no wait"})
        self.assertEqual(status, 409, out)
        self.assertEqual(d.tasks[tid]["status"], "done")

    def test_the_rate_limit_counts_every_send(self):
        d.config["rate_limit_per_min"] = 3
        codes = [d.send({"from": "s-lead", "to": "core", "text": f"note {i}"})[0] for i in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(set(codes[3:]), {429})

    def test_a_schedule_a_lifetime_away_is_refused(self):
        with self.assertRaises(ValueError):
            d.schedule_when({"at": "2999-01-01 09:00"}, time.time())

    def test_an_issue_with_no_readable_time_still_sorts(self):
        self.assertIsInstance(d.issue_ts("yesterday"), float)
        self.assertEqual(d.issue_ts("12.5"), 12.5)

    def test_open_questions_outlive_the_newest_500(self):
        now = time.time()
        ask = {"id": "m1", "from": "s-core", "to": "user", "expects_reply": True, "text": "Merge?", "ts": now - 3600}
        old = {"id": "m2", "from": "s-core", "to": "s-lead", "text": "fyi", "ts": now - 3500}
        rest = [{"id": f"m{i}", "from": "s-core", "to": "s-lead", "text": "x", "ts": now - 600} for i in range(3, 503)]
        kept = [m["id"] for m in d.recent_messages([ask, old] + rest, now)]
        self.assertEqual((kept[0], len(kept)), ("m1", 501))
        self.assertNotIn("m2", kept)

    def test_tidy_drops_the_settings_of_sessions_that_are_gone(self):
        kept = dict(d.config["sessions"])
        self.addCleanup(lambda: (d.config["sessions"].clear(), d.config["sessions"].update(kept)))
        d.config["sessions"].update({"s-lead": {"nickname": "lead"}, "s-long-gone": {"nickname": "old"}})
        d.tidy()
        self.assertIn("s-lead", d.config["sessions"])
        self.assertNotIn("s-long-gone", d.config["sessions"])

    def test_ids_are_not_handed_out_again_after_start_clean(self):
        if not os.path.realpath(d.HOME).startswith(os.path.realpath(tempfile.gettempdir())):
            self.skipTest("PEERLINK_HOME is exported and isn't a temp folder: Start clean would wipe it")
        was = d.next_msg[0], d.next_task[0]
        self.addCleanup(lambda: (d.next_msg.__setitem__(0, was[0]), d.next_task.__setitem__(0, was[1])))
        d.next_msg[0], d.next_task[0] = 41, 7
        d.start_clean(["history", "tasks"])
        d.next_msg[0], d.next_task[0] = 1, 1  # as a restart finds them: both files are empty now
        d.load_messages()
        d.load_tasks()
        self.assertEqual((d.next_msg[0], d.next_task[0]), (41, 7))

    def test_a_request_body_is_read_only_when_its_length_makes_sense(self):
        def body(length, data=b""):
            h = d.Handler.__new__(d.Handler)
            h.headers, h.rfile = {"Content-Length": length}, io.BytesIO(data)
            return h.body()
        self.assertEqual(body("7", b'{"a":1}'), {"a": 1})
        self.assertEqual(body("-1", b'{"a":1}'), {})
        self.assertEqual(body("lots"), {})
        with self.assertRaises(ValueError):
            body(str(d.BODY_MAX + 1))

    @unittest.skipIf(os.name == "nt", "ps")
    def test_one_ps_gives_every_parent(self):
        self.assertEqual(d.posix_parents().get(os.getpid()), os.getppid())


if __name__ == "__main__":
    unittest.main()
