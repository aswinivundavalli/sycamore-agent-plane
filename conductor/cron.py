"""A small, dependency-free cron expression parser for scheduled runs.

Time zone
---------
**Everything here is UTC.** Expressions are evaluated against UTC wall-clock
time, :meth:`Cron.matches` treats a naive ``datetime`` as UTC (an aware one is
converted to UTC first) and :meth:`Cron.next_after` takes and returns unix
timestamps. There is no local-time or DST handling: ``0 9 * * *`` fires at
09:00 UTC every day of the year.

Syntax
------
Five whitespace-separated fields::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-7 (0 and 7 are Sunday)

Each field is a comma-separated list of items, and each item is one of

* ``*`` - every value of the field;
* ``n`` - a single value;
* ``a-b`` - an inclusive range (``a <= b``; reversed ranges are rejected);
* ``*/s``, ``a-b/s``, ``a/s`` - every ``s``-th value of the whole field, of the
  range, or from ``a`` to the field's maximum (``s >= 1``).

Months accept ``jan``..``dec`` and weekdays ``sun``..``sat`` (case-insensitive)
anywhere a number is allowed, including in ranges (``mon-fri``). The macros
``@hourly``, ``@daily``/``@midnight``, ``@weekly`` (Sunday 00:00),
``@monthly`` and ``@yearly``/``@annually`` stand for their usual expansions.

Day semantics (Vixie cron)
--------------------------
When BOTH day-of-month and day-of-week are restricted, a day matches if EITHER
field matches (``0 0 1 * mon`` = the 1st of every month *and* every Monday).
When at least one of them is unrestricted, both must match (which reduces to
the restricted one). As in Vixie cron, a field counts as unrestricted when it
starts with ``*`` - so ``*/2`` in day-of-month is "unrestricted" for this rule
even though it only allows odd days.

Search
------
:meth:`Cron.next_after` does not iterate minute by minute. It walks the
calendar field by field: if the month is not allowed it jumps to the first day
of the next allowed month; within a month it scans day numbers with integer
arithmetic for the first allowed day; then it jumps to the next allowed hour
and the next allowed minute (bisecting the sorted value tuples). Rare
schedules such as ``0 0 29 2 *`` are therefore found in a few dozen steps.
Expressions that can never match (``0 0 31 2 *``) raise :class:`CronError`
once the search horizon (:data:`SEARCH_YEARS`) is exhausted.
"""

from __future__ import annotations

import bisect
import calendar
import math
from datetime import datetime, timezone

__all__ = ["CronError", "Cron", "parse", "SEARCH_YEARS"]

#: How far ahead :meth:`Cron.next_after` searches before declaring an
#: expression impossible. Five years per the original design, widened to eight
#: because that is the longest gap between two Feb 29ths (e.g. 2096 -> 2104,
#: 2100 not being a leap year); any expression that matches at all matches
#: within this horizon.
SEARCH_YEARS = 8


class CronError(ValueError):
    """An invalid or unsatisfiable cron expression."""


MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_WEEKDAYS = {name: i for i, name in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

# (name, min, max, value names)
_FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _MONTHS),
    ("day-of-week", 0, 7, _WEEKDAYS),
)


def _err(expr: str, problem: str) -> CronError:
    return CronError(f"invalid cron expression {expr!r}: {problem}")


def _value(expr: str, field: str, text: str, lo: int, hi: int,
           names: dict[str, int] | None) -> int:
    if names is not None and text.lower() in names:
        return names[text.lower()]
    if not text.isdigit():
        hint = ""
        if names is not None:
            hint = f" (expected a number or one of {', '.join(names)})"
        raise _err(expr, f"{field} field: {text!r} is not a valid value{hint}")
    n = int(text)
    if not lo <= n <= hi:
        raise _err(expr, f"{field} field: {n} is out of range {lo}-{hi}")
    return n


def _parse_field(expr: str, spec: tuple, text: str) -> set[int]:
    field, lo, hi, names = spec
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise _err(expr, f"{field} field {text!r} has an empty list item")
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit():
                raise _err(expr, f"{field} field: step {step_text!r} in "
                                 f"{item!r} is not a positive number")
            step = int(step_text)
            if step == 0:
                raise _err(expr, f"{field} field: step of 0 in {item!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            if not a or not b:
                raise _err(expr, f"{field} field: malformed range {base!r}")
            start = _value(expr, field, a, lo, hi, names)
            end = _value(expr, field, b, lo, hi, names)
            if start > end:
                raise _err(expr, f"{field} field: reversed range {base!r} "
                                 f"({start} > {end})")
        elif base == "":
            raise _err(expr, f"{field} field: missing value in {item!r}")
        else:
            start = _value(expr, field, base, lo, hi, names)
            end = hi if slash else start
        values.update(range(start, end + 1, step))
    return values


class Cron:
    """A parsed cron expression. Build one with :func:`parse`."""

    __slots__ = ("expr", "minutes", "hours", "days", "months", "weekdays",
                 "dom_restricted", "dow_restricted")

    def __init__(self, expr: str, minutes, hours, days, months, weekdays,
                 dom_restricted: bool, dow_restricted: bool):
        self.expr = expr
        self.minutes: tuple[int, ...] = tuple(sorted(minutes))
        self.hours: tuple[int, ...] = tuple(sorted(hours))
        self.days: frozenset[int] = frozenset(days)
        self.months: tuple[int, ...] = tuple(sorted(months))
        #: Sunday = 0 .. Saturday = 6 (7 is folded into 0).
        self.weekdays: frozenset[int] = frozenset(weekdays)
        self.dom_restricted = dom_restricted
        self.dow_restricted = dow_restricted

    def __str__(self) -> str:
        return self.expr

    def __repr__(self) -> str:
        return f"Cron({self.expr!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Cron):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def _key(self):
        return (self.minutes, self.hours, self.days, self.months,
                self.weekdays, self.dom_restricted, self.dow_restricted)

    # -- matching ---------------------------------------------------------

    def _day_ok(self, day: int, weekday: int) -> bool:
        """``weekday`` is cron-style: Sunday = 0."""
        dom = day in self.days
        dow = weekday in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow

    def matches(self, dt: datetime) -> bool:
        """Whether the minute containing ``dt`` matches (naive = UTC)."""
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return (dt.minute in self.minutes and dt.hour in self.hours
                and dt.month in self.months
                and self._day_ok(dt.day, (dt.weekday() + 1) % 7))

    # -- searching --------------------------------------------------------

    def next_after(self, ts: float) -> float:
        """The first matching minute strictly after unix timestamp ``ts``.

        The result has zero seconds. Raises :class:`CronError` when nothing
        matches within :data:`SEARCH_YEARS` years.
        """
        start = (math.floor(ts / 60) + 1) * 60
        t = datetime.fromtimestamp(start, timezone.utc)
        y, mo, d, h, mi = t.year, t.month, t.day, t.hour, t.minute
        last_year = y + SEARCH_YEARS
        months, hours, minutes = self.months, self.hours, self.minutes
        while y <= last_year:
            # Month: jump to the first day of the next allowed month.
            if mo not in months:
                i = bisect.bisect_left(months, mo)
                if i == len(months):
                    y, mo = y + 1, months[0]
                else:
                    mo = months[i]
                d, h, mi = 1, 0, 0
                continue
            # Day: scan the rest of this month with integer arithmetic.
            first_wd, mdays = calendar.monthrange(y, mo)  # Monday = 0
            found = 0
            for dd in range(d, mdays + 1):
                if self._day_ok(dd, (first_wd + dd) % 7):  # == (wd+1)%7
                    found = dd
                    break
            if not found:
                mo += 1
                if mo > 12:
                    y, mo = y + 1, 1
                d, h, mi = 1, 0, 0
                continue
            if found != d:
                d, h, mi = found, 0, 0
            # Hour.
            i = bisect.bisect_left(hours, h)
            if i == len(hours):
                d, h, mi = d + 1, 0, 0
                continue
            if hours[i] != h:
                h, mi = hours[i], 0
            # Minute.
            i = bisect.bisect_left(minutes, mi)
            if i == len(minutes):
                h, mi = h + 1, 0
                continue
            return float(calendar.timegm((y, mo, d, h, minutes[i], 0)))
        raise _err(self.expr, f"no matching time within {SEARCH_YEARS} years")


def parse(expr: str) -> Cron:
    """Parse a 5-field cron expression or ``@macro``; raise :class:`CronError`."""
    if not isinstance(expr, str):
        raise CronError(f"invalid cron expression {expr!r}: not a string")
    text = expr.strip()
    if text.startswith("@"):
        expanded = MACROS.get(text.lower())
        if expanded is None:
            raise _err(expr, f"unknown macro {text!r} (known: "
                             f"{', '.join(MACROS)})")
        fields = expanded.split()
    else:
        fields = text.split()
    if len(fields) != 5:
        raise _err(expr, f"expected 5 fields (minute hour day-of-month month "
                         f"day-of-week), got {len(fields)}")
    sets = [_parse_field(expr, spec, f) for spec, f in zip(_FIELDS, fields)]
    weekdays = {0 if v == 7 else v for v in sets[4]}
    return Cron(expr, sets[0], sets[1], sets[2], sets[3], weekdays,
                dom_restricted=not fields[2].startswith("*"),
                dow_restricted=not fields[4].startswith("*"))
