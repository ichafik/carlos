"""Simple age calculator.

Run it directly and enter your date of birth in YYYY-MM-DD format
to see your age in years.
"""

from datetime import date, datetime


def parse_date_of_birth(text: str) -> date:
    """Parse a YYYY-MM-DD string into a date. Raises ValueError if invalid."""
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def calculate_age(birth_date: date, today: date | None = None) -> int:
    """Return the age in full years for the given birth date.

    `today` can be supplied to make the function deterministic (useful in tests).
    """
    if today is None:
        today = date.today()

    if birth_date > today:
        raise ValueError("Date of birth cannot be in the future.")

    age = today.year - birth_date.year
    # Subtract a year if the birthday hasn't happened yet this year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def main() -> None:
    raw = input("Enter your date of birth (YYYY-MM-DD): ")
    try:
        birth_date = parse_date_of_birth(raw)
        age = calculate_age(birth_date)
    except ValueError as exc:
        print(f"Invalid input: {exc}")
        return
    print(f"You are {age} years old.")


if __name__ == "__main__":
    main()