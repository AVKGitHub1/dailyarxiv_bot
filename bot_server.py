import datetime
import logging
import time

import slackbot_daily_arxiv as bot

TIMES = ["08:30"]  # Times to send messages (24-hour format)
SEND_WINDOW_SECONDS = 10 * 60
FAR_AWAY_THRESHOLD_SECONDS = 30 * 60
NEAR_TIME_POLL_SECONDS = 30
RETRY_DELAY_SECONDS = 60
SECONDS_PER_DAY = 24 * 60 * 60

# setup logging with date and time in the format YYYY-MM-DD HH:MM:SS
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def parse_schedule_seconds(times):
    schedule_seconds = []
    for t in times:
        parsed = datetime.datetime.strptime(t, "%H:%M").time()
        schedule_seconds.append(parsed.hour * 3600 + parsed.minute * 60)
    if not schedule_seconds:
        raise ValueError("TIMES must contain at least one HH:MM entry.")
    return schedule_seconds


SCHEDULE_SECONDS = parse_schedule_seconds(TIMES)


def seconds_since_midnight(now):
    return now.hour * 3600 + now.minute * 60 + now.second


def circular_time_distance_seconds(a, b):
    raw_diff = abs(a - b)
    return min(raw_diff, SECONDS_PER_DAY - raw_diff)


def get_next_send_index(now, sent_today):
    now_seconds = seconds_since_midnight(now)
    for idx, scheduled_seconds in enumerate(SCHEDULE_SECONDS):
        if sent_today[idx]:
            continue
        if circular_time_distance_seconds(now_seconds, scheduled_seconds) < SEND_WINDOW_SECONDS:
            return idx
    return None


def nearest_schedule_distance(now):
    now_seconds = seconds_since_midnight(now)
    return min(
        circular_time_distance_seconds(now_seconds, scheduled_seconds)
        for scheduled_seconds in SCHEDULE_SECONDS
    )


def run_scheduler():
    sent_today = [False] * len(SCHEDULE_SECONDS)
    active_date = datetime.date.today()

    while True:
        now = datetime.datetime.now()
        if now.date() != active_date:
            active_date = now.date()
            sent_today = [False] * len(SCHEDULE_SECONDS)
            logger.info("New day detected. Reset daily send flags for %s.", active_date.isoformat())

        send_idx = get_next_send_index(now, sent_today)
        if send_idx is not None:
            try:
                bot.main_slack_send()
                sent_today[send_idx] = True
                logger.info("Posted daily arXiv summary for slot %s.", TIMES[send_idx])
            except Exception:
                logger.exception("Failed to send message for slot %s.", TIMES[send_idx])
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            time.sleep(SEND_WINDOW_SECONDS)
            continue

        distance = nearest_schedule_distance(now)
        if distance > FAR_AWAY_THRESHOLD_SECONDS:
            sleep_seconds = max(1, distance - SEND_WINDOW_SECONDS)
            logger.info(
                "No sends due soon. Sleeping for %s seconds (nearest slot in %s seconds).",
                sleep_seconds,
                distance,
            )
            time.sleep(sleep_seconds)
        else:
            time.sleep(NEAR_TIME_POLL_SECONDS)


if __name__ == "__main__":
    run_scheduler()
