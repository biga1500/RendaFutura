from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit
import urllib.error

import pytest

from . import jhsf_cash_research as research


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('JHSF_tests_must_not_use_live_network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture(scope='module')
def frozen_inputs(tmp_path_factory):
    if not (ROOT / research.CACHE / 'cvm611700v3-'
            f"{research.ORIGINALS['611700'][0]}.pdf").is_file():
        pytest.skip('isolated JHSF original cache unavailable')
    report = research.build_report(ROOT)
    names = {source['file']: source['sha256'] for source in report['sources']}
    for request in report['network']['requests']:
        name = f"{research.CACHE}/request-{request['ordinal']:02d}.json"
        names[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    # Freeze exactly the bytes actually read; never inventory the live cache tree.
    bodies = {name: (ROOT / name).read_bytes() for name in names}
    assert all(hashlib.sha256(body).hexdigest() == names[name] for name, body in bodies.items())
    target = tmp_path_factory.mktemp('JHSF_required_inputs')
    for name, body in bodies.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return target, names


@pytest.fixture(scope='module')
def evidence(frozen_inputs):
    return research.load_jhsf_evidence(frozen_inputs[0])


def test_six_native_indices_cover_all_categories_and_blank_subject_revisions(evidence):
    assert [p['year'] for p in evidence['index_periods']] == list(range(2021, 2027))
    assert all(p['all_categories'] and p['index_scan_complete'] for p in evidence['index_periods'])
    assert all(p['cash_history_complete'] is False for p in evidence['index_periods'])
    documents = {o['document']['sequence']: o['document'] for o in evidence['originals']}
    assert documents['572739']['subject'] == documents['611700']['subject'] == ''
    assert documents['572739']['published_at'] == '2023-01-09'
    assert documents['572739']['reference_date'] == '2022-12-12'


def test_original_four_installments_preserve_reported_precision_and_dates(evidence):
    notice = evidence['original_December_notice']
    assert [r['gross_per_share_decimal'] for r in notice['installments']] == ['0.0480475919'] * 4
    assert [(r['record_date'], r['payment_date']) for r in notice['installments']] == [
        ('2022-12-15', '2022-12-26'), ('2023-01-17', '2023-01-26'),
        ('2023-02-14', '2023-02-27'), ('2023-03-16', '2023-03-27')]
    assert not notice['gross_amount_selected_as_final']
    assert all('32.500.000' in r['source_locator']['literal_row'] for r in notice['installments'])


def test_missing_revision_and_index_only_PDF_never_validate_January_amounts(evidence):
    statuses = {o['document']['sequence']: o['status'] for o in evidence['originals']}
    assert statuses['572739'] == 'empty_ENET_HTML_not_original_notice'
    assert statuses['573827'] == 'native_form_index_only_no_cash_fields'
    assert len(evidence['pending_amount_groups']) == 3
    assert all(p['selected_final_gross_per_share_decimal'] is None
               and not p['target_group_complete'] for p in evidence['pending_amount_groups'])
    assert all(len(p['blockers']) == 2 for p in evidence['pending_amount_groups'])
    assert not any(e['record_date'] < '2023-04-01' for e in evidence['events'])


def test_literal_board_revision_changes_record_and_ex_but_not_payment(evidence):
    prior, current = (evidence['boards'][seq] for seq in ['611118', '611700'])
    assert prior['installments'][0]['record_date'] == '2023-04-18'
    assert current['installments'][0]['record_date'] == '2023-04-19'
    assert current['installments'][0]['ex_date'] == '2023-04-20'
    assert current['installments'][0]['payment_date'] == '2023-04-28'
    assert [r['payment_date'] for r in current['installments']] == [
        '2023-04-28', '2023-05-29', '2023-06-28']
    assert current['currency_total_decimal'] == '97500000.00'
    assert not current['installment_per_share_amounts_derived']
    assert evidence['board_revision']['historical_versions_retained']
    assert not evidence['board_revision']['independent_authors']


def test_exact_native_gross_proves_only_complete_April_entitlement_group(evidence):
    event, = evidence['events']
    assert (event['cvm'], event['isin'], event['share_class']) == (20605, 'BRJHSFACNOR2', 'ON')
    assert event['gross_per_share_decimal'] == '0.04794072360'
    assert event['record_date'] == '2023-04-19'
    assert event['payment_date'] == '2023-04-28'
    assert event['published_at'] == '2023-04-18'
    assert event['target_group_complete'] and not event['issues']
    group, = evidence['complete_groups']
    assert group['installment_count'] == len(group['events']) == 1
    assert group['record_date'] == '2023-04-19'
    assert not group['entire_issuer_history_complete']
    assert event['source_ref'] in {s['id'] for s in evidence['sources']}
    assert all(s['acquisition_receipt_verified'] for s in event['primary_evidence'])
    assert not any(event[k] for k in ('cash_model_eligible', 'gross_reconstructed_from_net',
                                      'settlement_verified', 'point_in_time_archive_verified'))


def test_complete_entitlement_does_not_claim_all_board_installment_gross_amounts(evidence):
    assert evidence['April_board_all_native_gross_installments_verified'] is False
    assert [r['record_date'] for r in evidence['other_April_board_entitlements_not_promoted']] == [
        '2023-05-18', '2023-06-19']
    assert evidence['complete_groups'][0]['scope'] == 'one_entitlement_group_not_entire_April_board_declaration'


def test_unmapped_native_installment_prevents_complete_group(frozen_inputs, monkeypatch):
    original = research.cvm.parse_pdf
    def unresolved(body, document, cutoff):
        result = original(body, document, cutoff)
        if document['sequence'] == '611834':
            result['unmapped_installment_rows'] = [{'payment_date': None}]
        return result
    monkeypatch.setattr(research.cvm, 'parse_pdf', unresolved)
    with pytest.raises(ValueError, match='unmapped_installments_block_complete_group'):
        research.load_jhsf_evidence(frozen_inputs[0])


@pytest.mark.parametrize('change', ['reference', 'category', 'version', 'publication', 'installment', 'amount', 'payment'])
def test_revision_cannot_cross_identity_period_or_change_unreviewed_fields(evidence, change):
    prior, current = (deepcopy(evidence['boards'][seq]) for seq in ['611118', '611700'])
    if change == 'reference':
        current['document']['reference_date'] = '2023-04-14'
    elif change == 'category':
        current['document']['category'] = 'Aviso aos Acionistas'
    elif change == 'version':
        current['document']['version'] = 4
    elif change == 'publication':
        current['document']['published_at'] = prior['document']['published_at']
    elif change == 'installment':
        current['installments'].pop()
    elif change == 'amount':
        current['installments'][0]['currency_total_decimal'] = '1'
    else:
        current['installments'][0]['payment_date'] = '2023-04-20'
    with pytest.raises(ValueError):
        research.board_revision(prior, current)


def test_changed_original_bytes_are_rejected_before_semantic_parsing(evidence, frozen_inputs):
    board = evidence['boards']['611700']
    body = (frozen_inputs[0] / board['source']['file']).read_bytes()
    with pytest.raises(ValueError, match='original_hash_mismatch'):
        research.parse_board(body + b'changed', board['document'], board['source'])


def test_parent_API_is_read_only_and_does_not_scan_unrelated_cache(frozen_inputs, monkeypatch):
    target, names = frozen_inputs
    rogue = target / 'raw/primary-research/unrelated-download-in-progress.json'
    rogue.write_bytes(b'{incomplete download')
    monkeypatch.setattr(Path, 'rglob', lambda *args, **kwargs: pytest.fail('whole_cache_scan'))
    result = research.verified_jhsf_calendar(target, research.IDENTITY, research.CUTOFF)
    assert len(result['events']) == len(result['complete_groups']) == 1
    assert len(result['pending_amount_groups']) == 3
    assert result['history_complete'] is result['cash_model_eligible'] is False
    assert all(hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
               for name, digest in names.items())


@pytest.mark.parametrize(('field', 'value'), [('ticker', 'JHSF4'), ('cvm', 1), ('share_class', 'PN'),
                                           ('isin', 'BRJHSFACNPR1')])
def test_parent_API_requires_exact_identity(frozen_inputs, field, value):
    identity = {**research.IDENTITY, field: value}
    with pytest.raises(ValueError, match='exact_JHSF3'):
        research.verified_jhsf_calendar(frozen_inputs[0], identity, research.CUTOFF)


def test_comparison_retains_three_amount_conflicts_and_never_selects_exchange_values(frozen_inputs):
    report = research.build_report(frozen_inputs[0])
    before = deepcopy(report['pending_amount_groups'])
    rows = report['cached_comparison']['rows'][:3]
    assert len(rows) == 3
    assert all(r['original_CVM_notice_gross'] == '0.0480475919' for r in rows)
    assert all(r['cached_exchange_entitlement_gross'] == '0.0479407236' for r in rows)
    assert all(r['selected_final_amount'] is None and r['unresolved'] for r in rows)
    assert report['pending_amount_groups'] == before
    assert report['cached_comparison']['selection_completed_before_market_comparison']
    assert report['network']['B3_network_requests'] == 0
    assert len(report['network']['requests']) == 12
    assert report['model_admissions'] == 0


def test_no_fake_independence_settlement_or_history_completion(evidence):
    assert evidence['source_independence']['author_lineages'] == ['JHSF_issuer']
    assert evidence['source_independence']['CVM_and_RI_are_not_independent_authors']
    for field in ('history_complete', 'cash_model_eligible', 'settlement_verified',
                  'point_in_time_archive_verified', 'financial_gates_waived',
                  'missing_as_zero', 'parent_files_changed', 'parent_pointers_changed'):
        assert evidence[field] is False


def fake_document(sequence='123'):
    url = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
           f'&CodigoInstituicao=1&numProtocolo=1234&numSequencia={sequence}&numVersao=1')
    return {'url': url, 'sequence': sequence, 'version': 1, 'cvm': 20605}


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Type': 'text/plain'}


@pytest.mark.parametrize('status', [401, 403, 429, 302])
def test_denials_and_redirects_persistently_stop_new_requests(tmp_path, monkeypatch, status):
    document = fake_document()
    calls = []
    class Denied:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'stopped', {}, io.BytesIO(b'stop'))
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = Denied()
    _, result = fetcher._get(document['url'], document)
    assert result['access_stop']
    other = research.Fetcher(tmp_path)
    other.opener = Denied()
    with pytest.raises(ValueError, match='prior_access_stop'):
        other._get(research.ROBOT_URL)
    assert calls == [document['url']]


def test_robots_disallow_prevents_document_and_never_fetches_denied_origin(tmp_path, monkeypatch):
    doc = fake_document()
    monkeypatch.setattr(research, 'indexed_documents', lambda reader: ([doc], []))
    monkeypatch.setattr(research, 'cached_document', lambda *args: None)
    calls = []
    class Robots:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            return Response(b'User-agent: *\nDisallow: /ENET/\n')
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = Robots()
    with pytest.raises(ValueError, match='robots_disallow'):
        fetcher.acquire(doc)
    assert calls == [research.ROBOT_URL]


def test_404_robots_and_original_request_are_both_budgeted_without_retry(tmp_path, monkeypatch):
    doc = fake_document()
    monkeypatch.setattr(research, 'indexed_documents', lambda reader: ([doc], []))
    monkeypatch.setattr(research, 'cached_document', lambda *args: None)
    calls = []
    class Server:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            if request.full_url == research.ROBOT_URL:
                raise urllib.error.HTTPError(request.full_url, 404, 'absent', {}, io.BytesIO(b'not found'))
            return Response(b'%PDF-synthetic network fixture')
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = Server()
    fetcher.acquire(doc)
    assert len(fetcher.ledger('request')) == len(fetcher.ledger('response')) == 2
    with pytest.raises(ValueError, match='already_attempted_no_retry'):
        fetcher.acquire(doc)
    assert calls == [research.ROBOT_URL, doc['url']]


def test_persistent_cap_counts_unfinished_reservations(tmp_path):
    for i in range(research.MAX_REQUESTS):
        research.save_body(tmp_path, f'request-{i + 1:02d}.json', research.dump({'url': f'unused-{i}'}))
    with pytest.raises(ValueError, match='request_budget_exhausted'):
        research.Fetcher(tmp_path)._get(research.ROBOT_URL)


@pytest.mark.parametrize('url', [
    'https://ri.b3.com.br/', 'https://www.b3.com.br/', 'https://bmfbovespa.com.br/',
    'https://bovespa.com.br/', 'https://bvmf.com.br/', 'https://example.com/exchangeproxy',
    'https://ri.azzas2154.com.br/', 'https://allos.com.br/', 'https://www.santander.com.br/ri/',
    'https://www.engie.com.br/', 'https://cdn-sites-assets.mziq.com/',
    'https://user@www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx',
    fake_document()['url'] + '&numSequencia=124',
])
def test_network_rejects_exchange_denied_and_ambiguous_origins(url):
    with pytest.raises(ValueError):
        research.official_url({**fake_document(), 'url': url})


def test_no_redirect_handler_never_follows_location():
    assert research._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://ri.b3.com.br/') is None


def test_saves_are_confined_and_immutable(tmp_path):
    for name in ('../current.json', 'current/pointer.json', 'C:/current.json'):
        with pytest.raises(ValueError, match='filename'):
            research.save_body(tmp_path, name, b'data')
    name = research.save_body(tmp_path, 'safe.json', b'original')
    assert name.startswith(research.CACHE + '/')
    with pytest.raises(ValueError, match='immutable'):
        research.save_body(tmp_path, 'safe.json', b'changed')
