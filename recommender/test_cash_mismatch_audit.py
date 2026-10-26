from datetime import date
from types import SimpleNamespace

from .audit_cash_mismatches import discrepancies


def rows(values):
    return SimpleNamespace(installments=[{'record_date': '2026-01-10', 'payment_date': day,
        'gross_per_unit': value, 'label': label} for day, value, label in values])


def test_distinct_installments_can_match_one_declared_total():
    source = rows([('2026-03-01', .4, 'DIVIDENDO'), ('2026-11-01', .6, 'JCP')])
    assert discrepancies(source, {date(2026, 1, 10): 1}, [], date(2026, 9, 30)) == []


def test_net_like_ratio_is_diagnostic_never_converted_to_gross():
    result = discrepancies(rows([('2026-03-01', .85, 'JCP')]),
        {date(2026, 1, 10): 1}, [], date(2026, 9, 30))
    assert result[0]['reported_to_declared_ratio'] == .85
    assert result[0]['reported_installment_sum'] == .85
    assert result[0]['reason'] == 'amount_not_explained_by_documented_share_factors'


def test_only_verified_action_ratios_can_explain_adjusted_amounts():
    source = rows([('2026-03-01', .5, 'DIVIDENDO')])
    expected = {date(2026, 1, 10): 1}
    assert discrepancies(source, expected, [{'date': '2026-02-01', 'ratio': 2}], date(2026, 9, 30)) == []
    assert discrepancies(source, expected, [], date(2026, 9, 30))


def test_missing_source_group_is_not_zero():
    result = discrepancies(rows([]), {date(2026, 1, 10): 1}, [], date(2026, 9, 30))
    assert result[0]['reported_installment_sum'] is None
    assert result[0]['reason'] == 'calendar_group_missing'
