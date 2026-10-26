"""Pinned four-code replay and fail-closed adapter/acquisition tests."""

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import urllib.error

import pytest

from . import corporate_action_research as module


MARKET = Path(__file__).parent / 'data/market'


def identity(ticker):
    cvm, cnpj, share, isin = module.IDENTITIES[ticker]
    return {'ticker': ticker, 'cvm': cvm, 'cnpj': cnpj, 'share_class': share, 'isin': isin}


@pytest.fixture(scope='module')
def documents():
    return module.load_documents(MARKET)


def supplement(ticker):
    return (MARKET / f'raw/expanded/b3-supplement-{ticker[:4]}.json').read_bytes()


def canonical(ticker, documents, *, as_of='2026-09-30', start='2021-09-30'):
    return module.canonicalize_supplement(identity(ticker), supplement(ticker), documents,
                                         as_of, window_start=start)


@pytest.mark.parametrize('ticker', module.IDENTITIES)
def test_genuine_non_cash_events_remain_blocked_not_renamed(ticker, documents):
    result = canonical(ticker, documents)
    assert len(result['non_income_events']) == 1
    action = result['non_income_events'][0]
    assert action['income_amount'] == '0'
    assert action['principal_in_kind_not_income'] and action['holder_share_factor'] == '1'
    assert not action['cash_dividend_or_JCP'] and not action['is_own_share_split']
    assert result['blockers']
    assert not result['model_admission_granted'] and not result['prices_or_cash_modified']
    assert not result['settlement_confirmed'] and not result['point_in_time_archive_verified']
    assert not action['settlement_confirmed']
    assert result['supplement_sha256'] == hashlib.sha256(supplement(ticker)).hexdigest()


@pytest.mark.parametrize('ticker', ['BRAP3', 'BRAP4'])
def test_bradespar_transfers_vale_instead_of_multiplying_own_shares(ticker, documents):
    result = canonical(ticker, documents)
    action = result['non_income_events'][0]
    assert action['kind'] == 'in_kind_capital_return'
    assert action['received_isin'] == 'BRVALEACNOR0' and action['received_security'] == 'VALE3'
    assert action['received_units_per_origin_unit'] == '0.332373453'
    assert action['record_date'] == '2021-12-16'
    assert action['ex_date'] == '2021-12-17'
    assert action['announced_delivery_date'] == '2021-12-20'
    assert action['quantity_rounding'] == 'floor_whole_shares'
    assert result['canonical_share_actions'] == []  # Bonus predates this return window.
    assert [item['sequence'] for item in action['evidence']] == ['440701', '451548']
    assert result['blockers'] == ['in_kind_capital_return_needs_multi_asset_history']


@pytest.mark.parametrize('ticker,interim_isin', [('ITUB3', 'BRXPARACNOR9'), ('ITUB4', 'BRXPARACNPR6')])
def test_itau_keeps_origin_and_has_separate_one_to_one_xpart_entitlement(ticker, interim_isin, documents):
    result = canonical(ticker, documents)
    action = result['non_income_events'][0]
    assert action['kind'] == 'spin_off_and_subsequent_merger'
    assert action['xpart_isin'] == interim_isin and action['xpart_units_per_origin_unit'] == '1'
    assert action['holder_share_factor'] == '1'
    assert action['xpart_units_per_final_received_unit'] == '43.3128323'
    assert float(action['final_received_units_per_origin_unit']) == pytest.approx(1 / 43.3128323)
    assert action['final_received_isin'] is None and action['announced_delivery_date'] is None
    assert action['record_date'] == '2021-10-01' and action['ex_date'] == '2021-10-04'
    assert action['merger_approved_at'] == '2021-10-01'
    assert len(result['raw_in_window_actions']) == 3  # Both spin-off legs plus December bonus.
    assert result['blockers'] == ['spin_off_needs_multi_asset_history']
    assert not action['reference_opening_is_executed_quote']


@pytest.mark.parametrize('ticker', ['ITUB3', 'ITUB4'])
def test_only_real_same_class_bonuses_generate_standard_factors(ticker, documents):
    result = canonical(ticker, documents)
    actions = result['canonical_share_actions']
    assert [(item['date'], item['ratio']) for item in actions] == [
        ('2025-03-18', 1.10), ('2025-12-26', 1.03)]
    assert [item['present_in_cached_supplement'] for item in actions] == [False, True]
    for action in actions:
        assert action['same_security_ratio_primary_verified']
        assert action['unchanged_independent_price_regime_checks_still_required']
        assert not action['automatic_price_factor_application_allowed']
        assert action['isin'] == identity(ticker)['isin']
    assert all(item['ratio'] not in (2, 100) for item in actions)


def test_bradespar_rounded_headline_and_exact_cached_count_ratio_not_silently_replaced(documents):
    result = canonical('BRAP3', documents, start='2021-08-31')
    assert result['canonical_share_actions'][0]['ratio'] == 1.1295
    assert not result['canonical_share_actions'][0]['automatic_price_factor_application_allowed']
    assert result['source_issues'][0]['cached_exchange_factor'] == '1.1294774625'
    assert 'bonus_ratio_precision_difference_requires_explicit_basis' in result['blockers']


@pytest.mark.parametrize('ticker', module.IDENTITIES)
def test_all_inputs_are_preserved(ticker, documents):
    original_identity = identity(ticker)
    raw = supplement(ticker)
    before = deepcopy(documents)
    module.canonicalize_supplement(original_identity, raw, documents, '2026-09-30', window_start='2021-09-30')
    assert original_identity == identity(ticker) and documents == before and raw == supplement(ticker)


@pytest.mark.parametrize('ticker', module.IDENTITIES)
@pytest.mark.parametrize('field,value', [('cvm', True), ('cvm', 19349), ('share_class', 'UNIT'),
                                        ('isin', 'BRVALEACNOR0'), ('cnpj', '00000000000000')])
def test_cross_issuer_or_class_identity_is_rejected(ticker, field, value, documents):
    row = {**identity(ticker), field: value}
    with pytest.raises(ValueError, match='identity_mismatch|CNPJ_mismatch'):
        module.canonicalize_supplement(row, supplement(ticker), documents, '2026-09-30', window_start='2021-09-30')


@pytest.mark.parametrize('ticker', module.IDENTITIES)
def test_raw_format_changes_or_unsupported_label_cannot_be_blindly_relabelled(ticker, documents):
    modified = json.loads(supplement(ticker))
    modified[0]['stockDividends'][0]['label'] = 'BONIFICACAO'
    with pytest.raises(ValueError, match='unreviewed_corporate_action_supplement_hash'):
        module.canonicalize_supplement(identity(ticker), json.dumps(modified).encode(), documents,
                                        '2026-09-30', window_start='2021-09-30')


@pytest.mark.parametrize('sequence', module.PINNED_DOCUMENTS)
def test_every_pinned_primary_receipt_has_exact_bytes_index_issuer_and_date(sequence, documents):
    raw, receipt = documents[sequence]
    proof = module._proof(documents, sequence, date(2026, 9, 30))
    assert proof['sha256'] == hashlib.sha256(raw).hexdigest()
    assert proof['sequence'] == sequence
    assert raw.startswith(b'%PDF-') and receipt['content_type'] == 'text/html'
    assert proof['published_at'] == module.PINNED_DOCUMENTS[sequence][1]


@pytest.mark.parametrize('field,value', [('cvm', 18724), ('cnpj', '03847461000192'),
    ('sequence', '451548'), ('version', 2), ('published_at', '2021-01-01'),
    ('url', 'https://www.b3.com.br/not-requested'), ('sha256', '0' * 64),
    ('index_sha256', '0' * 64), ('file', '../escape.pdf'), ('bytes', 1)])
def test_primary_identity_and_hash_metadata_tampering_fails(field, value, documents):
    mutated = deepcopy(documents)
    raw, receipt = mutated['429639']
    receipt[field] = value
    with pytest.raises(ValueError, match='pinned_primary_action_identity_mismatch'):
        module._proof(mutated, '429639', date(2026, 9, 30))


def test_primary_body_mutation_cannot_be_blessed_by_updating_its_receipt(documents):
    mutated = deepcopy(documents)
    raw, receipt = mutated['429639']
    raw += b' modified'
    receipt.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    mutated['429639'] = raw, receipt
    with pytest.raises(ValueError, match='pinned_primary_action_hash_mismatch'):
        module._proof(mutated, '429639', date(2026, 9, 30))


def test_primary_publication_cannot_be_backdated_to_entitlement_date(documents):
    with pytest.raises(ValueError, match='primary_action_publication_after_as_of'):
        module._proof(documents, '429639', date(2021, 10, 1))


def test_effective_event_dates_not_credit_or_foreign_ADR_dates(documents):
    march = canonical('ITUB3', documents, as_of='2025-03-18', start='2025-01-01')
    assert [(item['date'], item['credit_date']) for item in march['canonical_share_actions']] == [
        ('2025-03-18', '2025-03-20')]
    december = canonical('ITUB4', documents, as_of='2025-12-26', start='2025-12-01')
    assert december['canonical_share_actions'][0]['date'] == '2025-12-26'
    assert december['canonical_share_actions'][0]['record_date'] == '2025-12-23'
    assert december['canonical_share_actions'][0]['credit_date'] == '2025-12-30'


def test_window_after_capital_events_does_not_manufacture_new_event(documents):
    for ticker in module.IDENTITIES:
        result = canonical(ticker, documents, start='2026-01-01')
        assert not result['non_income_events'] and not result['canonical_share_actions']
        assert not result['model_admission_granted']


@pytest.mark.parametrize('window,as_of', [('2026-09-30', '2026-09-30'),
    ('2026-10-01', '2026-09-30'), ('2021-01-01', '30/09/2026')])
def test_invalid_time_windows_fail(documents, window, as_of):
    with pytest.raises(ValueError):
        canonical('ITUB3', documents, start=window, as_of=as_of)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denials_stop_without_retry_or_different_endpoint(tmp_path, monkeypatch, documents, status):
    _, receipt = documents['429639']
    document = module.index_document(MARKET, '429639')
    monkeypatch.setattr(module, 'index_document', lambda *_: document)
    calls = []

    class Denied:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'Denied', None, None)

    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *_: Denied())
    with pytest.raises(urllib.error.HTTPError) as error:
        module.acquire_document(tmp_path, '429639')
    assert error.value.code == status
    with pytest.raises(ValueError, match='primary_host_access_denied_no_retry'):
        module.acquire_document(tmp_path, '429639')
    assert calls == [receipt['url']]
    assert len(list((tmp_path / module.CACHE).glob('http-denial-*.json'))) == 1


@pytest.mark.parametrize('target', ['https://www.b3.com.br/anything', 'https://www.rad.cvm.gov.br/other',
                                  'https://example.org/anything'])
def test_redirects_are_not_followed(target):
    with pytest.raises(ValueError, match='redirect_not_followed'):
        module._NoRedirect().redirect_request(None, None, 302, '', {}, target)


@pytest.mark.parametrize('relative', ['../escape', '/tmp/escape', 'raw/../escape', r'raw\escape'])
def test_proof_paths_cannot_escape_owned_cache(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe_proof_path'):
        module._path(tmp_path, relative)


def test_immutable_cache_never_overwrites_existing_proof(tmp_path):
    target = tmp_path / 'proof'
    module._immutable(target, b'original')
    module._immutable(target, b'original')
    with pytest.raises(ValueError, match='immutable_proof_conflict'):
        module._immutable(target, b'replacement')
    assert target.read_bytes() == b'original'


def test_actual_unchanged_price_gate_exposes_additional_brap_vendor_regime_problem():
    result = module.inspect_cached_price_regimes(MARKET)
    records = {row['ticker']: row for row in result['records']}
    for ticker, raw_price in [('BRAP3', 54), ('BRAP4', 55.78)]:
        record = records[ticker]
        assert not record['price_gate_pass']
        assert record['reason'] == 'historical_quote_or_share_scale_disagrees_with_B3'
        anchor = next(row for row in record['anchors'] if row['date'] == '2021-12-16')
        assert anchor['independent_raw_quote'] == raw_price
        assert anchor['cached_vendor_quote'] < raw_price * .55
        assert not record['reconciliation_thresholds_changed']
    for ticker in ['ITUB3', 'ITUB4']:
        assert records[ticker]['price_gate_pass']
        assert records[ticker]['price_regime'] == 'split_adjusted'
        assert records[ticker]['independent_matches'] >= 4
    assert result['network_requests'] == 0 and not result['prices_or_cash_modified']


def test_no_exchange_request_or_activation_when_writing_report(monkeypatch):
    pointer = MARKET / 'current.json'
    before = pointer.read_bytes()
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *_: pytest.fail('offline proof replay must not request a URL'))
    path, report = module.write_report(MARKET)
    assert pointer.read_bytes() == before
    assert report['newly_model_ready_stocks'] == 0
    assert not report['parent_modules_edited'] and not report['dataset_activated']
    assert report['B3_network_requests'] == 0 and not report['missing_data_imputed']
    assert len(report['records']) == 4 and len(report['source_receipts']) == 13
    assert path.is_relative_to(MARKET / module.CACHE)
    assert path.read_bytes() == module._json_bytes(report)
