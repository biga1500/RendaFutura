from copy import deepcopy
import hashlib
import json
from pathlib import Path
import urllib.error

import pytest

from . import comgas_cash_research as module


ROOT = Path(__file__).parent / 'data/market'


def document(sequence='123'):
    url = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
           f'&CodigoInstituicao=1&numProtocolo=100&numSequencia={sequence}&numVersao=1')
    return {'sequence': sequence, 'version': 1, 'cvm': 15636, 'published_at': '2022-12-21',
            'url': url, 'index_evidence': {'raw_row': {'CNPJ_Companhia': '61.856.571/0001-17',
                                                    'Codigo_CVM': '15636'}}}


def sample():
    pages = ['Comgas CNPJ 61.856.571/0001-17. Juros sobre capital proprio. '
             'Acoes ordinarias, no valor de R$ 0,116210115892 por acao. '
             'Posicao acionaria verificada em 26 de dezembro de 2022. '
             'O pagamento aos acionistas ocorrera em 05 de janeiro de 2023. '
             'Acoes preferenciais, no valor de R$ 0,127831127481 por acao.']
    review = {'sequence': '123', 'sha256': 'a' * 64,
        'record_quote': 'Posicao acionaria verificada em 26 de dezembro de 2022',
        'payment_quote': 'O pagamento aos acionistas ocorrera em 05 de janeiro de 2023',
        'events': [{'label': 'JCP', 'kind_quote': 'Juros sobre capital proprio',
            'amount_quote': 'Acoes ordinarias, no valor de R$ 0,116210115892 por acao',
            'gross_ptbr': '0,116210115892'}]}
    doc = document()
    return pages, review, doc, {'sha256': 'a' * 64, 'url': doc['url']}


def test_literal_ON_gross_record_and_payment_roles_without_input_mutation():
    args = sample()
    original = deepcopy(args)
    event, = module.parse_reviewed_notice(*args)
    assert args == original
    assert event['share_class'] == 'ON'
    assert event['isin'] == 'BRCGASACNOR0'
    assert event['gross_per_share_decimal'] == '0.116210115892'
    assert event['record_date'] == '2022-12-26'
    assert event['payment_date'] == '2023-01-05'
    assert not event['cash_model_eligible']
    assert not event['settlement_verified']


@pytest.mark.parametrize('change', ['PN', 'amount', 'hash', 'CNPJ', 'ex_date', 'payment_missing'])
def test_unproven_class_amount_identity_or_date_cannot_become_an_ON_event(change):
    pages, review, doc, source = sample()
    if change == 'PN':
        review['events'][0].update(amount_quote='Acoes preferenciais, no valor de R$ 0,127831127481 por acao',
                                    gross_ptbr='0,127831127481')
    elif change == 'amount':
        review['events'][0]['gross_ptbr'] = '0,127831127481'
    elif change == 'hash':
        source['sha256'] = 'b' * 64
    elif change == 'CNPJ':
        pages[0] = pages[0].replace('61.856.571/0001-17', '00.000.000/0001-00')
    elif change == 'ex_date':
        review['record_quote'] = 'Acoes negociadas ex juros em 27 de dezembro de 2022'
        pages[0] += ' ' + review['record_quote']
    elif change == 'payment_missing':
        pages[0] = pages[0].replace('O pagamento aos acionistas ocorrera em 05 de janeiro de 2023.', '')
    with pytest.raises(ValueError):
        module.parse_reviewed_notice(pages, review, doc, source)


def test_dates_never_use_ex_day_or_tax_exemption_deadline():
    with pytest.raises(ValueError, match='record_date_role'):
        module._quote_day('A partir de 27 de dezembro de 2022 ex dividendos', 'record')
    with pytest.raises(ValueError, match='payment_date_role'):
        module._quote_day('Imunes devem apresentar declaracao ate 02 de janeiro de 2023', 'payment')
    with pytest.raises(ValueError, match='one_explicit_date'):
        module._quote_day('Pagamento ocorrera em 05 de janeiro de 2023 ou 06 de janeiro de 2023', 'payment')


def test_no_zero_or_PN_division_fallback():
    for text in ('0,000000', '-1,0', 'unknown', '1.000'):
        with pytest.raises(ValueError):
            module._amount(text)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_current_batch_and_future_acquisition(tmp_path, monkeypatch, status):
    docs = [document('123'), document('124')]
    monkeypatch.setattr(module, 'documents', lambda reader: (docs, [], []))
    monkeypatch.setattr(module, 'cached_document', lambda *args: None)
    calls = []

    class Denied:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'Denied', {}, None)

    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: Denied())
    results = module.acquire(tmp_path, ['123', '124'])
    assert len(calls) == len(results) == 1
    with pytest.raises(ValueError, match='prior_denial'):
        module.acquire(tmp_path, ['124'])
    assert len(calls) == 1


def test_persistent_16_request_cap_includes_search_and_RI_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'documents', lambda reader: ([document()], [], []))
    monkeypatch.setattr(module, 'cached_document', lambda *args: None)
    for number in range(16):
        module._save(tmp_path, 'request', module._dump({'request_number': number + 1,
            'url': 'already_requested_' + str(number)}), '.json')
    with pytest.raises(ValueError, match='budget_exhausted'):
        module.acquire(tmp_path, ['123'])
    with pytest.raises(ValueError, match='budget_or_no_retry'):
        module.acquire_ri(tmp_path)


def test_network_is_exact_CVM_only_no_exchange_proxy_or_credentials():
    for url in ('https://www.b3.com.br/', 'https://example.com/proxy?url=cvm.gov.br',
                'https://user:secret@www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx',
                document()['url'] + '&numSequencia=124'):
        doc = document()
        doc['url'] = url
        with pytest.raises(ValueError):
            module._official_url(doc)


@pytest.fixture(scope='module')
def real_report():
    if not (ROOT / module.CACHE / 'reviewed-notices.json').is_file():
        pytest.skip('original primary research cache unavailable')
    writes = []
    original_save = module._save

    def capture(*args, **kwargs):
        name = original_save(*args, **kwargs)
        writes.append(name)
        return name

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, '_save', capture)
        patch.setattr(module.urllib.request, 'build_opener', lambda *args: pytest.fail('offline_report_sent_request'))
        target, report = module.build_report(ROOT)
    assert writes and all(name.startswith(module.CACHE + '/') for name in writes)
    assert target.startswith(module.CACHE + '/')
    return report


def test_original_native_forms_select_only_exact_ON_ISIN_and_latest_version(real_report):
    events = real_report['events']
    assert len(events) == 19
    assert all(e['isin'] == 'BRCGASACNOR0' and e['share_class'] == 'ON' for e in events)
    assert all(e['gross_per_share_decimal'] != '0' for e in events)
    assert not any(e['document_sequence'] == '1058859' for e in events)
    assert sum(e['document_sequence'] == '1061747' for e in events) == 2
    assert {(e['label'], e['gross_per_share_decimal']) for e in events
            if e['record_date'] == '2023-03-24'} == {
        ('JCP', '0.12707560066'), ('DIVIDENDO', '2.95450718746')}
    docs = {d['document']['sequence']: d for d in real_report['documents']}
    assert docs['566184']['event_count'] == 0
    assert 'debt_ISINs' in docs['566184']['reason']
    assert docs['568565']['event_count'] == 0
    assert 'index_only' in docs['568565']['reason']
    assert docs['550751']['status'] == 'unusable'


def test_primary_payment_disagreement_is_preserved_without_selected_payment(real_report):
    event, = [e for e in real_report['events'] if e['record_date'] == '2022-11-03']
    assert event['scheduled_payment_date'] == '2022-11-14'
    assert event['payment_date'] is None
    assert event['issues'] == ['primary_payment_date_conflict']
    conflict, = real_report['payment_conflicts']
    assert conflict['DFP_reported_payment_date'] == '2022-11-11'
    assert not real_report['calendar_complete']


def test_missing_march_schedule_is_not_silently_filled_from_secondary_calendar(real_report):
    missing = real_report['exact_missing']
    assert {(row['record_date'], row['label']) for row in missing} == {
        ('2022-03-24', 'JCP'), ('2022-03-24', 'DIVIDENDO')}
    assert not any(e['record_date'] == '2022-03-24' for e in real_report['events'])
    assert all(row['payment_date'] == '2022-04-04' for row in real_report['cross_source_candidates'])
    assert all(not row['cash_model_eligible'] for row in real_report['cross_source_candidates'])
    assert real_report['network']['new_requests'] == 16
    assert real_report['network']['exchange_requests'] == 0
    assert not real_report['active_model_changed']


def test_all_reported_original_hashes_still_match_cache(real_report):
    for source in real_report['sources']:
        body = (ROOT / source['file']).read_bytes()
        assert hashlib.sha256(body).hexdigest() == source['sha256']


def test_corrupted_original_cache_fails_closed(tmp_path):
    folder = tmp_path / module.CACHE
    folder.mkdir(parents=True)
    (folder / ('cvm123v1-' + 'a' * 64 + '.pdf')).write_bytes(b'%PDF-changed-original')
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        module.cached_document(module.Reader(tmp_path), document())
