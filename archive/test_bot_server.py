"""Check for the archived host runner: python -m unittest discover -s archive

Slack, arXiv and the clock are mocked, so nothing is posted. The live suite in
tests/ never imports this folder.
"""

import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Resolve the archived copies, never the live modules with the same names.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_server  # noqa: E402
import slackbot_daily_arxiv as bot  # noqa: E402

CONFIG = {"slack_token": "test-only", "channel": "test-only", "cols": [], "categories": [], "subcat": []}


class StopLoop(BaseException):
    pass


class ArchivedRunnerTests(unittest.TestCase):
    def test_the_runner_uses_the_archived_copies(self):
        here = Path(__file__).resolve().parent
        self.assertEqual(Path(bot.__file__).resolve().parent, here)
        self.assertEqual(Path(bot.arxivscraper.__file__).resolve().parent, here)
        self.assertEqual(bot.CONFIG_PATH, here.parent / "config.yml")

    def test_the_ported_fixes_are_present(self):
        papers = [{"id": "2609.00001", "title": "Quantum DNA <!channel>", "abstract": "a & b at 3.5 K.",
                   "authors": ["Yann LeCun"], "selection": "author", "selected_for": "Yann LeCun"}]
        payload = bot.payload_from_papers("2026-09-09", papers)
        self.assertIn("*Title:* Quantum DNA &lt;!channel&gt;", payload["msg_text"])
        self.assertIn("*Authors:* Yann LeCun", payload["msg_text"])
        self.assertIn("*Abstract:* a &amp; b at 3.5 K.", payload["thread_text"])
        with self.assertLogs(level="WARNING"):
            watchlists = bot.load_watchlists(["(Google)", "Ada Lovelace"], [])
        self.assertEqual(watchlists[0], ["Ada Lovelace"])

    def test_a_failed_abstract_thread_does_not_repost_the_digest(self):
        class Tuesday2130(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 6, 21, 30)

        clock = types.SimpleNamespace(datetime=Tuesday2130, date=datetime.date,
                                      time=datetime.time, timedelta=datetime.timedelta)
        posts, sleeps = [], []

        def post(client, text, thread_ts=None, *, channel=None):
            posts.append(thread_ts)
            if thread_ts:
                raise RuntimeError("thread failed")
            return {"ts": "1.1"}

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise StopLoop

        with patch.object(bot_server, "datetime", clock), \
                patch.object(bot_server, "time", types.SimpleNamespace(sleep=sleep)), \
                patch.object(bot, "load_config", return_value=CONFIG), \
                patch.object(bot, "make_slack_client"), \
                patch.object(bot, "build_daily_payload", return_value={"msg_text": "digest", "thread_text": "abstracts"}), \
                patch.object(bot, "post_to_slack", side_effect=post), \
                self.assertLogs(level="ERROR"):
            with self.assertRaises(StopLoop):
                bot_server.run_scheduler()
        # One digest and one failed thread attempt; the slot then counts as sent.
        self.assertEqual(posts, [None, "1.1"])
        self.assertTrue(all(seconds > bot_server.RETRY_DELAY_SECONDS for seconds in sleeps))


if __name__ == "__main__":
    unittest.main()
