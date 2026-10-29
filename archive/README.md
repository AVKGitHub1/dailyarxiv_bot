# Archived pre-Docker host runner

These files ran the bot directly on a Windows host before the Docker dashboard existed. The container (`web_app.py`) now owns Slack delivery. Nothing here is part of the app, the image, or the live test suite.

## What is here

- `bot_server.py`: Host scheduler loop. It sends at 21:30, Sunday through Thursday, in the machine's local timezone.
- `slack_bot_test.py`: Sends one digest to Slack immediately.
- `message_retrieve_test.py`: Builds one digest and writes it to `test_results.txt` without posting.
- `run_bot_example.vbs`: Windows launcher that starts `bot_server.py` in the background.
- `slackbot_daily_arxiv.py`, `arxiv_source.py`: Frozen copies of the digest and arXiv code these scripts use.
- `changes.md`: Design record of the work that brought the container to parity with this runner.
- `test_bot_server.py`: Offline check for the runner.

## Independence from the live code

The archive has no code ties to the live app in either direction:

- The live code never imports anything from `archive/`, and the image never copies it.
- These scripts import only the copies in this folder. Python puts a script's own folder first on its import path, so `python archive/bot_server.py` loads `archive/slackbot_daily_arxiv.py`, not the live module of the same name.
- The copies are frozen, apart from four bug fixes that could misfire in Slack. Message text is escaped, so a paper cannot trigger `<!channel>`. Capitalization is preserved. A watchlist line with only a parenthetical, such as `(Google)`, is skipped instead of crashing. A failed abstract thread no longer reposts the digest. Other changes in the live code do not reach these copies, and changing them cannot affect the dashboard.

The archive shares only data files with the repository. It reads `config.yml`, `important_people.txt`, and `keywords.txt` from the repository root. It never sees the dashboard's approved watchlists, previews, or sent-paper history, which live in the container's data volume.

`tests/test_packaging.py` in the live suite fails if either direction of that independence breaks.

## Before running anything here

These scripts post to the **same channel** as the container, and the container cannot see what they send. Running the host scheduler alongside the container posts two digests every night. A one-off send can make the container post the same papers again later.

Before using the host scheduler, stop the container's scheduler with `SCHEDULER_ENABLED=0` or stop the container. Before relying on the container again, remove any host registration of the old scheduler. Check the Windows startup folder for the VBS and Task Scheduler for a task invoking `bot_server.py`. Registrations that point at the old repository-root path now fail to start, because the file moved here.

## Running the archived scripts

Run these from the repository root with the project's dependencies installed. The two manual scripts take an optional day offset from today; the default is tomorrow.

Dry run, with no Slack post:

```powershell
python archive\message_retrieve_test.py
```

Send one digest to Slack:

```powershell
python archive\slack_bot_test.py
```

Host scheduler. Its entry point refuses to start unless you opt in:

```powershell
$env:ARXIV_LEGACY_SCHEDULER = "1"
python archive\bot_server.py
```

`run_bot_example.vbs` launches `archive\bot_server.py` silently. Fill in its paths, and set `ARXIV_LEGACY_SCHEDULER=1` in its environment.

Schedule notes for the host scheduler:

- Edit `TIMES` in `archive/bot_server.py` to change the send time.
- Time is interpreted in the machine's local timezone.
- A failed run retries every minute within a two-hour window.
- Daily send flags reset when the local date changes. They are not saved, so a restart inside the window can send that slot again.
- If the digest posts but its abstract thread fails, the error is logged and the slot still counts as sent, so the digest is never posted twice.
- A failed digest post fetches arXiv again on every retry. The container avoids that; this frozen runner does not.

## Checking the runner

```powershell
python -m unittest discover -s archive
```

Slack, arXiv, and the clock are mocked, so nothing is posted. Do not run the archive with the live suite in one `pytest` session, because both folders contain modules with the same names. The repository's `pytest.ini` collects only `tests/` for that reason.
