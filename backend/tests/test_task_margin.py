import unittest
from datetime import date, timedelta

from app.core.task_margin import calculate_schedule_margin


class ScheduleMarginTest(unittest.TestCase):
    def setUp(self):
        self.business_day = date(2026, 10, 21)

    def deadline(self, days):
        return self.business_day + timedelta(days=days - 1)

    def task(self, remaining_days, daily_hours, deadline_days):
        return {
            "remaining_days": remaining_days,
            "daily_hours": daily_hours,
            "deadline": self.deadline(deadline_days),
        }

    def test_twenty_and_thirty_day_tasks_at_22_hours_have_zero_margin(self):
        for days in (20, 30):
            with self.subTest(days=days):
                result = calculate_schedule_margin(
                    [self.task(days, 22, days)], self.business_day
                )
                self.assertEqual(result["hours"], 0)
                self.assertEqual(result["bottleneck_days"], days)
                self.assertEqual(result["bottleneck_date"], self.deadline(days).isoformat())
                self.assertFalse(result["sufficient"])
                self.assertIsNone(result["reason"])

    def test_only_22_hours_spare_over_twenty_or_thirty_days_is_not_sufficient(self):
        plans = (
            ([self.task(20, 5, 20)] * 4 + [self.task(18, 1, 20)], 20),
            ([self.task(30, 5, 30)] * 4 + [self.task(19, 2, 30)], 30),
        )
        for tasks, days in plans:
            with self.subTest(days=days):
                result = calculate_schedule_margin(tasks, self.business_day)
                self.assertEqual(result["hours"], 22)
                self.assertEqual(result["bottleneck_days"], days)
                self.assertFalse(result["sufficient"])

    def test_later_deadline_work_already_in_late_window_counts_at_earlier_deadline(self):
        result = calculate_schedule_margin(
            [
                self.task(2, 6, 4),
                {
                    "remaining_days": 1,
                    "daily_hours": 5,
                    "deadline": self.deadline(3).isoformat(),
                },
            ],
            self.business_day,
        )

        # At day 3, task 1 has one scheduled day (6 h) and task 2 has one (5 h).
        self.assertEqual(result["hours"], 55)
        self.assertEqual(result["bottleneck_days"], 3)
        self.assertEqual(result["bottleneck_date"], self.deadline(3).isoformat())
        self.assertTrue(result["sufficient"])

    def test_only_deadline_checkpoints_are_evaluated_and_margin_can_exceed_22(self):
        # These tasks overlap at 24 h on day 3, but the deadline checkpoint has
        # 42 h of cumulative capacity margin through day 3.
        result = calculate_schedule_margin(
            [self.task(1, 12, 3), self.task(1, 12, 3)], self.business_day
        )

        self.assertEqual(result["hours"], 42)
        self.assertEqual(result["bottleneck_days"], 3)
        self.assertTrue(result["sufficient"])

    def test_negative_margin_is_returned(self):
        result = calculate_schedule_margin(
            [self.task(2, 12, 2), self.task(2, 12, 2)], self.business_day
        )

        self.assertEqual(result["hours"], -4)
        self.assertFalse(result["sufficient"])
        self.assertIsNone(result["reason"])

    def test_insufficient_days_including_nonfuture_deadline(self):
        cases = (
            self.task(3, 1, 2),
            {
                "remaining_days": 1,
                "daily_hours": 1,
                "deadline": (self.business_day - timedelta(days=1)).isoformat(),
            },
        )
        for task in cases:
            with self.subTest(task=task):
                result = calculate_schedule_margin([task], self.business_day)
                self.assertIsNone(result["hours"])
                self.assertFalse(result["sufficient"])
                self.assertEqual(result["reason"], "insufficient_days")

    def test_empty_and_invalid_plans_fail_closed(self):
        empty = calculate_schedule_margin([], self.business_day)
        self.assertEqual(empty["business_date"], self.business_day.isoformat())
        self.assertIsNone(empty["hours"])
        self.assertFalse(empty["sufficient"])
        self.assertEqual(empty["reason"], "no_pending_tasks")

        invalid_tasks = (
            {"remaining_days": 1, "daily_hours": 1},
            self.task(1, 0, 2),
            self.task(1, float("nan"), 2),
            self.task(1, 1, 2) | {"deadline": "not-a-date"},
            {"remaining_days": 1.5, "daily_hours": 1, "deadline": self.deadline(2)},
        )
        for task in invalid_tasks:
            with self.subTest(task=task):
                result = calculate_schedule_margin([task], self.business_day)
                self.assertIsNone(result["hours"])
                self.assertIsNone(result["bottleneck_date"])
                self.assertIsNone(result["bottleneck_days"])
                self.assertFalse(result["sufficient"])
                self.assertEqual(result["reason"], "invalid_plan")

    def test_equal_minimum_uses_later_deadline(self):
        result = calculate_schedule_margin(
            [self.task(1, 1, 2), self.task(1, 22, 3)], self.business_day
        )

        self.assertEqual(result["hours"], 43)
        self.assertEqual(result["bottleneck_days"], 3)
        self.assertEqual(result["bottleneck_date"], self.deadline(3).isoformat())

    def test_thresholds_are_strict(self):
        min_margin_equal_22 = calculate_schedule_margin(
            [self.task(1, 22, 2)], self.business_day
        )
        self.assertEqual(min_margin_equal_22["hours"], 22)
        self.assertFalse(min_margin_equal_22["sufficient"])

        average_equal_18_hours = calculate_schedule_margin(
            [self.task(6, 18, 6)], self.business_day
        )
        self.assertEqual(average_equal_18_hours["hours"], 24)
        self.assertFalse(average_equal_18_hours["sufficient"])

    def test_every_deadline_must_pass_average_threshold_not_only_bottleneck(self):
        result = calculate_schedule_margin(
            [self.task(1, 21, 2), self.task(8, 21, 10)], self.business_day
        )

        # Day 2 has the minimum margin (23 > 22); by day 10 the margin is 31,
        # still above the minimum, but below the required 4 * 10 = 40 hours.
        self.assertEqual(result["hours"], 23)
        self.assertEqual(result["bottleneck_days"], 2)
        self.assertFalse(result["sufficient"])


if __name__ == "__main__":
    unittest.main()
