"""Tests for conductor.cron (UTC-only cron parsing and next-run search)."""

from __future__ import annotations

import calendar
import time
import unittest
from datetime import datetime, timedelta, timezone

from conductor import cron
from conductor.cron import Cron, CronError, parse


def ts(*args) -> float:
    """Unix timestamp of a UTC wall-clock time."""
    return float(calendar.timegm(datetime(*args).timetuple()))


def utc(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)


class ParseSyntaxTests(unittest.TestCase):
    def test_star(self):
        c = parse("* * * * *")
        self.assertEqual(c.minutes, tuple(range(60)))
        self.assertEqual(c.hours, tuple(range(24)))
        self.assertEqual(c.days, frozenset(range(1, 32)))
        self.assertEqual(c.months, tuple(range(1, 13)))
        self.assertEqual(c.weekdays, frozenset(range(7)))

    def test_numbers(self):
        c = parse("5 4 3 2 1")
        self.assertEqual((c.minutes, c.hours, c.months), ((5,), (4,), (2,)))
        self.assertEqual(c.days, {3})
        self.assertEqual(c.weekdays, {1})

    def test_range(self):
        self.assertEqual(parse("10-15 * * * *").minutes, (10, 11, 12, 13, 14, 15))
        self.assertEqual(parse("7-7 * * * *").minutes, (7,))

    def test_star_step(self):
        self.assertEqual(parse("*/15 * * * *").minutes, (0, 15, 30, 45))
        self.assertEqual(parse("* */6 * * *").hours, (0, 6, 12, 18))
        self.assertEqual(parse("* * */10 * *").days, {1, 11, 21, 31})

    def test_range_step(self):
        self.assertEqual(parse("10-30/10 * * * *").minutes, (10, 20, 30))
        self.assertEqual(parse("1-10/4 * * * *").minutes, (1, 5, 9))

    def test_start_step_runs_to_max(self):
        self.assertEqual(parse("50/3 * * * *").minutes, (50, 53, 56, 59))
        self.assertEqual(parse("* 20/2 * * *").hours, (20, 22))
        self.assertEqual(parse("* * * 6/3 *").months, (6, 9, 12))

    def test_lists(self):
        self.assertEqual(parse("1,2,10-12,30-40/5 * * * *").minutes,
                         (1, 2, 10, 11, 12, 30, 35, 40))
        self.assertEqual(parse("5,5,1 * * * *").minutes, (1, 5))

    def test_month_names(self):
        self.assertEqual(parse("* * * jan,MAR,Dec *").months, (1, 3, 12))
        self.assertEqual(parse("* * * Jun-aug *").months, (6, 7, 8))
        self.assertEqual(parse("* * * feb/4 *").months, (2, 6, 10))

    def test_weekday_names(self):
        self.assertEqual(parse("* * * * mon-fri").weekdays, {1, 2, 3, 4, 5})
        self.assertEqual(parse("* * * * SUN,Sat").weekdays, {0, 6})
        self.assertEqual(parse("* * * * Wed").weekdays, {3})

    def test_dow_seven_is_sunday(self):
        self.assertEqual(parse("* * * * 7").weekdays, {0})
        self.assertEqual(parse("* * * * 5-7").weekdays, {5, 6, 0})
        self.assertEqual(parse("* * * * 0-7").weekdays, frozenset(range(7)))
        self.assertEqual(parse("0 0 * * 7"), parse("0 0 * * 0"))

    def test_macros(self):
        cases = {
            "@hourly": "0 * * * *",
            "@daily": "0 0 * * *",
            "@midnight": "0 0 * * *",
            "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *",
            "@yearly": "0 0 1 1 *",
            "@annually": "0 0 1 1 *",
        }
        for macro, expansion in cases.items():
            with self.subTest(macro=macro):
                self.assertEqual(parse(macro), parse(expansion))
        self.assertEqual(parse("@DAILY"), parse("0 0 * * *"))

    def test_whitespace_tolerated(self):
        self.assertEqual(parse("  0\t0  *   * *\n"), parse("0 0 * * *"))

    def test_expr_and_str(self):
        c = parse("*/5 9-17 * * mon-fri")
        self.assertEqual(c.expr, "*/5 9-17 * * mon-fri")
        self.assertEqual(str(c), "*/5 9-17 * * mon-fri")
        self.assertEqual(str(parse("@weekly")), "@weekly")
        self.assertIn("mon-fri", repr(c))
        self.assertIsInstance(c, Cron)


class ValidationTests(unittest.TestCase):
    def assertCronError(self, expr, *fragments):
        with self.assertRaises(CronError) as cm:
            parse(expr)
        msg = str(cm.exception)
        self.assertIn(repr(expr), msg)
        for frag in fragments:
            self.assertIn(frag, msg)
        return msg

    def test_is_value_error(self):
        self.assertTrue(issubclass(CronError, ValueError))

    def test_field_count(self):
        self.assertCronError("* * * *", "expected 5 fields", "got 4")
        self.assertCronError("* * * * * *", "got 6")
        self.assertCronError("", "got 0")
        self.assertCronError("   ", "got 0")

    def test_ranges_per_field(self):
        self.assertCronError("60 * * * *", "minute", "60", "0-59")
        self.assertCronError("* 24 * * *", "hour", "0-23")
        self.assertCronError("* * 0 * *", "day-of-month", "1-31")
        self.assertCronError("* * 32 * *", "day-of-month")
        self.assertCronError("* * * 0 *", "month", "1-12")
        self.assertCronError("* * * 13 *", "month")
        self.assertCronError("* * * * 8", "day-of-week", "0-7")
        self.assertCronError("0-60 * * * *", "minute")
        self.assertCronError("* 22-25 * * *", "hour")

    def test_empty_items(self):
        self.assertCronError("1,,2 * * * *", "empty")
        self.assertCronError("1, * * * *", "empty")
        self.assertCronError(",1 * * * *", "empty")

    def test_step_zero(self):
        self.assertCronError("*/0 * * * *", "step of 0")
        self.assertCronError("1-10/0 * * * *", "step of 0")

    def test_bad_steps(self):
        self.assertCronError("*/ * * * *", "step")
        self.assertCronError("*/x * * * *", "step")
        self.assertCronError("*/-1 * * * *", "step")
        self.assertCronError("/5 * * * *", "missing value")

    def test_reversed_range(self):
        self.assertCronError("30-10 * * * *", "reversed range", "30-10")
        self.assertCronError("* * * * fri-mon", "reversed range")
        self.assertCronError("* * * dec-jan *", "reversed range")

    def test_malformed_values(self):
        self.assertCronError("a * * * *", "minute", "'a'")
        self.assertCronError("-5 * * * *", "malformed range")
        self.assertCronError("5- * * * *", "malformed range")
        self.assertCronError("1-2-3 * * * *", "minute")
        self.assertCronError("1.5 * * * *", "minute")
        self.assertCronError("* * * foo *", "month", "jan")
        self.assertCronError("* * * * funday", "day-of-week", "sun")
        # Names only work in their own field.
        self.assertCronError("* * jan * *", "day-of-month")
        self.assertCronError("* * * mon *", "month")

    def test_unknown_macro(self):
        self.assertCronError("@fortnightly", "unknown macro")
        self.assertCronError("@reboot", "unknown macro")

    def test_not_a_string(self):
        with self.assertRaises(CronError):
            parse(None)  # type: ignore[arg-type]


class MatchesTests(unittest.TestCase):
    def test_basic(self):
        c = parse("30 9 * * *")
        self.assertTrue(c.matches(datetime(2026, 3, 4, 9, 30)))
        self.assertTrue(c.matches(datetime(2026, 3, 4, 9, 30, 59)))
        self.assertFalse(c.matches(datetime(2026, 3, 4, 9, 31)))
        self.assertFalse(c.matches(datetime(2026, 3, 4, 10, 30)))

    def test_aware_converted_to_utc(self):
        c = parse("0 12 * * *")
        plus2 = timezone(timedelta(hours=2))
        self.assertTrue(c.matches(datetime(2026, 1, 1, 14, 0, tzinfo=plus2)))
        self.assertFalse(c.matches(datetime(2026, 1, 1, 12, 0, tzinfo=plus2)))
        self.assertTrue(c.matches(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)))

    def test_weekday(self):
        c = parse("0 0 * * mon")
        self.assertTrue(c.matches(datetime(2026, 11, 16)))   # Monday
        self.assertFalse(c.matches(datetime(2026, 11, 15)))  # Sunday
        sun = parse("0 0 * * 7")
        self.assertTrue(sun.matches(datetime(2026, 11, 15)))

    def test_dom_dow_or_when_both_restricted(self):
        c = parse("0 0 13 * fri")
        self.assertTrue(c.matches(datetime(2026, 11, 13)))  # Fri 13th
        self.assertTrue(c.matches(datetime(2026, 11, 20)))  # a Friday
        self.assertTrue(c.matches(datetime(2026, 12, 13)))  # a Sunday the 13th
        self.assertFalse(c.matches(datetime(2026, 11, 14)))

    def test_dom_dow_and_when_one_is_star(self):
        dom_only = parse("0 0 13 * *")
        self.assertTrue(dom_only.matches(datetime(2026, 12, 13)))
        self.assertFalse(dom_only.matches(datetime(2026, 11, 20)))
        dow_only = parse("0 0 * * fri")
        self.assertTrue(dow_only.matches(datetime(2026, 11, 20)))
        self.assertFalse(dow_only.matches(datetime(2026, 12, 13)))

    def test_star_step_counts_as_unrestricted(self):
        # Vixie: a field starting with '*' is "unrestricted" for the OR rule,
        # so */2 in day-of-month is AND-ed with the weekday.
        c = parse("0 0 */2 * mon")
        self.assertTrue(c.matches(datetime(2026, 11, 23)))   # Mon, odd day
        self.assertFalse(c.matches(datetime(2026, 11, 16)))  # Mon, even day
        self.assertFalse(c.matches(datetime(2026, 11, 17)))  # Tue, odd day

    def test_month(self):
        c = parse("0 0 1 jan,jul *")
        self.assertTrue(c.matches(datetime(2026, 7, 1)))
        self.assertFalse(c.matches(datetime(2026, 8, 1)))


class NextAfterTests(unittest.TestCase):
    def nxt(self, expr, *start):
        return utc(parse(expr).next_after(ts(*start)))

    def test_every_minute(self):
        self.assertEqual(self.nxt("* * * * *", 2026, 1, 1, 0, 0, 30),
                         datetime(2026, 1, 1, 0, 1))

    def test_strictly_after(self):
        c = parse("30 9 * * *")
        on = ts(2026, 3, 4, 9, 30)
        self.assertEqual(utc(c.next_after(on)), datetime(2026, 3, 5, 9, 30))
        # Just before the match finds it; fractional seconds inside it don't.
        self.assertEqual(utc(c.next_after(on - 0.5)), datetime(2026, 3, 4, 9, 30))
        self.assertEqual(utc(c.next_after(on + 0.5)), datetime(2026, 3, 5, 9, 30))
        self.assertEqual(utc(c.next_after(on + 59)), datetime(2026, 3, 5, 9, 30))

    def test_seconds_zero_and_float(self):
        r = parse("*/7 * * * *").next_after(ts(2026, 1, 1, 0, 0, 13) + 0.25)
        self.assertIsInstance(r, float)
        self.assertEqual(r % 60, 0)
        self.assertEqual(utc(r), datetime(2026, 1, 1, 0, 7))

    def test_hour_and_minute_rollover(self):
        self.assertEqual(self.nxt("15 * * * *", 2026, 1, 1, 10, 20),
                         datetime(2026, 1, 1, 11, 15))
        self.assertEqual(self.nxt("0 9 * * *", 2026, 1, 1, 10, 0),
                         datetime(2026, 1, 2, 9, 0))
        self.assertEqual(self.nxt("5,10 3,4 * * *", 2026, 1, 1, 3, 10),
                         datetime(2026, 1, 1, 4, 5))

    def test_year_rollover(self):
        self.assertEqual(self.nxt("0 0 1 1 *", 2026, 6, 1),
                         datetime(2027, 1, 1))
        self.assertEqual(self.nxt("59 23 31 12 *", 2026, 12, 31, 23, 59),
                         datetime(2027, 12, 31, 23, 59))
        self.assertEqual(self.nxt("* * * * *", 2026, 12, 31, 23, 59),
                         datetime(2027, 1, 1, 0, 0))

    def test_month_ends_31st_only_in_long_months(self):
        c = parse("0 0 31 * *")
        t = ts(2026, 1, 1)
        seen = []
        for _ in range(7):
            t = c.next_after(t)
            seen.append(utc(t).month)
        self.assertEqual(seen, [1, 3, 5, 7, 8, 10, 12])

    def test_30th_skips_february(self):
        self.assertEqual(self.nxt("0 0 30 * *", 2026, 1, 30, 12),
                         datetime(2026, 3, 30))

    def test_leap_day(self):
        c = parse("0 0 29 2 *")
        self.assertEqual(utc(c.next_after(ts(2025, 3, 1))), datetime(2028, 2, 29))
        self.assertEqual(utc(c.next_after(ts(2028, 2, 29))), datetime(2032, 2, 29))
        # 2100 is not a leap year: 2096 -> 2104.
        self.assertEqual(utc(c.next_after(ts(2096, 3, 1))), datetime(2104, 2, 29))

    def test_feb_last_day_leap_and_non_leap(self):
        c = parse("0 12 28,29 2 *")
        self.assertEqual(utc(c.next_after(ts(2027, 2, 28, 12))), datetime(2028, 2, 28, 12))
        self.assertEqual(utc(c.next_after(ts(2028, 2, 28, 12))), datetime(2028, 2, 29, 12))

    def test_weekly_is_sunday_midnight(self):
        r = self.nxt("@weekly", 2026, 11, 11, 15, 0)  # a Wednesday
        self.assertEqual(r, datetime(2026, 11, 15))
        self.assertEqual(r.weekday(), 6)  # Python: Sunday = 6
        self.assertEqual(self.nxt("@weekly", 2026, 11, 15), datetime(2026, 11, 22))

    def test_other_macros(self):
        start = (2026, 11, 15, 10, 30)
        self.assertEqual(self.nxt("@hourly", *start), datetime(2026, 11, 15, 11))
        self.assertEqual(self.nxt("@daily", *start), datetime(2026, 11, 16))
        self.assertEqual(self.nxt("@monthly", *start), datetime(2026, 12, 1))
        self.assertEqual(self.nxt("@yearly", *start), datetime(2027, 1, 1))

    def test_step_forms(self):
        self.assertEqual(self.nxt("*/15 * * * *", 2026, 1, 1, 0, 46),
                         datetime(2026, 1, 1, 1, 0))
        self.assertEqual(self.nxt("0 */6 * * *", 2026, 1, 1, 13),
                         datetime(2026, 1, 1, 18))
        self.assertEqual(self.nxt("0 20/2 * * *", 2026, 1, 1, 22, 1),
                         datetime(2026, 1, 2, 20))
        self.assertEqual(self.nxt("0 0 1 */4 *", 2026, 2, 1),
                         datetime(2026, 5, 1))
        self.assertEqual(self.nxt("0 9-17/4 * * *", 2026, 1, 1, 13),
                         datetime(2026, 1, 1, 17))

    def test_weekdays_only(self):
        # Friday 2026-11-20 18:00 -> Monday 09:00.
        self.assertEqual(self.nxt("0 9 * * mon-fri", 2026, 11, 20, 18),
                         datetime(2026, 11, 23, 9))

    def test_dom_or_dow_search(self):
        c = parse("0 0 1 * mon")
        got = []
        t = ts(2026, 11, 25)
        for _ in range(4):
            t = c.next_after(t)
            got.append(utc(t))
        self.assertEqual(got, [datetime(2026, 11, 30), datetime(2026, 12, 1),
                               datetime(2026, 12, 7), datetime(2026, 12, 14)])

    def test_friday_13th(self):
        # Restrict both and confine the month so only the OR rule applies.
        self.assertEqual(self.nxt("0 0 13 2 fri", 2026, 2, 7),
                         datetime(2026, 2, 13))
        self.assertEqual(self.nxt("0 0 13 2 fri", 2026, 2, 13),
                         datetime(2026, 2, 20))

    def test_result_always_matches(self):
        exprs = ["*/7 3-5 * * *", "0 0 1 * mon", "15 10 * 2,8 sat",
                 "0 0 31 * *", "@weekly", "1-5/2 */5 10-20 * *"]
        for expr in exprs:
            c = parse(expr)
            t = ts(2026, 1, 1)
            for _ in range(50):
                n = c.next_after(t)
                with self.subTest(expr=expr, after=utc(t)):
                    self.assertGreater(n, t)
                    self.assertTrue(c.matches(utc(n)))
                t = n

    def test_matches_brute_force(self):
        # Compare with a minute-by-minute scan over a few days.
        for expr in ["0 0 13 * fri", "*/20 */7 * * *", "45 23 * * sun"]:
            c = parse(expr)
            start = ts(2026, 11, 10)
            expected = start + 60
            while not c.matches(utc(expected)):
                expected += 60
            self.assertEqual(c.next_after(start), expected, expr)

    def test_impossible_raises(self):
        for expr in ["0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6,9,11 *"]:
            with self.subTest(expr=expr):
                with self.assertRaises(CronError) as cm:
                    parse(expr).next_after(ts(2026, 1, 1))
                self.assertIn(repr(expr), str(cm.exception))
                self.assertIn("no matching time", str(cm.exception))

    def test_impossible_is_fast(self):
        t0 = time.perf_counter()
        with self.assertRaises(CronError):
            parse("0 0 31 2 *").next_after(ts(2026, 1, 1))
        self.assertLess(time.perf_counter() - t0, 0.1)

    def test_performance_leap_day(self):
        c = parse("0 0 29 2 *")
        start = ts(2025, 3, 1)
        t0 = time.perf_counter()
        for _ in range(1000):
            r = c.next_after(start)
        elapsed = time.perf_counter() - t0
        self.assertEqual(utc(r), datetime(2028, 2, 29))
        self.assertLess(elapsed, 1.0)

    def test_search_horizon_constant(self):
        self.assertGreaterEqual(cron.SEARCH_YEARS, 5)


if __name__ == "__main__":
    unittest.main()
