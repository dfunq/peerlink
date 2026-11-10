"""Cron for peerlink's schedules: the five fields (minute hour day-of-month month weekday), read as cron reads
them, and the next minute one matches. Times are this machine's local time."""

CRON_NAMES = ({}, {}, {}, {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())},
              {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())})
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def parse_cron(expr):
    """The five fields of a cron (minute hour day-of-month month weekday) as sets; ValueError when malformed."""
    parts = str(expr or "").lower().split()
    if len(parts) != 5:
        raise ValueError("a cron has five fields: minute hour day-of-month month weekday, like 0 9 * * 1-5")
    fields = []
    for part, (lo, hi), names in zip(parts, CRON_RANGES, CRON_NAMES):
        values = set()
        for item in part.split(","):
            rng, slash, step = item.partition("/")
            num = lambda v: names[v] if v in names else int(v)
            try:
                a, b = (lo, hi) if rng == "*" else (num(rng.split("-")[0]), num(rng.split("-")[1])) if "-" in rng else (num(rng), hi if slash else num(rng))
                step = int(step) if slash else 1
            except (ValueError, IndexError):
                raise ValueError(f"can't read {item!r} in {expr!r}")
            if not (lo <= a <= b <= hi) or step < 1:
                raise ValueError(f"{item!r} is out of range in {expr!r}")
            values.update(range(a, b + 1, step))
        fields.append(values)
    if 7 in fields[4]:
        fields[4] = (fields[4] - {7}) | {0}
    # As cron reads it: with both day fields restricted, a day matching either runs.
    fields.append((parts[2] == "*", parts[4] == "*"))
    return fields


def cron_next(expr, after):
    """The first minute after `after` (epoch seconds) the cron matches, in local time."""
    import datetime as dt
    minute, hour, dom, month, dow, (any_dom, any_dow) = parse_cron(expr)
    t = dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(100000):
        wd = t.isoweekday() % 7
        day_ok = (t.day in dom and wd in dow) if any_dom or any_dow else (t.day in dom or wd in dow)
        if t.month not in month:
            t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
        elif not day_ok:
            t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
        elif t.hour not in hour:
            t = (t + dt.timedelta(hours=1)).replace(minute=0)
        elif t.minute not in minute:
            t += dt.timedelta(minutes=1)
        else:
            return t.timestamp()
    raise ValueError(f"{expr!r} never runs")
