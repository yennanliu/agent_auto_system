from datetime import UTC, datetime

import pytest

from src.automation.cron_utils import is_valid_cron, next_fire, normalize_cron


def test_normalize_expands_macros():
    assert normalize_cron("@hourly") == "0 * * * *"
    assert normalize_cron("@daily") == "0 0 * * *"
    assert normalize_cron("@midnight") == "0 0 * * *"
    assert normalize_cron("@weekly") == "0 0 * * 0"
    assert normalize_cron("@monthly") == "0 0 1 * *"


def test_normalize_collapses_whitespace_and_is_case_insensitive():
    assert normalize_cron("  0   8    *  *  * ") == "0 8 * * *"
    assert normalize_cron("@DAILY") == "0 0 * * *"


def test_normalize_leaves_plain_cron_untouched():
    assert normalize_cron("*/15 * * * *") == "*/15 * * * *"


@pytest.mark.parametrize("expr", ["0 8 * * *", "*/15 * * * *", "@daily", "@hourly", "0 0 1 1 *"])
def test_is_valid_true(expr):
    assert is_valid_cron(expr) is True


@pytest.mark.parametrize("expr", ["", "   ", None, "not a cron", "99 99 * * *", "* * *"])
def test_is_valid_false(expr):
    assert is_valid_cron(expr) is False


def test_next_fire_hourly():
    base = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    assert next_fire("0 * * * *", base) == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)


def test_next_fire_daily_at_eight():
    base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_fire("0 8 * * *", base) == datetime(2026, 1, 2, 8, 0, tzinfo=UTC)


def test_next_fire_is_strictly_after_base():
    base = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)  # exactly on a fire time
    assert next_fire("0 * * * *", base) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_next_fire_accepts_macro():
    base = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    assert next_fire("@daily", base) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_next_fire_pins_naive_base_to_utc():
    naive = datetime(2026, 1, 1, 10, 30)  # no tzinfo
    result = next_fire("0 * * * *", naive)
    assert result.tzinfo is not None


def test_next_fire_raises_on_invalid():
    with pytest.raises(ValueError):
        next_fire("nonsense", datetime(2026, 1, 1, tzinfo=UTC))
