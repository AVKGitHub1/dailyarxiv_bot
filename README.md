# Daily arXiv Slack Bot

Code adapted from the original script and initial author/keyword lists by Adam Shaw.

## What this project does

This bot:

- Queries arXiv for configured categories and subcategories on the current date.
- Flags papers as important if they match people in `important_people.txt` or terms in `keywords.txt`.
- Posts a formatted digest to a Slack channel.
- Can run once or on a daily schedule.

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

- Python 3
- A Slack bot token with permission to post to your target channel
- Python packages from `requirements.txt`:
  - `arxivscraper`
  - `pandas`
  - `slack_sdk`
  - `pyyaml`

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

`exple_config.yml`

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
TIMES = ["08:30"]  # 24-hour HH:MM
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
