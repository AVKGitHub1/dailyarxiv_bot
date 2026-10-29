"""arXiv fetching, watchlist matching, and Slack digest formatting for the dashboard."""

import datetime
import logging
import os
import re
from pathlib import Path
from time import sleep

import pandas as pd
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

import arxiv_source as arxivscraper

logger = logging.getLogger(__name__)

MAX_DISPLAY_AUTHORS = 15

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yml"


def make_slack_client(token):
    """One client configuration for every send, so no path silently diverges."""
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


def strip_parentheticals(name):
    """Drop notes such as "(MIT)" so only the name itself is matched."""
    return re.sub(r"\s*\([^)]*\)", "", name)


def load_watchlists(important_people, keywords):
    entries = []
    for person in important_people:
        person = person.strip()
        if not person:
            continue
        parts = strip_parentheticals(person).split()
        if not parts:
            # A line such as "(Google)" has no name to match. Skip it rather
            # than let one bad entry fail every regeneration and scheduled run.
            logger.warning("Skipping watchlist entry without a name outside parentheses: %r", person)
            continue
        entries.append((person, parts))
    keywords = [kw.strip() for kw in keywords if kw.strip()]
    important_people = [person for person, _parts in entries]
    important_firsts = [parts[0] for _person, parts in entries]
    important_lasts = [parts[-1] for _person, parts in entries]

    return (
        important_people,
        important_firsts,
        important_lasts,
        [first.lower() for first in important_firsts],
        [last.lower() for last in important_lasts],
        [kw.lower() for kw in keywords],
    )


def fetch_papers_for_date(date_str, config, *, date_from=None):
    """Fetch an inclusive OAI metadata date range, defaulting to a single day."""
    date_str = datetime.date.fromisoformat(date_str).isoformat()
    date_from = datetime.date.fromisoformat(date_from or date_str).isoformat()
    if date_from > date_str:
        raise ValueError("The first paper date must not be after the last paper date.")
    frames = []
    for category, subcategories in zip(config["categories"], config["subcat"]):
        if not subcategories:
            scraper = arxivscraper.Scraper(
                category=category,
                date_from=date_from,
                date_until=date_str,
                t=5,
            )
        else:
            scraper = arxivscraper.Scraper(
                category=category,
                date_from=date_from,
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


def slack_escape(text):
    """Escape the three characters Slack reads as markup, so text cannot form links or mentions."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_abstract_text(text):
    return " ".join(str(text).split())


def is_lowercased(text):
    """True for text the original arxivscraper lowercased.

    arxiv_source keeps arXiv's own capitalization, which must not be rewritten.
    The case restoration below applies only to lowercased text, for example
    papers saved before arxiv_source existed.
    """
    return any(ch.isalpha() for ch in text) and text == text.lower()


def to_sentence_case(text):
    """Capitalize the first letter and each letter that starts a new sentence."""
    chars = list(normalize_abstract_text(text))
    capitalize_next = True
    after_terminator = False
    for i, ch in enumerate(chars):
        if capitalize_next and ch.isalpha():
            chars[i] = ch.upper()
            capitalize_next = False
        if ch in ".!?":
            after_terminator = True
        elif ch in ")]\"'":
            pass  # A sentence may end inside brackets or quotes: "(see fig. 1.) next".
        elif ch.isspace():
            # Only ". " ends a sentence; "3.5" and "arxiv.org" do not.
            capitalize_next = capitalize_next or after_terminator
            after_terminator = False
        else:
            after_terminator = False
    return "".join(chars)


def display_title(title):
    title = normalize_abstract_text(title)
    return title[:1].upper() + title[1:] if is_lowercased(title) else title


def display_abstract(abstract):
    abstract = normalize_abstract_text(abstract)
    return to_sentence_case(abstract) if is_lowercased(abstract) else abstract


def display_author(author):
    author = normalize_abstract_text(author)
    if is_lowercased(author):
        return " ".join(name.capitalize() for name in author.split())
    return author


def format_authors(authors):
    if len(authors) > MAX_DISPLAY_AUTHORS:
        return "MANY AUTHORS"
    return ", ".join(display_author(author) for author in authors)


def arxiv_link(identifier):
    return "www.arxiv.org/abs/" + slack_escape(identifier)


def build_message(date, df, imp_author_idx, imp_keyword_idx, which_authors):
    lines = []
    lines.append(f"*Papers for {date}*:")
    lines.append("-----------------------")
    if len(imp_author_idx) > 0:
        lines.append("")
        lines.append("*Important by author*: ")
        for idx in imp_author_idx:
            lines.append("*Title:* " + slack_escape(display_title(df.title[idx])))
            lines.append("*Selected for:* " + slack_escape(which_authors[idx][:-2]))
            lines.append("*Authors:* " + slack_escape(format_authors(df.authors[idx])))
            lines.append("*Link:* " + arxiv_link(df.id[idx]))
            lines.append("")
    else:
        lines.append("")
        lines.append("No papers found with specified authors!")
    lines.append("-----------------------")

    if len(imp_keyword_idx) > 0:
        lines.append("")
        lines.append("*Important by keywords:* ")
        for idx in imp_keyword_idx:
            lines.append("*Title:* " + slack_escape(display_title(df.title[idx])))
            lines.append("*Authors:* " + slack_escape(format_authors(df.authors[idx])))
            lines.append("*Link:* " + arxiv_link(df.id[idx]))
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
        lines.append("*Title:* " + slack_escape(display_title(df.title[idx])))
        lines.append("*Link:* " + arxiv_link(df.id[idx]))
        lines.append("*Abstract:* " + slack_escape(display_abstract(df.abstract[idx])))
        lines.append("")
    return "\n".join(lines)


def paper_date_label(date_str, date_from=None):
    return f"{date_from} through {date_str}" if date_from and date_from != date_str else date_str


def build_daily_payload(*, config, watchlists, target_date, date_from=None):
    """Fetch and classify one date range; the dashboard reviews the result before sending."""
    (
        important_people,
        _important_firsts,
        _important_lasts,
        important_firsts_lower,
        important_lasts_lower,
        keywords_lower,
    ) = load_watchlists(*watchlists)

    date_str = datetime.date.fromisoformat(target_date).isoformat()
    try:
        df = fetch_papers_for_date(date_str, config, date_from=date_from)
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
    return {
        "date_str": date_str,
        "date_from": date_from or date_str,
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


def payload_from_papers(date_str, papers, *, date_from=None):
    """Format an already reviewed selection without fetching arXiv again."""
    df = pd.DataFrame(papers)
    author_idx = [idx for idx, paper in enumerate(papers) if paper["selection"] == "author"]
    keyword_idx = [idx for idx, paper in enumerate(papers) if paper["selection"] != "author"]
    which_authors = [paper.get("selected_for", "") + ", " for paper in papers]
    date_label = paper_date_label(date_str, date_from)
    return {
        "msg_text": build_message(date_label, df, author_idx, keyword_idx, which_authors),
        "thread_text": build_abstract_thread_message(date_label, df, author_idx, keyword_idx),
    }


def post_to_slack(slack_client, msg_text, thread_ts=None, *, channel):
    try:
        return slack_client.chat_postMessage(channel=channel, text=msg_text, thread_ts=thread_ts)
    except SlackApiError as e:
        logger.error("Slack rejected the message: %s", e.response.get("error", "unknown error"))
        raise
