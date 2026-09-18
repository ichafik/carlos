"""Quadratic (2nd degree) equation solver.

Solves  a*x^2 + b*x + c = 0  for x.

Run it directly and enter the three coefficients a, b and c.
Handles real roots, a single (double) root, complex roots, and
falls back to a linear solution when a == 0.
"""

import cmath
import math


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
    """
    if a == 0:
        if b == 0:
            if c == 0:
                raise ValueError("Every x is a solution (0 = 0).")
            raise ValueError("No solution (the equation reduces to a false constant).")
        # Linear: b*x + c = 0
        return (-c / b,)

    d = discriminant(a, b, c)

    if d > 0:
        sqrt_d = math.sqrt(d)
        x1 = (-b - sqrt_d) / (2 * a)
        x2 = (-b + sqrt_d) / (2 * a)
        return tuple(sorted((x1, x2)))

    if d == 0:
        return (-b / (2 * a),)

    # d < 0 -> complex conjugate roots
    sqrt_d = cmath.sqrt(d)
    x1 = (-b - sqrt_d) / (2 * a)
    x2 = (-b + sqrt_d) / (2 * a)
    return (x1, x2)


def format_root(x) -> str:
    """Pretty-print a root, dropping trailing .0 and tidying complex numbers."""
    if isinstance(x, complex):
        re, im = x.real, x.imag
        re_s = format_root(re)
        im_s = format_root(abs(im))
        sign = "+" if im >= 0 else "-"
        return f"{re_s} {sign} {im_s}i"
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.6g}"


def parse_coefficient(raw: str, name: str = "coefficient") -> float:
    """Convert user text to a finite float.

    Raises ValueError if the text is not a number or is inf/-inf/nan,
    since those would produce meaningless results in the solver.
    """
    try:
        val = float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw.strip()!r}.") from None
    if not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number, got {raw.strip()!r}.")
    return val


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