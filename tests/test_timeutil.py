from datetime import UTC, datetime

from polledge.timeutil import local_to_utc, to_local, utcnow


def test_utcnow_is_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds()) < 5


def test_prague_conversion_with_daylight_saving():
    assert local_to_utc(datetime(2026, 10, 27, 10, 0)) == datetime(2026, 10, 27, 9, 0)  # CET, UTC+1
    assert local_to_utc(datetime(2026, 7, 1, 10, 0)) == datetime(2026, 7, 1, 8, 0)  # CEST, UTC+2
    assert to_local(datetime(2026, 7, 1, 8, 0)) == datetime(2026, 7, 1, 10, 0)
    assert to_local(None) is None
