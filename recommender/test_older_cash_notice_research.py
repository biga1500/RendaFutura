"""Adversarial field-role checks and offline replay of the actual old originals."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import urllib.error

import pytest

from . import older_cash_notice_research as research


ROOT = Path(__file__).parent / 'data/market'
URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
       '&CodigoInstituicao=1&numProtocolo=123&numSequencia=123&numVersao=1')


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('tests_must_never_use_live_network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture
def fields():
    quotes = {
        'issuer': 'O BANESTES S.A. (B3: BEES3 e BEES4)',
        'label': 'pagamento de Juros sobre o Capital Proprio Intermediarios',
        'amount': 'valor bruto por acao, ordinaria e preferencial, de R$ 0,0748258034',
        'net': 'valor liquido de R$ 0,0636019329 por acao',
        'record': 'data-base de 30.06.2022',
        'payment': 'Os pagamentos serao realizados no dia 15.07.2022',
        'ex': 'a partir do dia 01.07.2022',
        'approval': 'Vitoria, ES, 27 de junho de 2022',
    }
    document = {'cvm': 1155, 'sequence': '123', 'version': 1,
        'reference_date': '2022-06-27', 'published_at': '2022-06-27', 'url': URL,
        'category': 'Fato Relevante', 'type': '',
        'index_evidence': {'sha256': 'a' * 64, 'raw_row': {
            'Codigo_CVM': '1155', 'CNPJ_Companhia': '28.127.603/0001-78',
            'Link_Download': URL, 'Versao': '1', 'Data_Entrega': '2022-06-27'}}}
    review = {k: document[k] for k in ('cvm', 'sequence', 'version', 'reference_date', 'published_at')}
    review.update(sha256='b' * 64, index_sha256='a' * 64,
        spans={k: {'page': 1, 'quote': v} for k, v in quotes.items()}, facts=[{
            'label': 'JCP', 'class_scope': ['ON', 'PN'], 'amount': '0.0748258034',
            'record_date': '2022-06-30', 'payment_date': '2022-07-15',
            'ex_date': '2022-07-01', 'approval_date': '2022-06-27',
            'evidence': {'label': 'label', 'amount': 'amount', 'class_scope': 'amount',
                'record_date': 'record', 'payment_date': 'payment', 'ex_date': 'ex', 'approval_date': 'approval'}}])
    return ['\n'.join(quotes.values())], review, document, {'sha256': 'b' * 64}


def replay(fields):
    return research.reviewed_facts(*fields)


def test_explicit_gross_is_read_directly_with_separate_net_span(fields):
    fact = replay(fields)['facts'][0]
    assert fact['amount'] == '0.0748258034'
    assert fact['gross_reconstructed_from_net'] is False


@pytest.mark.parametrize('amount', ['0.0636019329', '0.084177', '0.074826', '0.09540109749'])
def test_net_rounded_or_whole_group_amount_cannot_replace_printed_installment(fields, amount):
    fields[1]['facts'][0]['amount'] = amount
    with pytest.raises(ValueError, match='exact_gross_per_share'):
        replay(fields)


def test_pointing_at_explicit_net_span_does_not_authorize_gross_up(fields):
    fields[1]['facts'][0].update(amount='0.0636019329')
    fields[1]['facts'][0]['evidence']['amount'] = 'net'
    with pytest.raises(ValueError, match='net_or_principal'):
        replay(fields)


@pytest.mark.parametrize('field,value', [
    ('record_date', '2022-07-01'), ('record_date', '2022-07-15'),
    ('payment_date', '2022-06-30'), ('payment_date', '2022-07-01'),
    ('approval_date', '2022-07-15'), ('ex_date', '2022-06-30'),
])
def test_record_ex_payment_and_approval_are_not_interchangeable(fields, field, value):
    fields[1]['facts'][0][field] = value
    with pytest.raises(ValueError, match='exact_.*date'):
        replay(fields)


def test_reassigning_quote_and_date_still_requires_payment_role(fields):
    fact = fields[1]['facts'][0]
    fact['payment_date'] = fact['record_date']
    fact['evidence']['payment_date'] = 'record'
    with pytest.raises(ValueError, match='payment_date_role'):
        replay(fields)


@pytest.mark.parametrize('classes', [['ON'], ['PN'], ['PNA'], ['PN', 'ON']])
def test_joint_class_amount_cannot_be_relabelled_as_one_class(fields, classes):
    fields[1]['facts'][0]['class_scope'] = classes
    with pytest.raises(ValueError, match='class_scope'):
        replay(fields)


def test_ON_only_amount_cannot_be_assigned_to_PN(fields):
    fields[0][0] = fields[0][0].replace('ordinaria e preferencial', 'ordinaria')
    fields[1]['spans']['amount']['quote'] = fields[1]['spans']['amount']['quote'].replace('ordinaria e preferencial', 'ordinaria')
    fields[1]['facts'][0]['class_scope'] = ['PN']
    with pytest.raises(ValueError, match='class_scope'):
        replay(fields)


@pytest.mark.parametrize('label', ['DIVIDENDO', 'PRINCIPAL', 'AMORTIZACAO'])
def test_JCP_narrative_cannot_become_dividend_or_return_of_principal(fields, label):
    fields[1]['facts'][0]['label'] = label
    with pytest.raises(ValueError, match='label|principal'):
        replay(fields)


@pytest.mark.parametrize('key,value', [
    ('cvm', 14605), ('version', 2), ('sequence', '999'),
    ('published_at', '2022-06-28'), ('reference_date', '2022-12-12'),
    ('sha256', 'f' * 64), ('index_sha256', 'f' * 64),
])
def test_review_must_bind_exact_original_index_identity_and_version(fields, key, value):
    fields[1][key] = value
    with pytest.raises(ValueError, match='identity_mismatch|hash_mismatch'):
        replay(fields)


def test_unseen_page_or_invented_quote_is_not_evidence(fields):
    fields[1]['spans']['record']['page'] = 2
    with pytest.raises(ValueError, match='page_span'):
        replay(fields)
    fields[1]['spans']['record'].update(page=1, quote='data-base de 29.06.2022')
    with pytest.raises(ValueError, match='quote_not_found'):
        replay(fields)


@pytest.mark.parametrize('field,value', [
    ('cvm', 14605), ('reference_date', '2023-06-27'),
    ('category', 'Relatorio Proventos'), ('type', 'Outra reuniao'),
])
def test_revision_cannot_cross_issuer_period_category_or_document_type(fields, field, value):
    old = replay(fields)
    new = deepcopy(old)
    new['document'].update(version=2, published_at='2022-06-28')
    new['document'][field] = value
    with pytest.raises(ValueError, match='family_mismatch'):
        research.audit_revision(old, new)


def test_large_document_sequence_does_not_make_a_revision(fields):
    old = replay(fields)
    new = deepcopy(old)
    new['document']['sequence'] = '999999999'
    with pytest.raises(ValueError, match='chronology'):
        research.audit_revision(old, new)


@pytest.mark.parametrize('url', [
    'https://b3.com.br/api/test', 'https://sistemaswebb3-listados.b3.com.br/api',
    'https://ri.azzas2154.com.br/', 'https://allos.com.br/',
    'https://proxy.test/?url=' + URL, 'http://www.rad.cvm.gov.br/robots.txt',
    URL + '&extra=1', URL.replace('www.rad.cvm.gov.br', 'www.rad.cvm.gov.br.evil.test'),
])
def test_acquisition_rejects_exchange_proxy_denied_origins_and_lookalikes(tmp_path, url):
    with pytest.raises(ValueError, match='unapproved_URL'):
        research.Fetcher(tmp_path)._get(url)
    assert not research.Fetcher(tmp_path).ledger('request')


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_persistently_after_fetcher_restart(tmp_path, status):
    fetcher = research.Fetcher(tmp_path)
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(URL, status, 'denied', {}, io.BytesIO(b'denied'))
    fetcher.opener = SimpleNamespace(open=denied)
    _, response = fetcher._get(URL)
    assert response['http_status'] == status
    with pytest.raises(ValueError, match='access_stop'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)
    assert len(fetcher.ledger('request')) == 1


def test_twenty_request_budget_includes_failed_and_robots_requests(tmp_path):
    for ordinal in range(20):
        research.save_body(tmp_path, f'request-{ordinal + 1:02d}.json',
                           research.shared._dump({'ordinal': ordinal + 1, 'url': f'cached:{ordinal}'}))
    with pytest.raises(ValueError, match='request_budget'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)


def test_acquisition_cannot_use_unindexed_document(tmp_path, monkeypatch):
    monkeypatch.setattr(research, 'indexed_documents', lambda reader: ([], []))
    with pytest.raises(ValueError, match='exact_native_index'):
        research.Fetcher(tmp_path).acquire({'url': URL})


def test_scope_is_confined_and_saved_original_is_immutable(tmp_path):
    research.save_body(tmp_path, 'sample.bin', b'original')
    with pytest.raises(ValueError, match='immutable'):
        research.save_body(tmp_path, 'sample.bin', b'different')
    with pytest.raises(ValueError, match='unsafe'):
        research.save_body(tmp_path, '../../../current.json', b'different')


@pytest.fixture(scope='module')
def frozen():
    if not (ROOT / research.CACHE / 'reviewed-notices.json').is_file():
        pytest.skip('actual immutable research caches not distributed')
    return research.load_older_cash_evidence(ROOT)


def test_complete_original_groups_replay_without_comparator_reads(frozen):
    snapshot = frozen.snapshot_for(ROOT)
    groups = {g['id']: g for ticker in research.TARGETS for g in research.complete_groups(snapshot, ticker)}
    expected = {
        'ENMT3:2022-03-22': ('0.91039385099', 1),
        'ENMT4:2022-03-22': ('0.91039385099', 1),
        'ENMT4:2023-08-15': ('0.76600797052', 1),
        'ENMT3:2023-11-14': ('1.87482625607', 1),
        'ENMT4:2023-11-14': ('1.10881828421', 1),
        'BEES3:2022-06-30': ('0.09540109749', 2),
        'BEES4:2022-06-30': ('0.09540109749', 2),
        'BEES3:2022-12-15': ('0.0833426245', 1),
        'BEES4:2022-12-15': ('0.0833426245', 1),
    }
    assert {k: (g['gross_total_decimal'], g['installment_count']) for k, g in groups.items()} == expected
    for ticker in ('BEES3', 'BEES4'):
        rows = groups[ticker + ':2022-06-30']['events']
        assert [(r['payment_date'], r['gross_per_share_decimal']) for r in rows] == [
            ('2022-07-15', '0.0748258034'), ('2022-08-01', '0.02057529409')]
    assert not any('independent_cash' in source.get('roles', []) for source in snapshot['preloaded_sources'])


def test_frozen_loader_reused_for_all_stocks_twice_and_never_reopens_originals(frozen, monkeypatch):
    def no_reload(*args, **kwargs):
        raise AssertionError('preloaded_path_must_not_scan_or_parse_again')
    monkeypatch.setattr(research, 'read_evidence', no_reload)
    for _ in range(2):
        for ticker, target in research.TARGETS.items():
            result = research.verified_older_cash_calendar(ROOT, {'ticker': ticker, **target}, date(2026, 9, 30), evidence=frozen)
            assert result['verified_by_older_cash_loader']
            assert result['review_manifest_sha256'] == research.REVIEW_SHA256
            for row in result['events']:
                assert row['complete_group_id'] and row['target_group_complete'] is True
                assert row['verified_by_older_cash_loader'] is True
                assert row['source_locator']['format'] == 'CVM_reviewed_old_notice_complete_group'
            assert not result['cash_model_eligible'] and not result['history_complete']
            assert not result['settlement_verified'] and not result['point_in_time_archive_verified']


def test_frozen_loader_cannot_cross_folders_or_accept_plain_dictionary(frozen, tmp_path):
    stock = {'ticker': 'ENMT3', **research.TARGETS['ENMT3']}
    with pytest.raises(ValueError, match='exact_folder'):
        research.verified_older_cash_calendar(tmp_path, stock, research.CUTOFF, evidence=frozen)
    with pytest.raises(ValueError, match='frozen_evidence'):
        research.verified_older_cash_calendar(ROOT, stock, research.CUTOFF, evidence=frozen.snapshot_for(ROOT))
    with pytest.raises(FrozenInstanceError):
        frozen.review_sha256 = 'f' * 64


def test_caller_mutation_cannot_poison_reused_build_snapshot(frozen):
    stock = {'ticker': 'ENMT3', **research.TARGETS['ENMT3']}
    first = research.verified_older_cash_calendar(ROOT, stock, research.CUTOFF, evidence=frozen)
    first['events'][0]['record_date'] = '2022-03-21'
    first['events'].clear()
    second = research.verified_older_cash_calendar(ROOT, stock, research.CUTOFF, evidence=frozen)
    assert second['events'][0]['record_date'] == '2022-03-22'


@pytest.mark.parametrize('key,value', [('cvm', 1155), ('share_class', 'PN'), ('isin', 'BRENMTACNPR2')])
def test_adapter_rejects_wrong_exact_security(frozen, key, value):
    stock = {'ticker': 'ENMT3', **research.TARGETS['ENMT3'], key: value}
    with pytest.raises(ValueError, match='exact_identity'):
        research.verified_older_cash_calendar(ROOT, stock, research.CUTOFF, evidence=frozen)


def test_unknown_ticker_or_unreviewed_cutoff_does_not_emit_rows(tmp_path):
    assert research.verified_older_cash_calendar(tmp_path, {'ticker': 'PETR3'}, research.CUTOFF)['events'] == []
    stock = {'ticker': 'ENMT3', **research.TARGETS['ENMT3']}
    assert research.verified_older_cash_calendar(tmp_path, stock, '2026-08-31')['events'] == []


def test_ENMT_does_not_use_old_record_or_old_equal_class_amount(frozen):
    snapshot = frozen.snapshot_for(ROOT)
    assert '21 de marco' in snapshot['reviews']['479947']['spans']['old_record']['quote']
    assert '1,37307285851' in snapshot['reviews']['685367']['spans']['old_amount']['quote']
    on = research.complete_groups(snapshot, 'ENMT3')
    assert on[0]['events'][0]['record_date'] == '2022-03-22'
    assert on[1]['events'][0]['gross_per_share_decimal'] == '1.87482625607'
    components = on[1]['events'][0]['source_locator']['narrative_components']
    assert len(components) == 2
    assert not any(g['record_date'] == '2023-08-15' for g in on)


def test_same_reference_higher_version_different_provento_is_not_a_revision(frozen):
    snapshot = frozen.snapshot_for(ROOT)
    old, other = snapshot['native']['565624'][0], snapshot['native']['613540'][0]
    assert old['native_protocol'] == '1040604'
    assert other['native_protocol'] == '1040658'
    assert other['fiscal_year'] == '2023'
    for ticker in ('BEES3', 'BEES4'):
        groups = research.complete_groups(snapshot, ticker)
        assert groups[1]['record_date'] == '2022-12-15'
        assert groups[1]['events'][0]['label'] == 'JCP'
        assert groups[1]['events'][0]['payment_date'] == '2022-12-29'


def test_unresolved_December_monthly_record_is_retained_and_not_relocated(frozen):
    snapshot = frozen.snapshot_for(ROOT)
    monthly = snapshot['monthly']['468095'][-1]
    assert monthly['record_date'] == '2022-12-30'
    assert monthly['payment_date'] == '2023-02-01'
    assert monthly['amount'] == '0.02057529409'
    gap = snapshot['manifest']['remaining_gaps'][0]
    assert gap['record_dates_in_conflict'] == ['2022-12-29', '2022-12-30']
    assert not gap['emit_to_parent_calendar']
    for ticker in ('BEES3', 'BEES4'):
        assert not any(g['record_date'].startswith('2022-12-3') or g['record_date'] == '2022-12-29'
                       for g in research.complete_groups(snapshot, ticker))


def test_native_rows_cannot_cross_class_or_disagree_with_notice_role_dates(frozen):
    snapshot = frozen.snapshot_for(ROOT)
    narrative = snapshot['reviews']['686045']['facts'][1]
    pn_only = [r for r in snapshot['native']['686059'] if r['isin'] == research.TARGETS['ENMT4']['isin']]
    with pytest.raises(ValueError, match='exact_class_ISIN'):
        research._native_for_stock(pn_only, 'ENMT3', narrative)
    wrong = deepcopy(snapshot['native']['686059'])
    wrong[0]['record_date'] = '2023-11-15'
    with pytest.raises(ValueError, match='role_date_mismatch'):
        research._native_for_stock(wrong, 'ENMT3', narrative)


def test_original_or_manifest_hash_change_prevents_loader_construction(monkeypatch):
    original = research.shared.Reader.read
    def changed(reader, name, *args, **kwargs):
        body, source = original(reader, name, *args, **kwargs)
        if name.endswith('reviewed-notices.json'):
            source = {**source, 'sha256': 'f' * 64}
        return body, source
    monkeypatch.setattr(research.shared.Reader, 'read', changed)
    with pytest.raises(ValueError, match='manifest_hash'):
        research.load_older_cash_evidence(ROOT)


def test_changed_native_original_bytes_cannot_construct_frozen_loader(monkeypatch):
    original = research.shared.Reader.read
    def changed(reader, name, *args, **kwargs):
        if 'cvm686059v2-' in name:
            raise ValueError('original_content_hash_mismatch')
        return original(reader, name, *args, **kwargs)
    monkeypatch.setattr(research.shared.Reader, 'read', changed)
    with pytest.raises(ValueError, match='original_content_hash'):
        research.load_older_cash_evidence(ROOT)


def test_bad_preloaded_payload_digest_is_rejected(frozen):
    broken = object.__new__(research.OlderCashEvidence)
    object.__setattr__(broken, 'folder', frozen.folder)
    object.__setattr__(broken, 'review_sha256', frozen.review_sha256)
    object.__setattr__(broken, '_payload', frozen._payload + b' ')
    object.__setattr__(broken, '_payload_sha256', frozen._payload_sha256)
    with pytest.raises(ValueError, match='preloaded_evidence_hash'):
        broken.snapshot_for(ROOT)


def test_unmapped_native_annex_is_never_silently_treated_as_single_payment(frozen, monkeypatch):
    import pdfplumber
    snapshot = frozen.snapshot_for(ROOT)
    document = snapshot['originals']['686059']['document']
    source = snapshot['originals']['686059']['source']
    body = (ROOT / source['file']).read_bytes()
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [table for page in pdf.pages for table in page.extract_tables()]
    tables[-1].append(['04/12/2023', '1,00000000000'])
    class FakePDF:
        pages = [SimpleNamespace(extract_tables=lambda: tables)]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(pdfplumber, 'open', lambda *args, **kwargs: FakePDF())
    with pytest.raises(ValueError, match='installment_annex_requires'):
        research.native_facts(body, document, source)


def test_monthly_calendar_cannot_swap_entitlement_and_payment_headers(monkeypatch):
    import pdfplumber
    table = [
        ['Mes de Referencia', 'Data de Pagamento', 'Data Ex-Direito', 'Data-base de Direito',
         'Valor Bruto por acao (ON e PN)'],
        ['Janeiro', '31/01/2022', '01/02/2022', '02/03/2022', '0,02057529409'],
    ]
    class FakePDF:
        pages = [SimpleNamespace(extract_tables=lambda: [table])]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(pdfplumber, 'open', lambda *args, **kwargs: FakePDF())
    with pytest.raises(ValueError, match='exact_twelve_month'):
        research.monthly_rows(b'cached', {'reference_date': '2022-01-24'}, {})


def test_denial_status_is_persisted_even_when_error_body_is_unreadable(tmp_path):
    class Unreadable:
        def read(self, *args):
            raise OSError('unreadable')
        def close(self):
            pass
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(URL, 403, 'denied', {}, Unreadable())
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = SimpleNamespace(open=denied)
    _, response = fetcher._get(URL)
    assert response['http_status'] == 403
    with pytest.raises(ValueError, match='access_stop'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)


def test_cached_acquisition_stays_within_eighteen_attempts_and_keeps_non_PDF_failures(frozen):
    fetcher = research.Fetcher(ROOT)
    assert len(fetcher.ledger('request')) == 18 <= research.MAX_REQUESTS
    assert not any(r['http_status'] in (401, 403, 429) for r in fetcher.ledger('response'))
    snapshot = frozen.snapshot_for(ROOT)
    assert {d['sequence'] for d in snapshot['diagnostics'] if 'empty_ENET_HTML' in d['code']} == {'480363', '520158'}
    assert all(p['all_categories'] and p['index_scan_complete'] for p in snapshot['periods'])
    assert [p['year'] for p in snapshot['periods']] == list(range(2021, 2027))


def test_adapter_is_read_only_without_asserting_shared_pointer_state(frozen, monkeypatch):
    import builtins
    original_open, original_io_open = builtins.open, io.open
    def guarded_open(original):
        def checked(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in 'wax+'):
                raise AssertionError('adapter_must_not_open_files_for_writing')
            return original(file, mode, *args, **kwargs)
        return checked
    def no_write(*args, **kwargs):
        raise AssertionError('adapter_must_not_write')
    monkeypatch.setattr(builtins, 'open', guarded_open(original_open))
    monkeypatch.setattr(io, 'open', guarded_open(original_io_open))
    monkeypatch.setattr(research, 'save_body', no_write)
    for ticker, target in research.TARGETS.items():
        research.verified_older_cash_calendar(ROOT, {'ticker': ticker, **target}, research.CUTOFF, evidence=frozen)
