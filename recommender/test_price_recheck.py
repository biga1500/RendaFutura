"""Explicit price-date regression tests and the two reviewed cached cases."""

from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path

import pytest

from . import price_recheck as recheck
from .share_basis import b3_evidence, reconcile_prices
from .test_selected_price_research import chart, monthly_points


REPO = Path(__file__).resolve().parent.parent


def row(day=None, quote='20,00', record='30/09/2021'):
    return {'typeStock': 'ON', 'corporateAction': 'DIVIDENDO', 'valueCash': '0,10',
        'quotedPerShares': '1', 'dateClosingPricePriorExDate': day,
        'lastDatePriorEx': record, 'closingPricePriorExDate': quote}


def test_unknown_price_dates_preserve_cash_and_original_rows():
    rows = [row(None, '20,00'), row('31/12/9999', '30,00', '31/12/2021')]
    original = deepcopy(rows)
    result = recheck.explicit_reference_view(rows, 'ON')
    assert rows == original
    assert result['quotes'] == {}
    assert result['cash'] == {date(2021, 9, 30): .1, date(2021, 12, 31): .1}
    assert len(result['undated_prices']) == 2
    for before, after in zip(rows, result['view']):
        assert after == {**before, 'closingPricePriorExDate': None}


def test_real_dated_conflicts_still_block_even_outside_window():
    with pytest.raises(ValueError, match='conflicting_B3_reference_quotes'):
        recheck.explicit_reference_view([row('30/09/2021', '20,00'),
                                         row('30/09/2021', '30,00')], 'ON')
    with pytest.raises(ValueError, match='conflicting_B3_reference_quotes'):
        recheck.explicit_reference_view([row('30/09/2006', '20,00'),
                                         row('30/09/2006', '30,00')], 'ON')


def test_missing_dates_do_not_supply_fourth_anchor_or_early_regime():
    rows = [row(d, '10,00') for d in ('31/01/2022', '28/02/2022', '31/03/2022')]
    typed = recheck.explicit_reference_view(rows + [row(None, '10,00')], 'ON')
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        reconcile_prices(chart(monthly_points()), 'BAZA3', [], typed['quotes'], recheck.END)
    typed = recheck.explicit_reference_view(rows + [row('30/04/2022', '10,00')], 'ON')
    actions = [{'date': '2021-12-03', 'ratio': 2}]
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_prices(chart(monthly_points()), 'BAZA3', actions, typed['quotes'], recheck.END)


def test_wrong_share_class_and_malformed_prices_are_not_hidden():
    other = {**row(None), 'typeStock': 'PN'}
    typed = recheck.explicit_reference_view([row('30/09/2021'), other], 'ON')
    assert typed['view'][1] == other
    assert typed['quotes'] == {date(2021, 9, 30): 20.}
    with pytest.raises(ValueError):
        recheck.explicit_reference_view([row('2021-09-30')], 'ON')
    with pytest.raises(ValueError, match='invalid_B3_reference_quote'):
        recheck.explicit_reference_view([row(None, 'nan')], 'ON')


@pytest.fixture(scope='module')
def context():
    return recheck.Context(REPO)


def test_issuer_receipt_history_does_not_duplicate_cash_pages(context):
    record, rows, _, _, _, pages = context.inputs('BMEB4')
    assert len(rows) == 118
    assert pages == ['raw/expanded/BMEB4-b3-cash-1.json']
    assert 'raw/expanded/BMEB3-b3-cash-1.json' in record['source_files']


@pytest.mark.parametrize(('ticker', 'expected_dates'), [('BRSR5', 25), ('CEEB5', 23)])
def test_cached_reviewed_prices_pass_existing_gate_with_no_source_edits(context, ticker, expected_dates):
    record, original, body, _, actions, _ = context.inputs(ticker)
    before = deepcopy(original)
    _, quotes = b3_evidence(original, record['share_class'])
    core_prices, core_matches, _ = reconcile_prices(body, ticker, actions, quotes, recheck.END)
    assert len(core_matches) == expected_dates
    assert sum(recheck.START <= day <= recheck.END for day in core_prices) == 61
    result = recheck.reviewed_inputs(REPO, record, context=context)
    assert original == before == result['original_cash_rows']
    assert len(result['price_matches']) == expected_dates
    assert sum(recheck.START <= day <= recheck.END for day in result['prices']) == 61
    assert result['actions'] == []
    assert result['undated_prices']
    for name in record['source_files']:
        assert hashlib.sha256((REPO / recheck.MARKET / name).read_bytes()).hexdigest() == context.sources[name]['sha256']


def test_reviewed_adapter_rejects_wrong_identity_and_cutoff(context):
    identity = context.records['BRSR5']
    with pytest.raises(ValueError, match='exact_identity_mismatch'):
        recheck.reviewed_inputs(REPO, {**identity, 'share_class': 'PNB'}, context=context)
    with pytest.raises(ValueError, match='outside_reviewed'):
        recheck.reviewed_inputs(REPO, identity, date(2026, 8, 31), context=context)
    with pytest.raises(ValueError, match='outside_reviewed'):
        recheck.reviewed_inputs(REPO, {'ticker': 'ITSA4'}, context=context)


def test_report_is_immutable_and_confined(tmp_path):
    path = recheck.write_report(tmp_path, {'model_admissions': 0})
    assert path == tmp_path / recheck.REPORT
    assert recheck.write_report(tmp_path, {'model_admissions': 0}) == path
    with pytest.raises(ValueError, match='will_not_be_overwritten'):
        recheck.write_report(tmp_path, {'model_admissions': 1})
    assert not (tmp_path / 'current.json').exists()


def test_focused_source_integration_and_report_without_network():
    report = recheck.build_report(REPO)
    assert report['price_cases_resolved'] == ['BRSR5', 'CEEB5']
    assert report['source_candidates_recovered'] == ['BRSR5', 'CEEB5']
    records = {r['ticker']: r for r in report['records']}
    assert records['BRSR5']['source_replay']['ledger_rows'] == 26
    assert records['CEEB5']['source_replay']['remaining_reason'] is None
    assert records['CEEB5']['source_replay']['ledger_rows'] > 0
    assert all(records[ticker]['baseline']['passed'] for ticker in ('BRSR5', 'CEEB5'))
    assert records['IGTI3']['explicit_date_trial']['reason'] == 'unverified_share_price_regime_requires_raw_history'
    assert records['LREN3']['primary_action_context']['action_ratios_alone_resolve_price_conflicts'] is False
    assert len(records['LREN3']['primary_action_context']['material_conflicts']) == 3
    assert not records['ITSA3']['reexecuted'] and not records['ITSA4']['reexecuted']
    assert report['required_monthly_returns'] == 60
    assert report['model_admissions'] == report['network_requests'] == 0
    assert report['all_read_inputs_hash_verified_unchanged']
    assert not report['mtime_used_as_publication_or_retrieval']
    assert not any(s.get('observed_at_basis') == 'local_file_mtime_not_network_retrieval'
                   for s in report['sources'])
