"""Independent offline PLPL source, relocation, and production-path review."""

import argparse
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import io
import json
import math
from pathlib import Path
import socket
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from lxml import html
from pypdf import PdfReader
import pytest

from . import cash_repair_proofs as repairs
from . import cvm_proventos_documents as cvm
from . import inventory_candidates as bridge
from . import issuer_independent_evidence as dispatch
from . import round2_plpl_cash as plpl
from .collect_market_data import month_end, shift_month
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page
from .share_basis import reconcile_payments, reconcile_prices


REPO = Path(__file__).resolve().parents[1]
MARKET = REPO / 'recommender/data/market'
REPORT = REPO.parent / 'work/recovery_round2_20261025/plpl-independent-review.json'
IDENTITY = {'ticker': 'PLPL3', 'cvm': 25070, 'share_class': 'ON', 'isin': 'BRPLPLACNOR5'}
CUTOFF, START = date(2026, 9, 30), date(2021, 9, 30)
PACKET_SHA = '40787917c55c7031efd81059b4e0fb4a961197f28dbe303e6d7567dd4fcd2617'
PACKET = 'raw/primary-research/all-pending/round2-plpl/evidence-' + PACKET_SHA + '.json'
PAYMENT = 'raw/expanded/PLPL3-payments.html'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def collect():
    inventory = load_inventory(MARKET)
    stock = next(r for r in inventory['stocks'] if r['ticker'] == 'PLPL3')
    cache = bridge._Cache(MARKET, inventory)
    body, observed = cache.read(PAYMENT, 'independent_PLPL_payment_review')
    parsed = inspect_payment_page(body, 'PLPL3', CUTOFF, observed)
    packet_body = (MARKET / PACKET).read_bytes()
    packet = json.loads(packet_body)
    documents = {}
    for sequence, entry in packet['documents'].items():
        raw = (MARKET / entry['source']['file']).read_bytes()
        pages = [p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages] if raw.startswith(b'%PDF-') else []
        documents[sequence] = {'body': raw, 'pages': pages, 'entry': entry}
    proof = plpl.load_proof(IDENTITY, CUTOFF, cache)
    evidence = plpl.load_evidence(MARKET, IDENTITY, CUTOFF)
    price_file = 'raw/expanded/PLPL3-prices.json'
    if not (MARKET / price_file).is_file():
        price_file = 'raw/inventory/daily-prices/PLPL3.json'
    return dict(inventory=inventory, stock=stock, cache=cache, parsed=parsed, observed=observed,
        payment_body=body, packet=packet, packet_body=packet_body, documents=documents,
        proof=proof, evidence=evidence, price_file=price_file,
        price_body=(MARKET / price_file).read_bytes())


def groups(proof):
    return {'2024-01-18': {'original_record_date': '2024-01-15',
        'expected_original': [{'label': 'DIVIDENDO', 'payment_date': '2024-02-08',
            'gross_per_share': 0.50439835, 'source_row': 'table:2:row:4'}], 'facts': proof['facts']}}


def apply(data, parsed=None):
    return repairs.apply_groups(data['parsed'] if parsed is None else parsed, IDENTITY, CUTOFF,
        data['proof'], repair_id=plpl.REPAIR_ID, payment_sha=plpl.PAYMENT_SHA,
        groups=groups(data['proof']))


def fingerprints(data):
    files = {PACKET, PAYMENT, data['price_file'], repairs.FCA_FILE,
             'raw/expanded/b3-detail-25070.json'}
    for name in ('cash_source', 'quote_source', 'page_source', 'script_source'):
        files.add(data['packet'][name]['file'])
    for doc in data['packet']['documents'].values():
        files.add(doc['source']['file'])
        files.add(doc['document']['index_evidence']['file'])
        files.add('raw/primary-research/all-pending/round2-plpl/response-'
                  + sha(doc['source']['url'].encode()) + '.json')
    files.update(p.relative_to(MARKET).as_posix() for p in MARKET.glob('**/current.json'))
    return {name: sha((MARKET / name).read_bytes()) for name in sorted(files)}


@pytest.fixture(autouse=True)
def offline():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network_forbidden')):
        yield


@pytest.fixture(scope='module')
def data():
    return collect()


def test_exact_packet_identity_and_registered_security(data):
    assert sha(data['packet_body']) == PACKET_SHA
    assert data['packet']['identity'] == IDENTITY
    assert {k: data['stock'][k] for k in IDENTITY} == IDENTITY
    assert data['packet']['cutoff'] == CUTOFF.isoformat()
    assert data['packet']['model_admission'] is False
    detail = json.loads((MARKET / 'raw/expanded/b3-detail-25070.json').read_bytes())
    assert detail['cnpj'] == '24230275000180' and int(detail['codeCVM']) == 25070
    assert [r for r in detail['otherCodes'] if r['code'] == 'PLPL3'] == [
        {'code': 'PLPL3', 'isin': 'BRPLPLACNOR5'}]


def test_all_six_document_index_transport_hash_and_publication_bindings(data):
    assert set(data['documents']) == {'708190', '708742', '985325', '986313', '1062946', '1063185'}
    pdf_count = 0
    for sequence, reading in data['documents'].items():
        entry = reading['entry']
        document, source = entry['document'], entry['source']
        index = document['index_evidence']
        body = (MARKET / index['file']).read_bytes()
        assert sha(body) == index['sha256']
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read(index['member']).decode('latin1')), delimiter=';'))
        row = rows[index['csv_line'] - 2]
        assert row == index['raw_row'] and int(row['Codigo_CVM']) == 25070
        assert row['CNPJ_Companhia'] == '24.230.275/0001-80'
        query = parse_qs(urlsplit(row['Link_Download']).query)
        assert query['numSequencia'] == [sequence]
        assert query['numVersao'] == [str(document['version'])]
        assert int(row['Versao']) == document['version']
        assert row['Link_Download'] == source['url'] == document['url']
        assert cvm.day(row['Data_Entrega']) == document['published_at'] <= CUTOFF.isoformat()
        assert sha(reading['body']) == source['sha256'] and len(reading['body']) == source['bytes']
        receipt = json.loads((MARKET / 'raw/primary-research/all-pending/round2-plpl'
            / ('response-' + sha(source['url'].encode()) + '.json')).read_bytes())
        assert receipt['status'] == 200 and receipt['error'] is None
        assert all(receipt[k] == source[k] for k in source)
        pdf_count += bool(reading['pages'])
    assert pdf_count == 5
    assert not data['documents']['1063185']['body'].startswith(b'%PDF')
    assert data['packet']['final_structured_form_unavailable_not_substituted_silently'] == '1063185'


def test_literal_approval_record_ex_payment_and_gross_roles(data):
    notice = repairs.text(data['documents']['708190']['pages'][0])
    assert '0,50439835364' in notice and '18 de janeiro de 2024' in notice
    assert '19 de janeiro de 2024' in notice and '08 de fevereiro de 2024' in notice
    document = data['documents']['708742']
    form = cvm.parse_pdf(document['body'], document['entry']['document'], CUTOFF)
    event, = form['events']
    assert (event['isin'], event['approval_date'], event['record_date'], event['payment_date'],
            event['gross_per_unit_decimal']) == (
        'BRPLPLACNOR5', '2024-01-15', '2024-01-18', '2024-02-08', '0.50439835364')
    fact, = data['proof']['facts']
    assert (fact['approval_date'], fact['record_date'], fact['ex_date'], fact['payment_date']) == (
        '2024-01-15', '2024-01-18', '2024-01-19', '2024-02-08')
    assert fact['published_at'] == '2024-01-15'
    assert document['entry']['document']['published_at'] == '2024-01-17'
    assert fact['gross_per_share_decimal'] == '0.50439835364'


def test_final_notice_amends_the_named_distribution_and_ratifies_other_terms(data):
    original = repairs.text(data['documents']['985325']['pages'][0])
    final = repairs.text(data['documents']['1062946']['pages'][0])
    assert '0,49282986754' in original and '29 de dezembro de 2025' in original
    assert '1o de julho de 2026' in original
    assert '22 de dezembro de 2025' in final
    assert 'de 1o de julho de 2026 para 02 de julho de 2026' in final
    assert 'ratificadas e inalteradas' in final
    stale = cvm.parse_pdf(data['documents']['986313']['body'],
        data['documents']['986313']['entry']['document'], CUTOFF)
    event, = stale['events']
    assert event['gross_per_unit_decimal'] == '0.49282986754'
    assert event['payment_date'] == '2026-07-01'
    actual = next(r for r in data['evidence']['calendar']['events'] if r['record_date'] == '2025-12-29')
    assert actual['payment_date'] == '2026-07-02'
    assert actual['original_reported_payment_date'] == '2026-07-01'
    assert actual['payment_amendment_published_at'] == '2026-06-25'
    assert data['proof']['contract']['no_old_form_used_to_override_final_notice'] is True


def test_record_relocation_preserves_all_unrelated_source_rows_and_raw_report(data):
    before = deepcopy(data['parsed'])
    output = apply(data)
    assert data['parsed'] == before and output['report'] == before['report']
    assert not any(r['record_date'] == '2024-01-15' for r in output['events'])
    corrected, = [r for r in output['events'] if r['record_date'] == '2024-01-18']
    assert corrected['gross_per_share_decimal'] == '0.50439835364'
    assert corrected['approval_date'] == '2024-01-15'
    assert corrected['reported_original_fields'] == [r for r in before['events']
        if r['record_date'] == '2024-01-15']
    assert [r for r in output['events'] if r['record_date'] != '2024-01-18'] == [
        r for r in before['events'] if r['record_date'] != '2024-01-15']
    audit = output['exact_cash_repairs'][plpl.REPAIR_ID]
    assert audit['groups'][0]['original_record_date'] == '2024-01-15'
    assert audit['source_bytes_modified'] is False and audit['independent_comparator_changed'] is False


def test_idempotent_unchanged_view_and_changed_tagged_row_rejected(data):
    output = apply(data)
    assert apply(data, output) == output
    changed = deepcopy(output)
    next(r for r in changed['events'] if r.get('exact_cash_repair_id'))['gross_per_share'] = 99.
    with pytest.raises(repairs.ProofError, match='modified_view'):
        apply(data, changed)


def test_reintroduced_old_group_cannot_bypass_idempotence_guard(data):
    changed = apply(data)
    changed['events'].extend(deepcopy([r for r in data['parsed']['events']
                                     if r['record_date'] == '2024-01-15']))
    with pytest.raises(repairs.ProofError):
        apply(data, changed)


def test_untagged_target_duplicate_cannot_bypass_idempotence_guard(data):
    changed = apply(data)
    duplicate = deepcopy(next(r for r in changed['events'] if r.get('exact_cash_repair_id')))
    duplicate.pop('exact_cash_repair_id')
    duplicate['source_row'] = 'unexpected_target_duplicate'
    changed['events'].append(duplicate)
    with pytest.raises(repairs.ProofError):
        apply(data, changed)


@pytest.mark.parametrize('change', ['target_occupied', 'old_amount', 'old_payment', 'old_source_row', 'hash'])
def test_initial_relocation_exact_source_and_empty_target_guards(data, change):
    parsed = deepcopy(data['parsed'])
    old = next(r for r in parsed['events'] if r['record_date'] == '2024-01-15')
    if change == 'target_occupied':
        parsed['events'].append({**old, 'record_date': '2024-01-18'})
    elif change == 'old_amount':
        old['gross_per_share'] += .001
    elif change == 'old_payment':
        old['payment_date'] = '2024-02-09'
    elif change == 'old_source_row':
        old['source_row'] = 'table:2:row:99'
    else:
        parsed['report']['sha256'] = '0' * 64
    with pytest.raises(repairs.ProofError):
        apply(data, parsed)


@pytest.mark.parametrize('key,value', [('cvm', 1), ('isin', 'BRPLPLACNOR6'), ('share_class', 'PN')])
def test_exact_identity_and_reference_guards(data, key, value):
    with pytest.raises(ValueError):
        plpl.load_evidence(MARKET, {**IDENTITY, key: value}, CUTOFF)
    with pytest.raises(ValueError):
        plpl.apply_exact_repairs(data['parsed'], {**IDENTITY, key: value}, CUTOFF, data['cache'])
    with pytest.raises(ValueError):
        plpl.load_evidence(MARKET, IDENTITY, date(2026, 8, 31))
    assert plpl.load_evidence(MARKET, {'ticker': 'PLPL3F'}, CUTOFF) is None
    assert plpl.apply_exact_repairs(data['parsed'], {'ticker': 'PLPL3F'}, CUTOFF, data['cache']) is data['parsed']


def test_pinned_original_hash_and_document_index_mismatch_rejected(data):
    with pytest.raises(ValueError, match='integrity_failure'):
        plpl.read(MARKET.resolve(), PACKET, '0' * 64)
    entry = deepcopy(data['documents']['708742']['entry'])
    entry['document']['version'] = 2
    with pytest.raises(ValueError, match='primary_index_binding'):
        plpl.document(data['cache'], entry)


@pytest.mark.parametrize('day', ['2021-09-01', '2024-01-19', '2026-10-01'])
def test_in_window_or_later_action_requires_primary_review(day):
    with pytest.raises(ValueError, match='new_primary_action_review'):
        plpl.validate_primary_actions(MARKET, IDENTITY, CUTOFF, [{'date': day, 'ratio': 1.1}])


def test_fundamentus_precision_is_preserved_and_not_claimed_exact_native_gross(data):
    events = data['evidence']['calendar']['events']
    assert len(events) == 6 and len({r['record_date'] for r in events}) == 6
    original = {r['record_date']: r for r in data['parsed']['events']}
    for event in events:
        amount = Decimal(event['reported_amount_decimal'])
        assert amount == Decimal(event['gross_per_share_decimal'])
        assert len(event['reported_amount_decimal'].partition('.')[2]) == 4
        record = '2024-01-15' if event['record_date'] == '2024-01-18' else event['record_date']
        reported = Decimal(str(original[record]['gross_per_share']))
        assert reported.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP) == amount
    last = next(r for r in events if r['record_date'] == '2025-12-29')
    assert Decimal(last['gross_per_share_decimal']) != Decimal('0.49282986754')
    assert Decimal(last['gross_per_share_decimal']) == Decimal('0.4928')
    checks = data['evidence']['checks']
    assert data['evidence']['pagination_basis'] == (
        'secondary_provider_cash_table_with_primary_date_corrections')
    assert checks['source_completeness_certified'] is False
    assert checks['ultimate_upstream_independence_verified'] is False
    assert checks['gross_reconstructed_from_net'] is False
    assert checks['settlement_verified'] is False and checks['point_in_time_archive_verified'] is False


@pytest.mark.parametrize('change', ['JCP', 'quantity', 'bad_date', 'negative'])
def test_secondary_parser_rejects_changed_cash_type_unit_date_or_sign(data, change):
    body = (MARKET / data['packet']['cash_source']['file']).read_bytes()
    tree = html.fromstring(body)
    table, = [t for t in tree.xpath('//table') if t.xpath('.//th')
        and [plpl.compact(c.text_content()) for c in t.xpath('.//tr')[0].xpath('./th|./td')]
        == ['data', 'valor', 'tipo', 'datadepagamento', 'porquantasacoes']]
    cells = table.xpath('.//tr')[1].xpath('./td')
    index, value = {'JCP': (2, 'JCP'), 'quantity': (4, '1000'),
                    'bad_date': (3, '31/06/2026'), 'negative': (1, '-0,4928')}[change]
    cells[index].clear()
    cells[index].text = value
    with pytest.raises(ValueError):
        plpl.parse_calendar(html.tostring(tree))


def test_all_61_real_monthly_quotes_are_required_by_ordinary_gate(data):
    quotes = data['evidence']['raw_quotes']
    assert len(quotes) == 61
    native = json.loads((MARKET / data['packet']['quote_source']['file']).read_bytes())
    actual = {date.fromisoformat(r['date']): r['closed'] for r in native['data']['tickers'][0]['history']}
    assert all(actual[day] == value for day, value in quotes.items())
    monthly = {}
    for day, value in sorted(actual.items()):
        monthly[month_end(day.year, day.month)] = (day, value)
    assert {day for day, _ in monthly.values()} == set(quotes)
    assert max((month - row[0]).days for month, row in monthly.items()) <= 3
    prices, matches, _ = reconcile_prices(data['price_body'], 'PLPL3', [], quotes, CUTOFF,
        evidence_provider=data['evidence']['provider'])
    assert len(matches) == 61
    assert {shift_month(CUTOFF, -i) for i in range(61)} <= prices.keys()
    bad = deepcopy(quotes)
    day = date(2024, 8, 30)
    bad[day] *= 1.1
    with pytest.raises(ValueError, match='disagrees_with_independent_source'):
        reconcile_prices(data['price_body'], 'PLPL3', [], bad, CUTOFF,
            evidence_provider=data['evidence']['provider'])


def production_probe(data):
    before = fingerprints(data)
    parsed_before = deepcopy(data['parsed'])
    checks, cache = {}, bridge._Cache(MARKET, data['inventory'])
    candidate = bridge._one(deepcopy(data['stock']), cache, CUTOFF, checks)
    assert data['parsed'] == parsed_before and fingerprints(data) == before
    assert len(candidate['independent_price_matches']) == 61
    assert len(candidate['ledger']) == 5 and len(candidate['independent_cash_matches']) == 5
    assert candidate['price_source_file'] == data['price_file']
    assert candidate['share_actions'] == []
    assert checks['cash_pagination'] == 'secondary_provider_cash_table_with_primary_date_corrections'
    rows = {r['record_date']: r for r in candidate['ledger']}
    assert '2024-01-15' not in rows and rows['2024-01-18']['payment_date'] == '2024-02-08'
    assert rows['2024-01-18']['gross_per_unit'] == 0.50439835364
    assert rows['2025-12-29']['payment_date'] == '2026-07-02'
    assert rows['2025-12-29']['gross_per_unit'] == 0.49283
    original = {r['record_date']: r for r in data['parsed']['events']}
    for record, row in rows.items():
        assert row['share_normalization_factor'] == 1.
        if record != '2024-01-18':
            assert row['gross_per_unit'] == original[record]['gross_per_share']
            assert row['payment_date'] == original[record]['payment_date']
        assert row['point_in_time_archive_verified'] is False
        assert 'not_settlement_confirmed' in row['financial_confirmation']
    assert dispatch.load_evidence(MARKET, IDENTITY, CUTOFF)['expected_cash'] == data['evidence']['expected_cash']
    return candidate, checks, before


def test_real_dispatch_payments_and_candidate_preserve_original_amounts_and_sources(data):
    production_probe(data)


def relocation_guard_probe(data):
    output = apply(data)
    output['events'].extend(deepcopy([r for r in data['parsed']['events'] if r['record_date'] == '2024-01-15']))
    try:
        second = apply(data, output)
    except repairs.ProofError as error:
        origin = {'old_group_reintroduction_rejected': True, 'reason': str(error)}
    else:
        origin = {'old_group_reintroduction_rejected': False,
            'resulting_record_dates': [r['record_date'] for r in second['events']
                                      if r['record_date'] in ('2024-01-15', '2024-01-18')]}
    target = apply(data)
    duplicate = deepcopy(next(r for r in target['events'] if r.get('exact_cash_repair_id')))
    duplicate.pop('exact_cash_repair_id')
    duplicate['source_row'] = 'unexpected_target_duplicate'
    target['events'].append(duplicate)
    try:
        apply(data, target)
    except repairs.ProofError as error:
        origin.update(untagged_target_duplicate_rejected=True, target_rejection_reason=str(error))
    else:
        origin.update(untagged_target_duplicate_rejected=False)
    if origin['old_group_reintroduction_rejected']:
        return origin
    history, _ = bridge._payments('PLPL3', bridge._Cache(MARKET, data['inventory']), CUTOFF, identity=IDENTITY)
    history.installments.append({'ticker': 'PLPL3', 'record_date': '2024-01-15',
        'payment_date': '2024-02-08', 'gross_per_unit': .50439835})
    try:
        reconcile_payments('PLPL3', history, data['evidence']['expected_cash'], [], CUTOFF,
                           evidence_provider=data['evidence']['provider'])
        downstream = 'accepted'
    except ValueError as error:
        downstream = str(error)
    return {**origin, 'ordinary_downstream_gate': downstream}


class Results:
    def __init__(self):
        self.rows, self.subtests, self.exit_code = [], [], None

    def pytest_runtest_logreport(self, report):
        if 'subtestreport' in type(report).__name__.lower():
            self.subtests.append({'test': report.nodeid, 'outcome': report.outcome})
            return
        if report.when == 'call' or report.failed:
            self.rows.append({'test': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
                **({'failure': report.longreprtext} if report.failed else {})})


def quick_bmeb_review():
    """Read the actual pinned sources and ordinary production path only."""
    from . import round2_bmeb_cash as bmeb
    from . import round2_share_actions as actions

    inventory = load_inventory(MARKET)
    stock, = [r for r in inventory['stocks'] if r['ticker'] == 'BMEB4']
    cache = bridge._Cache(MARKET, inventory)
    proof = bmeb.load_proof(MARKET, stock, cache=cache)
    packet = actions.packet(MARKET)
    target = packet['targets']['BMEB4']
    source_refs = [*proof['sources'], *target['cash'], *target['quotes'], target['comparison']]
    names = {r['file'] for r in source_refs}
    names.add(bmeb.PAYMENT_FILE)
    names.update(p.relative_to(MARKET).as_posix() for p in MARKET.glob('**/current.json'))
    before = {name: sha((MARKET / name).read_bytes()) for name in sorted(names)}
    notice_body, notice_ref = bmeb.source(MARKET, bmeb.NOTICE_URL)
    issuer_body, _ = bmeb.source(MARKET, bmeb.ISSUER_NOTICE)
    assert notice_body == issuer_body
    pages = [p.extract_text() for p in PdfReader(io.BytesIO(notice_body)).pages]
    assert len(pages) == 1 and '17.184.037/0001-10' in pages[0]
    assert 'R$ 0,308633' in pages[0] and 'R$ 0,262338' in pages[0]
    fact, = proof['facts']
    assert {k: fact[k] for k in ('ticker', 'cvm', 'share_class', 'isin')} == {
        'ticker': 'BMEB4', 'cvm': 1325, 'share_class': 'PN', 'isin': 'BRBMEBACNPR5'}
    assert fact['gross_per_share_decimal'] == '0.308633'
    assert fact['net_per_share_decimal'] == '0.262338'
    assert fact['gross_reconstructed_from_net'] is False
    assert (fact['approval_date'], fact['record_date'], fact['ex_date'], fact['payment_date']) == (
        '2023-03-07', '2023-03-10', '2023-03-13', '2023-03-17')
    body, observed = cache.read(bmeb.PAYMENT_FILE, 'independent_BMEB_original')
    parsed = inspect_payment_page(body, 'BMEB4', CUTOFF, observed)
    parsed_before = deepcopy(parsed)
    original, = [r for r in parsed['events'] if r['record_date'] == '2023-03-10']
    assert original['gross_per_share'] == 0.262338
    corrected = bmeb.apply_exact_repairs(parsed, stock, CUTOFF, cache)
    assert parsed == parsed_before
    assert [r for r in corrected['events'] if r['record_date'] != '2023-03-10'] == [
        r for r in parsed['events'] if r['record_date'] != '2023-03-10']
    repaired, = [r for r in corrected['events'] if r['record_date'] == '2023-03-10']
    assert repaired['gross_per_share'] == 0.308633
    literal_b3_rows = []
    for ref in target['cash']:
        raw = (MARKET / ref['file']).read_bytes()
        assert sha(raw) == ref['sha256']
        literal_b3_rows.extend(r for r in json.loads(raw)['results']
            if r['typeStock'].strip() == 'PN' and r['lastDatePriorEx'] == '10/03/2023')
    assert literal_b3_rows
    checks = {}
    candidate = bridge._one(stock, cache, CUTOFF, checks)
    quote_checks = checks['independent_raw_quote_evidence']
    assert len(candidate['independent_price_matches']) == 61
    assert len(quote_checks['monthly_inputs']) == 61
    assert all(r['date_matches'] and r['split_adjusted_matches'] for r in quote_checks['monthly_inputs'])
    assert all(r['agrees'] for r in quote_checks['all_original_independent_quotes'])
    assert candidate['share_actions'] == [{'date': '2021-12-03', 'ratio': 2.0}]
    assert len(candidate['independent_cash_matches']) == 11 and len(candidate['ledger']) == 12
    paid, = [r for r in candidate['ledger'] if r['record_date'] == '2023-03-10']
    assert paid['gross_per_unit'] == 0.308633 and paid['payment_date'] == '2023-03-17'
    after = {name: sha((MARKET / name).read_bytes()) for name in before}
    assert before == after
    return {'scope': 'quick_independent_source_and_ordinary_path_review_not_full_financial_admission',
        'actual_pinned_PDFs_read': True,
        'visual_review_pages': {'CVM594440': [1], 'CVM594437': [2], 'CVM432674': [1]},
        'notice': notice_ref, 'native_PN_gross': '0.308633', 'native_PN_net': '0.262338',
        'original_secondary_amount': 0.262338, 'corrected_ledger_gross': paid['gross_per_unit'],
        'net_to_gross_reconstruction_performed': False,
        'gross_net_class_grid': fact['source_locator']['normalized_grid'],
        'date_roles': {k: fact[k] for k in ('approval_date', 'record_date', 'ex_date', 'payment_date')},
        'literal_cached_B3_rows_for_corrected_group': literal_b3_rows,
        'primary_actions': candidate['share_actions'],
        'ordinary_one_passed': True, 'required_monthly_matches': 61,
        'both_used_price_regimes_checked': quote_checks['used_regimes'],
        'all_existing_B3_quote_comparisons_retained': quote_checks['all_original_independent_quotes'],
        'required_cash_groups': 11, 'ledger_rows': 12,
        'source_fingerprints_before': before, 'source_fingerprints_after': after,
        'settlement_or_upstream_independence_certified': False,
        'financial_validation_or_publication_performed': False,
        'existing_bmeb_test_suite_run_by_this_sidecar': False}


def build_report(results):
    data = collect()
    candidate, checks, before = production_probe(data)
    guard = relocation_guard_probe(data)
    findings = []
    if not guard['old_group_reintroduction_rejected'] or not guard['untagged_target_duplicate_rejected']:
        findings.append({'priority': 3, 'file': 'recommender/cash_repair_proofs.py', 'line': 178,
            'title': 'Idempotence does not reject reintroduced source or untagged target rows',
            'evidence': guard,
            'impact': 'The helper can return both the old entitlement and its relocated replacement. The ordinary downstream cash gate rejects the old-date example; no duplicate admission observed in the pinned production path.',
            'needed_change': 'Before returning an already repaired view, check the complete original and target record groups for the relocation, including untagged rows.'})
    fields = ('record_date', 'payment_date', 'gross_per_unit', 'published_at',
              'primary_declaration_id', 'share_normalization_factor')
    return {'schema_version': 1, 'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'independent_offline_PLPL_cash_and_original_record_date_review',
        'decision': ('changes_requested_on_relocation_guard' if findings else
                     'blocked_by_review_test_failure' if results.exit_code != 0 else
                     'supported_pinned_source_adapter_with_stated_limits'),
        'identity': IDENTITY, 'packet_sha256': PACKET_SHA, 'network_used': False,
        'core_or_proofs_modified_by_review': False, 'findings': findings,
        'resolved_findings': [{'priority': 3, 'file': 'recommender/cash_repair_proofs.py',
            'status': 'resolved' if not findings else 'not_resolved',
            'cause': 'The prior idempotence branch validated only tagged replacement events before returning the whole parsed view.',
            'original_reproduction': [
                'Repair Jan15 entitlement to Jan18, reintroduce the original untagged Jan15 event, then reapply: previously accepted both groups.',
                'Repair Jan15 to Jan18, add an untagged duplicate at Jan18, then reapply: previously accepted the duplicate.'
            ],
            'confirmed_fix': 'The branch now compares the complete target record group with the audited replacements and rejects any row at the relocated origin before returning.',
            'current_reproduction_results': guard,
            'unchanged_valid_reapplication_passes': True}],
        'resolved_pagination_label': {
            'previous': 'full_native_issuer_table',
            'current': checks['cash_pagination'],
            'source_completeness_certified': data['evidence']['checks']['source_completeness_certified'],
            'ultimate_upstream_independence_verified': data['evidence']['checks']['ultimate_upstream_independence_verified'],
            'generic_native_default_not_overridden': True},
        'BMEB4_quick_independent_review': quick_bmeb_review(),
        'primary_review': {'PDF_skill_applied': True,
            'visual_review_method': 'Poppler pdftoppm rendered to memory; no source or image files written',
            'rendered_pages': {'708190': [1], '708742': [1], '985325': [1, 2], '986313': [1], '1062946': [1]},
            'text_extracted_from_all_PDF_pages': True,
            'documents': [{**doc['entry']['source'],
                'sequence': seq, 'version': doc['entry']['document']['version'],
                'published_at': doc['entry']['document']['published_at'],
                'format': 'PDF' if doc['pages'] else 'retained_HTML_not_usable_as_primary_form'}
                for seq, doc in data['documents'].items()],
            '2024_roles': {'approval': '2024-01-15', 'record': '2024-01-18', 'ex': '2024-01-19',
                'payment': '2024-02-08', 'gross_per_share': '0.50439835364',
                'notice_publication': '2024-01-15', 'structured_form_publication': '2024-01-17'},
            '2025_2026_revision': data['proof']['contract'],
            'final_unavailable_form_not_overridden_by_stale_form': True},
        'production': {'actual_one_passed': True, 'required_monthly_anchors': 61,
            'independent_monthly_matches': len(candidate['independent_price_matches']),
            'returned_Yahoo_monthly_dictionary_rows': len(candidate['prices']),
            'payment_rows': len(candidate['ledger']),
            'ledger': [{k: r.get(k) for k in fields} for r in candidate['ledger']],
            'source_fingerprints_before': before, 'source_fingerprints_after': fingerprints(data),
            'current_cash_pagination_label': checks['cash_pagination'],
            'financial_validation_or_model_publication_performed': False},
        'secondary_amount_review': {'events': data['evidence']['calendar']['events'],
            'all_six_original_amounts_round_to_Fundamentus_four_decimals': True,
            'Investidor10_precision_preserved_except_exact_primary_2024_repair': True,
            'primary_gross_amounts_directly_reviewed_for_records': ['2024-01-18', '2025-12-29'],
            'other_records_not_individually_proved_gross_by_this_primary_packet': [
                '2021-04-30', '2022-04-29', '2023-04-28', '2025-01-28'],
            'not_an_independent_upstream_supplier_audit': True},
        'provenance_and_scientific_limits': [
            'A DIVIDENDO label and four decimals do not establish full gross precision, independent upstream supply, settlement, or absence of unlisted distributions/JCP.',
            'The actual five in-window groups pass existing amount tolerances; all original Investidor10 amounts remain except the documented 2024 primary repair. Agreement within tolerances is not exact-primary validation of the other groups.',
            'The final notice explicitly changes the named 2025 distribution payment to July 2 and ratifies other terms. The unavailable v2 structured form remains HTML; the old July 1 form is not applied over the final notice.',
            'Fundamentus is a secondary calendar. The qualified pagination label is now explicit; source completeness and ultimate upstream independence remain uncertified.',
            'Only 61 months are independently corroborated by Enfoque. The ordinary Yahoo dictionary can retain earlier observations; their presence is not extra independent price validation.',
            'The common raw/split-adjusted detector reports split_adjusted with no actions; those labels are numerically indistinguishable here and do not prove a split adjustment occurred.',
            'No source bytes, immutable packet, source dates, active pointer, or published model are rewritten by this review; no settlement or point-in-time archive is certified.'
        ],
        'reviewed_code_sha256': {name: sha((REPO / 'recommender' / name).read_bytes()) for name in (
            'round2_plpl_cash.py', 'cash_repair_proofs.py', 'inventory_candidates.py', 'issuer_independent_evidence.py')},
        'tests': {'exit_code': results.exit_code, 'passed': sum(r['outcome'] == 'passed' for r in results.rows),
            'failed': sum(r['outcome'] == 'failed' for r in results.rows),
            'independent_tests_passed': sum(r['outcome'] == 'passed' and
                'test_round2_plpl_evidence_review.py' in r['test'] for r in results.rows),
            'cash_regressions_passed': sum(r['outcome'] == 'passed' and
                'test_round2_plpl_evidence_review.py' not in r['test'] for r in results.rows),
            'subtests': results.subtests, 'results': results.rows}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--cash-regressions', action='store_true')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    results = Results()
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network_forbidden')):
        files = [str(Path(__file__).resolve())]
        if args.cash_regressions:
            files.extend(str(REPO / 'recommender' / name) for name in (
                'test_sanb4_cash_repair.py', 'test_radl_cash_repair.py'))
        results.exit_code = int(pytest.main([*files, '-q', '-p', 'no:cacheprovider'], plugins=[results]))
        if args.report:
            report = build_report(results)
            REPORT.write_text(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            print(json.dumps({'report': str(REPORT), 'decision': report['decision'],
                'passed': report['tests']['passed'], 'failed': report['tests']['failed']}))
    return results.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
