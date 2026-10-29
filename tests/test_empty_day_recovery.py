import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import slackbot_daily_arxiv as bot
from digest_service import DigestService
from web_state import StateStore


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "slack_token": "test-token", "channel": "test-channel",
    "cols": ["id", "title", "authors", "abstract", "created", "updated"],
    "categories": ["physics", "quant-ph"], "subcat": [["physics.atom-ph"], []],
}


def record(identifier, *, matches=True):
    return {
        "id": identifier, "title": "Atomic arrays" if matches else "Unrelated topic",
        "authors": ["Someone Else"], "abstract": "An abstract.",
        # A paper's original submission date must not exclude a late OAI update.
        "created": "2026-10-27", "updated": "",
    }


class EmptyDayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(self.temp.name, ROOT)
        self.store.update(lambda state: state.update(authors=[], keywords=["atom"]))
        self.restart()
        self.records_by_date = {}
        self.queries = []

        def scraper(**kwargs):
            self.queries.append(kwargs)
            rows = [row for date, records in self.records_by_date.items()
                    if kwargs["date_from"] <= date <= kwargs["date_until"] for row in records]
            # Both categories return the same cross-listed papers, exercising
            # the real fetch, deduplication, classification and delivery code.
            return Mock(scrape=Mock(return_value=rows))

        self.source = self.enterContext(patch.object(bot.arxivscraper, "Scraper", side_effect=scraper))
        self.enterContext(patch.object(bot, "sleep"))
        self.send = self.enterContext(patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}))

    def restart(self):
        if hasattr(self, "service"):
            self.service.close()
        self.service = DigestService(StateStore(self.temp.name, ROOT), CONFIG)
        self.addCleanup(self.service.close)

    def run_day(self, date, *, action="scheduled"):
        slot = None
        if action == "scheduled":
            local_date = datetime.date.fromisoformat(date) - datetime.timedelta(days=1)
            slot = f"{local_date.isoformat()} 21:30"
        self.service.start(action, target_date=date, slot=slot)
        self.service.worker.join(timeout=10)
        self.assertFalse(self.service.worker.is_alive())
        return self.store.read()

    def assert_range(self, start, end):
        self.assertEqual(len(self.queries[-2:]), 2)
        for query in self.queries[-2:]:
            self.assertEqual((query["date_from"], query["date_until"]), (start, end))

    def test_empty_day_survives_restart_and_recovers_both_days_without_duplicates(self):
        self.store.update(lambda state: state.update(baseline_ids=["already-sent"]))
        # A day with source records but no watchlist matches is also a quiet day.
        self.records_by_date["2026-10-29"] = [record("unrelated", matches=False)]
        state = self.run_day("2026-10-29")
        self.assert_range("2026-10-29", "2026-10-29")
        self.assertEqual(state["scheduled_empty_since"], "2026-10-29")
        self.assertEqual(state["preview"]["papers"], [])
        self.restart()

        self.records_by_date["2026-10-29"] = [record("late"), record("already-sent")]
        self.records_by_date["2026-10-30"] = [record("current")]
        self.send.reset_mock()
        state = self.run_day("2026-10-30")
        self.assert_range("2026-10-29", "2026-10-30")
        self.assertEqual(state["job"]["status"], "success")
        self.assertEqual([p["id"] for p in state["last_message"]["papers"]], ["late", "current"])
        self.assertEqual(state["baseline_ids"], ["late", "already-sent", "current"])
        self.assertIsNone(state["scheduled_empty_since"])
        self.assertEqual(state["last_message"]["date_from"], "2026-10-29")
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("Papers for 2026-10-29 through 2026-10-30", self.send.call_args_list[0].args[1])
        self.assertNotIn("already-sent", self.send.call_args_list[0].args[1])
        self.assertIn("Abstracts for 2026-10-29 through 2026-10-30", self.send.call_args_list[1].args[1])

        self.records_by_date["2026-11-02"] = [record("next")]
        state = self.run_day("2026-11-02")
        self.assert_range("2026-11-02", "2026-11-02")
        self.assertEqual([p["id"] for p in state["last_message"]["papers"]], ["next"])

    def test_consecutive_empty_runs_retain_earliest_date_across_weekend(self):
        for date in ("2026-10-29", "2026-10-30", "2026-11-02"):
            state = self.run_day(date)
            self.assert_range("2026-10-29", date)
            self.assertEqual(state["scheduled_empty_since"], "2026-10-29")
            self.assertEqual(state["job"]["status"], "success")
            self.restart()
        self.records_by_date["2026-10-29"] = [record("late")]
        state = self.run_day("2026-11-03")
        self.assert_range("2026-10-29", "2026-11-03")
        self.assertIsNone(state["scheduled_empty_since"])
        self.assertEqual(state["last_message"]["papers"][0]["id"], "late")

    def test_manual_preview_neither_starts_nor_clears_scheduled_recovery(self):
        state = self.run_day("2026-10-28", action="regenerate")
        self.assertIsNone(state["scheduled_empty_since"])
        self.send.assert_not_called()
        self.run_day("2026-10-29")
        self.records_by_date["2026-10-30"] = [record("manual")]
        self.send.reset_mock()
        state = self.run_day("2026-10-30", action="regenerate")
        self.assert_range("2026-10-30", "2026-10-30")
        self.assertEqual(state["scheduled_empty_since"], "2026-10-29")
        self.assertEqual(state["preview"]["papers"][0]["id"], "manual")
        self.send.assert_not_called()

    def test_fetch_failure_preserves_pending_range_and_last_preview(self):
        previous = self.run_day("2026-10-29")["preview"]
        self.send.reset_mock()
        with patch.object(bot.arxivscraper, "Scraper", side_effect=TimeoutError("offline")), \
                self.assertLogs(level="ERROR"):
            state = self.run_day("2026-10-30")
        self.assertEqual(state["job"]["status"], "error")
        self.assertEqual(state["scheduled_empty_since"], "2026-10-29")
        self.assertEqual(state["preview"], previous)
        self.assertNotIn("2026-10-29 21:30", state["schedule_slots"])
        self.send.assert_not_called()

    def test_slack_failure_and_restart_do_not_lose_recovered_papers(self):
        self.run_day("2026-10-29")
        self.records_by_date["2026-10-29"] = [record("late")]
        with patch.object(bot, "post_to_slack", side_effect=RuntimeError("offline")), \
                self.assertLogs("digest_service", level="ERROR"):
            state = self.run_day("2026-10-30")
        self.assertEqual(state["job"]["status"], "error")
        self.assertEqual(state["scheduled_empty_since"], "2026-10-29")
        self.assertEqual(state["baseline_ids"], [])
        self.assertNotIn("2026-10-29 21:30", state["schedule_slots"])
        self.restart()
        state = self.run_day("2026-10-30")
        self.assert_range("2026-10-29", "2026-10-30")
        self.assertIsNone(state["scheduled_empty_since"])
        self.assertEqual(state["last_message"]["papers"][0]["id"], "late")

    def test_already_sent_matches_are_not_an_empty_day(self):
        self.store.update(lambda state: state.update(baseline_ids=["already-sent"]))
        self.records_by_date["2026-10-29"] = [record("already-sent")]
        state = self.run_day("2026-10-29")
        self.assertIsNone(state["scheduled_empty_since"])
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("was already sent", self.send.call_args.args[1])

        self.run_day("2026-10-30")
        self.records_by_date["2026-10-30"] = [record("already-sent")]
        self.send.reset_mock()
        state = self.run_day("2026-11-02")
        self.assert_range("2026-10-30", "2026-11-02")
        self.assertIsNone(state["scheduled_empty_since"])
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("2026-10-30 through 2026-11-02", self.send.call_args.args[1])

    def test_upgrade_carries_forward_only_a_successful_empty_scheduled_preview(self):
        for action, status, papers, expected in (
            ("scheduled", "success", [], "2026-10-29"),
            ("regenerate", "success", [], None),
            ("scheduled", "error", [], None),
            ("scheduled", "success", [record("match")], None),
        ):
            with self.subTest(action=action, status=status, papers=bool(papers)):
                def old_state(state):
                    state.pop("scheduled_empty_since")
                    state["preview"] = {"date": "2026-10-29", "papers": papers}
                    state["job"] = {"action": action, "status": status}

                self.store.update(old_state)
                self.restart()
                self.assertEqual(self.store.read()["scheduled_empty_since"], expected)

    def test_invalid_range_is_rejected_before_fetching(self):
        with self.assertRaises(ValueError):
            bot.fetch_papers_for_date("2026-10-29", CONFIG, date_from="2026-10-30")
        self.source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
