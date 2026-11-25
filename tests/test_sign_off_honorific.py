"""A persona name with an honorific signs off with the first real name."""
import pytest

from sellbird.email_style import sign_off_first_name, with_sign_off


@pytest.mark.parametrize("name,expected", [
    ("Dr. Jane Roe", "Jane"),
    ("Dr Jane Roe", "Jane"),
    ("Prof. Jane Roe", "Jane"),
    ("د. سارة أحمد", "سارة"),
    ("Jane Roe", "Jane"),
    ("Dr.", "Dr."),
    ("", ""),
])
def test_sign_off_first_name(name, expected):
    assert sign_off_first_name(name) == expected


def test_sign_off_is_not_the_title():
    assert with_sign_off("Hi,\n\nShort note.", "Dr. Jane Roe").endswith("\n\nJane")
    assert not with_sign_off("Hi,\n\nShort note.", "Dr. Jane Roe").endswith("\n\nDr.")
