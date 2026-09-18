"""Quadratic (2nd degree) equation solver.

Solves  a*x^2 + b*x + c = 0  for x.

Run it directly and enter the three coefficients a, b and c.
Handles real roots, a single (double) root, complex roots, and
falls back to a linear solution when a == 0.
"""

import cmath
import math

# Largest magnitude accepted for a coefficient. The discriminant squares b and
# multiplies a*c, so anything above ~1e154 saturates float64 to inf and yields
# inf/nan roots. 1e150 leaves headroom while covering any realistic input.
MAX_COEFFICIENT = 1e150

# Relative tolerance for treating b^2 and 4ac as equal (i.e. discriminant == 0).
# Perfect squares like x^2 - 1.4x + 0.49 give b^2 - 4ac = -2.2e-16 in binary
# floating point; without this we'd report spurious complex roots.
# A relative tolerance (rather than absolute) keeps the check scale-invariant.
# Rounding noise is ~1e-16 relative; 1e-14 gives 100x headroom while still
# distinguishing genuinely distinct roots that differ by more than ~1e-7.
DISCRIMINANT_REL_TOL = 1e-14


def validate_coefficient(value: float, name: str = "coefficient") -> float:
    """Ensure a coefficient is a finite number within MAX_COEFFICIENT bounds.

    Raises ValueError otherwise. Returns the value unchanged on success.
    """
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}.")
    if abs(value) > MAX_COEFFICIENT:
        raise ValueError(
            f"{name} magnitude too large (|{value:.3g}| > {MAX_COEFFICIENT:.0e})."
        )
    return value


def discriminant(a: float, b: float, c: float) -> float:
    """Return the discriminant b^2 - 4ac."""
    return b * b - 4 * a * c


def solve_quadratic(a: float, b: float, c: float) -> tuple:
    """Solve a*x^2 + b*x + c = 0.

    Returns a tuple of roots:
      - two real roots (x1, x2) with x1 <= x2 when discriminant > 0
      - one real root (x,) when discriminant == 0
      - two complex roots (x1, x2) when discriminant < 0
      - one real root (x,) when a == 0 and b != 0 (linear equation)

    Raises ValueError when a == 0 and b == 0:
      - c == 0 -> infinite solutions
      - c != 0 -> no solution
    Raises ValueError if any coefficient is non-finite or exceeds MAX_COEFFICIENT.
    """
    validate_coefficient(a, "Coefficient a")
    validate_coefficient(b, "Coefficient b")
    validate_coefficient(c, "Coefficient c")

    if a == 0:
        if b == 0:
            if c == 0:
                raise ValueError("Every x is a solution (0 = 0).")
            raise ValueError("No solution (the equation reduces to a false constant).")
        # Linear: b*x + c = 0
        return (-c / b,)

    b_squared = b * b
    four_ac = 4 * a * c

    # Double root. Compare b^2 and 4ac with a relative tolerance so that
    # rounding noise (e.g. -2.2e-16) is not mistaken for a negative discriminant.
    if math.isclose(b_squared, four_ac, rel_tol=DISCRIMINANT_REL_TOL):
        return (-b / (2 * a),)

    disc = b_squared - four_ac

    if disc > 0:
        # Numerically stable form. The textbook (-b ± sqrt(d)) / 2a suffers
        # catastrophic cancellation when b^2 >> 4ac because sqrt(d) ~= |b|.
        # Instead compute the root that does NOT cancel, then derive the other
        # from the product of roots (x1 * x2 = c / a).
        q_term = -0.5 * (b + math.copysign(math.sqrt(disc), b))
        root_a = q_term / a
        root_b = c / q_term
        return tuple(sorted((root_a, root_b)))

    # disc < 0 -> complex conjugate roots. No cancellation risk here because
    # sqrt(disc) is purely imaginary and b is real.
    sqrt_disc = cmath.sqrt(disc)
    root_a = (-b - sqrt_disc) / (2 * a)
    root_b = (-b + sqrt_disc) / (2 * a)
    return (root_a, root_b)


def format_root(root: float | complex) -> str:
    """Pretty-print a root, dropping trailing .0 and tidying complex numbers."""
    if isinstance(root, complex):
        real_part = format_root(root.real)
        imag_part = format_root(abs(root.imag))
        sign = "+" if root.imag >= 0 else "-"
        return f"{real_part} {sign} {imag_part}i"
    if float(root).is_integer():
        return str(int(root))
    return f"{root:.6g}"


def parse_coefficient(raw: str, name: str = "coefficient") -> float:
    """Convert user text to a bounded, finite float.

    Raises ValueError if the text is not a number, is inf/-inf/nan, or its
    magnitude exceeds MAX_COEFFICIENT, since those would produce meaningless
    (inf/nan) results in the solver.
    """
    text = raw.strip()
    try:
        val = float(text)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {text!r}.") from None
    if not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number, got {text!r}.")
    return validate_coefficient(val, name)


def read_coefficient(name: str) -> float:
    """Prompt for a single numeric coefficient and validate it."""
    raw = input(f"Enter coefficient {name}: ")
    return parse_coefficient(raw, name=f"Coefficient {name}")


def main() -> None:
    print("Solve a*x^2 + b*x + c = 0")
    try:
        a = read_coefficient("a")
        b = read_coefficient("b")
        c = read_coefficient("c")
    except ValueError as exc:
        print(f"Invalid input: {exc}")
        return

    try:
        roots = solve_quadratic(a, b, c)
    except ValueError as exc:
        print(f"Result: {exc}")
        return

    if a == 0:
        print(f"Linear equation. x = {format_root(roots[0])}")
    elif len(roots) == 1:
        print(f"One real (double) root: x = {format_root(roots[0])}")
    elif isinstance(roots[0], complex):
        print(f"Two complex roots: x1 = {format_root(roots[0])}, x2 = {format_root(roots[1])}")
    else:
        print(f"Two real roots: x1 = {format_root(roots[0])}, x2 = {format_root(roots[1])}")


if __name__ == "__main__":
    main()