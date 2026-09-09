"""Serialized background jobs; previewing never posts to Slack."""

import datetime
import logging
import os
import re
import secrets
import signal
import threading
import time
from zoneinfo import ZoneInfo

import bot_server
import slackbot_daily_arxiv as bot
from web_state import timestamp

logger = logging.getLogger(__name__)


def paper_key(paper):
    # A revised arXiv version is still the same paper for send deduplication.
    return re.sub(r"v\d+$", "", paper["id"])


def new_papers(state):
    previous = set(state["baseline_ids"])
    return [paper for paper in (state["preview"] or {}).get("papers", []) if paper_key(paper) not in previous]


def slack_chunks(text, limit=35000):
    """Stay below Slack's 40,000-character truncation limit, including long abstracts."""
    while text:
        end = len(text) if len(text) <= limit else text.rfind("\n", 0, limit)
        if end <= 0:
            end = limit
        yield text[:end]
        text = text[end:].lstrip("\n")


class DigestService:
    def __init__(self, store, config, timezone_name="America/Los_Angeles", config_loader=None):
        self.store = store
        self.config = config
        # The old host script called load_config() on every send, so an edited
        # config.yml took effect on the next run. Keep that.
        self.config_loader = config_loader
        self.timezone = ZoneInfo(timezone_name)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.scheduler = None
        self.last_tick = time.monotonic()

        def recover(state):
            if state["job"] and state["job"]["status"] == "running":
                state["job"].update(status="interrupted", message="The previous job was interrupted by a restart. Review the last message before trying again.", finished_at=timestamp())

        store.update(recover)

    def default_date(self):
        return (datetime.datetime.now(self.timezone).date() + datetime.timedelta(days=1)).isoformat()

    def start(self, action, *, target_date=None, preview_id=None, slot=None):
        if action not in ("regenerate", "send", "scheduled"):
            raise ValueError("Unknown action.")
        if action != "send":
            try:
                target_date = datetime.date.fromisoformat(target_date or self.default_date()).isoformat()
            except ValueError:
                raise ValueError("Choose a valid paper date.") from None
        if not self.lock.acquire(blocking=False):
            raise ValueError("A job is already running. Please wait for it to finish.")
        try:
            state = self.store.read()
            if action == "send":
                if not state["preview"] or state["preview"]["id"] != preview_id:
                    raise ValueError("The preview changed. Refresh this page before sending.")
                if not new_papers(state):
                    raise ValueError("There are no new papers to send.")
            if slot and slot in state["schedule_slots"]:
                raise ValueError("This scheduled slot has already completed.")
            self.store.update(lambda state: state.update(job={
                "action": action, "status": "running", "started_at": timestamp(),
                "message": "Fetching papers from arXiv…" if action != "send" else "Sending new papers to Slack…",
            }))
            self.worker = threading.Thread(target=self._run, args=(action, target_date, slot), daemon=True)
            self.worker.start()
        except Exception:
            self.lock.release()
            raise

    def _finish(self, status, message):
        self.store.update(lambda state: state["job"].update(status=status, message=message, finished_at=timestamp()))

    def _reload_config(self):
        """Pick up config.yml edits without a restart, as the host script did."""
        if not self.config_loader:
            return
        try:
            self.config = self.config_loader()
        except Exception:
            # A broken edit must not silence delivery; the last good values still work.
            logger.exception("Could not reload config.yml; keeping the last valid configuration")

    def _run(self, action, target_date, slot):
        try:
            self._reload_config()
            if action != "send":
                self._regenerate(target_date)
            if action != "regenerate":
                self._send(action, slot)
            else:
                count = len(new_papers(self.store.read()))
                self._finish("success", f"Preview regenerated. {count} new papers ready to send.")
        except Exception:
            logger.exception("Digest job failed (%s)", action)
            self._finish("error", "The job failed. The last successful message is saved. Check the container logs for details and try again.")
        finally:
            self.lock.release()

    def _regenerate(self, target_date):
        state = self.store.read()
        payload = bot.build_daily_payload(
            config=self.config, target_date=target_date,
            watchlists=(state["authors"], state["keywords"]),
        )
        preview = {
            "id": secrets.token_hex(12), "date": payload["date_str"],
            "generated_at": timestamp(), "papers": payload["papers"],
        }
        self.store.update(lambda state: state.update(preview=preview))

    def _complete_slot(self, state, slot):
        if slot:
            state["schedule_slots"][slot] = timestamp()
            # Keep a month of persistent restart guards.
            state["schedule_slots"] = dict(sorted(state["schedule_slots"].items())[-150:])

    def _client(self):
        return bot.make_slack_client(self.config["slack_token"])

    def _send(self, action, slot):
        state = self.store.read()
        papers = new_papers(state)
        if not papers:
            if action != "scheduled":
                self.store.update(lambda state: self._complete_slot(state, slot))
                self._finish("success", "No new papers to send. The last Slack message is unchanged.")
                return
            # The old host script posted a notice every scheduled weekday. Keep that
            # heartbeat so a silent channel still means the bot is broken.
            preview = state["preview"]
            if preview["papers"]:
                # Matches existed but every one went out earlier; the old script's
                # "no papers found" wording would be untrue here.
                notice = "\n".join([
                    f"*Papers for {preview['date']}*:",
                    "-----------------------",
                    "",
                    "The only matching paper was already sent earlier today."
                    if len(preview["papers"]) == 1 else
                    f"All {len(preview['papers'])} matching papers were already sent earlier today.",
                    "-----------------------",
                ])
                message = "No new papers since the last send. Posted the daily notice to Slack."
            else:
                notice = bot.payload_from_papers(preview["date"], [])["msg_text"]
                message = "No new papers. Posted the daily notice to Slack."
            bot.post_to_slack(self._client(), notice, channel=self.config["channel"])
            self.store.update(lambda state: self._complete_slot(state, slot))
            self._finish("success", message)
            return
        preview = state["preview"]
        client = self._client()
        payload = bot.payload_from_papers(preview["date"], papers)
        # The first chunk anchors the abstract thread; continuations post at the
        # top level so the title list stays scannable in-channel.
        parent_chunks = list(slack_chunks(payload["msg_text"])) or [payload["msg_text"]]
        response = bot.post_to_slack(client, parent_chunks[0], channel=self.config["channel"])
        if not response or not response.get("ts"):
            raise RuntimeError("Slack did not confirm delivery.")
        last_message = {
            "date": preview["date"], "sent_at": timestamp(), "papers": papers,
            "slack_ts": response["ts"], "abstracts_sent": False,
        }

        def record_delivery(state):
            state["last_message"] = last_message
            state["baseline_ids"] = [paper_key(paper) for paper in preview["papers"]]
            self._complete_slot(state, slot)

        # Record the parent before posting the abstract thread. A thread error
        # must never cause a retry to post the same parent again.
        self.store.update(record_delivery)
        try:
            for chunk in parent_chunks[1:]:
                bot.post_to_slack(client, chunk, channel=self.config["channel"])
        except Exception:
            logger.exception("Digest delivered, but its continuation messages failed")
            self._finish("partial", f"Sent {len(papers)} papers, but part of the title list did not reach Slack. The full list is available here.")
            return
        try:
            for chunk in slack_chunks(payload["thread_text"] or ""):
                bot.post_to_slack(client, chunk, thread_ts=response["ts"], channel=self.config["channel"])
        except Exception:
            logger.exception("Digest delivered, but its abstract thread failed")
            self._finish("partial", f"Sent {len(papers)} papers. Some abstracts could not be posted to the Slack thread; they are available here.")
            return
        self.store.update(lambda state: state["last_message"].update(abstracts_sent=True))
        self._finish("success", f"Sent {len(papers)} new papers to Slack.")

    def due_slot(self, now):
        if now.weekday() not in bot_server.ALLOWED_WEEKDAYS:
            return None
        current = bot_server.seconds_since_midnight(now)
        completed = self.store.read()["schedule_slots"]
        for label, seconds in zip(bot_server.TIMES, bot_server.SCHEDULE_SECONDS):
            slot = f"{now.date().isoformat()} {label}"
            if seconds <= current < min(86400, seconds + bot_server.SEND_WINDOW_SECONDS) and slot not in completed:
                return slot
        return None

    def scheduler_healthy(self):
        return bool(self.scheduler and self.scheduler.is_alive()
                    and time.monotonic() - self.last_tick < 120)

    def start_scheduler(self):
        def loop():
            retry_at = {}
            try:
                while not self.stop_event.is_set():
                    # The tick is independent of any in-flight send, so a stale
                    # timestamp means the loop itself stalled.
                    self.last_tick = time.monotonic()
                    try:
                        now = datetime.datetime.now(self.timezone)
                        slot = self.due_slot(now)
                        if slot and now.timestamp() >= retry_at.get(slot, 0) and not self.lock.locked():
                            self.start("scheduled", target_date=(now.date() + datetime.timedelta(days=1)).isoformat(), slot=slot)
                            retry_at = {slot: now.timestamp() + bot_server.RETRY_DELAY_SECONDS}
                    except Exception:
                        logger.exception("Scheduler iteration failed")
                    self.stop_event.wait(5)
            finally:
                if not self.stop_event.is_set():
                    # A dead scheduler with a live web server is invisible in Slack.
                    # Exit so the container restart policy takes over.
                    logger.critical("Scheduler loop exited unexpectedly; terminating for restart")
                    os.kill(os.getpid(), signal.SIGTERM)

        self.scheduler = threading.Thread(target=loop, daemon=True, name="daily-arxiv-scheduler")
        self.scheduler.start()

    def close(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join(timeout=6)
        if self.worker:
            self.worker.join(timeout=35)
