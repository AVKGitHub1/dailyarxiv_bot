import datetime
import os
import re
from pathlib import Path
from time import sleep

import arxiv_source as arxivscraper
import pandas as pd
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

#setup logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MAX_DISPLAY_AUTHORS = 15

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yml"


def make_slack_client(token):
    """One client configuration for every sender, so no path silently diverges."""
    client = WebClient(token=token, timeout=30)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client


def load_config(path=None):
    path = path or os.environ.get("ARXIV_CONFIG", CONFIG_PATH)
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


def load_lines(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.exception("Error reading lines from %s: %s", path, e)
        return []


def load_watchlists(important_people=None, keywords=None):
    if important_people is None:
        important_people = load_lines(BASE_DIR / "important_people.txt")
    if keywords is None:
        keywords = load_lines(BASE_DIR / "keywords.txt")
    important_people = [name.strip() for name in important_people if name.strip()]
    keywords = [kw.strip() for kw in keywords if kw.strip()]
    names = [re.sub(r"\s*\([^)]*\)", "", name).split() for name in important_people]
    important_firsts = [name[0] for name in names]
    important_lasts = [name[-1] for name in names]
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


def fetch_papers_for_date(date_str, config=None):
    config = config if config is not None else load_config()
    frames = []
    for category, subcategories in zip(config["categories"], config["subcat"]):
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
        if not isinstance(output, list):
            raise RuntimeError("arXiv returned an invalid response; please try again later.")
        frames.append(pd.DataFrame(output, columns=config["cols"]))
        sleep(3)

    if frames:
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.DataFrame([], columns=config["cols"])

    return df.drop_duplicates(subset="id").reset_index(drop=True)


def match_author(first_name, last_name, important_firsts, important_lasts):
    if not first_name:
        return None
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
        authors = [name for name in df.authors[paper_id] if name.split()]
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
    if len(authors) > MAX_DISPLAY_AUTHORS:
        return "MANY AUTHORS"
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


def build_daily_payload(date_diff=None, *, config=None, watchlists=None, target_date=None):
    (
        important_people,
        _important_firsts,
        _important_lasts,
        important_firsts_lower,
        important_lasts_lower,
        keywords_lower,
    ) = load_watchlists(*(watchlists or (None, None)))

    if target_date is not None:
        tomorrow_date = datetime.date.fromisoformat(target_date)
    elif date_diff is not None:
        tomorrow_date = datetime.date.today() + datetime.timedelta(days=date_diff)
    else:
        tomorrow_date = datetime.date.today() + datetime.timedelta(days=1)
    date_str = tomorrow_date.strftime("%Y-%m-%d")

    try:
        df = fetch_papers_for_date(date_str, config=config)
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
        "papers": [
            {
                "id": str(df.id[idx]),
                "title": normalize_abstract_text(df.title[idx]),
                "abstract": normalize_abstract_text(df.abstract[idx]),
                "authors": list(df.authors[idx]),
                "selection": "author" if idx in imp_author_idx else "keyword",
                "selected_for": which_authors[idx].rstrip(", "),
            }
            for idx in imp_author_idx + imp_keyword_idx
        ],
    }


def payload_from_papers(date_str, papers):
    """Format an already reviewed selection without fetching arXiv again."""
    df = pd.DataFrame(papers)
    author_idx = [idx for idx, paper in enumerate(papers) if paper["selection"] == "author"]
    keyword_idx = [idx for idx, paper in enumerate(papers) if paper["selection"] != "author"]
    which_authors = [paper.get("selected_for", "") + ", " for paper in papers]
    return {
        "msg_text": build_message(date_str, df, author_idx, keyword_idx, which_authors),
        "thread_text": build_abstract_thread_message(date_str, df, author_idx, keyword_idx),
    }


def post_to_slack(slack_client, msg_text, thread_ts=None, *, channel=None):
    try:
        response = slack_client.chat_postMessage(
            channel=channel if channel is not None else load_config()["channel"],
            text=msg_text,
            thread_ts=thread_ts,
        )
        return response
    except SlackApiError as e:
        assert e.response["ok"] is False
        assert e.response["error"]
        logger.exception("Error posting to Slack: %s", e.response["error"])
        raise

    
def main_ret_message(date_diff=None):
    payload = build_daily_payload(date_diff=date_diff)
    return payload["msg_text"], payload["thread_text"]

def main_slack_send(date_diff=None):
    config = load_config()
    slackclient = make_slack_client(config["slack_token"])
    payload = build_daily_payload(date_diff=date_diff, config=config)
    parent_response = post_to_slack(slackclient, payload["msg_text"], channel=config["channel"])

    if payload["thread_text"]:
        post_to_slack(slackclient, payload["thread_text"], thread_ts=parent_response["ts"], channel=config["channel"])
