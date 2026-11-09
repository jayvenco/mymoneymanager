import pytest

from app.utils.money import MoneyError, format_eur, parse_money


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1234,56", 123456),
        ("1234.56", 123456),
        ("1.234,56", 123456),
        ("1,234.56", 123456),
        ("€ 12", 1200),
        ("0,5", 50),
        ("12.5", 1250),
        ("1.234", 123400),
        ("-12,30", -1230),
        ("0.125", None),
        ("1.234.567,89", 123456789),
    ],
)
def test_parse_money(text, cents):
    if cents is None:
        with pytest.raises(MoneyError):
            parse_money(text)
    else:
        assert parse_money(text) == cents


@pytest.mark.parametrize("bad", ["", "abc", "12,345", "1,2,3x", "  "])
def test_parse_money_rejects(bad):
    with pytest.raises(MoneyError):
        parse_money(bad)


def test_format_eur():
    assert format_eur(123456) == "€ 1.234,56"
    assert format_eur(5) == "€ 0,05"
    assert format_eur(-100) == "-€ 1,00"
    assert format_eur(100000000) == "€ 1.000.000,00"
    assert format_eur(1500, sign=True) == "+€ 15,00"


def test_roundtrip_is_exact():
    for cents in (0, 1, 99, 100, 123456789):
        assert parse_money(format_eur(cents)) == cents
