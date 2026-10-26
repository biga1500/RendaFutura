"""BPAC5 exact notice, whole cached event and date-only repair regression tests."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket

import pytest

from . import btg_notice_calendar as btg


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def calendar():
    return btg.verified_calendar(ROOT, btg.STOCK)


@pytest.fixture(scope='module')
def inputs(calendar):
    reader = btg.evidence.EvidenceReader(ROOT)
    documents, _ = btg.indexed_documents(reader, btg.CUTOFF)
    payloads = btg.cached_sources(reader)
    parsed = btg.inspector.inspect_payment_page(payloads['expanded/BPAC5-payments.html'], 'BPAC5',
        btg.CUTOFF, datetime(2026, 10, 24, tzinfo=timezone.utc))
    return {'documents': documents, 'text': btg.one_page_text((ROOT / btg.NOTICE).read_bytes()),
        'detail': payloads['expanded/b3-detail-22616.json'],
        'page': payloads['expanded/BPAC3-b3-cash-1.json'], 'parsed': parsed}


def test_exact_notice_all_reported_amounts_dates_and_separate_unusable_latest_form(calendar):
    form = calendar['notice']
    assert form['approval_date'] == form['published_at'] == '2022-12-13'
    assert form['record_date'] == '2022-12-16' and form['ex_date'] == '2022-12-19'
    assert form['payment_date'] == '2023-02-15'
    assert form['amounts'] == {'share_gross': '0.065353099', 'share_net': '0.055550134',
                              'unit_gross': '0.196059297', 'unit_net': '0.166650402'}
    assert form['cash_kind'] == 'JCP' and form['tax_withholding_reported_percent'] == 15
    assert not form['gross_reconstructed_from_net'] and not form['is_settlement_confirmation']
    unusable = calendar['unusable_latest_native_form']
    assert unusable['document']['sequence'] == '566342' and unusable['document']['version'] == 2
    assert unusable['body_is_valid_PDF_but_has_no_issuer_or_event_rows']
    assert not unusable['model_eligible'] and not unusable['older_native_version_used']
    assert not unusable['claim_v2_amended_payment_dates']
    assert not calendar['cash_model_eligible'] and not calendar['parent_admission_performed']


def test_whole_cash_group_has_explicit_ON_PNA_and_reported_UNIT_values(calendar):
    proof = calendar['cash_type_proof']
    assert proof['label'] == 'JCP' and proof['whole_event_complete']
    assert [(r['row'], r['raw']['typeStock'], r['raw']['valueCash']) for r in proof['components']] == [
        (11, 'ON', '0,065353099'), (52, 'PNA', '0,065353099'), (93, 'UNT', '0,196059297')]
    assert not proof['cash_type_inferred_from_amount_or_status']
    assert not proof['UNIT_amount_multiplied_from_share'] and not proof['CVM_notice_reports_ISIN']


def test_all_actual_source_bytes_receipts_and_publication_dates_close(calendar):
    assert len(calendar['evidence_sources']) == 15 and len(calendar['IPE_coverage']) == 6
    assert [r['issuer_rows_before_cutoff'] for r in calendar['IPE_coverage']] == [123, 105, 95, 122, 122, 81]
    for source in calendar['evidence_sources']:
        body = (ROOT / source['file']).read_bytes()
        assert hashlib.sha256(body).hexdigest() == source['sha256']
        assert source['bytes'] == len(body) and source['access'] == 'existing_cache_read_only'
        assert 'retrieved_at' in source and source['roles']
    notice = next(s for s in calendar['evidence_sources'] if s['file'] == btg.NOTICE)
    assert notice['published_at'] == '2022-12-13'
    assert notice['retrieved_at'] == '2026-10-25T11:23:57.3911894Z'
    assert notice['url'] == calendar['selected_documents']['notice']['url']


def test_public_date_only_repair_preserves_raw_vendor_precision_amount_and_all_other_rows(inputs):
    before = deepcopy(inputs['parsed'])
    out = btg.repair_inspected_calendar(ROOT, btg.STOCK, btg.CUTOFF, inputs['parsed'])
    assert inputs['parsed'] == before
    old = {e['source_row']: e for e in before['events']}
    new = {e['source_row']: e for e in out['events']}
    assert list(old) == list(new)
    assert [row for row in old if old[row] != new[row]] == ['table:2:row:13']
    event = new['table:2:row:13']
    assert event['payment_date'] == '2023-02-15' and old[event['source_row']]['payment_date'] == '2022-02-15'
    assert event['record_date'] == '2022-12-16'
    assert event['gross_per_share'] == event['reported_amount'] == old[event['source_row']]['gross_per_share'] == .065353
    assert event['primary_exact_gross_per_share_decimal'] == '0.065353099'
    assert not event['cash_model_eligible'] and not event['financially_confirmed']
    assert not event['full_settlement_on_date_verified'] and event['paid_status_as_of_verified'] is False
    audit = out['btg_notice_calendar_repair']
    assert audit['raw_vendor_amount_text'] == '0,06535300'
    assert audit['vendor_display_decimal_places'] == 8
    assert audit['reviewed_numeric_comparison_decimal_places'] == 6
    assert not audit['display_padding_independently_verified'] and not audit['vendor_gross_amount_changed']
    assert audit['removed_issues'] == [{'code': 'payment_before_record_date', 'blocking': True, 'row': 'table:2:row:13'}]
    assert audit['original_issues'] == before['report']['issues']
    assert out['report']['issues'] == [i for i in before['report']['issues'] if i not in audit['removed_issues']]
    assert out['status'] == 'available'


@pytest.mark.parametrize('fault', ['CNPJ', 'issuer', 'type', 'gross_label', 'missing_net', 'missing_unit',
    'duplicated_amount', 'gross_changed', 'net_changed', 'unit_net_changed', 'record_missing', 'ex_missing',
    'payment_missing', 'bad_payment_year', 'signature', 'published', 'version', 'tax'])
def test_notice_parser_requires_whole_explicit_identity_type_gross_net_and_all_dates(calendar, inputs, fault):
    text = ' '.join(inputs['text'].split())
    doc = deepcopy(calendar['selected_documents']['notice'])
    if fault == 'CNPJ': text = text.replace('30.306.294/0001-45', '30.306.295/0001-45')
    elif fault == 'issuer': text = text.replace('BANCO BTG PACTUAL S.A.', 'OUTRO BANCO S.A.')
    elif fault == 'type': text = text.replace('juros sobre capital pr\u00f3prio complementar', 'dividendos complementares')
    elif fault == 'gross_label': text = text.replace('Valor bruto por a\u00e7\u00e3o ordin\u00e1ria', 'Valor liquido por a\u00e7\u00e3o ordin\u00e1ria')
    elif fault == 'missing_net': text = text.replace('0,055550134', 'nao informado')
    elif fault == 'missing_unit': text = text.replace('0,196059297', 'nao informado')
    elif fault == 'duplicated_amount': text += '\na) Valor bruto por a\u00e7\u00e3o ordin\u00e1ria ou a\u00e7\u00e3o preferencial: R$ 0,065353099;'
    elif fault == 'gross_changed': text = text.replace('0,065353099', '0,075353099')
    elif fault == 'net_changed': text = text.replace('0,055550134', '0,065353099')
    elif fault == 'unit_net_changed': text = text.replace('0,166650402', '0,196059297')
    elif fault == 'record_missing': text = text.replace('no final do dia 16', 'em data a definir 16')
    elif fault == 'ex_missing': text = text.replace('a partir de 19', 'em data futura 19')
    elif fault == 'payment_missing': text = text.replace('ocorrer\u00e1 no dia 15', 'ocorrer\u00e1 em data futura 15')
    elif fault == 'bad_payment_year': text = text.replace('15 de fevereiro de 2023', '15 de fevereiro de 2022')
    elif fault == 'signature': text = text.replace('Rio de Janeiro, 13 de dezembro de 2022', 'Rio de Janeiro, 14 de dezembro de 2022')
    elif fault == 'published': doc['published_at'] = '2022-12-14'
    elif fault == 'version': doc['version'] = 2
    elif fault == 'tax': text = text.replace('15% referente', '20% referente')
    with pytest.raises(btg.evidence.ProofError):
        btg.parse_notice_text(text, doc)


@pytest.mark.parametrize('fault', ['drop_ON', 'drop_PNA', 'drop_UNIT', 'duplicate', 'wrong_class',
    'wrong_type', 'net_amount', 'wrong_UNIT_amount', 'quote_unit', 'ratio', 'approval', 'record',
    'CVM', 'CNPJ', 'ISIN', 'pagination'])
def test_whole_cash_group_never_drops_other_classes_or_infers_UNITs(inputs, calendar, fault):
    detail, page = deepcopy(inputs['detail']), deepcopy(inputs['page'])
    rows = [r for r in page['results'] if r['dateApproval'] == '13/12/2022' and r['lastDatePriorEx'] == '16/12/2022']
    if fault.startswith('drop_'):
        kind = {'drop_ON': 'ON', 'drop_PNA': 'PNA', 'drop_UNIT': 'UNT'}[fault]
        page['results'].remove(next(r for r in rows if r['typeStock'] == kind)); page['page']['totalRecords'] -= 1
    elif fault == 'duplicate': page['results'].append(deepcopy(rows[0])); page['page']['totalRecords'] += 1
    elif fault == 'wrong_class': rows[1]['typeStock'] = 'PN'
    elif fault == 'wrong_type': rows[1]['corporateAction'] = 'DIVIDENDO'
    elif fault == 'net_amount': rows[1]['valueCash'] = '0,055550134'
    elif fault == 'wrong_UNIT_amount': rows[2]['valueCash'] = '0,065353099'
    elif fault == 'quote_unit': rows[1]['quotedPerShares'] = '1000'
    elif fault == 'ratio': rows[1]['ratio'] = '3'
    elif fault == 'approval': rows[1]['dateApproval'] = '14/12/2022'
    elif fault == 'record': rows[1]['lastDatePriorEx'] = '15/12/2022'
    elif fault == 'CVM': detail['codeCVM'] = '99999'
    elif fault == 'CNPJ': detail['cnpj'] = '30306295000145'
    elif fault == 'ISIN': detail['otherCodes'][1]['isin'] = 'BRBPACACNOR7'
    elif fault == 'pagination': page['page']['totalPages'] = 2
    with pytest.raises(btg.evidence.ProofError):
        btg.prove_whole_cash_group(calendar['notice'], detail, page)


@pytest.mark.parametrize('fault', ['extra_target', 'missing_target', 'wrong_ticker', 'wrong_ISIN', 'wrong_type',
    'gross', 'net', 'unit', 'approval', 'well_ordered_different_date', 'missing_blocker', 'extra_blocker',
    'wrong_display_format', 'out_of_reviewed_precision'])
def test_date_repair_does_not_cover_other_source_issues_or_change_amounts(inputs, calendar, fault):
    parsed = deepcopy(inputs['parsed']); raw = '0,06535300'
    event, = [e for e in parsed['events'] if e['record_date'] == calendar['notice']['record_date']]
    if fault == 'extra_target': parsed['events'].append(deepcopy(event))
    elif fault == 'missing_target': parsed['events'].remove(event)
    elif fault == 'wrong_ticker': event['ticker'] = 'BPAC3'
    elif fault == 'wrong_ISIN': event['isin'] = 'BRBPACACNOR7'
    elif fault == 'wrong_type': event['label'] = 'DIVIDENDO'
    elif fault == 'gross': event['gross_per_share'] = .065354
    elif fault == 'net': event['reported_amount'] = .055550134
    elif fault == 'unit': event['quoted_quantity'] = 3
    elif fault == 'approval': event['approval_date'] = '2022-12-14'
    elif fault == 'well_ordered_different_date': event['date_order_valid'] = True; event['payment_date'] = '2023-03-15'
    elif fault == 'missing_blocker': parsed['report']['issues'] = [i for i in parsed['report']['issues'] if i['code'] != 'payment_before_record_date']
    elif fault == 'extra_blocker': parsed['report']['issues'].append({'code': 'unknown_tax_basis', 'blocking': True, 'row': event['source_row']})
    elif fault == 'wrong_display_format': raw = '0,196059297'
    elif fault == 'out_of_reviewed_precision': raw = '0,06535400'; event.update(gross_per_share=.065354, reported_amount=.065354)
    before = deepcopy(parsed)
    with pytest.raises((btg.evidence.ProofError, ValueError)):
        btg._repair_parsed(parsed, calendar, raw)
    assert parsed == before


@pytest.mark.parametrize('role', ['notice', 'board', 'native'])
def test_later_unknown_revision_no_old_notice_or_native_fallback(inputs, calendar, role):
    docs = deepcopy(inputs['documents'])
    current = calendar['selected_documents'][role]
    later = deepcopy(next(d for d in docs if d['sequence'] == current['sequence']))
    later.update(sequence='999999', version=current['version'] + 1, published_at='2023-01-01')
    later['url'] = later['url'].replace(current['sequence'], '999999').replace(
        'numVersao=' + str(current['version']), 'numVersao=' + str(later['version']))
    docs.append(later)
    with pytest.raises(btg.evidence.ProofError, match='unreviewed_latest_document'):
        btg.select_documents(docs)


def test_notice_changed_reference_date_is_still_found_by_full_protocol_family(inputs):
    docs = deepcopy(inputs['documents'])
    later = deepcopy(next(d for d in docs if d['sequence'] == '565973'))
    later.update(sequence='999999', version=2, published_at='2023-01-01', reference_date='2023-01-01',
        protocol='022616IPE131220220204490814-22')
    docs.append(later)
    with pytest.raises(btg.evidence.ProofError, match='unreviewed_latest_document'):
        btg.select_documents(docs)


@pytest.mark.parametrize('name', [btg.evidence.IPE_MANIFEST, btg.NOTICE, btg.NOTICE + '-source.json',
    btg.EMPTY, btg.EMPTY.removesuffix('.pdf') + '-source.json', btg.PAYMENTS,
    'raw/expanded/BPAC3-b3-cash-1.json', 'raw/expanded/b3-detail-22616.json', btg.evidence.EXCHANGE_MANIFEST])
def test_corrupt_actual_original_bytes_fail_closed_without_changing_them(name, monkeypatch, tmp_path):
    original = (ROOT / name).read_bytes(); altered = tmp_path / 'bad.bin'; altered.write_bytes(original + b' ')
    real_safe = btg.evidence.shared._safe
    monkeypatch.setattr(btg.evidence.shared, '_safe', lambda root, ref: altered if ref == name else real_safe(root, ref))
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        btg.verified_calendar(ROOT, btg.STOCK)
    assert (ROOT / name).read_bytes() == original


@pytest.mark.parametrize('key,value', [('ticker', 'BPAC3'), ('cvm', '22616'), ('cvm', 9999),
    ('share_class', 'PN'), ('isin', 'BRBPACACNOR7')])
def test_exact_identity_required(key, value):
    with pytest.raises(btg.evidence.ProofError, match='exact_BPAC5_identity'):
        btg.verified_calendar(ROOT, {**btg.STOCK, key: value})


def test_public_wrapper_is_network_free_and_read_only(inputs, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('network_or_write_forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    real_open = io.open
    def readonly(file, mode='r', *args, **kwargs):
        if any(c in mode for c in 'wax+'): forbidden()
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', readonly)
    assert btg.repair_inspected_calendar(ROOT, btg.STOCK, btg.CUTOFF, inputs['parsed'])['status'] == 'available'


def test_public_wrapper_rejects_unmatched_event_or_issue_edits_even_when_source_hash_is_unchanged(inputs, monkeypatch, calendar):
    monkeypatch.setattr(btg, 'verified_calendar', lambda *args: calendar)
    for change in ('row', 'issue'):
        parsed = deepcopy(inputs['parsed'])
        if change == 'row': parsed['events'][0]['gross_per_share'] = 9999
        else: parsed['report']['issues'].pop()
        with pytest.raises(btg.evidence.ProofError, match='not_exact_original_inspector_output'):
            btg.repair_inspected_calendar(ROOT, btg.STOCK, btg.CUTOFF, parsed)


def test_report_owned_content_addressed_idempotent_only(tmp_path, calendar, monkeypatch):
    monkeypatch.setattr(btg, 'verified_calendar', lambda *args: deepcopy(calendar))
    report, name = btg.build_report(tmp_path)
    original = (tmp_path / name).read_bytes()
    assert report['network_requests'] == 0 and not report['pointers_written']
    assert not report['parent_files_changed'] and not report['parent_admission_performed']
    assert btg.build_report(tmp_path)[1] == name and (tmp_path / name).read_bytes() == original
    assert all(p.relative_to(tmp_path).as_posix().startswith(btg.CACHE + '/') for p in tmp_path.rglob('*') if p.is_file())


def test_frozen_parent_inspector_hook_passes_normal_cash_and_price_gates_no_financial_override(monkeypatch):
    from . import inventory_candidates as parent
    from .market_inventory import load_inventory

    inventory = load_inventory(ROOT)
    row, = [r for r in inventory['stocks'] if r['ticker'] == 'BPAC5']
    original = parent.inspect_payment_page
    reconcile_cash, reconcile_prices = parent.reconcile_payments, parent.reconcile_prices
    audits = []
    def inspected(body, ticker, cutoff, observed):
        parsed = original(body, ticker, cutoff, observed)
        if ticker == 'BPAC5':
            parsed = btg.repair_inspected_calendar(ROOT, {**row, 'isin': btg.STOCK['isin']}, cutoff, parsed)
            audits.append(parsed['btg_notice_calendar_repair'])
        return parsed
    monkeypatch.setattr(parent, 'inspect_payment_page', inspected)
    candidates, pending, _ = parent.build_candidates(ROOT, {**inventory, 'stocks': [row]}, btg.CUTOFF)
    assert pending == []
    candidate, = candidates
    assert candidate['ticker'] == 'BPAC5'
    assert candidate['checks']['independent_cash_matches'] == 15
    assert candidate['checks']['independent_price_matches'] == 15
    assert candidate['checks']['share_regime'] == 'split_adjusted'
    assert candidate['financial_basis_start'] == '2021-07-01'
    assert candidate['checks']['extended_training_history'] == {
        'status': 'not_used', 'reason': 'historical_quote_or_share_scale_disagrees_with_B3'}
    assert not candidate['financially_validated']
    assert candidate['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert all(not a['vendor_gross_amount_changed'] for a in audits)
    assert parent.reconcile_payments is reconcile_cash and parent.reconcile_prices is reconcile_prices
