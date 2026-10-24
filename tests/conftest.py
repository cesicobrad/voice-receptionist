from datetime import date, datetime

import pytest

from app.booking import BookingService

# Monday 2 November 2026, 09:00. Clinic: Mon/Tue/Thu 08-18, Wed 10-19, Fri 08-14, weekend closed.
NOW = datetime(2026, 11, 2, 9, 0)
MON, TUE, WED, THU, FRI, SAT = (date(2026, 11, d) for d in range(2, 8))


@pytest.fixture
def svc(tmp_path):
    return BookingService(tmp_path / "test.db", now=lambda: NOW)
