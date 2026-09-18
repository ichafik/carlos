"""Tests for apps/quadratic_solver.py

Project layout:
    CARLOS/
    ├── apps/
    │   └── quadratic_solver.py
    └── test/
        └── quadratic_solver.py   (this file)

Run from the project root (CARLOS/):
    python -m unittest test.quadratic_solver -v
or
    python test/quadratic_solver.py
"""

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.quadratic_solver import (  # noqa: E402
    MAX_COEFFICIENT,
    discriminant,
    format_root,
    main,
    parse_coefficient,
    solve_quadratic,
    validate_coefficient,
)


class TestValidateCoefficient(unittest.TestCase):
    def test_returns_value_unchanged(self):
        self.assertEqual(validate_coefficient(2.5), 2.5)

    def test_accepts_zero_and_negatives(self):
        self.assertEqual(validate_coefficient(0.0), 0.0)
        self.assertEqual(validate_coefficient(-7.0), -7.0)

    def test_accepts_exact_bound(self):
        self.assertEqual(validate_coefficient(MAX_COEFFICIENT), MAX_COEFFICIENT)
        self.assertEqual(validate_coefficient(-MAX_COEFFICIENT), -MAX_COEFFICIENT)

    def test_rejects_above_bound(self):
        with self.assertRaises(ValueError) as ctx:
            validate_coefficient(1e151, name="Coefficient b")
        self.assertIn("too large", str(ctx.exception))
        self.assertIn("Coefficient b", str(ctx.exception))

    def test_rejects_below_negative_bound(self):
        with self.assertRaises(ValueError):
            validate_coefficient(-1e200)

    def test_rejects_non_finite(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_coefficient(bad)


class TestParseCoefficient(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(parse_coefficient("3"), 3.0)

    def test_decimal_and_negative(self):
        self.assertEqual(parse_coefficient("-0.25"), -0.25)

    def test_scientific_notation(self):
        self.assertEqual(parse_coefficient("1e3"), 1000.0)

    def test_strips_whitespace(self):
        self.assertEqual(parse_coefficient("  2 \n"), 2.0)

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_coefficient("abc", name="Coefficient a")
        self.assertIn("must be a number", str(ctx.exception))
        self.assertIn("Coefficient a", str(ctx.exception))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_coefficient("")

    def test_inf_raises(self):
        for text in ("inf", "Infinity", "-inf", "+INF"):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                parse_coefficient(text)
            self.assertIn("finite", str(ctx.exception))

    def test_nan_raises(self):
        for text in ("nan", "NaN", "-nan"):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                parse_coefficient(text)
            self.assertIn("finite", str(ctx.exception))

    def test_overflow_literal_raises(self):
        # A literal too large for a float parses as inf and must be rejected
        with self.assertRaises(ValueError):
            parse_coefficient("1e999")

    def test_magnitude_above_bound_raises(self):
        # Finite, but large enough to saturate b*b or 4*a*c to inf
        for text in ("1e151", "-1e200", "1e308"):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                parse_coefficient(text)
            self.assertIn("too large", str(ctx.exception))

    def test_magnitude_at_bound_accepted(self):
        self.assertEqual(parse_coefficient("1e150"), 1e150)
        self.assertEqual(parse_coefficient("-1e150"), -1e150)


class TestDiscriminant(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(discriminant(1, -3, 2), 1)

    def test_zero(self):
        self.assertEqual(discriminant(1, 2, 1), 0)

    def test_negative(self):
        self.assertEqual(discriminant(1, 0, 1), -4)


class TestSolveQuadratic(unittest.TestCase):
    def test_two_real_roots(self):
        # x^2 - 3x + 2 = 0 -> x = 1, 2
        self.assertEqual(solve_quadratic(1, -3, 2), (1.0, 2.0))

    def test_roots_are_sorted(self):
        # 2x^2 + 3x - 2 = 0 -> x = -2, 0.5
        roots = solve_quadratic(2, 3, -2)
        self.assertEqual(roots, (-2.0, 0.5))
        self.assertLess(roots[0], roots[1])

    def test_negative_leading_coefficient(self):
        # -x^2 + 1 = 0 -> x = -1, 1
        self.assertEqual(solve_quadratic(-1, 0, 1), (-1.0, 1.0))

    def test_double_root(self):
        # x^2 + 2x + 1 = 0 -> x = -1 (twice)
        self.assertEqual(solve_quadratic(1, 2, 1), (-1.0,))

    def test_complex_roots(self):
        # x^2 + 1 = 0 -> x = ±i
        roots = solve_quadratic(1, 0, 1)
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(isinstance(r, complex) for r in roots))
        self.assertAlmostEqual(roots[0], complex(0, -1))
        self.assertAlmostEqual(roots[1], complex(0, 1))

    def test_complex_roots_with_real_part(self):
        # x^2 + 2x + 5 = 0 -> x = -1 ± 2i
        roots = solve_quadratic(1, 2, 5)
        self.assertAlmostEqual(roots[0], complex(-1, -2))
        self.assertAlmostEqual(roots[1], complex(-1, 2))

    def test_roots_satisfy_equation(self):
        a, b, c = 3, -7, 2
        for x in solve_quadratic(a, b, c):
            self.assertAlmostEqual(a * x * x + b * x + c, 0, places=9)

    def test_irrational_roots(self):
        # x^2 - 2 = 0 -> x = ±sqrt(2)
        roots = solve_quadratic(1, 0, -2)
        self.assertAlmostEqual(roots[0], -(2 ** 0.5))
        self.assertAlmostEqual(roots[1], 2 ** 0.5)

    def test_linear_when_a_is_zero(self):
        # 2x + 4 = 0 -> x = -2
        self.assertEqual(solve_quadratic(0, 2, 4), (-2.0,))

    def test_infinite_solutions_raises(self):
        with self.assertRaises(ValueError) as ctx:
            solve_quadratic(0, 0, 0)
        self.assertIn("Every x", str(ctx.exception))

    def test_no_solution_raises(self):
        with self.assertRaises(ValueError) as ctx:
            solve_quadratic(0, 0, 5)
        self.assertIn("No solution", str(ctx.exception))

    def test_huge_coefficients_rejected_not_garbage(self):
        # Previously these returned (-inf, inf) or nan roots silently
        for coeffs in [(1, 1e200, 1), (1e200, 1, 1e200), (1, 1, 1e308)]:
            with self.subTest(coeffs=coeffs), self.assertRaises(ValueError) as ctx:
                solve_quadratic(*coeffs)
            self.assertIn("too large", str(ctx.exception))

    def test_non_finite_coefficients_rejected(self):
        with self.assertRaises(ValueError):
            solve_quadratic(1, float("inf"), 1)
        with self.assertRaises(ValueError):
            solve_quadratic(float("nan"), 1, 1)

    # --- floating-point robustness -------------------------------------------

    def test_perfect_square_with_rounding_noise_gives_real_double_root(self):
        # (x - 0.7)^2 = x^2 - 1.4x + 0.49. In binary floats b^2 - 4ac = -2.2e-16,
        # which used to produce spurious complex roots with tiny imaginary parts.
        roots = solve_quadratic(1.0, -1.4, 0.49)
        self.assertEqual(len(roots), 1)
        self.assertNotIsInstance(roots[0], complex)
        self.assertAlmostEqual(roots[0], 0.7, places=12)

    def test_more_perfect_squares_with_non_representable_roots(self):
        # (x - r)^2 for several r that are not exact in binary
        for r in (0.1, 0.3, 1.1, -2.7, 123.456):
            with self.subTest(r=r):
                roots = solve_quadratic(1.0, -2 * r, r * r)
                self.assertEqual(len(roots), 1, roots)
                self.assertNotIsInstance(roots[0], complex)
                self.assertAlmostEqual(roots[0], r, places=10)

    def test_double_root_tolerance_is_scale_invariant(self):
        # Same perfect square scaled by 1e100 and 1e-100 must still be detected
        for scale in (1e100, 1e-100):
            with self.subTest(scale=scale):
                roots = solve_quadratic(scale, -1.4 * scale, 0.49 * scale)
                self.assertEqual(len(roots), 1, roots)
                self.assertAlmostEqual(roots[0], 0.7, places=12)

    def test_genuinely_tiny_negative_discriminant_stays_complex(self):
        # x^2 + 1e-13 = 0 has a real negative discriminant (-4e-13), not noise.
        # An absolute tolerance would wrongly collapse this to a double root.
        roots = solve_quadratic(1, 0, 1e-13)
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(isinstance(r, complex) for r in roots))

    def test_genuinely_tiny_positive_discriminant_stays_two_real_roots(self):
        # (x - 1)(x - 1.000001): b^2 and 4ac differ by only ~2.5e-13 relative,
        # well above rounding noise, so these must remain two distinct roots.
        roots = solve_quadratic(1.0, -2.000001, 1.000001)
        self.assertEqual(len(roots), 2, roots)
        self.assertAlmostEqual(roots[0], 1.0, places=9)
        self.assertAlmostEqual(roots[1], 1.000001, places=9)

    def test_no_catastrophic_cancellation_when_b_squared_dominates(self):
        # x^2 + 1e8 x + 1 = 0 -> roots ~ -1e8 and -1e-8.
        # Textbook formula returned ~ -7.45e-9 (25% off) for the small root.
        roots = solve_quadratic(1, 1e8, 1)
        self.assertAlmostEqual(roots[0] / -1e8, 1.0, places=12)
        self.assertAlmostEqual(roots[1] / -1e-8, 1.0, places=12)

    def test_no_cancellation_with_negative_b(self):
        # x^2 - 1e8 x + 1 = 0 -> roots ~ 1e-8 and 1e8 (mirror of the above)
        roots = solve_quadratic(1, -1e8, 1)
        self.assertAlmostEqual(roots[0] / 1e-8, 1.0, places=12)
        self.assertAlmostEqual(roots[1] / 1e8, 1.0, places=12)

    def test_no_cancellation_with_tiny_leading_coefficient(self):
        # 1e-200 x^2 + x + 1 = 0 -> roots ~ -1e200 and -1.
        # Textbook formula returned 0.0 for the second root.
        roots = solve_quadratic(1e-200, 1, 1)
        self.assertAlmostEqual(roots[0] / -1e200, 1.0, places=12)
        self.assertAlmostEqual(roots[1], -1.0, places=12)

    def test_stable_formula_roots_satisfy_equation_relative(self):
        # Residual check using relative error, across badly-conditioned inputs
        for a, b, c in [(1, 1e8, 1), (1, -1e8, 1), (1e-200, 1, 1), (3, -7, 2), (2, 3, -2)]:
            for root in solve_quadratic(a, b, c):
                with self.subTest(coeffs=(a, b, c), root=root):
                    residual = a * root * root + b * root + c
                    scale = max(abs(a * root * root), abs(b * root), abs(c), 1e-300)
                    self.assertLess(abs(residual) / scale, 1e-12)

    def test_large_but_bounded_coefficients_produce_finite_roots(self):
        roots = solve_quadratic(1e150, 0, -1e150)  # x^2 = 1
        self.assertEqual(roots, (-1.0, 1.0))
        roots = solve_quadratic(1, 1e150, 1)
        self.assertTrue(all(math.isfinite(r) for r in roots))


class TestFormatRoot(unittest.TestCase):
    def test_integer_float(self):
        self.assertEqual(format_root(2.0), "2")

    def test_negative_integer_float(self):
        self.assertEqual(format_root(-1.0), "-1")

    def test_decimal(self):
        self.assertEqual(format_root(0.5), "0.5")

    def test_complex_positive_imaginary(self):
        self.assertEqual(format_root(complex(-1, 2)), "-1 + 2i")

    def test_complex_negative_imaginary(self):
        self.assertEqual(format_root(complex(-1, -2)), "-1 - 2i")

    def test_pure_imaginary(self):
        self.assertEqual(format_root(complex(0, 1)), "0 + 1i")


class TestMain(unittest.TestCase):
    def run_main(self, inputs):
        with patch("builtins.input", side_effect=inputs), patch("builtins.print") as mock_print:
            main()
        # Return every printed line as a list of strings
        return [call.args[0] for call in mock_print.call_args_list]

    def test_two_real_roots(self):
        out = self.run_main(["1", "-3", "2"])
        self.assertIn("Two real roots: x1 = 1, x2 = 2", out)

    def test_double_root(self):
        out = self.run_main(["1", "2", "1"])
        self.assertIn("One real (double) root: x = -1", out)

    def test_complex_roots(self):
        out = self.run_main(["1", "2", "5"])
        self.assertIn("Two complex roots: x1 = -1 - 2i, x2 = -1 + 2i", out)

    def test_linear(self):
        out = self.run_main(["0", "2", "4"])
        self.assertIn("Linear equation. x = -2", out)

    def test_no_solution(self):
        out = self.run_main(["0", "0", "5"])
        self.assertTrue(any(line.startswith("Result: No solution") for line in out))

    def test_invalid_number(self):
        out = self.run_main(["abc", "1", "1"])
        self.assertIn("Invalid input: Coefficient a must be a number, got 'abc'.", out)

    def test_infinite_coefficient_rejected(self):
        out = self.run_main(["1", "inf", "1"])
        self.assertIn("Invalid input: Coefficient b must be a finite number, got 'inf'.", out)

    def test_nan_coefficient_rejected(self):
        out = self.run_main(["1", "2", "nan"])
        self.assertIn("Invalid input: Coefficient c must be a finite number, got 'nan'.", out)

    def test_huge_coefficient_rejected(self):
        out = self.run_main(["1", "1e200", "1"])
        self.assertTrue(
            any(line.startswith("Invalid input: Coefficient b magnitude too large") for line in out),
            out,
        )

    def test_stops_prompting_after_invalid_input(self):
        # After a bad "a", main must not ask for b and c
        with patch("builtins.input", side_effect=["nan"]) as mock_input, patch("builtins.print"):
            main()
        self.assertEqual(mock_input.call_count, 1)

    def test_accepts_decimals_and_whitespace(self):
        out = self.run_main([" 0.5 ", "0", "-2"])
        self.assertIn("Two real roots: x1 = -2, x2 = 2", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)