# Slack delivery parity: changes to apply

Purpose: bring the Dockerised web/scheduler path (`digest_service.py`, `web_app.py`) back to
parity with the original host script path (`slackbot_daily_arxiv.py` + `bot_server.py`) where it
silently diverged, and to harden delivery.

Apply these on the machine that actually runs the container. Nothing here touches `config.yml`;
the Slack token stays where it is.

Already at parity (do not change): shared message builders (`build_message`,
`build_abstract_thread_message`, `format_authors`, `to_sentence_case`), shared schedule constants
imported from `bot_server` (`TIMES`, `SCHEDULE_SECONDS`, `SEND_WINDOW_SECONDS`,
`ALLOWED_WEEKDAYS`), target date = tomorrow, 60 s in-window retry, abstracts in a thread on the
parent's `ts`.

---

## 1. Post the "nothing today" message on quiet scheduled days

**Problem.** The old script posted every allowed weekday (Sun-Thu 21:30) even with zero hits,
producing `No papers found with specified authors! / ...keywords!`. `DigestService._send` returns
early instead, so a quiet day and a dead scheduler look identical in Slack.

**Scope.** Scheduled runs only. A manual admin send never reaches this branch - `start()` already
rejects it with `"There are no new papers to send."`.

**Verified.** `bot.payload_from_papers(date, [])` renders the old quiet-day message
byte-for-byte and returns `thread_text is None`, so no new formatting code is needed.

### Edits - `digest_service.py`

`_run`: pass the action through.

```python
            if action != "regenerate":
                self._send(action, slot)
```

`_send`: change the signature and replace the empty-papers early return.

```python
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
            heartbeat = bot.payload_from_papers(preview["date"], [])
            bot.post_to_slack(self._client(), heartbeat["msg_text"], channel=self.config["channel"])
            self.store.update(lambda state: self._complete_slot(state, slot))
            self._finish("success", "No new papers. Posted the daily notice to Slack.")
            return
```

**Deliberately not done:** the heartbeat does **not** overwrite `state["last_message"]` or
`state["baseline_ids"]`. Overwriting `last_message` would blank the dashboard's "papers in the
last message" panel on every quiet day; shrinking `baseline_ids` to a possibly smaller preview
could cause re-sends.

**Failure behaviour:** if the heartbeat post itself fails, the exception propagates to `_run`, the
slot is not marked complete, and the scheduler retries after `RETRY_DELAY_SECONDS` - same as the
old script.

---

## 2. Chunk the parent message instead of aborting the send

**Problem.** `_send` raises `ValueError` when `msg_text` exceeds 39 000 chars. On a scheduled run
that means ten consecutive failures inside the 10-minute window and nothing reaches Slack at all.
The abstract thread already chunks; the parent does not.

**Decision.** Reuse `slack_chunks` for the parent. The first chunk's `ts` anchors the abstract
thread. Continuation chunks post as further top-level channel messages (not into the thread) so
the title list stays scannable in-channel.

### Edits - `digest_service.py` `_send`

Delete the length guard and its comment:

```python
        # Keep all titles in the parent message. Refuse a truncated digest so
        # the persisted paper list always agrees with what Slack received.
        if len(payload["msg_text"]) > 39000:
            raise ValueError("Digest is too long for a single Slack message. Narrow the watchlists before sending.")
```

Replace the parent post with:

```python
        payload = bot.payload_from_papers(preview["date"], papers)
        parent_chunks = list(slack_chunks(payload["msg_text"])) or [payload["msg_text"]]
        response = bot.post_to_slack(client, parent_chunks[0], channel=self.config["channel"])
        if not response or not response.get("ts"):
            raise RuntimeError("Slack did not confirm delivery.")
```

The `or [payload["msg_text"]]` fallback guards the `parent_chunks[0]` index - `slack_chunks("")`
yields nothing.

Keep `self.store.update(record_delivery)` exactly where it is (immediately after the first chunk
is confirmed). That preserves the existing invariant: **a retry must never re-post the parent.**
Then post continuations and abstracts after it:

```python
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
```

Two separate `try` blocks so the dashboard message distinguishes "titles truncated" from
"abstracts missing" - those need different follow-up.

**Note:** with everything now delivered, the persisted paper list agrees with Slack in all success
paths, which is what the deleted guard was protecting.

---

## 3. Dedupe baseline - reviewed, keep as-is

`_send` sets `state["baseline_ids"] = [paper_key(p) for p in preview["papers"]]`, i.e. it
*replaces* the baseline rather than accumulating. **Decision: leave unchanged.**

Known caveat to be aware of when operating the dashboard: if an admin regenerates a **past** date
and sends it, the baseline is reset to that date's papers, so the next scheduled run can re-post
papers already delivered on the intervening days. Regenerating a past date is a deliberate,
low-frequency admin action, so this is accepted rather than fixed. If it ever bites, the fix is to
union new keys into `baseline_ids` and trim to the last ~2000 entries.

---

## 4. Delivery hardening

### 4a. Slack rate-limit retry handler

**Problem.** The old script posted at most 2 messages per run. The new path posts 1 parent + N
thread chunks (and now + parent continuations) back to back with no 429 handling, so a large day
can drop into the `partial` state on a rate limit rather than a real error.

**Do it once, shared.** Add a factory to `slackbot_daily_arxiv.py` so both the container path and
the legacy path build an identically configured client - divergence between the two clients is
exactly the class of bug this document exists to close.

```python
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler


def make_slack_client(token):
    client = WebClient(token=token, timeout=30)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client
```

Then:

- `slackbot_daily_arxiv.main_slack_send`: `slackclient = make_slack_client(config["slack_token"])`
- `digest_service`: add a helper and use it in both `_send` and the change-1 heartbeat path:

```python
    def _client(self):
        return bot.make_slack_client(self.config["slack_token"])
```

  then replace `client = WebClient(token=self.config["slack_token"], timeout=30)` with
  `client = self._client()` and drop the now-unused `from slack_sdk import WebClient` import.

**Verify the import path before committing** - this was not runnable in the authoring environment
(`slack_sdk` is not installed there). Against the pinned `slack_sdk==3.44.1`:

```
python -c "from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler; print('ok')"
```

**Accepted risk:** rate-limit sleeps can push a send past `close()`'s 35 s worker join and the
45 s `stop_grace_period`. On shutdown the daemon thread is killed with the process; the parent
message is already recorded by then, so a restart cannot duplicate it.

### 4b. Restart when the scheduler thread dies or stalls

**Problem.** `/healthz` returns 503 when `service.scheduler` is not alive, but a Docker
HEALTHCHECK does not restart anything and `restart: unless-stopped` only fires on process exit.
Today a dead scheduler leaves the web UI up and Slack quiet indefinitely.

Two parts.

**(i) Heartbeat, so a *hung* loop is detectable, not just a dead one.** The loop body already
swallows every `Exception`, so outright death is unlikely; a stall is the realistic failure. The
loop ticks every 5 s regardless of whether a send is in flight (`start()` spawns a worker thread
and returns), so a stall check cannot false-positive on a long send.

In `DigestService.__init__`: `self.last_tick = time.monotonic()`.
In `start_scheduler`'s `loop`, at the top of each iteration: `self.last_tick = time.monotonic()`.
Add:

```python
    def scheduler_healthy(self):
        return bool(self.scheduler and self.scheduler.is_alive()
                    and time.monotonic() - self.last_tick < 120)
```

and switch `web_app.health` to use it:

```python
        if start_scheduler and not service.scheduler_healthy():
            return jsonify(status="scheduler stopped"), 503
```

**(ii) Exit the process so the restart policy takes over.** In `start_scheduler`'s `loop`, wrap
the `while` in `try/finally` and, if the loop exits without `stop_event` being set, signal the
main thread:

```python
            finally:
                if not self.stop_event.is_set():
                    logger.critical("Scheduler loop exited unexpectedly; terminating for restart")
                    os.kill(os.getpid(), signal.SIGTERM)
```

SIGTERM hits `web_app.main`'s handler, which raises `SystemExit` in the main thread, waitress
unwinds, and the `finally` there calls `digest_service.close()` - a clean shutdown, and
`restart: unless-stopped` in `compose.yaml` restarts regardless of exit code.

New imports needed in `digest_service.py`: `os`, `signal`, `time`.

**(iii) Autoheal sidecar** - this is what covers the hung-but-alive case, since part (ii) only
fires when the thread actually exits. In `compose.yaml`:

```yaml
  autoheal:
    image: willfarrell/autoheal:1.2.0
    restart: unless-stopped
    environment:
      AUTOHEAL_CONTAINER_LABEL: autoheal
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

and label the bot service:

```yaml
    labels:
      autoheal: "true"
```

If mounting the Docker socket is not acceptable on the host, skip the sidecar - part (ii) alone
still covers thread death, and part (i) still surfaces a stall in `/healthz` for manual
follow-up. Record which option was taken.

### 4c. Retire the host-side runner

**Problem.** `bot_server.run_scheduler` and `run_bot_example.vbs` still work and read the same
`channel` from the same `config.yml`. If either is still registered on the host (Task Scheduler /
startup folder), the channel gets two digests a night and the container's dedupe cannot see the
host's sends.

1. **Check the host first:** look for the VBS in the startup folder and for a Task Scheduler entry
   invoking `bot_server.py`. Disable whatever is found - this is the actual double-post risk; the
   code guard below only makes a future mistake loud.
2. Guard the entry point in `bot_server.py` so importing it for constants is unaffected (add
   `import os` at the top):

```python
if __name__ == "__main__":
    if os.environ.get("ARXIV_LEGACY_SCHEDULER") != "1":
        raise SystemExit(
            "bot_server.py is the pre-Docker scheduler. The container (web_app.py) now owns "
            "Slack delivery; running both double-posts to the same channel. "
            "Set ARXIV_LEGACY_SCHEDULER=1 to override."
        )
    run_scheduler()
```

3. `README.md`: mark `bot_server.py` and `run_bot_example.vbs` as legacy/pre-Docker, and state
   that they target the same channel as the container.

**Optional follow-up, not required:** `digest_service` imports `bot_server` only for the schedule
constants, which is why the file must stay in the image. Moving `TIMES`, `SEND_WINDOW_SECONDS`,
`RETRY_DELAY_SECONDS` and `ALLOWED_WEEKDAYS` into a small `schedule_config.py` imported by both
would let `bot_server.py` drop out of the Dockerfile `COPY` and the `.dockerignore` allowlist
entirely.

---

## 5. Tests to add - `tests/test_web.py`

The existing suite already covers the no-new-papers path, parent-failure retry, thread-failure
partial, and `slack_chunks`. Extend it:

1. Scheduled run with zero new papers posts exactly one message equal to
   `payload_from_papers(date, [])["msg_text"]`, marks the slot complete, and leaves
   `last_message` and `baseline_ids` untouched. **This will require updating the existing
   no-new-papers test** (around `tests/test_web.py:216`) - it currently asserts `post_to_slack`
   was not called, which is only true for the manual path now.
2. Manual admin send with zero new papers still raises `"There are no new papers to send."` and
   posts nothing.
3. A digest whose `msg_text` exceeds 39 000 chars posts N top-level messages, and the abstract
   thread's `thread_ts` equals the **first** chunk's `ts`.
4. Continuation-chunk failure yields status `partial`, and a subsequent retry does not re-post the
   parent (`baseline_ids` / `last_message` already recorded).
5. `make_slack_client` attaches a `RateLimitErrorRetryHandler`.
6. `scheduler_healthy()` returns False once `last_tick` is older than 120 s.

## 6. Rollout

1. Apply 1, 2, 4a, 4c; run `python -m pytest tests/test_web.py tests/test_arxiv_source.py`.
2. Apply 4b; `docker compose build && docker compose up -d`; confirm `/healthz` is `ok` and the
   first-launch admin password is **not** reprinted (the `/data` volume persists).
3. Dry-run the quiet-day path: as admin, regenerate a date with no matches, then wait for or
   simulate a scheduled slot, and confirm exactly one notice lands in Slack.
4. Confirm the host-side legacy runner is disabled (4c step 1) **before** the first scheduled slot
   after cutover.
