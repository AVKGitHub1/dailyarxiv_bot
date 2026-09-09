"""Serialized background jobs; previewing never posts to Slack."""

import datetime
import logging
import re
import secrets
import threading
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

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
    def __init__(self, store, config, timezone_name="America/Los_Angeles"):
        self.store = store
        self.config = config
        self.timezone = ZoneInfo(timezone_name)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.scheduler = None

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

    def _run(self, action, target_date, slot):
        try:
            if action != "send":
                self._regenerate(target_date)
            if action != "regenerate":
                self._send(slot)
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

    def _send(self, slot):
        state = self.store.read()
        papers = new_papers(state)
        if not papers:
            self.store.update(lambda state: self._complete_slot(state, slot))
            self._finish("success", "No new papers to send. The last Slack message is unchanged.")
            return
        preview = state["preview"]
        client = WebClient(token=self.config["slack_token"], timeout=30)
        payload = bot.payload_from_papers(preview["date"], papers)
        # Keep all titles in the parent message. Refuse a truncated digest so
        # the persisted paper list always agrees with what Slack received.
        if len(payload["msg_text"]) > 39000:
            raise ValueError("Digest is too long for a single Slack message. Narrow the watchlists before sending.")
        response = bot.post_to_slack(client, payload["msg_text"], channel=self.config["channel"])
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

    def start_scheduler(self):
        def loop():
            retry_at = {}
            while not self.stop_event.is_set():
                try:
                    now = datetime.datetime.now(self.timezone)
                    slot = self.due_slot(now)
                    if slot and now.timestamp() >= retry_at.get(slot, 0) and not self.lock.locked():
                        self.start("scheduled", target_date=(now.date() + datetime.timedelta(days=1)).isoformat(), slot=slot)
                        retry_at = {slot: now.timestamp() + bot_server.RETRY_DELAY_SECONDS}
                except Exception:
                    logger.exception("Scheduler iteration failed")
                self.stop_event.wait(5)

        self.scheduler = threading.Thread(target=loop, daemon=True, name="daily-arxiv-scheduler")
        self.scheduler.start()

    def close(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join(timeout=6)
        if self.worker:
            self.worker.join(timeout=35)
