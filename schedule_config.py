"""Delivery schedule for the container's scheduler and the dashboard header.

Times are interpreted in the container's TZ (America/Los_Angeles by default).
"""

import datetime

TIMES = ["21:30"]  # Send times, 24-hour HH:MM.
SEND_WINDOW_SECONDS = 2 * 60 * 60  # Retry the 21:30 slot until 23:30.
RETRY_DELAY_SECONDS = 60
SECONDS_PER_DAY = 24 * 60 * 60
ALLOWED_WEEKDAYS = {6, 0, 1, 2, 3}  # Sunday(6) through Thursday(3)


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
