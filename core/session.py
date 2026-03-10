import pytz
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def get_market_session(current_time=None):
    """
    Determines the current US market session based on NY time.
    Returns: 'PRE', 'RTH', 'POST', or 'CLOSED'
    """
    ny_tz = pytz.timezone("US/Eastern")
    if current_time:
        if hasattr(current_time, "tzinfo") and current_time.tzinfo is None:
            now_ny = ny_tz.localize(current_time)
        else:
            now_ny = current_time.astimezone(ny_tz)
    else:
        now_ny = datetime.now(ny_tz)

    if now_ny.weekday() >= 5:
        return "CLOSED"

    t = now_ny.time()
    pre_start = datetime.strptime("04:00", "%H:%M").time()
    rth_start = datetime.strptime("09:30", "%H:%M").time()
    rth_end = datetime.strptime("16:00", "%H:%M").time()

    if pre_start <= t < rth_start:
        if t < datetime.strptime("04:05", "%H:%M").time():
            return "CLOSED"
        return "PRE"
    elif rth_start <= t < rth_end:
        return "RTH"
    return "CLOSED"


def is_extended_hours(current_time=None):
    return get_market_session(current_time) in ["PRE", "POST"]


def is_rth(current_time=None):
    return get_market_session(current_time) == "RTH"
