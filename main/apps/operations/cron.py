"""Standard 5-field cron expressions: minute hour day-of-month month day-of-week.

Supports *, lists (1,15), ranges (1-5), steps (*/15, 0-30/10), month and weekday names,
and the usual shortcuts (@hourly, @daily, @weekly, @monthly, @yearly). As in cron, when
both day-of-month and day-of-week are restricted, a day matching either one runs.

Times are wall-clock in the dashboard's TIME_ZONE. Across DST changes: a time skipped by
the spring change doesn't run that day; a time repeated in autumn runs once.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7)]
NAMES = {
    "month": {
        m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)
    },
    "weekday": {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())},
}
SHORTCUTS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
             "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}  # fmt: skip


class CronError(ValueError):
    pass


def _value(token: str, name: str, lo: int, hi: int) -> int:
    token = token.lower()
    if token in NAMES.get(name, {}):
        return NAMES[name][token]
    if not token.isdigit():
        raise CronError(f"{name}: {token!r} isn't a number")
    value = int(token)
    if not lo <= value <= hi:
        raise CronError(f"{name}: {value} is outside {lo}-{hi}")
    return value


def _field(text: str, name: str, lo: int, hi: int) -> set[int]:
    values = set()
    for part in text.split(","):
        base, _, step = part.partition("/")
        step_n = 1
        if step:
            if not step.isdigit() or int(step) == 0:
                raise CronError(f"{name}: bad step {step!r}")
            step_n = int(step)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _value(a, name, lo, hi), _value(b, name, lo, hi)
            if start > end:
                raise CronError(f"{name}: range {base} runs backwards")
        else:
            start = _value(base, name, lo, hi)
            end = hi if step else start
        values.update(range(start, end + 1, step_n))
    if name == "weekday" and 7 in values:  # 7 is also Sunday
        values.discard(7)
        values.add(0)
    return values


class Cron:
    def __init__(self, expression: str):
        expr = SHORTCUTS.get(expression.strip().lower(), expression.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("use five fields: minute hour day month weekday (e.g. 0 3 * * *)")
        self.expression = expr
        self.minute, self.hour, self.day, self.month, self.weekday = (
            _field(text, name, lo, hi) for text, (name, lo, hi) in zip(parts, FIELDS, strict=True)
        )
        self.day_any, self.weekday_any = parts[2] == "*", parts[4] == "*"

    def _day_matches(self, d) -> bool:
        if d.month not in self.month:
            return False
        dom, dow = d.day in self.day, (d.isoweekday() % 7) in self.weekday
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return dow
        if self.weekday_any:
            return dom
        return dom or dow

    def next_after(self, after: datetime, tz: str) -> datetime:
        """First run strictly after `after` (aware), as an aware datetime."""
        zone = ZoneInfo(tz)
        local = after.astimezone(zone)
        day = local.date()
        for _ in range(366 * 5):
            if self._day_matches(day):
                for h in sorted(self.hour):
                    for m in sorted(self.minute):
                        naive = datetime.combine(day, time(h, m))
                        candidate = naive.replace(tzinfo=zone)
                        # Skipped by a spring-forward change: the wall time doesn't exist.
                        if (
                            candidate.astimezone(ZoneInfo("UTC"))
                            .astimezone(zone)
                            .replace(tzinfo=None)
                            != naive
                        ):
                            continue
                        if candidate > after:
                            return candidate
            day += timedelta(days=1)
        raise CronError("this schedule never runs")


def validate(expression: str) -> str:
    return Cron(expression).expression
