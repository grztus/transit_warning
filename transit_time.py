import datetime
import time

import pytz


def port_timestamp_to_utc(timestamp, port):
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=pytz.utc)
    if port == 30003:
        return timestamp + datetime.timedelta(hours=time.altzone / 60 / 60)
    return timestamp
