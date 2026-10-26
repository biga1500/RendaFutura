from datetime import date

import pytest

from .collect_income_data import payment_in_window


@pytest.mark.parametrize('day,included', [('2021-09-30', False), ('2021-10-01', True),
                                         ('2021-10-15', True), ('2021-10-31', True),
                                         ('2026-09-30', True), ('2026-10-01', False)])
def test_payment_window_includes_every_day_of_first_return_month(day, included):
    assert payment_in_window(date.fromisoformat(day), date(2021, 10, 31), date(2026, 9, 30)) is included
