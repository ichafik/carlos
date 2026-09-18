"""Tests for apps/age_calculator.py

Project layout:
    CARLOS/
    ├── apps/
    │   └── age_calculator.py
    └── test/
        └── age_calculator.py   (this file)

Run from the project root (CARLOS/):
    python -m unittest test.age_calculator -v
or
    python test/age_calculator.py
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

# Make the project root importable so `apps` resolves no matter where
# the test is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.age_calculator import calculate_age, parse_date_of_birth, main  # noqa: E402


class TestParseDateOfBirth(unittest.TestCase):
    def test_valid_date(self):
        self.assertEqual(parse_date_of_birth("1990-05-15"), date(1990, 5, 15))

    def test_strips_whitespace(self):
        self.assertEqual(parse_date_of_birth("  2000-01-01 "), date(2000, 1, 1))

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            parse_date_of_birth("15/05/1990")

    def test_nonsense_string_raises(self):
        with self.assertRaises(ValueError):
            parse_date_of_birth("not a date")

    def test_impossible_date_raises(self):
        with self.assertRaises(ValueError):
            parse_date_of_birth("2023-02-30")


class TestCalculateAge(unittest.TestCase):
    TODAY = date(2026, 9, 18)

    def test_birthday_already_passed_this_year(self):
        self.assertEqual(calculate_age(date(1990, 5, 15), self.TODAY), 36)

    def test_birthday_is_today(self):
        self.assertEqual(calculate_age(date(1990, 9, 18), self.TODAY), 36)

    def test_birthday_later_this_year(self):
        self.assertEqual(calculate_age(date(1990, 12, 25), self.TODAY), 35)

    def test_birthday_tomorrow(self):
        self.assertEqual(calculate_age(date(1990, 9, 19), self.TODAY), 35)

    def test_born_today_is_zero(self):
        self.assertEqual(calculate_age(self.TODAY, self.TODAY), 0)

    def test_leap_day_birthday_non_leap_year(self):
        # Born Feb 29, 2000; on Feb 28, 2025 they are still 24, on Mar 1 they are 25
        self.assertEqual(calculate_age(date(2000, 2, 29), date(2025, 2, 28)), 24)
        self.assertEqual(calculate_age(date(2000, 2, 29), date(2025, 3, 1)), 25)

    def test_future_birth_date_raises(self):
        with self.assertRaises(ValueError):
            calculate_age(date(2030, 1, 1), self.TODAY)

    def test_defaults_to_today(self):
        expected = calculate_age(date(2000, 1, 1), date.today())
        self.assertEqual(calculate_age(date(2000, 1, 1)), expected)


class TestMain(unittest.TestCase):
    @patch("builtins.print")
    @patch("builtins.input", return_value="1990-05-15")
    @patch("apps.age_calculator.date")
    def test_main_prints_age(self, mock_date, _mock_input, mock_print):
        mock_date.today.return_value = date(2026, 9, 18)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        main()
        mock_print.assert_called_once_with("You are 36 years old.")

    @patch("builtins.print")
    @patch("builtins.input", return_value="hello")
    def test_main_handles_invalid_input(self, _mock_input, mock_print):
        main()
        printed = mock_print.call_args[0][0]
        self.assertTrue(printed.startswith("Invalid input:"))

    @patch("builtins.print")
    @patch("builtins.input", return_value="2999-01-01")
    def test_main_handles_future_date(self, _mock_input, mock_print):
        main()
        mock_print.assert_called_once_with(
            "Invalid input: Date of birth cannot be in the future."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)