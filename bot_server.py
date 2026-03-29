import datetime
import logging
import time

import slackbot_daily_arxiv as bot

TIMES = ["21:30"]  # Times to send messages (24-hour format)
SEND_WINDOW_SECONDS = 10 * 60
RETRY_DELAY_SECONDS = 60
SECONDS_PER_DAY = 24 * 60 * 60
ALLOWED_WEEKDAYS = {6, 0, 1, 2, 3}  # Sunday(6) through Thursday(3)

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


def get_next_send_index(now, sent_today):
    now_seconds = seconds_since_midnight(now)
    for idx, scheduled_seconds in enumerate(SCHEDULE_SECONDS):
        if sent_today[idx]:
            continue
        due_end = min(SECONDS_PER_DAY, scheduled_seconds + SEND_WINDOW_SECONDS)
        if scheduled_seconds <= now_seconds < due_end:
            return idx
    return None


def schedule_seconds_to_time(scheduled_seconds):
    hour = scheduled_seconds // 3600
    minute = (scheduled_seconds % 3600) // 60
    second = scheduled_seconds % 60
    return datetime.time(hour=hour, minute=minute, second=second)


def get_next_send_datetime(now, sent_today):
    for day_offset in range(0, 8):
        candidate_date = (now + datetime.timedelta(days=day_offset)).date()
        if not is_allowed_run_day(candidate_date):
            continue

        day_candidates = []
        for idx, scheduled_seconds in enumerate(SCHEDULE_SECONDS):
            if day_offset == 0 and sent_today[idx]:
                continue

            candidate_dt = datetime.datetime.combine(
                candidate_date,
                schedule_seconds_to_time(scheduled_seconds),
            )
            if day_offset == 0 and candidate_dt <= now:
                continue

            day_candidates.append(candidate_dt)

        if day_candidates:
            return min(day_candidates)

    return None


def is_allowed_run_day(current_date):
    return current_date.weekday() in ALLOWED_WEEKDAYS


def seconds_until_next_allowed_day(now):
    for day_offset in range(1, 8):
        next_date = (now + datetime.timedelta(days=day_offset)).date()
        if is_allowed_run_day(next_date):
            next_start = datetime.datetime.combine(next_date, datetime.time.min)
            return max(1, int((next_start - now).total_seconds()))
    return SECONDS_PER_DAY


def run_scheduler():
    sent_today = [False] * len(SCHEDULE_SECONDS)
    active_date = datetime.date.today()

    while True:
        now = datetime.datetime.now()
        if now.date() != active_date:
            active_date = now.date()
            sent_today = [False] * len(SCHEDULE_SECONDS)
            logger.info("New day detected. Reset daily send flags for %s.", active_date.isoformat())

        if is_allowed_run_day(now.date()):
            send_idx = get_next_send_index(now, sent_today)
        else:
            send_idx = None

        if send_idx is not None:
            try:
                bot.main_slack_send()
                sent_today[send_idx] = True
                logger.info("Posted daily arXiv summary for slot %s.", TIMES[send_idx])
            except Exception:
                logger.exception("Failed to send message for slot %s.", TIMES[send_idx])
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            continue

        next_send_dt = get_next_send_datetime(now, sent_today)
        if next_send_dt is not None:
            sleep_seconds = max(1, int((next_send_dt - now).total_seconds()))
            logger.info(
                "No sends due now. Sleeping for %s seconds (next slot at %s).",
                sleep_seconds,
                next_send_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            time.sleep(sleep_seconds)
        else:
            sleep_seconds = seconds_until_next_allowed_day(now)
            logger.info(
                "No future slot found from current schedule. Sleeping for %s seconds.",
                sleep_seconds,
            )
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    run_scheduler()
