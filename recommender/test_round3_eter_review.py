"""Independent offline review of ETER originals and cross-year revision isolation."""
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
from unittest.mock import patch
import zipfile

import pytest

from . import cash_repair_proofs as generic
from . import inventory_candidates as candidates
from . import round3_eter_cash as eter
from .collect_market_data import financial_features, shift_month, stock_observations
from .inventory_financials import prepare_financials
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page
from .share_basis import factor_after


ROOT = Path(__file__).parent / 'data/market'
REPORT = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025/round3-eter-independent-review.json'
REVIEWED_CODE = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
    Path(eter.__file__), Path(generic.__file__))}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('independent review attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def inventory():
    return load_inventory(ROOT)


@pytest.fixture(scope='module')
def identity(inventory):
    return next(r for r in inventory['stocks'] if r['ticker'] == 'ETER3')


@pytest.fixture
def cache(inventory):
    return candidates._Cache(ROOT, inventory)


@pytest.fixture(scope='module')
def evidence(inventory, identity):
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')):
        return eter.load_proof(identity, generic.CUTOFF, candidates._Cache(ROOT, inventory))


def original(cache):
    body, observed = cache.read('raw/expanded/ETER3-payments.html', 'independent_original_payment_review')
    assert hashlib.sha256(body).hexdigest() == eter.PAYMENT_SHA
    return inspect_payment_page(body, 'ETER3', generic.CUTOFF, observed)


def repaired(value, cache, evidence, identity):
    with patch.object(eter, 'load_proof', return_value=deepcopy(evidence)):
        return eter.apply_exact_repairs(value, identity, generic.CUTOFF, cache)


@pytest.mark.parametrize('record,gross,ex,payment,published,sequence,version', [
    ('2023-11-10', '0.07661590000', '2023-11-13', '2024-03-28', '2024-03-20', '734624', 2),
    ('2024-11-08', '0.05202290000', '2024-11-11', '2025-03-31', '2024-11-07', '824462', 1),
])
def test_exact_native_identity_gross_and_separate_date_roles(evidence, record, gross, ex, payment, published, sequence, version):
    fact = next(f for f in evidence['facts'] if f['record_date'] == record)
    assert {k: fact[k] for k in eter.IDENTITY} == eter.IDENTITY
    assert fact['gross_per_share_decimal'] == gross and fact['label'] == 'JCP'
    assert (fact['ex_date'], fact['payment_date'], fact['published_at']) == (ex, payment, published)
    assert (fact['source_locator']['form_sequence'], fact['source_locator']['form_version']) == (sequence, version)
    assert fact['gross_reconstructed_from_net'] is False


def test_actual_final_amendment_is_named_not_calendar_inference(cache):
    _, pages, document, _ = eter.native(cache, eter.SPECS['amendment'])
    assert document['version'] == 2 and document['published_at'] == '2024-03-19'
    assert eter.verify_amendment(pages) == {
        'initial_payment_date': '2024-03-29', 'final_payment_date': '2024-03-28',
        'amendment_published_at': '2024-03-19', 'date_inferred_from_holiday': False}
    changed = deepcopy(pages)
    changed[0] = changed[0].replace('28/03/2024', '27/03/2024')
    with pytest.raises(generic.ProofError, match='explicit_named_distribution'):
        eter.verify_amendment(changed)


def test_cross_year_latest_is_unique_for_actual_eter_reference(cache):
    doc, source = generic.index_document(cache, 2024, '734624', 2, 5762, eter.CNPJ,
                                         '2023-11-07', 'relatorio proventos')
    audit = source['revision_search']
    rows = audit['same_family_or_reference_candidates']
    assert {(generic.cvm.document_id(r['Link_Download'])) for r in rows} == {('683945', 1), ('734624', 2)}
    assert len(audit['index_sources']) == 4
    links = [r['native_revision_link'] for r in rows if 'native_revision_link' in r]
    assert len(links) == 1
    for side in ('selected', 'candidate'):
        assert links[0][side]['protocolo_provento'] == '1158896'
        assert links[0][side]['protocolo_ipe'] == '005762IPE071120230104523712-09'
        assert links[0][side]['isin_scope'] == ['BRETERACNOR3']
    assert doc['published_at'] == '2024-03-20'
    with pytest.raises(generic.ProofError, match='latest_native_revision_required_no_fallback'):
        generic.index_document(cache, 2023, '683945', 1, 5762, eter.CNPJ,
                               '2023-11-07', 'relatorio proventos')


def test_all_originals_and_receipts_still_hash_match(evidence):
    assert len({(s['file'], s['sha256']) for s in evidence['sources']}) == len(evidence['sources'])
    for source in evidence['sources']:
        assert hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
        assert source.get('settlement_verified', False) is False
        assert source.get('point_in_time_archive_verified', False) is False
    assert any(s.get('retrieval_basis') == 'actual_public_GET_receipt' for s in evidence['sources'])
    assert evidence['contract']['original_source_bytes_modified'] is False


@pytest.mark.parametrize('field,value', [('cvm', 1), ('share_class', 'PN'), ('isin', 'BRETERACNOR0')])
def test_wrong_exact_identity_rejected(cache, identity, field, value):
    with pytest.raises(generic.ProofError, match='exact_CVM_class_ISIN'):
        eter.load_proof({**identity, field: value}, generic.CUTOFF, cache)


def test_other_cutoff_rejected(cache, identity):
    with pytest.raises(generic.ProofError, match='eter_exact_reference_required'):
        eter.load_proof(identity, date(2026, 10, 1), cache)


def test_whole_groups_replaced_once_without_source_mutation(cache, evidence, identity):
    before = original(cache)
    frozen = deepcopy(before)
    after = repaired(before, cache, evidence, identity)
    assert before == frozen and len(after['events']) == len(before['events'])
    assert not any(r.get('record_date') == '2023-11-12' for r in after['events'])
    selected = [r for r in after['events'] if r.get('exact_cash_repair_id') == eter.REPAIR_ID]
    assert len(selected) == 2
    assert len({r['primary_declaration_id'] for r in selected}) == 2
    for fact in evidence['facts']:
        group = [r for r in after['events'] if r.get('record_date') == fact['record_date']]
        assert len(group) == 1 and group[0]['gross_per_share_decimal'] == fact['gross_per_share_decimal']
        assert group[0]['settlement_verified'] is group[0]['paid_status_as_of_verified'] is False
    untouched_before = [r for r in before['events'] if r.get('record_date') not in ('2023-11-12', '2024-11-08')]
    untouched_after = [r for r in after['events'] if r.get('exact_cash_repair_id') != eter.REPAIR_ID]
    assert untouched_before == untouched_after
    assert repaired(after, cache, evidence, identity) == after


@pytest.mark.parametrize('change', ['duplicate_origin', 'occupied_target', 'changed_amount', 'reintroduced_origin'])
def test_duplicates_and_changed_whole_groups_fail_closed(cache, evidence, identity, change):
    value = original(cache)
    first = next(r for r in value['events'] if r.get('record_date') == '2023-11-12')
    if change == 'duplicate_origin':
        value['events'].append(deepcopy(first))
    elif change == 'occupied_target':
        value['events'].append({**first, 'record_date': '2023-11-10'})
    elif change == 'changed_amount':
        first['gross_per_share'] = .07
    else:
        value = repaired(value, cache, evidence, identity)
        value['events'].append(deepcopy(first))
    with pytest.raises(generic.ProofError):
        repaired(value, cache, evidence, identity)


class MemoryIndex:
    """Synthetic CSV unit fixtures only; never a primary source or exchange response."""
    def __init__(self, rows_by_year):
        self.files, self.pins = {}, {}
        for year in range(2023, 2027):
            rows = rows_by_year.get(year, [])
            stream = io.StringIO()
            columns = ['Codigo_CVM', 'Nome_Companhia', *generic.REVISION_FIELDS]
            writer = csv.DictWriter(stream, fieldnames=columns, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(f'ipe_cia_aberta_{year}.csv', stream.getvalue().encode('latin1'))
            body = buffer.getvalue()
            digest = hashlib.sha256(body).hexdigest()
            self.pins[year] = digest
            self.files[f'raw/primary-research/cvm-documents/index-{year}-{digest}.zip'] = body

    def read(self, filename, role):
        return self.files[filename], datetime(2026, 10, 25, tzinfo=timezone.utc)

    def register_research_evidence(self, source):
        assert hashlib.sha256(self.files[source['file']]).hexdigest() == source['sha256']


def fixture_row(sequence, version, delivery, *, family='1111-11', reference='2023-11-07',
                category='Relatorio Proventos', protocol=None):
    return {'Codigo_CVM': '99999', 'Nome_Companhia': 'Synthetic unit test issuer',
        'CNPJ_Companhia': '00.000.000/0001-00', 'Categoria': category,
        'Data_Referencia': reference, 'Data_Entrega': delivery, 'Versao': str(version),
        'Protocolo_Entrega': f'099999IPE07112023{version:02d}{family}',
        'Link_Download': 'https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?'
            f'Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo={protocol or sequence}'
            f'&numSequencia={sequence}&numVersao={version}'}


def indexed(memory, sequence='1001', version=1, reference='2023-11-07'):
    return generic.index_document(memory, 2023, sequence, version, 99999,
        '00.000.000/0001-00', reference, 'relatorio proventos')


@pytest.mark.parametrize('basis', ['family', 'protocol'])
def test_cross_year_family_or_protocol_detects_changed_reference(monkeypatch, basis):
    first = fixture_row('1001', 1, '2023-11-08')
    next_row = fixture_row('2001', 2, '2024-03-20', reference='2024-03-19',
        family='1111-11' if basis == 'family' else '2222-22',
        protocol='2001' if basis == 'family' else '1001')
    if basis == 'protocol':
        first['Protocolo_Entrega'] = next_row['Protocolo_Entrega'] = ''
    memory = MemoryIndex({2023: [first], 2024: [next_row]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    with pytest.raises(generic.ProofError, match='latest_native_revision_required_no_fallback'):
        indexed(memory)


@pytest.mark.parametrize('reason', ['other_category', 'after_cutoff'])
def test_unrelated_category_or_future_publication_does_not_supersede(monkeypatch, reason):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 2, '2026-10-01' if reason == 'after_cutoff' else '2024-03-20',
        category='Aviso aos Acionistas' if reason == 'other_category' else 'Relatorio Proventos')
    memory = MemoryIndex({2023: [first], 2026 if reason == 'after_cutoff' else 2024: [other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    doc, _ = indexed(memory)
    assert doc['sequence'] == '1001'


def test_generic_independent_families_same_reference_must_not_supersede(monkeypatch):
    first = fixture_row('1001', 1, '2023-11-08', family='1111-11')
    unrelated = fixture_row('2001', 2, '2024-03-20', family='2222-22')
    memory = MemoryIndex({2023: [first], 2024: [unrelated]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    doc, _ = indexed(memory)
    assert doc['sequence'] == '1001'
    doc, _ = generic.index_document(memory, 2024, '2001', 2, 99999,
        '00.000.000/0001-00', '2023-11-07', 'relatorio proventos')
    assert doc['sequence'] == '2001'


def test_conflicting_nonempty_families_with_same_transport_protocol_fail_closed(monkeypatch):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 2, '2024-03-20', family='2222-22', protocol='1001')
    memory = MemoryIndex({2023: [first], 2024: [other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    with pytest.raises(generic.ProofError, match='conflicting_explicit_revision_families'):
        indexed(memory)


@pytest.mark.parametrize('same_provento', [True, False])
def test_blank_families_require_native_distribution_link_not_same_reference(monkeypatch, same_provento):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 2, '2024-03-20')
    parent = fixture_row('9001', 1, '2023-11-07', category='Ata')
    first['Protocolo_Entrega'] = other['Protocolo_Entrega'] = ''
    memory = MemoryIndex({2023: [first, parent], 2024: [other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)

    def unit_link(cache, row, code):
        return {'protocolo_provento': '700' if
            generic.cvm.document_id(row['Link_Download'])[0] == '1001' or same_provento else '701',
            'protocolo_ipe': parent['Protocolo_Entrega'], 'isin_scope': ['BRUNITACNOR0'], 'sources': []}

    monkeypatch.setattr(generic, '_structured_revision_link', unit_link)
    if same_provento:
        with pytest.raises(generic.ProofError, match='latest_native_revision_required_no_fallback'):
            indexed(memory)
    else:
        doc, source = indexed(memory)
        assert doc['sequence'] == '1001'
        assert len(source['revision_search']['same_family_or_reference_candidates']) == 1


def test_unknown_blank_family_relationship_cannot_admit_either_older_or_newer_form(monkeypatch):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 2, '2024-03-20')
    first['Protocolo_Entrega'] = other['Protocolo_Entrega'] = ''
    memory = MemoryIndex({2023: [first], 2024: [other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    for year, sequence, version in ((2023, '1001', 1), (2024, '2001', 2)):
        with pytest.raises(generic.ProofError, match='structured_revision_link_ambiguous'):
            generic.index_document(memory, year, sequence, version, 99999,
                '00.000.000/0001-00', '2023-11-07', 'relatorio proventos')


@pytest.mark.parametrize('conflict', ['parent', 'security', 'unindexed_parent'])
def test_same_native_distribution_with_conflicting_linkage_is_ambiguous(monkeypatch, conflict):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 2, '2024-03-20')
    parents = [fixture_row('9001', 1, '2023-11-07', category='Ata'),
               fixture_row('9002', 1, '2023-11-07', family='2222-22', category='Ata')]
    first['Protocolo_Entrega'] = other['Protocolo_Entrega'] = ''
    memory = MemoryIndex({2023: [first, *parents], 2024: [other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)

    def unit_link(cache, row, code):
        old = generic.cvm.document_id(row['Link_Download'])[0] == '1001'
        return {'protocolo_provento': '700', 'protocolo_ipe':
            '099999IPE07112023013333-33' if conflict == 'unindexed_parent' else
            parents[int(not old and conflict == 'parent')]['Protocolo_Entrega'],
            'isin_scope': ['BRUNITACNOR0' if old or conflict != 'security' else 'BRUNITACNPR0'],
            'sources': []}

    monkeypatch.setattr(generic, '_structured_revision_link', unit_link)
    with pytest.raises(generic.ProofError, match='structured_revision_link_ambiguous'):
        indexed(memory)


def test_two_distinct_final_documents_of_one_family_are_ambiguous(monkeypatch):
    first = fixture_row('1001', 1, '2023-11-08')
    other = fixture_row('2001', 1, '2023-11-08')
    memory = MemoryIndex({2023: [first, other]})
    monkeypatch.setattr(generic, 'INDEX_PINS', memory.pins)
    with pytest.raises(generic.ProofError, match='latest_native_revision_ambiguous'):
        indexed(memory)


@pytest.fixture(scope='module')
def production(inventory, identity, evidence):
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')), \
            patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('offline')):
        cache = candidates._Cache(ROOT, inventory)
        checks = {}
        candidate = candidates._one(identity, cache, generic.CUTOFF, checks)
        statements, diagnostics, sources = prepare_financials(ROOT, [identity], generic.CUTOFF)
        spec = identity['cvm'], identity['share_class'], identity['sector_code']
        current = financial_features(statements, candidate['payments'], generic.CUTOFF, spec)
        observations, coverage = stock_observations('ETER3', candidate['prices'], candidate['payments'],
            statements, generic.CUTOFF, spec)
    summary = {'candidate': candidate, 'checks': checks, 'current': current,
               'observations': observations, 'coverage': coverage}
    required_months = sorted(shift_month(generic.CUTOFF, -i) for i in range(61))
    compared_dates = set(candidate['independent_price_matches'])
    monthly_quotes = [{'month_end': str(month), 'last_trading_date': str(candidate['prices'][month][0]),
        'independently_compared': str(candidate['prices'][month][0]) in compared_dates}
        for month in required_months]
    observed_regimes = sorted({round(factor_after(date.fromisoformat(day), candidate['share_actions'], generic.CUTOFF), 12)
                              for day in compared_dates})
    required_regimes = sorted({round(factor_after(candidate['prices'][month][0], candidate['share_actions'], generic.CUTOFF), 12)
                              for month in required_months})
    quote_review = {'model_monthly_inputs': len(monthly_quotes),
        'total_monthly_prices': len(candidate['prices']),
        'ordinary_gate_comparison_dates': candidate['independent_price_matches'],
        'ordinary_gate_comparison_count': len(compared_dates),
        'monthly_last_trading_dates_compared': sum(r['independently_compared'] for r in monthly_quotes),
        'all_61_last_trading_dates_independently_verified': all(r['independently_compared'] for r in monthly_quotes),
        'ordinary_policy': '61 original-series monthly prices and at least four independent same-session anchors covering action regimes; independent quotes for every month are not required.',
        'required_action_regimes': required_regimes, 'independently_observed_action_regimes': observed_regimes,
        'ordinary_anchor_policy_satisfied': len(compared_dates) >= 4 and set(required_regimes).issubset(observed_regimes),
        'monthly_dates': monthly_quotes,
        'limitation': 'The 13 independently compared sessions are not 61 monthly last-trading-date comparisons. '
            'This exact coverage limitation does not add a 61-independent-quote publication gate.'}
    summary['quote_review'] = quote_review
    review = {'review_status': 'normal_gates_verified_guard_test_results_pending',
        'reviewed_code_sha256': REVIEWED_CODE, 'scope': 'independent ETER repair and generic cross-year selector',
        'ETER3': {'one_pass': True, 'current_financial_pass': current is not None,
            'current_financial': current, 'training_rows': len(observations),
            'ledger_rows': len(candidate['ledger']), 'payment_rows': len(candidate['payments']),
            'quote_review': quote_review,
            'facts': evidence['facts'], 'source_contract': evidence['contract'],
            'native_sources': [{k: s.get(k) for k in ('file', 'sha256', 'bytes', 'url', 'published_at',
                'retrieved_at', 'retrieval_basis', 'settlement_verified', 'point_in_time_archive_verified')}
                for s in evidence['sources']]},
        'findings': [],
        'resolved_findings': [{'priority': 'P2', 'file': 'recommender/cash_repair_proofs.py',
            'function': '_revision_candidates', 'status': 'fixed_with_passing_regression_no_xfail',
            'title': 'Same reference can merge independent nonempty document families',
            'resolution': 'Distinct nonempty families remain separate. Blank-family same-reference forms '
                'require URL/hash-bound native Protocolo Provento and compatible indexed parent IPE/security '
                'scope. Missing, contradictory or tied final linkage fails closed without older fallback.'}],
        'policy': {'network_requests': 0, 'production_overrides': [], 'source_originals_modified': False,
            'authorized_selector_edited': True, 'inventory_or_pointers_edited': False, 'other_core_files_edited': False,
            'admission_published': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False},
        'test_results': None}
    REPORT.write_text(json.dumps(review, indent=2, ensure_ascii=True, default=str) + '\n', encoding='utf-8')
    return summary


def test_actual_production_one_financial_training_and_no_lookahead(production):
    assert production['current'] is not None
    assert production['current'][1]['filing_id'] == '155704'
    assert production['current'][1]['version'] == '3'
    assert production['current'][1]['fiscal_end'] == '2025-12-31'
    assert production['current'][1]['accounts_scope'] == 'consolidated'
    assert production['current'][1]['profit_per_share'] == .793
    assert len(production['observations']) == 37
    assert all(r['published_at'] <= r['as_of'] and r['features_available_at'] <= r['as_of']
               and r['label_end'] <= str(generic.CUTOFF) for r in production['observations'])


def test_actual_production_dedup_and_complete_repaired_groups(production):
    candidate = production['candidate']
    assert len(candidate['ledger']) == len(candidate['payments']) == 14
    repair = production['checks']['exact_cash_repairs'][eter.REPAIR_ID]
    assert len(repair['groups']) == len(repair['new_events']) == 2
    assert len({r['primary_declaration_id'] for r in repair['new_events']}) == 2
    assert all(not r['settlement_verified'] for r in repair['new_events'])


def test_quote_coverage_is_reported_without_claiming_unverified_61_months(production):
    quote_review = production['quote_review']
    assert quote_review['model_monthly_inputs'] == 61
    assert quote_review['ordinary_gate_comparison_count'] == 13
    assert quote_review['monthly_last_trading_dates_compared'] == 0
    assert quote_review['ordinary_anchor_policy_satisfied'] is True
    assert quote_review['all_61_last_trading_dates_independently_verified'] is False
