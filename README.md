# Daily arXiv Slack Bot

Code adapted from the original script and initial author/keyword lists by Adam Shaw.

## What this project does

This bot:

- Queries arXiv metadata for configured categories, subcategories, and a selected date (the following day by default).
- Flags papers as important if they match people in `important_people.txt` or terms in `keywords.txt`.
- Posts a formatted digest to a Slack channel.
- Can run once or on a daily schedule.
- Runs in Docker with a public dashboard and password-protected admin controls.

## Docker dashboard

Docker Desktop must be running with its Linux/WSL 2 backend. If Docker or WSL was just installed and Windows requested a restart, restart Windows before starting Docker Desktop.

On Windows, start the engine before running Compose:

```powershell
docker desktop start --timeout 60
docker info
```

A missing `dockerDesktopLinuxEngine` pipe means the Linux engine is not running yet. Starting Docker Desktop is separate from restarting Windows. In Docker Desktop **Settings → General**, enable **Start Docker Desktop when you sign in to your computer** to bring the scheduled bot back after login. The container's `restart: unless-stopped` policy takes effect once Docker is running.

Keep your existing `config.yml` for the same Slack token, channel, and category settings. For a fresh checkout, copy `example_config.yml` to `config.yml` and fill in the token and channel. The config is mounted read-only; it is excluded from the image and build context.

From this project directory:

```powershell
docker compose up -d --build
docker compose logs arxiv-bot
```

Open **http://localhost:8765** on this computer, or **http://<this-computer's-LAN-IP>:8765** on another device. Use `ipconfig` to find the computer's LAN IPv4 address. The port is published on all host interfaces. On Windows, allow inbound TCP 8765 from local subnets on your Private/Domain network; `scripts/enable-docker-prerequisites.ps1` configures this along with the required WSL features when run as administrator. It never restarts Windows automatically.

On the first launch with a new data volume, the logs display:

```text
Admin username: admin
Admin password: <randomly generated password>
```

Save the password. It is printed only at initialization, and only its hash is stored in the database. Sign in through **Admin sign in** in the page header.

### Public and admin controls

Everyone can read the author and keyword textboxes, submit one suggested addition at a time, and view papers in the last successful Slack message. Clicking a paper's title opens its abstract; its separate arXiv link opens the paper page. Pending suggestions do not affect matching until an admin accepts them.

Admins have all public controls plus:

- **Accept / Dismiss** for pending suggestions. Accepted additions are used by the next regeneration or scheduled query.
- **Regenerate** to fetch matching papers for the selected date and replace the **Current preview**. It never sends to Slack or changes **Last message**.
- **Send N new papers** to send the preview's papers that were absent from the complete list used by the previous successful send. Send uses the displayed preview; it does not fetch again.

For example, after sending a list containing A and B, regenerating a preview containing A, B, and C makes Send post only C. The comparison list then becomes A, B, C, while **Last message** displays C. Clicking Send again sends nothing. Regenerating several times without sending keeps the same comparison list. New arXiv versions of the same paper are treated as the same paper.

There is no import of messages sent by the old standalone scripts. Until the first successful send through this service, **Last message** is empty and the comparison list is empty.

### Preserved schedule

The container uses the existing schedule from `bot_server.py`: **21:30, Sunday through Thursday**, in **America/Los_Angeles**, including daylight saving time. It still queries the following day's paper date. At each scheduled time it regenerates and sends using the same new-paper rule. Empty results produce no Slack post and leave the last message unchanged. The original ten-minute send window and one-minute retry delay are preserved; missed slots outside that window are not backfilled.

The web server and scheduler share one process and serialize their jobs. Completed schedule slots persist through restarts. Run one container for a given data volume, and stop the old `bot_server.py`/VBS scheduler before switching to the container to avoid two independent senders.

The digest still groups author matches before keyword matches and posts abstracts as Slack thread replies. Large abstract threads are split into smaller replies. A failed parent post leaves the comparison list unchanged. If a parent succeeds but a thread reply fails, the message is recorded as delivered and the dashboard reports the partial failure, so retrying does not duplicate the parent.

### Persistence and maintenance

The named `arxiv-data` volume stores approved watchlists, pending suggestions, the preview, the last message, comparison IDs, schedule slots, and admin credentials. `important_people.txt` and `keywords.txt` seed it once; subsequent accepted additions live in the volume, without rewriting those source files. Restarting or rebuilding the image preserves this data.

```powershell
# View status or recent job errors
docker compose ps
docker compose logs --tail 100 arxiv-bot

# Stop, retaining the database
docker compose down

# Rebuild after code changes
docker compose up -d --build

# Generate a replacement password, then reload credentials and sessions
docker compose exec arxiv-bot python web_app.py --reset-admin-password
docker compose restart arxiv-bot
```

To recover the password while the service is stopped, use `docker compose run --rm --no-deps arxiv-bot python web_app.py --reset-admin-password`, then start it normally. Do not use `docker compose down -v` unless you intend to erase all saved state. Back up the named volume while the service is stopped.

The application runs as a non-root user behind Waitress. Form submissions have CSRF protection, sessions expire after eight hours, and login/suggestion attempts are rate limited. The default HTTP setup is for your trusted LAN. If serving it through HTTPS, set `COOKIE_SECURE=1` in Compose so the session cookie is sent only over HTTPS.

### Local development and checks

Python 3.11 or newer is required for the pinned dependencies; the image uses Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Run the webpage locally without automatic Slack delivery
$env:SCHEDULER_ENABLED = "0"
.\.venv\Scripts\python.exe web_app.py
```

Local state defaults to `data/`. Configuration environment variables are `ARXIV_CONFIG` (path to config), `ARXIV_DATA_DIR` (database directory), `TZ` (default `America/Los_Angeles`), `PORT` (default `8765`), `SCHEDULER_ENABLED` (`1` by default), and `COOKIE_SECURE` (`0` by default).

The unit/integration suite mocks Slack and arXiv. An optional real-browser check also mocks both services and uses temporary state:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe tests\browser_smoke.py
```

This uses installed Microsoft Edge by default and checks public suggestions, admin approval, regeneration, sending only new papers, abstracts, and desktop/mobile layout. Screenshots are saved under `data/`. On another platform, install Chromium with `python -m playwright install chromium` and set `BROWSER_CHANNEL` to an empty string, or use another installed Playwright browser channel.

## Repository contents

- `slackbot_daily_arxiv.py`: Core logic for loading config, scraping arXiv, classifying papers, and building/posting the Slack message.
- `bot_server.py`: Scheduler loop that sends at one or more daily times.
- `example_config.yml`: Template config for Slack + arXiv query settings.
- `important_people.txt`: One person per line for author-based prioritization.
- `keywords.txt`: One keyword/phrase per line for keyword-based prioritization.
- `message_retrieve_test.py`: Generates the digest and writes it to `test_results.txt` without posting to Slack.
- `slack_bot_test.py`: Sends the digest immediately to Slack.
- `run_bot.vbs`: Optional Windows script for launching the scheduler in the background.

## Requirements

- Python 3.11 or newer
- A Slack bot token with permission to post to your target channel
- Python packages from `requirements.txt`:
  - `arxivscraper`
  - `pandas`
  - `slack_sdk`
  - `pyyaml`
  - `Flask`, `waitress`, and `tzdata` for the dashboard

## Setup

Install dependencies.

```powershell
pip install -r requirements.txt
```

Create your config file.

Required keys:

- `slack_token`: Slack bot token.
- `channel`: channel name example: "#dailyarxiv".
- `cols`: arXiv scraper output columns.
- `categories`: list of categories to query.
- `subcat`: list of subcategory lists; must be the same length as `categories`.

Example:

`example_config.yml`

## Run the bot

### Dry run (no Slack post)

Build and save the message locally:

```powershell
python message_retrieve_test.py
```

Output is written to `test_results.txt`.

### Send once to Slack

```powershell
python slack_bot_test.py
```

### Run scheduled daily sender

```powershell
python bot_server.py
```

Edit schedule times in `bot_server.py`:

```python
TIMES = ["21:30"]  # 24-hour HH:MM, Sunday through Thursday
```

Notes:

- Time is interpreted in the machine's local timezone.
- The scheduler uses a send window to avoid missing a slot if the process wakes a little late.
- Daily send flags reset automatically when the local date changes.

### Windows background launch (optional)

`run_bot_example.vbs` launches the scheduler silently. Update to the correct paths.

## Message structure

The Slack message contains:

- A date header.
- "Important by author" section.
- "Important by keywords" section.
- arXiv links in the form `www.arxiv.org/abs/<id>`.

## Troubleshooting

- `KeyError: Missing required config keys`: Check `config.yml` includes all required fields.
- Slack auth/channel errors: Verify token scopes and channel ID.
- Empty results: The selected date/category/subcategory combo may have no papers.

## Security and local config

- `config.yml` is gitignored and should stay local.
- Do not commit Slack tokens.
