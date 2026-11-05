import unittest
from datetime import date, timedelta

from usage_collector import doctor, periods


class PeriodShapeTest(unittest.TestCase):
    def test_week_keys(self):
        for good in ("2026-W01", "2026-W45", "2026-W53"):
            with self.subTest(good=good):
                self.assertTrue(periods.is_week(good))
        for bad in ("2026-W00", "2025-W53", "2026-W54", "2026-w01", "2026-02", None, 7):
            with self.subTest(bad=bad):
                self.assertFalse(periods.is_week(bad))

    def test_period_is_a_month_or_a_week(self):
        self.assertTrue(periods.is_period("2026-02"))
        self.assertTrue(periods.is_period("2026-W09"))
        self.assertFalse(periods.is_period("2026-13"))
        self.assertFalse(periods.is_period("../x"))


class BoundsTest(unittest.TestCase):
    def test_week_runs_monday_to_sunday(self):
        self.assertEqual(periods.bounds("2026-W01"), (date(2025, 12, 29), date(2026, 1, 4)))
        self.assertEqual(periods.bounds("2026-W45"), (date(2026, 11, 2), date(2026, 11, 8)))

    def test_month_runs_first_to_last_day(self):
        self.assertEqual(periods.bounds("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))


class PreviousWeekTest(unittest.TestCase):
    def test_last_completed_week(self):
        self.assertEqual(periods.previous_week(date(2026, 11, 9)), "2026-W45")
        self.assertEqual(periods.previous_week(date(2026, 11, 15)), "2026-W45")
        self.assertEqual(periods.previous_week(date(2026, 1, 5)), "2026-W01")
        self.assertEqual(periods.previous_week(date(2026, 1, 4)), "2025-W52")

    def test_the_week_before_a_53_week_year_ends(self):
        self.assertEqual(periods.previous_week(date(2027, 1, 4)), "2026-W53")


class DueWeeksTest(unittest.TestCase):
    TODAY = date(2026, 11, 9)

    def test_nothing_checked_yet_means_only_the_last_week(self):
        self.assertEqual(periods.due_weeks(None, self.TODAY), (["2026-W45"], []))

    def test_already_checked_means_nothing_due(self):
        self.assertEqual(periods.due_weeks("2026-W45", self.TODAY), ([], []))

    def test_missed_weeks_come_oldest_first(self):
        self.assertEqual(
            periods.due_weeks("2026-W42", self.TODAY),
            (["2026-W43", "2026-W44", "2026-W45"], []),
        )

    def test_weeks_starting_over_21_days_ago_are_skipped(self):
        due, skipped = periods.due_weeks("2026-W38", self.TODAY)
        self.assertEqual(skipped, ["2026-W39", "2026-W40", "2026-W41", "2026-W42"])
        self.assertEqual(due, ["2026-W43", "2026-W44", "2026-W45"])

    def test_a_week_starting_22_days_ago_is_skipped(self):
        self.assertEqual(periods.due_weeks("2026-W42", date(2026, 11, 10)),
                         (["2026-W44", "2026-W45"], ["2026-W43"]))

    def test_missed_weeks_across_a_53_week_year(self):
        self.assertEqual(periods.due_weeks("2026-W52", date(2027, 1, 12)),
                         (["2026-W53", "2027-W01"], []))

    def test_doctor_minimum_retention_covers_the_oldest_day_a_catch_up_reads(self):
        oldest_due = max(
            (today - periods.bounds(week)[0]).days
            for today in (date(2026, 11, 9) + timedelta(days=n) for n in range(7))
            for week in periods.due_weeks("2026-W30", today)[0])
        self.assertEqual(oldest_due, periods.MAX_AGE_DAYS)
        self.assertGreater(doctor.min_cleanup_days("week"), oldest_due)

    def test_unreadable_state_value_counts_as_never_checked(self):
        self.assertEqual(periods.due_weeks("garbage", self.TODAY), (["2026-W45"], []))

    def test_missed_weeks_across_a_year_end(self):
        due, _skipped = periods.due_weeks("2025-W52", date(2026, 1, 12))
        self.assertEqual(due, ["2026-W01", "2026-W02"])
