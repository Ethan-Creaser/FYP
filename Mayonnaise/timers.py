import utime


def now_ms():
    return utime.ticks_ms()


def elapsed_ms(now, then):
    return utime.ticks_diff(now, then)


def due(now, last_time, interval_ms):
    if last_time is None:
        return True
    return elapsed_ms(now, last_time) >= interval_ms
