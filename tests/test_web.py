import contextlib
import datetime
import io
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

import pandas as pd
from flask import request
from werkzeug.security import check_password_hash, generate_password_hash

import slackbot_daily_arxiv as bot
from digest_service import DigestService, new_papers, slack_chunks
import web_app
from web_app import DEVICE_COOKIE, SUGGEST_ADDRESS_LIMIT, RateLimiter, create_app
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
        # Whatever the shell sets, run against the container's defaults.
        self.enterContext(patch.dict(os.environ, {"TZ": "America/Los_Angeles", "COOKIE_SECURE": "0",
                                                  "TRUSTED_PROXY_HOPS": "0", "SCHEDULER_ENABLED": "1",
                                                  "PORT": "8765"}))
        # Fail loudly instead of reaching the network if a test forgets a patch.
        self.enterContext(patch("slack_sdk.web.base_client.BaseClient.api_call",
                                side_effect=AssertionError("unpatched Slack call")))
        self.enterContext(patch("arxiv_source.urlopen", side_effect=AssertionError("unpatched arXiv call")))
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

    def database_bytes(self, directory=None):
        return b"".join(path.read_bytes() for path in Path(directory or self.temp.name).iterdir() if path.is_file())

    def test_generated_password_is_printed_once_and_stored_only_as_a_hash(self):
        printed = re.search(r"Admin password: (\S+)", self.startup_output).group(1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            second = create_app(data_dir=self.temp.name, config=CONFIG)
        second.extensions["digest_service"].close()
        self.assertNotIn("Admin password:", output.getvalue())
        self.assertNotIn(printed.encode(), self.database_bytes())
        # setUp replaced that hash, so prove the generated password itself on a fresh store.
        with tempfile.TemporaryDirectory() as directory:
            fresh = StateStore(directory, ROOT)
            self.assertTrue(check_password_hash(fresh.read()["password_hash"], fresh.initial_password))
            self.assertNotIn(fresh.initial_password.encode(), self.database_bytes(directory))
        # A reset password is likewise stored only as a hash, and it rotates the session secret.
        secret = self.store.read()["session_secret"]
        reset = self.store.reset_password()
        state = self.store.read()
        self.assertTrue(check_password_hash(state["password_hash"], reset))
        self.assertNotEqual(state["session_secret"], secret)
        self.assertNotIn(reset.encode(), self.database_bytes())

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
        for kind, value in [("invalid", "term"), ("authors", "Onlyname"), ("authors", "John (MIT)"),
                            ("keywords", " "), ("keywords", "a\nb")]:
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
        self.assertIn("some abstracts in the thread", state["job"]["message"])
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
        with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": papers}), \
                patch.object(bot, "post_to_slack") as send:
            self.service.start("scheduled", target_date="2026-09-09", slot="2026-09-08 21:30")
            self.wait_job()
        self.assertEqual(send.call_count, 1)
        posted = send.call_args.args[1]
        # The heartbeat still lands, but it must not claim nothing was found.
        self.assertIn("*Papers for 2026-09-09*", posted)
        self.assertIn("All 2 matching papers were already sent.", posted)
        self.assertNotIn("No papers found", posted)
        self.assertIn("2026-09-08 21:30", self.store.read()["schedule_slots"])

    def test_favicons_are_served_at_the_root_and_are_cacheable(self):
        root = self.client.get("/favicon.ico")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.data[:4], bytes([0, 0, 1, 0]))  # ICONDIR: reserved, type=icon
        # Icons are exempt from the no-store rule the rest of the app uses.
        self.assertNotEqual(root.headers.get("Cache-Control"), "no-store")
        self.assertEqual(root.data, self.client.get("/static/favicon.ico").data)
        self.assertEqual(self.client.get("/static/favicon.svg").status_code, 200)
        for page in ("/", "/login"):
            markup = self.client.get(page).data
            self.assertIn(b'rel="icon"', markup)
            self.assertIn(b"favicon.svg", markup)

    def test_config_is_reread_for_every_regenerate_and_send(self):
        loads = []
        rotated = {**CONFIG, "channel": "#moved-channel", "slack_token": "rotated-token",
                   "categories": ["physics"], "subcat": [["quant-ph"]]}

        def loader():
            loads.append(True)
            return rotated

        self.service.config_loader = loader
        self.preview([paper("2609.00001")])
        with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send, \
                patch.object(bot, "make_slack_client") as client:
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
        with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send, \
                self.assertLogs("digest_service", level="ERROR"):
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
        posted = []

        def post(client, text, thread_ts=None, *, channel=None):
            posted.append(thread_ts)
            if len(posted) == 1:
                return {"ts": "123.1"}
            if thread_ts is None:
                raise RuntimeError("rate limited")
            return {"ts": "123.9"}

        with patch.object(bot, "post_to_slack", side_effect=post), \
                self.assertLogs("digest_service", level="ERROR"):
            self.service.start("send", preview_id="preview-1")
            self.wait_job()
        # One parent, one failed continuation, then the thread under the parent anyway.
        self.assertEqual(posted.count(None), 2)
        self.assertIn("123.1", posted)
        state = self.store.read()
        self.assertEqual(state["job"]["status"], "partial")
        self.assertIn("title list", state["job"]["message"])
        self.assertNotIn("some abstracts", state["job"]["message"])
        self.assertTrue(state["last_message"]["abstracts_sent"])
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

    def test_schedule_window_weekdays_dst_and_restart_guard(self):
        self.assertEqual(str(self.service.timezone), "America/Los_Angeles")
        zone = self.service.timezone
        for date, clock, due in [("2026-09-06", "21:30", True), ("2026-09-07", "21:35", True),
                                 ("2026-09-10", "21:39", True), ("2026-09-11", "21:30", False),
                                 ("2026-09-12", "21:30", False), ("2026-09-08", "21:29", False),
                                 ("2026-09-08", "21:40", True), ("2026-09-08", "22:30", True),
                                 ("2026-09-08", "23:29:59", True), ("2026-09-08", "23:30", False),
                                 ("2026-09-09", "00:00", False), ("2026-09-11", "22:30", False),
                                 ("2026-09-12", "22:30", False), ("2026-12-08", "22:30", True)]:
            now = datetime.datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=zone)
            self.assertEqual(bool(self.service.due_slot(now)), due, (date, clock))
        self.store.update(lambda state: state["schedule_slots"].update({"2026-09-08 21:30": "sent"}))
        restarted = DigestService(StateStore(self.temp.name, ROOT), CONFIG)
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted.due_slot(datetime.datetime(2026, 9, 8, 22, 30, tzinfo=zone)))
        # The slot follows Pacific wall-clock time on both sides of the DST change.
        for instant, due in (("2026-09-10T04:30:00+00:00", True), ("2026-12-10T05:30:00+00:00", True),
                             ("2026-12-10T04:30:00+00:00", False)):
            local = datetime.datetime.fromisoformat(instant).astimezone(self.service.timezone)
            self.assertEqual(bool(self.service.due_slot(local)), due, instant)

    def test_scheduled_timeout_recovers_later_in_window_without_duplicate_send(self):
        zone = self.service.timezone
        slot = self.service.due_slot(datetime.datetime(2026, 10, 6, 21, 30, tzinfo=zone))
        self.assertEqual(slot, "2026-10-06 21:30")
        with patch.object(bot, "build_daily_payload", side_effect=TimeoutError("arXiv timed out")), \
                patch.object(bot, "post_to_slack") as send, self.assertLogs("digest_service", level="ERROR"):
            self.service.start("scheduled", target_date="2026-10-07", slot=slot)
            self.wait_job()
        send.assert_not_called()
        self.assertNotIn(slot, self.store.read()["schedule_slots"])
        self.assertEqual(self.store.read()["job"]["status"], "error")

        # The unfinished slot survives a restart and remains eligible an hour later.
        restarted = DigestService(StateStore(self.temp.name, ROOT), CONFIG)
        self.addCleanup(restarted.close)
        later = datetime.datetime(2026, 10, 6, 22, 30, tzinfo=zone)
        self.assertEqual(restarted.due_slot(later), slot)
        with patch.object(bot, "build_daily_payload", return_value={
            "date_str": "2026-10-07", "papers": [paper("2610.00001")],
        }) as build, patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send:
            restarted.start("scheduled", target_date="2026-10-07", slot=slot)
            restarted.worker.join(timeout=10)
            self.assertFalse(restarted.worker.is_alive())
            self.assertEqual(restarted.store.read()["job"]["status"], "success")
            self.assertEqual(build.call_args.kwargs["target_date"], "2026-10-07")
            self.assertEqual(send.call_count, 2)  # Digest and its abstract thread.
            self.assertIsNone(restarted.due_slot(later))
            with self.assertRaisesRegex(ValueError, "already completed"):
                restarted.start("scheduled", target_date="2026-10-07", slot=slot)
            self.assertEqual(send.call_count, 2)

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

    def test_catch_up_dates_are_displayed_without_changing_the_date_input(self):
        self.preview([paper("2609.00001")])
        self.store.update(lambda state: state["preview"].update(date_from="2026-09-08"))
        self.store.update(lambda state: state.update(last_message={
            "date": "2026-09-09", "date_from": "2026-09-08",
            "sent_at": "2026-09-09T04:30:00+00:00", "papers": [paper("2609.00001")],
        }))
        self.login()
        page = self.client.get("/").data
        self.assertEqual(page.count(b"Papers for 2026-09-08 through 2026-09-09"), 2)
        self.assertIn(b'name="date" value="2026-09-09"', page)

    def test_scheduler_tick_queries_tomorrow_and_paces_retries(self):
        zone = self.service.timezone
        now = datetime.datetime(2026, 10, 6, 21, 30, tzinfo=zone)  # A Tuesday.
        with patch.object(self.service, "start") as start:
            retry_at = self.service.scheduler_tick(now, {})
            start.assert_called_once_with("scheduled", target_date="2026-10-07", slot="2026-10-06 21:30")
            later = now + datetime.timedelta(seconds=59)
            self.assertEqual(self.service.scheduler_tick(later, retry_at), retry_at)
            self.assertEqual(start.call_count, 1)
            retry_at = self.service.scheduler_tick(now + datetime.timedelta(seconds=60), retry_at)
            self.assertEqual(start.call_count, 2)
            with self.service.lock:  # A job is still running, so the tick waits instead.
                self.service.scheduler_tick(now + datetime.timedelta(seconds=180), retry_at)
            self.assertEqual(start.call_count, 2)
            self.service.scheduler_tick(datetime.datetime(2026, 10, 9, 21, 30, tzinfo=zone), {})  # Friday.
            self.assertEqual(start.call_count, 2)

    def test_scheduler_loop_survives_errors_and_terminates_the_process_if_it_dies(self):
        class LoopDied(BaseException):
            pass

        ticks, killed = [], threading.Event()

        def tick(now, retry_at):
            ticks.append(now)
            if len(ticks) == 1:
                raise RuntimeError("one bad iteration")
            raise LoopDied()

        with patch.object(self.service, "scheduler_tick", side_effect=tick), \
                patch("digest_service.os.kill", side_effect=lambda pid, sig: killed.set()) as kill, \
                patch("threading.excepthook"), \
                self.assertLogs("digest_service", level="ERROR") as logs:
            self.service.start_scheduler(poll_seconds=0.01)
            self.assertTrue(killed.wait(5))
            self.service.scheduler.join(5)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[0].tzinfo, self.service.timezone)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        self.assertTrue(any("Scheduler iteration failed" in line for line in logs.output))
        self.assertTrue(any("terminating for restart" in line for line in logs.output))

    def test_healthz_returns_503_when_the_enabled_scheduler_is_dead_or_stalled(self):
        with patch.object(DigestService, "start_scheduler"), contextlib.redirect_stdout(io.StringIO()):
            app = create_app(data_dir=self.temp.name, config=CONFIG, start_scheduler=True)
        service = app.extensions["digest_service"]
        self.addCleanup(service.close)
        client = app.test_client()
        self.assertIn(b"Daily delivery", client.get("/").data)
        response = client.get("/healthz")
        self.assertEqual((response.status_code, response.json), (503, {"status": "scheduler stopped"}))
        service.scheduler = Mock(is_alive=Mock(return_value=True))
        service.last_tick = time.monotonic()
        self.assertEqual(client.get("/healthz").status_code, 200)
        service.last_tick = time.monotonic() - 121
        self.assertEqual(client.get("/healthz").status_code, 503)
        service.scheduler = None

    def test_failed_logins_on_a_shared_address_cannot_lock_out_a_known_admin_browser(self):
        self.login()
        self.assertIsNotNone(self.client.get_cookie(DEVICE_COOKIE, path="/login"))
        self.post("/logout")
        # The test client always connects from 127.0.0.1, like every LAN client
        # behind Docker Desktop's port forwarding.
        attacker = self.app.test_client()
        wrong = {"username": "admin", "password": "wrong"}
        for _ in range(10):
            self.assertEqual(self.post("/login", wrong, client=attacker).status_code, 200)
        self.assertEqual(self.post("/login", wrong, client=attacker).status_code, 429)
        self.login()  # The admin's browser still has its own allowance.
        right = {"username": "admin", "password": "test-password"}
        self.assertEqual(self.post("/login", right, client=self.app.test_client()).status_code, 429)
        forged = self.app.test_client()
        forged.set_cookie(DEVICE_COOKIE, "forged", path="/login")
        self.assertEqual(self.post("/login", right, client=forged).status_code, 429)

    def test_successful_logins_do_not_use_up_the_login_allowance(self):
        right = {"username": "admin", "password": "test-password"}
        for _ in range(12):
            self.assertEqual(self.post("/login", right, client=self.app.test_client()).status_code, 302)

    def test_suggestion_allowance_is_per_browser_on_a_shared_address(self):
        for number in range(20):
            self.assertEqual(self.post("/suggest", {"kind": "keywords", "value": f"term {number}"}).status_code, 302)
        self.assertEqual(self.post("/suggest", {"kind": "keywords", "value": "term 20"}).status_code, 429)
        neighbour = self.app.test_client()
        self.assertEqual(self.post("/suggest", {"kind": "keywords", "value": "neighbour term"}, client=neighbour).status_code, 302)

    def test_forwarded_addresses_are_trusted_only_when_configured(self):
        for hops, expected in (("0", "127.0.0.1"), ("1", "203.0.113.7")):
            with patch.dict(os.environ, {"TRUSTED_PROXY_HOPS": hops}), contextlib.redirect_stdout(io.StringIO()):
                app = create_app(data_dir=self.temp.name, config=CONFIG)
            self.addCleanup(app.extensions["digest_service"].close)
            app.add_url_rule("/whoami", "whoami", lambda: request.remote_addr)
            response = app.test_client().get("/whoami", headers={"X-Forwarded-For": "203.0.113.7"})
            self.assertEqual(response.text, expected, hops)

    def test_a_slack_failure_retry_reuses_the_fetched_papers_across_a_restart(self):
        slot = "2026-10-06 21:30"
        payload = {"date_str": "2026-10-07", "papers": [paper("2610.00001")]}
        with patch.object(bot, "build_daily_payload", return_value=payload) as build:
            with patch.object(bot, "post_to_slack", side_effect=RuntimeError("invalid_auth")), \
                    self.assertLogs("digest_service", level="ERROR"):
                self.service.start("scheduled", target_date="2026-10-07", slot=slot)
                self.wait_job()
            preview_id = self.store.read()["preview"]["id"]
            restarted = DigestService(StateStore(self.temp.name, ROOT), CONFIG)
            self.addCleanup(restarted.close)
            with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}) as send:
                restarted.start("scheduled", target_date="2026-10-07", slot=slot)
                restarted.worker.join(timeout=10)
        self.assertEqual(build.call_count, 1)
        state = self.store.read()
        self.assertEqual(state["preview"]["id"], preview_id)
        self.assertEqual(state["job"]["status"], "success")
        self.assertEqual(send.call_count, 2)
        self.assertIn(slot, state["schedule_slots"])

    def test_a_retry_fetches_again_when_the_watchlists_changed(self):
        slot = "2026-10-06 21:30"
        payload = {"date_str": "2026-10-07", "papers": [paper("2610.00001")]}
        with patch.object(bot, "build_daily_payload", return_value=payload) as build:
            with patch.object(bot, "post_to_slack", side_effect=RuntimeError("offline")), \
                    self.assertLogs("digest_service", level="ERROR"):
                self.service.start("scheduled", target_date="2026-10-07", slot=slot)
                self.wait_job()
            self.store.update(lambda state: state["authors"].append("Grace Hopper"))
            with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}):
                self.service.start("scheduled", target_date="2026-10-07", slot=slot)
                self.wait_job()
        self.assertEqual(build.call_count, 2)
        self.assertIn("Grace Hopper", build.call_args.kwargs["watchlists"][0])
        self.assertEqual(self.store.read()["job"]["status"], "success")

    def test_a_normal_close_stops_the_scheduler_without_killing_the_process(self):
        with patch.object(self.service, "scheduler_tick", side_effect=lambda now, retry_at: retry_at), \
                patch("digest_service.os.kill") as kill:
            self.service.start_scheduler(poll_seconds=0.01)
            self.service.close()
        self.assertFalse(self.service.scheduler.is_alive())
        kill.assert_not_called()

    def test_main_closes_the_service_when_sigterm_arrives(self):
        handlers, service = {}, Mock()

        def serve(*args, **kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # What the OS delivers on docker stop.

        with patch.object(web_app, "create_app", return_value=Mock(extensions={"digest_service": service})) as create, \
                patch.object(web_app, "serve", side_effect=serve) as served, \
                patch.object(web_app.signal, "signal", side_effect=lambda sig, handler: handlers.__setitem__(sig, handler)), \
                patch.object(web_app.logging, "basicConfig"), \
                patch.object(sys, "argv", ["web_app.py"]):
            with self.assertRaises(SystemExit) as stopped:
                web_app.main()
        self.assertEqual(stopped.exception.code, 0)
        service.close.assert_called_once_with()
        create.assert_called_once_with(start_scheduler=True)
        self.assertEqual(served.call_args.kwargs["port"], 8765)

    def test_a_known_admin_browser_is_itself_limited_after_failed_logins(self):
        self.login()
        self.post("/logout")
        wrong = {"username": "admin", "password": "wrong"}
        for _ in range(10):
            self.assertEqual(self.post("/login", wrong).status_code, 200)
        self.assertEqual(self.post("/login", wrong).status_code, 429)
        # Its bucket is its own: another browser on the address is unaffected.
        self.assertEqual(self.post("/login", wrong, client=self.app.test_client()).status_code, 200)

    def test_known_browsers_keep_their_allowance_after_a_password_reset(self):
        self.login()
        self.post("/logout")
        password = self.store.reset_password()
        with contextlib.redirect_stdout(io.StringIO()):
            restarted = create_app(data_dir=self.temp.name, config=CONFIG)  # The documented restart.
        self.addCleanup(restarted.extensions["digest_service"].close)
        admin = restarted.test_client()
        admin.set_cookie(DEVICE_COOKIE, self.client.get_cookie(DEVICE_COOKIE, path="/login").value, path="/login")
        attacker = restarted.test_client()
        wrong = {"username": "admin", "password": "wrong"}
        for _ in range(10):
            self.post("/login", wrong, client=attacker)
        self.assertEqual(self.post("/login", wrong, client=attacker).status_code, 429)
        self.assertEqual(self.post("/login", {"username": "admin", "password": password}, client=admin).status_code, 302)

    def test_volumes_without_a_device_secret_get_one(self):
        self.store.update(lambda state: state.pop("device_secret"))
        self.assertTrue(StateStore(self.temp.name, ROOT).read()["device_secret"])

    def test_a_full_login_table_cannot_lock_out_a_known_admin_browser(self):
        self.login()
        self.post("/logout")
        logins = self.app.extensions["rate_limiters"]["login"]
        for number in range(RateLimiter.MAX_KEYS):
            logins.allow((("login", f"forged-{number}"), 10, 900))
        right = {"username": "admin", "password": "test-password"}
        self.assertEqual(self.post("/login", right, client=self.app.test_client()).status_code, 429)
        self.login()

    def test_the_per_address_suggestion_cap_holds_against_fresh_sessions(self):
        statuses = [self.post("/suggest", {"kind": "keywords", "value": f"flood {number}"},
                              client=self.app.test_client()).status_code
                    for number in range(SUGGEST_ADDRESS_LIMIT + 1)]
        self.assertEqual(statuses.count(302), SUGGEST_ADDRESS_LIMIT)
        self.assertEqual(statuses[-1], 429)
        # The flood leaves logins alone; they use a separate table.
        right = {"username": "admin", "password": "test-password"}
        self.assertEqual(self.post("/login", right, client=self.app.test_client()).status_code, 302)

    def test_a_retry_fetches_again_when_the_categories_changed(self):
        # Only the categories differ, so the fingerprint must cover them on their own.
        configs = iter([{**CONFIG, "categories": ["physics"], "subcat": [[]]},
                        {**CONFIG, "categories": ["quant-ph"], "subcat": [[]]}])
        self.service.config_loader = lambda: next(configs)
        slot = "2026-10-06 21:30"
        payload = {"date_str": "2026-10-07", "papers": [paper("2610.00001")]}
        with patch.object(bot, "build_daily_payload", return_value=payload) as build:
            with patch.object(bot, "post_to_slack", side_effect=RuntimeError("offline")), \
                    self.assertLogs("digest_service", level="ERROR"):
                self.service.start("scheduled", target_date="2026-10-07", slot=slot)
                self.wait_job()
            with patch.object(bot, "post_to_slack", return_value={"ts": "123.1"}):
                self.service.start("scheduled", target_date="2026-10-07", slot=slot)
                self.wait_job()
        self.assertEqual(build.call_count, 2)
        self.assertEqual(build.call_args.kwargs["config"]["categories"], ["quant-ph"])


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

    def test_slack_text_escapes_markup_characters(self):
        papers = [{"id": "2609.00001", "title": "Cooling to T<1 nK & N>10^4 atoms",
                   "abstract": "We reach <!channel> densities & more.", "authors": ["Ada <Lovelace>"],
                   "selection": "author", "selected_for": "Ada <Lovelace>"}]
        payload = bot.payload_from_papers("2026-09-09", papers)
        for text in (payload["msg_text"], payload["thread_text"]):
            self.assertNotRegex(text, "[<>]")
            self.assertNotRegex(text, "&(?!amp;|lt;|gt;)")
        self.assertIn("*Title:* Cooling to T&lt;1 nK &amp; N&gt;10^4 atoms", payload["msg_text"])
        self.assertIn("*Selected for:* Ada &lt;Lovelace&gt;", payload["msg_text"])
        self.assertIn("*Abstract:* We reach &lt;!channel&gt; densities &amp; more.", payload["thread_text"])

    def test_chunks_never_split_an_escaped_character(self):
        text = "x" * 34998 + "&amp;" + "y" * 10
        self.assertEqual(list(slack_chunks(text)), ["x" * 34998, "&amp;" + "y" * 10])

    def test_chunks_prefer_spaces_when_a_line_is_too_long(self):
        text = "word " * 10000
        chunks = list(slack_chunks(text))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 35000 for chunk in chunks))
        self.assertFalse(any(chunk.startswith(" ") for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_original_capitalization_reaches_slack_unchanged(self):
        papers = [{"id": "2609.00001", "title": "Quantum DNA and BEC in SU(N)",
                   "abstract": "We use e.g. lasers at 3.5 times arXiv.org rates. Then we stop.",
                   "authors": ["Yann LeCun", "Jean-Philippe Brantut", "Sylvain de L\u00e9s\u00e9leuc"],
                   "selection": "keyword", "selected_for": ""}]
        payload = bot.payload_from_papers("2026-09-09", papers)
        self.assertIn("*Title:* Quantum DNA and BEC in SU(N)", payload["msg_text"])
        self.assertIn("*Authors:* Yann LeCun, Jean-Philippe Brantut, Sylvain de L\u00e9s\u00e9leuc", payload["msg_text"])
        self.assertIn("*Abstract:* We use e.g. lasers at 3.5 times arXiv.org rates. Then we stop.", payload["thread_text"])

    def test_lowercased_legacy_text_is_still_restored(self):
        papers = [{"id": "2609.00001", "title": "quantum dna",
                   "abstract": "we trap atoms (see fig. 1.) then cool them to 3.5 k. done!",
                   "authors": ["ada lovelace"], "selection": "keyword", "selected_for": ""}]
        payload = bot.payload_from_papers("2026-09-09", papers)
        self.assertIn("*Title:* Quantum dna", payload["msg_text"])
        self.assertIn("*Authors:* Ada Lovelace", payload["msg_text"])
        self.assertIn("*Abstract:* We trap atoms (see fig. 1.) Then cool them to 3.5 k. Done!", payload["thread_text"])

    def test_a_parenthetical_only_watchlist_line_is_skipped_not_fatal(self):
        with self.assertLogs("slackbot_daily_arxiv", level="WARNING"):
            watchlists = bot.load_watchlists(["(Google)", "Ada Lovelace (Lab)", "  "], [])
        self.assertEqual(watchlists[0], ["Ada Lovelace (Lab)"])
        self.assertEqual(watchlists[4], ["lovelace"])
        frame = pd.DataFrame([{"id": "1", "title": "T", "authors": ["Ada Lovelace"], "abstract": "A"}])
        which_authors = bot.classify_papers(frame, watchlists[0], watchlists[3], watchlists[4], watchlists[5])[3]
        self.assertEqual(which_authors, ["Ada Lovelace (Lab), "])


if __name__ == "__main__":
    unittest.main()
