# Daily arXiv Slack Bot

Code adapted from the original script and initial author/keyword lists by Adam Shaw.

## What this project does

This bot:

- Queries arXiv metadata for configured categories, subcategories, and a selected date (the following day by default).
- Flags papers as important if they match people in `important_people.txt` or terms in `keywords.txt`.
- Posts a formatted digest to a Slack channel.
- Sends on a daily schedule, or on demand from the admin dashboard.
- Runs in Docker with a public dashboard and password-protected admin controls. The pre-Docker host scripts are archived in `archive/` and are not part of the app.

## Docker dashboard

Docker Desktop must be running with its Linux/WSL 2 backend. If Docker or WSL was just installed and Windows requested a restart, restart Windows before starting Docker Desktop.

On Windows, start the engine before running Compose:

```powershell
docker desktop start --timeout 60
docker info
```

A missing `dockerDesktopLinuxEngine` pipe means the Linux engine is not running yet. Starting Docker Desktop is separate from restarting Windows. In Docker Desktop **Settings → General**, enable **Start Docker Desktop when you sign in to your computer** to bring the scheduled bot back after login. The container's `restart: unless-stopped` policy takes effect once Docker is running.

Keep your existing `config.yml` for the same Slack token, channel, and category settings. For a fresh checkout, copy `example_config.yml` to `config.yml` and fill in the token and channel. The config is mounted read-only; it is excluded from the image and build context.

`config.yml` is re-read at the start of every regeneration and every send, as the original host script did, so a rotated token, a moved channel, or edited categories take effect on the next run without restarting the container. If an edit leaves the file invalid, the error is logged and that run continues on the last valid configuration rather than going silent — check `docker compose logs` after editing.

From this project directory:

```powershell
docker compose up -d --build
docker compose logs arxiv-bot
```

Open **http://localhost:8765** on this computer, or **http://<this-computer's-LAN-IP>:8765** on another device. Use `ipconfig` to find the computer's LAN IPv4 address. The port is published on all host interfaces. To serve on another port, set `ARXIV_PORT` in the shell or in a `.env` file next to `compose.yaml`; the published port, the app, and the container health check all follow it. The firewall script reads `ARXIV_PORT` the same way, or takes `-Port <n>`; an elevated PowerShell may not inherit variables set in your normal session. On Windows, allow inbound TCP 8765 from local subnets on your Private/Domain network; `scripts/enable-docker-prerequisites.ps1` configures this along with the required WSL features when run as administrator. It never restarts Windows automatically.

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

The container keeps the original host script's schedule, now set in `schedule_config.py`: **21:30, Sunday through Thursday**, in **America/Los_Angeles**, including daylight saving time. It still queries the following day's paper date. At each scheduled time it regenerates and sends using the same new-paper rule. Failed runs retry every minute within a **two-hour send window (21:30 up to, but not including, 23:30)**, stopping once the slot completes. When arXiv was fetched but Slack failed, the retry reuses those papers instead of querying arXiv again, as long as the watchlists and categories are unchanged; this holds across a restart too. A restart within that window resumes an unfinished slot; missed slots outside the window are not backfilled. The window is set by `SEND_WINDOW_SECONDS` in `schedule_config.py`.

If a scheduled query finds **no matching papers**, the next scheduled query includes that empty date through the new target date, inclusive. For example, an empty query for October 29 makes the next query cover **October 29–30**. Consecutive empty results retain the earliest empty date, including across weekends and restarts. The range returns to one day after matches are successfully delivered or confirmed already sent. Fetch or Slack delivery errors do not clear a pending range. A result containing only previously sent matches does not start a new empty period. These dates filter arXiv's OAI metadata modification dates, not the papers' original submission dates.

The dashboard and Slack digest display the full queried range. Manual **Regenerate** still queries only the selected date and does not change the scheduled catch-up range. Upgrading an existing data volume carries forward its latest successful empty scheduled preview, if present.

A scheduled run with nothing to send still posts a notice, matching the original host script, so a quiet channel always means the bot is broken rather than idle. When nothing matched at all, the notice is the original `No papers found with specified authors! / ...keywords!` text. When papers matched but had all been delivered by an earlier send, the notice says so instead, rather than falsely claiming nothing was found. Neither notice changes **Last message** or the comparison list. A manual **Send** with no new papers posts nothing and reports "There are no new papers to send."

The web server and scheduler share one process and serialize their jobs. Completed schedule slots persist through restarts. Run one container for a given data volume, and keep the archived host scheduler (`archive/bot_server.py` and its VBS launcher) stopped; both read the same `channel` from the same `config.yml`, so running both posts two digests a night and the container's deduplication cannot see the host's sends.

The digest still groups author matches before keyword matches and posts abstracts as Slack thread replies. A digest too long for one Slack message is split across several top-level messages, and the abstract thread hangs off the first of them; large abstract threads are likewise split into smaller replies. A failed parent post leaves the comparison list unchanged. If the parent succeeds but a continuation message or a thread reply fails, the message is recorded as delivered and the dashboard reports the partial failure, so retrying does not duplicate the parent. The abstract thread is still posted after a failed continuation message, and the dashboard names whichever parts are missing. The Slack client retries rate-limited requests up to three times.

### Scheduler health

`GET /healthz` returns 503 when the scheduler thread is dead **or** when its loop has not ticked in 120 seconds. If the loop exits unexpectedly, the process sends itself `SIGTERM` and shuts down cleanly, so Compose's `restart: unless-stopped` brings it back.

An `autoheal` sidecar could also restart the container on a *hung but alive* loop, but it requires mounting `/var/run/docker.sock`, which grants a third-party image host-root-equivalent access. **That sidecar was deliberately not added.** A stall therefore surfaces in `/healthz` and needs a manual `docker compose restart arxiv-bot`.

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

The application runs as a non-root user behind Waitress. Form submissions have CSRF protection, and sessions expire after eight hours of inactivity. Login and suggestion attempts are rate limited. Docker Desktop shows every LAN client to the container as one address, so the limits avoid relying on addresses alone:

- Only failed logins count, 10 per 15 minutes.
- A browser that has signed in before keeps its own login allowance, even after a password reset. Failed guesses from elsewhere on the LAN cannot lock it out.
- A browser signing in for the first time shares the allowance of its address. If someone exhausts it, wait 15 minutes or restart the container, which clears all limits.
- Suggestions are limited to 20 an hour per browser and 60 an hour per address.

The default HTTP setup is for your trusted LAN. If you serve it through a reverse proxy on this computer, set `ARXIV_BIND=127.0.0.1` so only the proxy can reach the app. Then set `TRUSTED_PROXY_HOPS` to the number of proxies, so client addresses come from `X-Forwarded-For`. Never set it while the port is reachable directly, because clients could forge that header. If serving it through HTTPS, set `COOKIE_SECURE=1` so the session cookie is sent only over HTTPS. Compose reads `ARXIV_BIND`, `TRUSTED_PROXY_HOPS`, and `COOKIE_SECURE` from the shell or `.env`, like `ARXIV_PORT`.

### Local development and checks

Python 3.11 or newer is required for the pinned dependencies; the image uses Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v   # or python -m pytest, if pytest is installed
.\.venv\Scripts\python.exe -m unittest discover -s archive     # the archived runner's own check

# Run the webpage locally without automatic Slack delivery
$env:SCHEDULER_ENABLED = "0"
.\.venv\Scripts\python.exe web_app.py
```

Local state defaults to `data/`. Configuration environment variables are `ARXIV_CONFIG` (path to config), `ARXIV_DATA_DIR` (database directory), `TZ` (default `America/Los_Angeles`), `PORT` (default `8765`; under Compose set `ARXIV_PORT` instead), `SCHEDULER_ENABLED` (`1` by default), `COOKIE_SECURE` (`0` by default), and `TRUSTED_PROXY_HOPS` (`0` by default). Compose also reads `ARXIV_BIND` (default `0.0.0.0`), the host address the port is published on.

The unit/integration suite mocks Slack and arXiv. An optional real-browser check also mocks both services and uses temporary state:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe tests\browser_smoke.py
```

This uses installed Microsoft Edge by default and checks public suggestions, admin approval, regeneration, sending only new papers, abstracts, and desktop/mobile layout. Screenshots are saved under `data/`. On another platform, install Chromium with `python -m playwright install chromium` and set `BROWSER_CHANNEL` to an empty string, or use another installed Playwright browser channel.

The browser tab icon is `static/favicon.svg`; `static/favicon.ico` is generated from it for browsers that do not render SVG icons. After editing the SVG, regenerate and commit both with `python scripts/make_favicon_ico.py` (needs `playwright` and an installed Microsoft Edge; the application itself never imports `playwright`).

## Repository contents

- `web_app.py`: Flask/Waitress dashboard and the container entry point; also provides `--reset-admin-password`.
- `digest_service.py`: Serialized regenerate/send jobs and the in-process scheduler.
- `schedule_config.py`: Delivery times, days, send window, and retry delay.
- `slackbot_daily_arxiv.py`: Config loading, watchlist matching, and Slack message formatting.
- `arxiv_source.py`: arXiv OAI-PMH client that adapts `arxivscraper` to the current endpoint, with pagination, bounded retries, and a per-query timeout.
- `web_state.py`: SQLite-backed state: watchlists, suggestions, preview, last message, schedule slots, and admin credentials.
- `templates/`, `static/`: Dashboard HTML, CSS, JavaScript, and favicons.
- `Dockerfile`, `compose.yaml`, `.dockerignore`: Container build and runtime.
- `example_config.yml`: Template config for Slack + arXiv query settings.
- `important_people.txt`, `keywords.txt`: Seed watchlists, one entry per line, copied into a new data volume once.
- `scripts/`: Windows WSL/firewall prerequisites script and the favicon generator.
- `tests/`, `pytest.ini`: Unit/integration suite and the optional browser smoke check.
- `docker_prompt.md`: The original request for the Docker dashboard.
- `archive/`: The pre-Docker host runner, frozen and independent of the app. See [`archive/README.md`](archive/README.md).

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
- `cols`: arXiv scraper output columns to keep. It must include at least `id`, `title`, `abstract`, and `authors`.
- `categories`: list of categories to query.
- `subcat`: list of subcategory lists; must be the same length as `categories`.

Example:

`example_config.yml`

## Run the bot

Run it with Docker as described under **Docker dashboard**. The pre-Docker scripts for a dry run, a one-off send, and the host scheduler are archived with their instructions in [`archive/README.md`](archive/README.md).

To change the delivery time or days, edit `schedule_config.py` and rebuild the image:

```python
TIMES = ["21:30"]  # 24-hour HH:MM, in the container's TZ
ALLOWED_WEEKDAYS = {6, 0, 1, 2, 3}  # Sunday through Thursday
```

## Message structure

The Slack message contains:

- A date header.
- "Important by author" section.
- "Important by keywords" section.
- arXiv links in the form `www.arxiv.org/abs/<id>`.

Titles, authors, and abstracts keep arXiv's own capitalization. Text saved in lowercase by older versions is still restored to sentence case. `&`, `<`, and `>` in paper text are escaped, so a title or abstract cannot form a Slack link or mention such as `<!channel>`.

## Troubleshooting

- `KeyError: Missing required config keys`: Check `config.yml` includes all required fields.
- Slack auth/channel errors: Verify token scopes and channel ID.
- Empty results: The selected date/category/subcategory combo may have no papers.
- `Skipping watchlist entry` in the logs: An author line has nothing outside parentheses, such as `(Google)`. It is ignored until fixed.

## Security and local config

- `config.yml` is gitignored and should stay local.
- Do not commit Slack tokens.
