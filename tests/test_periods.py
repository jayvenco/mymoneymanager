from datetime import date

from app.services.periods import (
    financial_month,
    financial_month_for,
    months_between,
    shift_ym,
    ym_of,
)


def test_calendar_month():
    p = financial_month("2026-02", 1)
    assert (p.start, p.end) == (date(2026, 2, 1), date(2026, 2, 28))


def test_salary_month_start_25():
    p = financial_month("2026-11", 25)
    assert (p.start, p.end) == (date(2026, 10, 25), date(2026, 11, 24))
    assert p.days == 31


def test_ym_of_boundaries():
    assert ym_of(date(2026, 10, 24), 25) == "2026-10"
    assert ym_of(date(2026, 10, 25), 25) == "2026-11"
    assert ym_of(date(2026, 12, 31), 25) == "2027-01"
    assert ym_of(date(2026, 12, 31), 1) == "2026-12"


def test_period_for_date_contains_date():
    for day in (date(2026, 1, 1), date(2026, 3, 25), date(2026, 12, 31)):
        assert financial_month_for(day, 25).contains(day)


def test_year_wrap_and_shift():
    assert financial_month("2026-01", 25).start == date(2025, 12, 25)
    assert shift_ym("2026-01", -1) == "2025-12"
    assert months_between("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
