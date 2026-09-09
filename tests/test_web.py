import contextlib
import datetime
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

import pandas as pd
from werkzeug.security import generate_password_hash

import slackbot_daily_arxiv as bot
from digest_service import DigestService, new_papers, slack_chunks
from web_app import create_app
from web_state import StateStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"slack_token": "test-token", "channel": "test-channel",
          "cols": ["id", "title", "authors", "abstract"], "categories": [], "subcat": []}


def paper(identifier, selection="keyword"):
    return {"id": identifier, "title": f"Quantum paper {identifier}", "abstract": "An abstract about atoms.",
            "authors": ["Ada Lovelace"], "selection": selection, "selected_for": ""}


class AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.app = create_app(data_dir=self.temp.name, config=CONFIG)
        self.app.config["TESTING"] = True
        self.store = self.app.extensions["store"]
        self.service = self.app.extensions["digest_service"]
        self.addCleanup(self.service.close)
        self.store.update(lambda state: state.update(password_hash=self.password_hash))
        self.client = self.app.test_client()
        self.startup_output = output.getvalue()

    def post(self, path, data=None, client=None):
        client = client or self.client
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf_token"]
        return client.post(path, data={"csrf_token": token, **(data or {})})

    def login(self):
        response = self.post("/login", {"username": "admin", "password": "test-password"})
        self.assertEqual(response.status_code, 302)

    def preview(self, papers, identifier="preview-1"):
        self.store.update(lambda state: state.update(preview={
            "id": identifier, "date": "2026-09-09", "generated_at": "2026-09-08T12:00:00+00:00", "papers": papers,
        }))

    def wait_job(self):
        self.service.worker.join(timeout=10)
        self.assertFalse(self.service.worker.is_alive())

    def test_public_dashboard_and_auth_boundaries(self):
        self.store.update(lambda state: state.update(job={
            "action": "regenerate", "status": "success",
            "message": "Preview regenerated. 21 new papers ready to send.",
        }))
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Last message", response.data)
        self.assertIn(b"Schedule paused", response.data)
        self.assertNotIn(b"Keep up with what matters.", response.data)
        self.assertNotIn(b"THE READING ROOM", response.data)
        self.assertNotIn(b"A LITTLE LESS SEARCHING", response.data)
        self.assertNotIn(b"The latest papers from the authors", response.data)
        self.assertNotIn(b"Regenerate refreshes this list", response.data)
        self.assertNotIn(b"Preview regenerated. 21 new papers ready to send.", response.data)
        self.assertIn(
            b'<p class="section-note">Suggestions join the watchlist after admin approval.</p>',
            response.data,
        )
        self.assertIsNone(self.client.get("/status").json["job"])
        self.assertNotIn(b"test-token", response.data)
        self.assertNotIn(b"/admin/send", response.data)
        for path in ("/admin/send", "/admin/regenerate", "/admin/suggestions/1"):
            self.assertEqual(self.post(path).status_code, 403)
        self.assertEqual(self.client.get("/admin/send").status_code, 405)

    def test_password_persisted_as_hash_and_only_printed_once(self):
        self.assertIn("Admin password:", self.startup_output)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            second = create_app(data_dir=self.temp.name, config=CONFIG)
        second.extensions["digest_service"].close()
        self.assertNotIn("Admin password:", output.getvalue())
        self.assertNotIn("test-password", Path(self.store.path).read_bytes().decode("utf-8", errors="ignore"))

    def test_login_logout_and_csrf(self):
        self.assertEqual(self.client.post("/login", data={"password": "test-password"}).status_code, 400)
        self.assertEqual(self.client.post("/suggest", data={"csrf_token": "é"}).status_code, 400)
        self.post("/login", {"username": "admin", "password": "wrong"})
        with self.client.session_transaction() as session:
            self.assertNotIn("admin", session)
        self.login()
        admin_page = self.client.get("/").data
        self.assertIn(b"/admin/send", admin_page)
        self.assertIn(b"Regenerate refreshes this list", admin_page)
        self.post("/logout")
        self.assertEqual(self.post("/admin/send").status_code, 403)

    def test_watchlists_are_case_insensitively_sorted(self):
        self.store.update(lambda state: state.update(
            authors=["zoe Alpha", "Ada Lovelace", "bob Builder"],
            keywords=["Zeeman", "atom", "Cavity"],
        ))
        page = self.client.get("/").data.decode()
        self.assertLess(page.index("Ada Lovelace"), page.index("bob Builder"))
        self.assertLess(page.index("bob Builder"), page.index("zoe Alpha"))
        self.assertLess(page.index("atom"), page.index("Cavity"))
        self.assertLess(page.index("Cavity"), page.index("Zeeman"))

    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.post("/login", {"username": "admin", "password": "wrong"}).status_code, 200)
        self.assertEqual(self.post("/login", {"username": "admin", "password": "wrong"}).status_code, 429)

    def test_suggestion_approval_is_persistent_and_public_cannot_approve(self):
        self.post("/suggest", {"kind": "authors", "value": "Ada Lovelace"})
        state = self.store.read()
        self.assertNotIn("Ada Lovelace", state["authors"])
        suggestion = state["suggestions"][0]
        self.assertEqual(self.post(f"/admin/suggestions/{suggestion['id']}", {"decision": "accept"}).status_code, 403)
        self.login()
        self.post(f"/admin/suggestions/{suggestion['id']}", {"decision": "accept"})
        persisted = StateStore(self.temp.name, ROOT).read()
        self.assertIn("Ada Lovelace", persisted["authors"])
        self.assertEqual(persisted["suggestions"], [])

    def test_suggestion_validation_dismissal_and_html_escaping(self):
        for kind, value in [("invalid", "term"), ("authors", "Onlyname"), ("keywords", " "), ("keywords", "a\nb")]:
            with self.assertRaises(ValueError):
                self.store.suggest(kind, value)
        self.store.suggest("keywords", "<script>alert(1)</script>")
        with self.assertRaises(ValueError):
            self.store.suggest("keywords", "<script>alert(1)</script>")
        self.login()
        page = self.client.get("/").data
        self.assertIn(b"&lt;script&gt;", page)
        self.assertNotIn(b"<script>alert(1)</script>", page)
        self.post("/admin/suggestions/1", {"decision": "reject"})
        self.assertEqual(self.store.read()["suggestions"], [])

    def test_regenerate_updates_preview_without_sending_or_changing_last_message(self):
        old = {"date": "2026-09-08", "sent_at": "2026-09-08T00:00:00+00:00", "papers": [paper("2609.00001")]}
        self.store.update(lambda state: state.update(last_message=old))
        with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": [paper("2609.00002")]}) as build, patch.object(bot, "post_to_slack") as send:
            self.service.start("regenerate", target_date="2026-09-09")
            self.wait_job()
        send.assert_not_called()
        self.assertEqual(build.call_args.kwargs["target_date"], "2026-09-09")
        self.assertEqual(self.store.read()["last_message"], old)
        self.assertEqual(self.store.read()["preview"]["papers"][0]["id"], "2609.00002")

    def test_send_uses_reviewed_preview_and_only_delta_from_full_previous_list(self):
        self.preview([paper("2609.00001"), paper("2609.00002")])
        self.store.update(lambda state: state.update(baseline_ids=["2609.00001"]))
        with patch.object(bot, "build_daily_payload") as build, patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send:
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        build.assert_not_called()
        self.assertEqual(send.call_count, 2)
        self.assertIn("2609.00002", send.call_args_list[0].args[1])
        self.assertNotIn("2609.00001", send.call_args_list[0].args[1])
        state = self.store.read()
        self.assertEqual([p["id"] for p in state["last_message"]["papers"]], ["2609.00002"])
        self.assertEqual(state["baseline_ids"], ["2609.00001", "2609.00002"])
        self.assertEqual(new_papers(state), [])
        with self.assertRaises(ValueError):
            self.service.start("send", preview_id="preview-1")
        self.preview([paper("2609.00001"), paper("2609.00002"), paper("2609.00003")])
        self.assertEqual([p["id"] for p in new_papers(self.store.read())], ["2609.00003"])

    def test_version_changes_do_not_resend_and_stale_preview_is_rejected(self):
        self.preview([paper("2609.00001v2"), paper("2609.00002")])
        self.store.update(lambda state: state.update(baseline_ids=["2609.00001"]))
        self.assertEqual([p["id"] for p in new_papers(self.store.read())], ["2609.00002"])
        with self.assertRaises(ValueError):
            self.service.start("send", preview_id="outdated")

    def test_slack_parent_failure_keeps_previous_delivery_and_allows_retry(self):
        self.preview([paper("2609.00001")])
        with patch.object(bot, "post_to_slack", side_effect=RuntimeError("offline")), self.assertLogs("digest_service", level="ERROR"):
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        state = self.store.read()
        self.assertIsNone(state["last_message"])
        self.assertEqual(state["baseline_ids"], [])
        self.assertEqual(state["job"]["status"], "error")
        self.assertEqual(len(new_papers(state)), 1)

    def test_thread_failure_records_parent_and_prevents_duplicate_retry_after_restart(self):
        self.preview([paper("2609.00001")])
        with patch.object(bot, "post_to_slack", side_effect=[{"ts": "123.1"}, RuntimeError("thread failed")]), self.assertLogs("digest_service", level="ERROR"):
            self.service.start("send", preview_id="preview-1", slot="2026-09-08 21:30")
            self.wait_job()
        state = StateStore(self.temp.name, ROOT).read()
        self.assertEqual(state["job"]["status"], "partial")
        self.assertFalse(state["last_message"]["abstracts_sent"])
        self.assertEqual(new_papers(state), [])
        self.assertIn("2026-09-08 21:30", state["schedule_slots"])

    def test_empty_scheduled_result_posts_the_quiet_day_notice_and_preserves_state(self):
        old = {"date": "2026-09-08", "sent_at": "2026-09-08T00:00:00+00:00", "papers": [paper("2609.00001")]}
        self.store.update(lambda state: state.update(last_message=old, baseline_ids=["2609.00001"]))
        with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": []}), patch.object(bot, "post_to_slack") as send:
            self.service.start("scheduled", target_date="2026-09-09", slot="2026-09-08 21:30")
            self.wait_job()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], bot.payload_from_papers("2026-09-09", [])["msg_text"])
        self.assertIsNone(send.call_args.kwargs.get("thread_ts"))
        state = self.store.read()
        self.assertEqual(state["job"]["status"], "success")
        self.assertIn("2026-09-08 21:30", state["schedule_slots"])
        # A quiet day must not blank the dashboard or shrink the comparison list.
        self.assertEqual(state["last_message"], old)
        self.assertEqual(state["baseline_ids"], ["2609.00001"])

    def test_quiet_notice_says_already_sent_when_every_match_was_delivered(self):
        papers = [paper("2609.00001"), paper("2609.00002")]
        self.store.update(lambda state: state.update(baseline_ids=["2609.00001", "2609.00002"]))
        with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": papers}),                 patch.object(bot, "post_to_slack") as send:
            self.service.start("scheduled", target_date="2026-09-09", slot="2026-09-08 21:30")
            self.wait_job()
        self.assertEqual(send.call_count, 1)
        posted = send.call_args.args[1]
        # The heartbeat still lands, but it must not claim nothing was found.
        self.assertIn("*Papers for 2026-09-09*", posted)
        self.assertIn("All 2 matching papers were already sent earlier today.", posted)
        self.assertNotIn("No papers found", posted)
        self.assertIn("2026-09-08 21:30", self.store.read()["schedule_slots"])

    def test_config_is_reread_for_every_regenerate_and_send(self):
        loads = []
        rotated = {**CONFIG, "channel": "#moved-channel", "slack_token": "rotated-token",
                   "categories": ["physics"], "subcat": [["quant-ph"]]}

        def loader():
            loads.append(True)
            return rotated

        self.service.config_loader = loader
        self.preview([paper("2609.00001")])
        with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send,                 patch.object(bot, "make_slack_client") as client:
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        self.assertEqual(len(loads), 1)
        self.assertEqual(send.call_args_list[0].kwargs["channel"], "#moved-channel")
        client.assert_called_with("rotated-token")

        # Regenerate re-reads too, so edited categories/subcat apply without a restart.
        with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": []}) as build:
            self.service.start("regenerate", target_date="2026-09-09")
            self.wait_job()
        self.assertEqual(len(loads), 2)
        self.assertEqual(build.call_args.kwargs["config"], rotated)

    def test_a_broken_config_edit_falls_back_to_the_last_valid_one(self):
        def loader():
            raise KeyError("Missing required config keys: ['channel']")

        self.service.config_loader = loader
        self.preview([paper("2609.00001")])
        with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send,                 self.assertLogs("digest_service", level="ERROR"):
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        state = self.store.read()
        self.assertEqual(state["job"]["status"], "success")
        self.assertEqual(send.call_args_list[0].kwargs["channel"], CONFIG["channel"])

    def test_an_injected_config_is_never_reloaded_from_disk(self):
        self.assertIsNone(self.service.config_loader)

    def test_manual_send_with_no_new_papers_is_rejected_before_posting(self):
        self.preview([paper("2609.00001")])
        self.store.update(lambda state: state.update(baseline_ids=["2609.00001"]))
        with patch.object(bot, "post_to_slack") as send:
            with self.assertRaises(ValueError) as caught:
                self.service.start("send", preview_id="preview-1")
        self.assertEqual(str(caught.exception), "There are no new papers to send.")
        send.assert_not_called()

    def test_oversized_digest_is_chunked_and_the_thread_hangs_off_the_first_chunk(self):
        papers = []
        for number in range(40):
            long_paper = paper(f"2609.{number:05d}")
            long_paper["title"] = " ".join(f"word{index}" for index in range(200))
            papers.append(long_paper)
        self.preview(papers)
        expected = bot.payload_from_papers("2026-09-09", papers)
        self.assertGreater(len(expected["msg_text"]), 39000)
        parent_chunks = list(slack_chunks(expected["msg_text"]))
        self.assertGreater(len(parent_chunks), 1)

        posted = []

        def record(client, text, thread_ts=None, *, channel=None):
            posted.append((text, thread_ts))
            return {"ts": f"123.{len(posted)}"}

        with patch.object(bot, "post_to_slack", side_effect=record):
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        top_level = [text for text, thread_ts in posted if thread_ts is None]
        threaded = [thread_ts for _text, thread_ts in posted if thread_ts is not None]
        self.assertEqual(top_level, parent_chunks)
        self.assertTrue(threaded)
        self.assertEqual(set(threaded), {"123.1"})
        state = self.store.read()
        self.assertEqual(state["job"]["status"], "success")
        self.assertTrue(state["last_message"]["abstracts_sent"])

    def test_continuation_failure_is_partial_and_never_reposts_the_parent(self):
        papers = []
        for number in range(40):
            long_paper = paper(f"2609.{number:05d}")
            long_paper["title"] = " ".join(f"word{index}" for index in range(200))
            papers.append(long_paper)
        self.preview(papers)
        with patch.object(bot, "post_to_slack", side_effect=[{"ts": "123.1"}, RuntimeError("rate limited")]) as send,                 self.assertLogs("digest_service", level="ERROR"):
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        self.assertEqual(send.call_count, 2)
        state = self.store.read()
        self.assertEqual(state["job"]["status"], "partial")
        self.assertIn("title list", state["job"]["message"])
        self.assertEqual(len(state["last_message"]["papers"]), len(papers))
        self.assertEqual(new_papers(state), [])
        # The retry cannot reach _send at all, so the parent is never posted twice.
        with patch.object(bot, "post_to_slack") as resend:
            with self.assertRaises(ValueError):
                self.service.start("send", preview_id="preview-1")
        resend.assert_not_called()

    def test_scheduler_health_reports_a_stalled_loop_as_unhealthy(self):
        self.assertFalse(self.service.scheduler_healthy())
        self.service.scheduler = Mock(is_alive=Mock(return_value=True))
        self.service.last_tick = time.monotonic()
        self.assertTrue(self.service.scheduler_healthy())
        self.service.last_tick = time.monotonic() - 121
        self.assertFalse(self.service.scheduler_healthy())
        self.service.scheduler = None

    def test_concurrent_jobs_are_rejected_and_fetch_errors_preserve_preview(self):
        self.preview([paper("2609.00001")])
        entered, release = threading.Event(), threading.Event()
        def fetch(**kwargs):
            entered.set()
            release.wait(5)
            raise RuntimeError("arXiv down")
        with patch.object(bot, "build_daily_payload", side_effect=fetch), self.assertLogs("digest_service", level="ERROR"):
            self.service.start("regenerate")
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ValueError):
                self.service.start("send", preview_id="preview-1")
            release.set()
            self.wait_job()
        self.assertEqual(self.store.read()["preview"]["id"], "preview-1")

    def test_schedule_keeps_days_time_window_and_restart_guard(self):
        zone = self.service.timezone
        for date, clock, due in [("2026-09-06", "21:30", True), ("2026-09-07", "21:35", True),
                                 ("2026-09-10", "21:39", True), ("2026-09-11", "21:30", False),
                                 ("2026-09-12", "21:30", False), ("2026-09-08", "21:29", False),
                                 ("2026-09-08", "21:40", False)]:
            now = datetime.datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=zone)
            self.assertEqual(bool(self.service.due_slot(now)), due, (date, clock))
        self.store.update(lambda state: state["schedule_slots"].update({"2026-09-08 21:30": "sent"}))
        restarted = DigestService(StateStore(self.temp.name, ROOT), CONFIG)
        self.assertIsNone(restarted.due_slot(datetime.datetime(2026, 9, 8, 21, 35, tzinfo=zone)))
        self.assertEqual(datetime.datetime(2026, 9, 8, 21, 30, tzinfo=zone).utcoffset().total_seconds(), -7 * 3600)
        self.assertEqual(datetime.datetime(2026, 12, 8, 21, 30, tzinfo=zone).utcoffset().total_seconds(), -8 * 3600)

    def test_last_message_titles_expand_and_link_to_arxiv(self):
        self.store.update(lambda state: state.update(last_message={
            "date": "2026-09-09", "sent_at": "2026-09-09T04:30:00+00:00", "papers": [paper("2609.00001")],
        }))
        response = self.client.get("/")
        self.assertIn(b"<details>", response.data)
        self.assertIn(b"An abstract about atoms.", response.data)
        self.assertIn(b'https://arxiv.org/abs/2609.00001', response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/healthz").json, {"status": "ok"})

    def test_long_author_lists_are_abbreviated_in_the_dashboard(self):
        long_paper = paper("2609.00001")
        long_paper["authors"] = [f"Author {number}" for number in range(16)]
        self.store.update(lambda state: state.update(last_message={
            "date": "2026-09-09", "sent_at": "2026-09-09T04:30:00+00:00",
            "papers": [long_paper],
        }))
        page = self.client.get("/").data
        self.assertIn(b"MANY AUTHORS", page)
        self.assertNotIn(b"Author 15", page)


class CoreTests(unittest.TestCase):
    def test_blank_lines_and_multiword_names(self):
        watchlists = bot.load_watchlists(["", "  ", "Ada Lovelace (Lab)", "Tai Hyun Yoon"], ["", "  ", "Atom"])
        self.assertEqual(watchlists[4], ["lovelace", "yoon"])
        self.assertEqual(watchlists[5], ["atom"])
        self.assertEqual(bot.match_author("a.", "lovelace", watchlists[3], watchlists[4]), 0)

    def test_author_priority_and_keyword_matching(self):
        frame = pd.DataFrame([
            {"id": "1", "title": "Atoms", "authors": ["Ada Lovelace"], "abstract": "Something"},
            {"id": "2", "title": "Other", "authors": ["Someone Else"], "abstract": "Atoms"},
            {"id": "3", "title": "Other", "authors": [""], "abstract": "Other"},
        ])
        indices = bot.classify_papers(frame, ["Ada Lovelace"], ["ada"], ["lovelace"], ["atom"])
        self.assertEqual(indices[:3], ([0], [1], [2]))

    def test_empty_message_and_long_thread_chunks(self):
        payload = bot.payload_from_papers("2026-09-09", [])
        self.assertIn("No papers found", payload["msg_text"])
        self.assertIsNone(payload["thread_text"])
        chunks = list(slack_chunks("x" * 80000))
        self.assertEqual("".join(chunks), "x" * 80000)
        self.assertTrue(all(len(chunk) <= 35000 for chunk in chunks))

    def test_slack_author_limit_is_more_than_fifteen(self):
        fifteen = [f"Author {number}" for number in range(15)]
        sixteen = fifteen + ["Author 15"]
        self.assertNotEqual(bot.format_authors(fifteen), "MANY AUTHORS")
        self.assertEqual(bot.format_authors(sixteen), "MANY AUTHORS")

    def test_slack_client_retries_rate_limits(self):
        client = bot.make_slack_client("test-token")
        self.assertTrue(any(isinstance(handler, RateLimitErrorRetryHandler)
                            for handler in client.retry_handlers))

    def test_arxiv_invalid_response_is_not_treated_as_an_empty_success(self):
        config = {**CONFIG, "categories": ["physics"], "subcat": [[]]}
        with patch.object(bot.arxivscraper, "Scraper") as scraper:
            scraper.return_value.scrape.return_value = None
            with self.assertRaises(RuntimeError):
                bot.fetch_papers_for_date("2026-09-09", config=config)


if __name__ == "__main__":
    unittest.main()
