from datetime import date

import pytest

from app.services.profiling import inference as inf


@pytest.mark.parametrize(
    ("value", "tag"),
    [
        ("", inf.BLANK),
        ("   ", inf.BLANK),
        (None, inf.BLANK),
        ("n/a", inf.PLACEHOLDER),
        ("unknown", inf.PLACEHOLDER),
        ("-", inf.PLACEHOLDER),
        ("10103", inf.INTEGER),
        (60, inf.INTEGER),
        ("1475.0", inf.DECIMAL),
        ("$7,050,000", inf.NUMERIC_TEXT),
        ("38.60M", inf.NUMERIC_TEXT),
        ("USD 60", inf.NUMERIC_TEXT),
        ("1,475.00", inf.NUMERIC_TEXT),
        ("ACT-0003710", inf.IDENTIFIER),
        ("SUB-00415", inf.IDENTIFIER),
        ("CT-000102", inf.IDENTIFIER),
        ("jane.smith@example.com", inf.EMAIL),
        ("JANE.SMITH@EXAMPLE.COM", inf.EMAIL),
        ("jane.smith@", inf.EMAIL_MALFORMED),
        ("jane.smith@example.com.", inf.EMAIL_MALFORMED),
        ("jane.smith at example.com", inf.EMAIL_MALFORMED),
        ("https://www.rivera.com", inf.URL),
        ("www.rivera.com", inf.URL_NO_SCHEME),
        ("2020/03/28", inf.DATE),
        ("06/20/22", inf.DATE),
        ("09-Feb-2022", inf.DATE),
        ("March 5, 2021", inf.DATE),
        ("2024-08-26T09:30:00Z", inf.DATETIME),
        ("08/26/2024 09:30", inf.DATETIME),
        ("26-Aug-2024 09:30 AM", inf.DATETIME),
        ("2024-08-26 09:30:00-05:00", inf.DATETIME),
        ("(415) 555-2671", inf.PHONE),
        ("415-555-2671", inf.PHONE),
        ("+1 415 555 2671", inf.PHONE),
        ("555-2671", inf.PHONE_SHORT),
        ("MFG", inf.CODE),
        ("CA", inf.CODE),
        ("Active", inf.TEXT),
        ("Rivera, Garcia and Kirk", inf.TEXT),
    ],
)
def test_tag_value(value, tag) -> None:
    assert inf.tag_value(value) == tag


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("12", 12.0),
        ("12.5", 12.5),
        ("$7,050,000", 7_050_000.0),
        ("38.60M", 38_600_000.0),
        ("2K", 2_000.0),
        ("USD 60", 60.0),
        ("1,475.00", 1475.0),
        ("12%", 12.0),
        ("twelve", None),
        ("ACT-0003710", None),
    ],
)
def test_parse_number(text, number) -> None:
    assert inf.parse_number(text) == number


def test_parse_date_reports_the_format_it_used() -> None:
    assert inf.parse_date("2020-03-28") == (date(2020, 3, 28), "%Y-%m-%d")
    assert inf.parse_date("03/28/2020") == (date(2020, 3, 28), "%m/%d/%Y")
    assert inf.parse_date("28-Mar-2020") == (date(2020, 3, 28), "%d-%b-%Y")
    assert inf.parse_date("March 28, 2020") == (date(2020, 3, 28), "%B %d, %Y")
    assert inf.parse_date("not a date") is None


def test_phone_digits() -> None:
    assert inf.phone_digits("(415) 555-2671") == "4155552671"
    assert inf.phone_digits("+1 415 555 2671") == "14155552671"
    assert inf.phone_digits("555-2671") == "5552671"
    assert inf.phone_digits("call me") is None


def test_shape() -> None:
    assert inf.shape("CT-000102") == "AA-999999"
    assert inf.shape("Jane Smith") == "Aaaa Aaaaa"
    assert inf.shape("2020-03-28") == "9999-99-99"
