import datetime
import sys
from pathlib import Path
from time import sleep

import arxivscraper
import pandas as pd
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

#setup logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


CONFIG_PATH = Path(__file__).with_name("config.yml")
def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    required_keys = ["slack_token", "channel", "cols", "categories", "subcat"]
    missing = [key for key in required_keys if key not in config]
    if missing:
        raise KeyError(f"Missing required config keys: {missing}")

    categories = config["categories"]
    subcat = config["subcat"]
    if len(categories) != len(subcat):
        raise ValueError(
            "Config validation error: 'categories' and 'subcat' must have the same length."
        )

    return config


CONFIG = load_config()
SLACK_TOKEN = CONFIG["slack_token"]
CHANNEL = CONFIG["channel"]
COLS = tuple(CONFIG["cols"])
CATEGORIES = CONFIG["categories"]
SUBCAT = CONFIG["subcat"]


def load_lines(path):
    try:
        with open(path, "r") as f:
            return f.read().split("\n")
    except Exception as e:
        logger.exception("Error reading lines from %s: %s", path, e)
        return []


def load_watchlists():
    important_people = load_lines("important_people.txt")
    important_firsts = [name.split()[0] for name in important_people]
    important_lasts = [name.split()[1] for name in important_people]
    keywords = load_lines("keywords.txt")
    keywords_lower = [kw.lower() for kw in keywords]
    important_firsts_lower = [first.lower() for first in important_firsts]
    important_lasts_lower = [last.lower() for last in important_lasts]

    return (
        important_people,
        important_firsts,
        important_lasts,
        important_firsts_lower,
        important_lasts_lower,
        keywords_lower,
    )


def fetch_papers_for_date(date_str):
    frames = []
    for cat_idx in range(len(CATEGORIES)):
        category = CATEGORIES[cat_idx]
        subcategories = SUBCAT[cat_idx]
        if not subcategories:
            scraper = arxivscraper.Scraper(
                category=category,
                date_from=date_str,
                date_until=date_str,
                t=5,
            )
        else:
            scraper = arxivscraper.Scraper(
                category=category,
                date_from=date_str,
                date_until=date_str,
                t=6,
                filters={"categories": subcategories},
            )

        output = scraper.scrape()
        frames.append(pd.DataFrame(output, columns=COLS))
        sleep(1)

    if frames:
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.DataFrame([], columns=COLS)

    return df.drop_duplicates(subset="id").reset_index()


def match_author(first_name, last_name, important_firsts, important_lasts):
    for ii, important_last in enumerate(important_lasts):
        if last_name != important_last:
            continue

        if len(first_name) == 1 or first_name[1] == ".":
            if first_name[0] == important_firsts[ii][0]:
                return ii
        elif first_name == important_firsts[ii]:
            return ii

    return None


def classify_papers(
    df,
    important_people,
    important_firsts_lower,
    important_lasts_lower,
    keywords_lower,
):
    num_retrieved = len(df)
    important_by_author = [False] * num_retrieved
    important_by_keyword = [False] * num_retrieved
    which_authors = [""] * num_retrieved

    for paper_id in range(num_retrieved):
        authors = df.authors[paper_id]
        first_names = [name.split()[0].lower() for name in authors]
        last_names = [name.split()[-1].lower() for name in authors]

        for first_name, last_name in zip(first_names, last_names):
            author_idx = match_author(
                first_name,
                last_name,
                important_firsts_lower,
                important_lasts_lower,
            )
            if author_idx is not None:
                important_by_author[paper_id] = True
                which_authors[paper_id] += important_people[author_idx] + ", "

        title_lower = df.title[paper_id].lower()
        abstract_lower = df.abstract[paper_id].lower()
        for kw in keywords_lower:
            if kw in title_lower or kw in abstract_lower:
                important_by_keyword[paper_id] = True

    imp_author_idx = []
    imp_keyword_idx = []
    other_idx = []
    for paper_id in range(num_retrieved):
        if important_by_author[paper_id]:
            imp_author_idx.append(paper_id)
        elif important_by_keyword[paper_id]:
            imp_keyword_idx.append(paper_id)
        else:
            other_idx.append(paper_id)

    return imp_author_idx, imp_keyword_idx, other_idx, which_authors


def format_authors(authors):
    if len(authors) > 10:
        return "_MANY AUTHORS_"
    cap_authors = [
        " ".join([name.capitalize() for name in author.split()]) for author in authors
    ]
    return ", ".join(cap_authors)

def normalize_abstract_text(text):
    return " ".join(str(text).split())


def to_sentence_case(text):
    normalized = normalize_abstract_text(text)
    if not normalized:
        return normalized
    try:
        chars = list(normalized)
        capitalize_next = True
        for i, ch in enumerate(chars):
            if capitalize_next and ch.isalpha():
                chars[i] = ch.upper()
                capitalize_next = False
            if ch in ".!?":
                capitalize_next = True
    except Exception as e:
        logger.exception("Error converting text to sentence case: %s", e)
        return normalized

    return "".join(chars)


def build_message(date, df, imp_author_idx, imp_keyword_idx, which_authors):
    lines = []
    lines.append(f"*Papers for {date}*:")
    lines.append("-----------------------")
    if len(imp_author_idx) > 0:
        lines.append("")
        lines.append("*Important by author*: ")
        for idx in imp_author_idx:
            lines.append("*Title:* " + df.title[idx].capitalize())
            lines.append("*Selected for:* " + which_authors[idx][:-2])
            lines.append("*Authors:* " + format_authors(df.authors[idx]))
            lines.append("*Link:* " + "www.arxiv.org/abs/" + df.id[idx])
            lines.append("")
    else:
        lines.append("")
        lines.append("No papers found with specified authors!")
    lines.append("-----------------------")

    if len(imp_keyword_idx) > 0:
        lines.append("")
        lines.append("*Important by keywords:* ")
        for idx in imp_keyword_idx:
            lines.append("*Title:* " + df.title[idx].capitalize())
            lines.append("*Authors:* " + format_authors(df.authors[idx]))
            lines.append("*Link:* " + "www.arxiv.org/abs/" + df.id[idx])
            lines.append("")
    else:
        lines.append("")
        lines.append("No papers found with specified keywords!")
    lines.append("-----------------------")

    return "\n".join(lines)


def build_abstract_thread_message(date, df, imp_author_idx, imp_keyword_idx):
    selected_idx = imp_author_idx + imp_keyword_idx
    if not selected_idx:
        return None

    lines = [f"*Abstracts for {date}*:", "-----------------------", ""]
    for idx in selected_idx:
        lines.append("*Title:* " + df.title[idx].capitalize())
        lines.append("*Link:* " + "www.arxiv.org/abs/" + df.id[idx])
        lines.append("*Abstract:* " + to_sentence_case(df.abstract[idx]))
        lines.append("")
    return "\n".join(lines)


def build_daily_payload(date_diff=None):
    (
        important_people,
        _important_firsts,
        _important_lasts,
        important_firsts_lower,
        important_lasts_lower,
        keywords_lower,
    ) = load_watchlists()

    if date_diff is not None:
        tomorrow_date = datetime.date.today() + datetime.timedelta(days=date_diff)
    else:
        tomorrow_date = datetime.date.today() + datetime.timedelta(days=1)
    date_str = tomorrow_date.strftime("%Y-%m-%d")

    try:
        df = fetch_papers_for_date(date_str)
    except Exception as ex:
        logger.exception("Error fetching papers for date %s: %s", date_str, ex)
        raise

    imp_author_idx, imp_keyword_idx, _other_idx, which_authors = classify_papers(
        df,
        important_people,
        important_firsts_lower,
        important_lasts_lower,
        keywords_lower,
    )
    msg_text = build_message(date_str, df, imp_author_idx, imp_keyword_idx, which_authors)

    thread_text = build_abstract_thread_message(
        date_str, df, imp_author_idx, imp_keyword_idx)

    return {
        "date_str": date_str,
        "df": df,
        "imp_author_idx": imp_author_idx,
        "imp_keyword_idx": imp_keyword_idx,
        "which_authors": which_authors,
        "msg_text": msg_text,
        "thread_text": thread_text,
    }


def post_to_slack(slack_client, msg_text, thread_ts=None):
    try:
        response = slack_client.chat_postMessage(
            channel=CHANNEL,
            text=msg_text,
            thread_ts=thread_ts,
        )
        return response
    except SlackApiError as e:
        assert e.response["ok"] is False
        assert e.response["error"]
        logger.exception("Error posting to Slack: %s", e.response["error"])
        return None

    
def main_ret_message(date_diff=None):
    payload = build_daily_payload(date_diff=date_diff)
    return payload["msg_text"], payload["thread_text"]

def main_slack_send(date_diff=None):
    slackclient = WebClient(token=SLACK_TOKEN)
    msg_txt, thread_text = main_ret_message(date_diff=date_diff)
    parent_response = post_to_slack(slackclient, msg_txt)
    if parent_response is None:
        return

    if thread_text:
        post_to_slack(slackclient, thread_text, thread_ts=parent_response["ts"])
