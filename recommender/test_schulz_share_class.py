"""Offline regression contracts for the exact SHUL4 PNU/native-PN binding."""

import builtins
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from . import cash_calendar_research, schulz_share_class as schulz
from . import selected_identity_research
from .collect_market_data import financial_features
from .inventory_candidates import _Cache, _cash_pages, _identity, build_candidates
from .inventory_financials import prepare_financials
from .security_registration import FCA_URL, SecurityRegistry
from .selected_identity_research import scan_fca
from .share_basis import b3_evidence
from .test_inventory_candidates import fixture as candidate_fixture, hashes, write
from .test_inventory_financials import annual, cache as financial_cache
from .test_security_registration import archive, filing, security


MARKET = Path(__file__).parent / 'data/market'
CUTOFF = date(2026, 9, 30)
IDENTITY = {'ticker': 'SHUL4', 'cvm': 14664, 'share_class': 'PNU', 'isin': 'BRSHULACNPR4'}
STOCK = {**IDENTITY, 'sector_code': 5, 'identity_status': 'official_cvm_fca'}
CLAUSE = ('84.693.183/0001-68 1466-4 acoes preferenciais da classe existente '
          'dividendo 10% (dez por cento) maior')


@pytest.fixture(autouse=True)
def offline_read_only_market(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('SHUL4 regression tests must not access the network')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    root = MARKET.resolve()

    def guard(original):
        def read_only(file, mode='r', *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve().is_relative_to(root):
                assert not any(flag in mode for flag in 'wax+'), 'real market cache is read-only'
            return original(file, mode, *args, **kwargs)
        return read_only

    monkeypatch.setattr(builtins, 'open', guard(builtins.open))
    monkeypatch.setattr(io, 'open', guard(io.open))


def statute_bytes(text=CLAUSE):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(('BT /F1 10 Tf 10 750 Td (' + text + ') Tj ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.fixture
def binding_inputs(tmp_path, monkeypatch):
    # Synthetic bytes have their own pinned digest; the real-cache test uses the
    # production digest and unmodified statute instead.
    body = statute_bytes()
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(schulz, 'STATUTE_SHA', digest)
    monkeypatch.setattr(schulz, 'allowed_tickers', lambda root: frozenset({'SHUL4'}))
    relative = schulz.CACHE + '/original-' + digest + '.pdf'
    receipt = {'file': relative, 'sha256': digest, 'bytes': len(body), 'url': schulz.STATUTE_URL,
               'retrieved_at': '2026-10-25T12:00:00+00:00', 'http_status': 200,
               'valid_pdf_signature': True, 'error': None}
    write(tmp_path, relative, body)
    write(tmp_path, schulz.CACHE + '/response.json', receipt)
    rows = []
    for year in range(2021, 2027):
        master = filing(CNPJ_CIA='84.693.183/0001-68', CD_CVM='14664', ID_DOC=str(year),
                        DT_REFER=f'{year}-01-01', DT_RECEB=f'{year}-04-21')
        preferred = security(CNPJ_Companhia=master['CNPJ_CIA'], Codigo_Negociacao='SHUL4',
            Data_Referencia=master['DT_REFER'], ID_Documento=master['ID_DOC'],
            Valor_Mobiliario='Acoes Preferenciais', Sigla_Classe_Acao_Preferencial='PNU',
            Classe_Acao_Preferencial='Preferencial Classe U')
        content = archive([master], [preferred], year=year)
        path = f'raw/inventory/fca_cia_aberta_{year}.zip'
        write(tmp_path, path, content)
        source = {'file': path, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                  'url': FCA_URL + f'fca_cia_aberta_{year}.zip',
                  'retrieved_at': '2026-10-25T00:00:00+00:00'}
        row, = scan_fca(content, year, ('SHUL4',), CUTOFF.isoformat())['exact_security_records']
        row['source'] = {**source, 'existing_receipts': [deepcopy(source)]}
        rows.append(row)
    detail = {'codeCVM': '14664', 'otherCodes': [{'code': 'SHUL4', 'isin': IDENTITY['isin']}]}
    detail_path = write(tmp_path, 'raw/expanded/b3-detail-14664.json', detail)
    mapping = {**IDENTITY, 'source': {'file': detail_path.relative_to(tmp_path).as_posix(),
        'sha256': hashlib.sha256(detail_path.read_bytes()).hexdigest(), 'bytes': detail_path.stat().st_size}}
    record = {**IDENTITY, 'FCA_search_complete_for_2021_2026': True,
              'FCA_exact_code_records': rows, 'cached_exact_code_mappings': [mapping]}
    report = {'records': [record]}

    def research(root, *, as_of, tickers):
        assert Path(root) == tmp_path and as_of == CUTOFF.isoformat() and tickers == ('SHUL4',)
        return deepcopy(report)

    monkeypatch.setattr(selected_identity_research, 'research', research)
    return report, receipt


def test_exact_binding_preserves_registered_class_sources_and_unknown_publication(tmp_path, binding_inputs):
    report, receipt = binding_inputs
    original, before = deepcopy(report), hashes(tmp_path)
    proof = schulz.class_binding(tmp_path, dict(STOCK), CUTOFF.isoformat())
    assert proof['identity'] == IDENTITY == schulz.IDENTITY
    assert proof['registered_class'] == 'PNU'
    assert proof['financial_source_class'] == proof['cash_source_class'] == 'PN'
    assert proof['financial_period_from'] == '2021-01-01'
    assert [row['year'] for row in proof['historical_registry']] == list(range(2021, 2027))
    for history, row in zip(proof['historical_registry'], report['records'][0]['FCA_exact_code_records']):
        assert history['filing'] == row['filing']
        assert history['member'] == row['security_member'] and history['csv_record'] == row['csv_record']
        assert history['source_sha256'] == row['source']['sha256']
    statute, = [source for source in proof['sources'] if source['file'] == receipt['file']]
    assert statute == {key: receipt[key] for key in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}
    assert proof['statute_pages'] == [1, 2]
    assert proof['only_preferred_class_in_each_received_FCA'] is True
    for key in ('statute_publication_verified', 'publication_date_inferred', 'PNU_expanded_as_Unica',
                'EPS_computed', 'original_files_modified', 'model_admitted'):
        assert proof[key] is False
    assert len(proof['sources']) == 8
    assert hashes(tmp_path) == before and report == original


@pytest.mark.parametrize('key,value', [('ticker', 'SHUL3'), ('ticker', 'SHUL4F'), ('ticker', 'OTHER4'),
    ('cvm', 123), ('cvm', '14664'), ('share_class', 'PN'), ('share_class', 'PNA'),
    ('share_class', None), ('isin', 'BRSHULACNOR1'), ('isin', None)])
def test_binding_rejects_any_nonexact_identity(tmp_path, key, value):
    with pytest.raises(ValueError, match='^Schulz_exact_identity_required$'):
        schulz.class_binding(tmp_path, {**STOCK, key: value}, CUTOFF)


@pytest.mark.parametrize('cutoff', ['2026-08-31', '2026-09-29', '2026-10-31'])
def test_binding_rejects_unreviewed_cutoffs_before_reading_sources(tmp_path, cutoff):
    with pytest.raises(ValueError, match='^unreviewed_class_binding_cutoff$'):
        schulz.class_binding(tmp_path, STOCK, cutoff)


@pytest.mark.parametrize('selection', [None, frozenset(), frozenset({'SHUL3', 'OTHER4'})])
def test_binding_requires_explicit_positive_yield_scope(tmp_path, monkeypatch, selection):
    monkeypatch.setattr(schulz, 'allowed_tickers', lambda root: selection)
    with pytest.raises(ValueError, match='^Schulz_outside_positive_yield_selection$'):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


@pytest.mark.parametrize('field,value', [('url', 'https://example.invalid/statute.pdf'),
    ('file', 'raw/other.pdf'), ('sha256', '0' * 64), ('http_status', 404),
    ('valid_pdf_signature', False), ('error', 'HTTPError')])
def test_statute_receipt_must_bind_original_bytes(tmp_path, binding_inputs, field, value):
    _, receipt = binding_inputs
    write(tmp_path, schulz.CACHE + '/response.json', {**receipt, field: value})
    with pytest.raises(ValueError, match='^statute_receipt_disagrees$'):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


@pytest.mark.parametrize('fault,reason', [('missing', None), ('changed', 'statute_original_changed'),
                                         ('size', 'unsafe_statute_original')])
def test_missing_or_tampered_statute_fails_closed(tmp_path, binding_inputs, fault, reason):
    _, receipt = binding_inputs
    path = tmp_path / receipt['file']
    if fault == 'missing':
        path.unlink()
    elif fault == 'changed':
        body = path.read_bytes()
        path.write_bytes(body.replace(b'84.693.183', b'00.000.000'))
    else:
        write(tmp_path, schulz.CACHE + '/response.json', {**receipt, 'bytes': receipt['bytes'] + 1})
    with pytest.raises(FileNotFoundError if fault == 'missing' else ValueError, match=reason):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


@pytest.mark.parametrize('missing', ['84.693.183/0001-68', '1466-4',
    'acoes preferenciais da classe existente', 'dividendo 10% (dez por cento) maior'])
def test_valid_PDF_without_exact_issuer_or_class_clause_is_rejected(tmp_path, binding_inputs, monkeypatch, missing):
    _, receipt = binding_inputs
    body = statute_bytes(CLAUSE.replace(missing, 'unrelated'))
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(schulz, 'STATUTE_SHA', digest)
    relative = schulz.CACHE + '/original-' + digest + '.pdf'
    write(tmp_path, relative, body)
    write(tmp_path, schulz.CACHE + '/response.json', {**receipt, 'file': relative,
        'sha256': digest, 'bytes': len(body)})
    with pytest.raises(ValueError, match='^statute_single_preferred_class_clause_missing$'):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


@pytest.mark.parametrize('fault,reason', [
    ('ambiguous', 'Schulz_registry_evidence_ambiguous'),
    ('incomplete_search', 'Schulz_registry_identity_disagrees'),
    ('wrong_cvm', 'Schulz_registry_identity_disagrees'),
    ('wrong_isin', 'Schulz_registry_identity_disagrees'),
    ('missing_year', 'Schulz_FCA_class_history_incomplete'),
    ('invalid_join', 'Schulz_historical_class_disagrees'),
    ('future_delivery', 'Schulz_historical_class_disagrees'),
    ('wrong_code', 'Schulz_historical_class_disagrees'),
    ('wrong_class', 'Schulz_historical_class_disagrees'),
    ('expanded_class', 'Schulz_historical_class_disagrees'),
    ('missing_receipt', 'Schulz_FCA_receipt_missing'),
    ('wrong_receipt_hash', 'Schulz_FCA_receipt_missing'),
    ('changed_archive', 'Schulz_FCA_source_changed'),
    ('missing_mapping', 'Schulz_exact_ISIN_mapping_disagrees'),
    ('wrong_mapping', 'Schulz_exact_ISIN_mapping_disagrees')])
def test_incomplete_or_conflicting_registry_evidence_fails_closed(tmp_path, binding_inputs, fault, reason):
    report, _ = binding_inputs
    record = report['records'][0]
    row = record['FCA_exact_code_records'][0]
    if fault == 'ambiguous':
        report['records'].append(deepcopy(record))
    elif fault == 'incomplete_search':
        record['FCA_search_complete_for_2021_2026'] = False
    elif fault == 'wrong_cvm':
        record['cvm'] = 123
    elif fault == 'wrong_isin':
        record['isin'] = 'BRSHULACNOR1'
    elif fault == 'missing_year':
        record['FCA_exact_code_records'].pop()
    elif fault == 'invalid_join':
        row['source_binding_valid'] = False
    elif fault == 'future_delivery':
        row['received_and_referenced_by_as_of'] = False
    elif fault == 'wrong_code':
        row['security']['Codigo_Negociacao'] = 'SHUL3'
    elif fault == 'wrong_class':
        row['security']['Sigla_Classe_Acao_Preferencial'] = 'PN'
    elif fault == 'expanded_class':
        row['security']['Classe_Acao_Preferencial'] = 'Preferencial Classe Unica'
    elif fault == 'missing_receipt':
        row['source']['existing_receipts'].clear()
    elif fault == 'wrong_receipt_hash':
        row['source']['existing_receipts'][0]['sha256'] = '0' * 64
    elif fault == 'changed_archive':
        (tmp_path / row['source']['file']).write_bytes(b'changed')
    elif fault == 'missing_mapping':
        record['cached_exact_code_mappings'].clear()
    else:
        record['cached_exact_code_mappings'][0]['isin'] = 'BRSHULACNOR1'
    with pytest.raises(ValueError, match='^' + reason + '$'):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


def test_second_preferred_class_in_same_filing_cannot_bind_generic_PN(tmp_path, binding_inputs):
    report, _ = binding_inputs
    row = report['records'][0]['FCA_exact_code_records'][0]
    master = filing(CNPJ_CIA='84.693.183/0001-68', CD_CVM='14664', ID_DOC='2021',
                    DT_REFER='2021-01-01', DT_RECEB='2021-04-21')
    common = {'CNPJ_Companhia': master['CNPJ_CIA'], 'Data_Referencia': master['DT_REFER'],
              'ID_Documento': master['ID_DOC'], 'Valor_Mobiliario': 'Acoes Preferenciais'}
    content = archive([master], [security(**common, Codigo_Negociacao='SHUL4',
        Sigla_Classe_Acao_Preferencial='PNU'), security(**common, Codigo_Negociacao='SHUL5',
        Sigla_Classe_Acao_Preferencial='PNA')], year=2021)
    write(tmp_path, row['source']['file'], content)
    row['source']['sha256'] = hashlib.sha256(content).hexdigest()
    row['source']['existing_receipts'][0]['sha256'] = row['source']['sha256']
    with pytest.raises(ValueError, match='^Schulz_multiple_preferred_classes_not_bound$'):
        schulz.class_binding(tmp_path, STOCK, CUTOFF)


def test_cash_view_changes_only_PN_label_without_mutating_sources(tmp_path, binding_inputs):
    proof = schulz.class_binding(tmp_path, STOCK, CUTOFF)
    rows = [{'typeStock': share, 'valueCash': '0,8414600000', 'quotedPerShares': '1',
             'lastDatePriorEx': '22/06/2026', 'corporateAction': 'JRS CAP PROPRIO',
             'nested_source': {'row': 2}} for share in ('PN', ' PN ', 'ON', 'PNA', 'PNB', 'PNU', '')]
    original = deepcopy(rows)
    view = schulz.cash_rows(rows, proof)
    assert rows == original
    for native, mapped in zip(original, view):
        expected = ({**native, 'typeStock': 'PNU', 'reported_typeStock': 'PN',
                     'source_class_binding': schulz.BINDING_ID} if native['typeStock'].strip() == 'PN'
                    else native)
        assert mapped == expected
    view[0]['nested_source']['row'] = 99
    assert rows == original
    native_cash, native_quotes = b3_evidence(original, 'PN')
    mapped_PN = [mapped for native, mapped in zip(original, view) if native['typeStock'] != 'PNU']
    assert b3_evidence(mapped_PN, 'PNU') == (native_cash, native_quotes)
    for share in ('ON', 'PNA', 'PNB'):
        assert b3_evidence(view, share) == b3_evidence(original, share)


@pytest.mark.parametrize('fault', ['id', 'identity'])
def test_cash_view_rejects_proof_for_other_binding_or_security(fault):
    proof = {'id': schulz.BINDING_ID, 'identity': dict(IDENTITY)}
    if fault == 'id':
        proof['id'] = 'unreviewed'
    else:
        proof['identity']['ticker'] = 'OTHER4'
    with pytest.raises(ValueError, match='^Schulz_cash_class_proof_required$'):
        schulz.cash_rows([{'typeStock': 'PN'}], proof)


@pytest.mark.parametrize('reported,expected', [('PNU', 'PNU'), ('', 'PN'), ('PNA', 'PNA'),
    ('B', 'PNB'), ('PNC', 'PNC'), ('D', 'PND'), ('U', None), ('UNKNOWN', None)])
def test_registry_preserves_explicit_PNU_and_existing_class_semantics(tmp_path, reported, expected):
    content = archive([filing(CD_CVM='14664')], [security(Codigo_Negociacao='SHUL4',
        Valor_Mobiliario='Acoes Preferenciais', Sigla_Classe_Acao_Preferencial=reported)])
    relative = 'raw/inventory/fca_cia_aberta_2026.zip'
    write(tmp_path, relative, content)
    inventory = {'stocks': [STOCK], 'sources': [{'file': relative, 'url': FCA_URL + 'fca_cia_aberta_2026.zip',
                                               'sha256': hashlib.sha256(content).hexdigest()}]}
    result = SecurityRegistry(tmp_path, inventory=inventory).status(STOCK, CUTOFF)
    assert result['security_type'] == 'preferred_share' and result['share_class'] == expected
    assert not result['exchange_trading_verified'] and not result['model_admission_granted']


def native_financial_fixture(tmp_path):
    row = annual(cvm=14664)
    next(entry for entry in row['tables']['DRE_con'] if entry['DS_CONTA'] == 'PN')['VL_CONTA'] = '0.8414600000'
    return financial_cache(tmp_path, row)


def test_financial_view_keeps_native_EPS_and_other_issuers_unchanged(tmp_path, binding_inputs):
    row = annual(cvm=14664)
    next(entry for entry in row['tables']['DRE_con'] if entry['DS_CONTA'] == 'PN')['VL_CONTA'] = '0.8414600000'
    financial_cache(tmp_path, row, annual(cvm=12345), official=True)
    native_specs = {'SHUL3': (14664, 'ON', 5), 'SHUL4': (14664, 'PN', 5),
                    'TEST3': (12345, 'ON', 0), 'TEST4': (12345, 'PN', 0)}
    native, _, _ = prepare_financials(tmp_path, native_specs, CUTOFF)
    before = hashes(tmp_path)
    inputs = [dict(STOCK), {'ticker': 'SHUL3', 'cvm': 14664, 'share_class': 'ON', 'sector_code': 5},
              {'ticker': 'TEST3', 'cvm': 12345, 'share_class': 'ON', 'sector_code': 0},
              {'ticker': 'TEST4', 'cvm': 12345, 'share_class': 'PN', 'sector_code': 0}]
    original = deepcopy(inputs)
    statements, diagnostics, sources = prepare_financials(tmp_path, inputs, CUTOFF)
    pn, = [record for record in native if record['cvm'] == 14664 and record['share_class'] == 'PN']
    pnu, = [record for record in statements if record['cvm'] == 14664 and record['share_class'] == 'PNU']
    assert pnu['share_class'] == 'PNU' and pnu['profit_per_share'] == pn['profit_per_share'] == .84146
    assert {key: value for key, value in pnu.items() if key not in ('share_class', 'financial_method')} == {
        key: value for key, value in pn.items() if key not in ('share_class', 'financial_method')}
    method = pnu['financial_method']
    assert method['source_share_class'] == 'PN' and method['registered_share_class'] == 'PNU'
    assert method['share_class_binding']['id'] == schulz.BINDING_ID
    assert {key: value for key, value in method.items() if key not in (
        'source_share_class', 'registered_share_class', 'share_class_binding')} == pn['financial_method']
    assert [record for record in statements if record['cvm'] != 14664] == [
        record for record in native if record['cvm'] != 14664]
    assert [record for record in statements if record['cvm'] == 14664 and record['share_class'] == 'ON'] == [
        record for record in native if record['cvm'] == 14664 and record['share_class'] == 'ON']
    assert financial_features(statements, [], CUTOFF, (14664, 'PNU', 5))[0][2] == .84146
    assert all(row['status'] == 'included' for row in diagnostics if row['kind'] == 'candidate')
    assert len([source for source in sources if source.get('url') == schulz.STATUTE_URL]) == 1
    assert inputs == original and hashes(tmp_path) == before


def test_financial_binding_excludes_periods_before_FCA_history(tmp_path, binding_inputs):
    for year in (2020, 2021):
        financial_cache(tmp_path, annual(cvm=14664, year=year, published=f'{year + 1}-03-01'), year=year)
    statements, diagnostics, _ = prepare_financials(tmp_path, [STOCK], CUTOFF)
    assert any(row['fiscal_end'] == '2021-12-31' and row['status'] == 'included' for row in statements)
    assert not any(row['fiscal_end'] == '2020-12-31' and row['status'] == 'included' for row in statements)
    assert any('exact_class_binding_does_not_cover_financial_period' in row.get('reasons', [])
               for row in diagnostics)


def test_binding_preserves_other_class_financial_history_before_2021(tmp_path, binding_inputs):
    financial_cache(tmp_path, annual(cvm=14664, year=2020, published='2021-03-01'), year=2020)
    ordinary = {'ticker': 'SHUL3', 'cvm': 14664, 'share_class': 'ON', 'sector_code': 5}
    native, _, _ = prepare_financials(tmp_path, [ordinary], CUTOFF)
    combined, _, _ = prepare_financials(tmp_path, [ordinary, STOCK], CUTOFF)
    historical_date = date(2021, 9, 30)
    expected = financial_features(native, [], historical_date, (14664, 'ON', 5))
    assert expected is not None
    assert financial_features(combined, [], historical_date, (14664, 'ON', 5)) == expected


def schulz_candidate_fixture(tmp_path):
    inventory = candidate_fixture(tmp_path, ticker='SHUL4')
    inventory['stocks'][0].update(STOCK)
    page = tmp_path / 'raw/expanded/SHUL4-b3-cash-1.json'
    payload = json.loads(page.read_bytes())
    for row in payload['results']:
        row['typeStock'] = 'PN'
    write(tmp_path, page.relative_to(tmp_path).as_posix(), payload)
    return inventory


def test_candidate_uses_native_PN_cash_under_exact_registered_identity(tmp_path, binding_inputs):
    inventory = schulz_candidate_fixture(tmp_path)
    original, before = deepcopy(inventory), hashes(tmp_path)
    prepared, excluded, receipts = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded, excluded
    candidate, = prepared
    assert candidate['statement_spec'] == (14664, 'PNU', 5)
    assert candidate['checks']['exact_share_class_binding']['id'] == schulz.BINDING_ID
    assert len(candidate['payments']) == len(candidate['independent_cash_matches']) == 5
    assert all(row['gross_per_unit'] == 1 for row in candidate['ledger'])
    assert not candidate['financially_validated']
    assert all(source['sha256'] == before[source['file']] for source in receipts)
    assert inventory == original and hashes(tmp_path) == before


@pytest.mark.parametrize('fault,reason', [('wrong_isin', 'Schulz_exact_identity_required'),
    ('wrong_cvm', 'Schulz_exact_identity_required'), ('wrong_ticker', 'Schulz_exact_identity_required'),
    ('cutoff', 'unreviewed_class_binding_cutoff'), ('scope', 'Schulz_outside_positive_yield_selection'),
    ('missing_statute', 'invalid_cached_source')])
def test_candidate_cannot_bypass_exact_binding(tmp_path, binding_inputs, monkeypatch, fault, reason):
    inventory = schulz_candidate_fixture(tmp_path)
    stock, cutoff = inventory['stocks'][0], CUTOFF
    if fault == 'wrong_isin':
        stock['isin'] = 'BRSHULACNOR1'
    elif fault == 'wrong_cvm':
        stock['cvm'] = 123
    elif fault == 'wrong_ticker':
        stock['ticker'] = 'OTHR4'
    elif fault == 'cutoff':
        cutoff = date(2026, 8, 31)
    elif fault == 'scope':
        monkeypatch.setattr(schulz, 'allowed_tickers', lambda root: frozenset())
    else:
        (tmp_path / schulz.CACHE / 'response.json').unlink()
    prepared, excluded, _ = build_candidates(tmp_path, inventory, cutoff)
    assert not prepared
    rejected, = excluded
    assert rejected['reason'] == reason


@pytest.mark.parametrize('fault', ['wrong_isin', 'wrong_cvm', 'wrong_ticker', 'cutoff', 'scope', 'missing_statute'])
def test_financial_adapter_cannot_fall_back_to_native_PN_without_exact_proof(
        tmp_path, binding_inputs, monkeypatch, fault):
    native_financial_fixture(tmp_path)
    stock, cutoff = dict(STOCK), CUTOFF
    if fault == 'wrong_isin':
        stock['isin'] = 'BRSHULACNOR1'
    elif fault == 'wrong_cvm':
        stock['cvm'] = 123
    elif fault == 'wrong_ticker':
        stock['ticker'] = 'OTHER4'
    elif fault == 'cutoff':
        cutoff = date(2026, 8, 31)
    elif fault == 'scope':
        monkeypatch.setattr(schulz, 'allowed_tickers', lambda root: None)
    else:
        (tmp_path / schulz.CACHE / 'response.json').unlink()
    statements, diagnostics, _ = prepare_financials(tmp_path, [stock], cutoff)
    assert financial_features(statements, [], cutoff, (stock['cvm'], 'PNU', 5)) is None
    assert not any(record['status'] == 'included' for record in statements)
    assert any(row['kind'] == 'candidate' and row['status'] == 'excluded' for row in diagnostics)


@pytest.mark.parametrize('fault,reason', [('wrong_identity', 'Schulz_exact_identity_required'),
    ('cutoff', 'unreviewed_class_binding_cutoff'), ('scope', 'Schulz_outside_positive_yield_selection')])
def test_cash_calendar_requires_same_exact_proof(tmp_path, monkeypatch, fault, reason):
    stock, cutoff = dict(STOCK), CUTOFF
    if fault == 'wrong_identity':
        stock['isin'] = 'BRSHULACNOR1'
    elif fault == 'cutoff':
        cutoff = date(2026, 8, 31)
    else:
        monkeypatch.setattr(schulz, 'allowed_tickers', lambda root: frozenset())
    with pytest.raises(ValueError, match='^' + reason + '$'):
        cash_calendar_research.load_cvm_calendar(tmp_path, stock, cutoff, [])


def test_cash_calendar_matches_native_ISIN_and_PN_amount_through_bound_view(
        tmp_path, binding_inputs, monkeypatch):
    from . import cvm_proventos_documents
    from .test_cvm_proventos_documents import DOC

    document = {**DOC, 'cvm': 14664}
    receipt = {'id': 'synthetic:CVM:SHUL4', 'issuer': 'cvm', 'url': document['url'], 'document': document}
    write(tmp_path, cash_calendar_research.CACHE + '/cvm-schulz-source.json', receipt)
    item = {'cvm': 14664, 'isin': IDENTITY['isin'], 'record_date': '2026-09-22',
            'payment_date': '2026-09-28', 'approval_date': '2026-09-17',
            'gross_per_unit_decimal': '0.1234567890', 'status': 'past_scheduled',
            'source_table': 2, 'source_row': 1, 'issues': []}
    monkeypatch.setattr(cash_calendar_research, '_verified_delivery',
                        lambda *args: {'delivered_at': document['published_at']})
    monkeypatch.setattr(cash_calendar_research, 'read_receipt', lambda *args: b'synthetic native form')
    monkeypatch.setattr(cvm_proventos_documents, 'parse_pdf',
                        lambda *args: {'events': [deepcopy(item)], 'issues': []})
    proof = schulz.class_binding(tmp_path, STOCK, CUTOFF)
    native = [{'typeStock': 'PN', 'lastDatePriorEx': '22/09/2026', 'valueCash': '0,1234567890',
               'quotedPerShares': '1', 'corporateAction': 'DIVIDENDO'}]
    original = deepcopy(native)
    calendar = cash_calendar_research.load_cvm_calendar(tmp_path, STOCK, CUTOFF, schulz.cash_rows(native, proof))
    event, = calendar['events']
    assert event['share_class'] == 'PNU' and event['isin'] == IDENTITY['isin']
    assert event['reported_per_share_decimal'] == event['gross_per_share_decimal'] == item['gross_per_unit_decimal']
    assert event['payment_date'] == item['payment_date'] and event['record_date'] == item['record_date']
    assert event['label'] == 'DIVIDENDO' and event['label_basis'] == 'exact_independent_class_record_and_gross_amount_match'
    assert not event['is_settlement_confirmation'] and not event['point_in_time_archive_verified']
    assert not calendar['settlement_verified'] and native == original
    assert not cash_calendar_research.load_cvm_calendar(tmp_path, STOCK, CUTOFF, native)['events']


@pytest.mark.parametrize('share', ['ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'])
def test_shared_identity_and_calendar_paths_do_not_require_Schulz_proof(tmp_path, monkeypatch, share):
    def forbidden(*args, **kwargs):
        pytest.fail('Shared classes must not use the SHUL4 binding')

    monkeypatch.setattr(schulz, 'class_binding', forbidden)
    stock = {'ticker': 'TEST4', 'cvm': 123, 'share_class': share, 'isin': 'BRTESTACNPR8',
             'sector_code': 1, 'identity_status': 'official_cvm_fca'}
    write(tmp_path, 'raw/expanded/b3-detail-123.json', {'codeCVM': '123', 'otherCodes': [
        {'code': 'TEST4', 'isin': stock['isin']}]})
    source_cache = _Cache(tmp_path, {'stocks': [stock]})
    _, codes = _identity(stock, source_cache, CUTOFF)
    assert codes == {'TEST4': stock['isin']} and not source_cache.exact_share_class_bindings
    calendar = cash_calendar_research.load_cvm_calendar(tmp_path, stock, CUTOFF, [])
    assert calendar['share_class'] == share and calendar['events'] == []


@pytest.fixture
def cached_market(monkeypatch):
    if not (MARKET / schulz.CACHE / 'response.json').is_file():
        pytest.skip('Optional immutable Schulz statute cache is absent')
    from .market_inventory import load_inventory

    inventory = load_inventory(MARKET)
    stock, = [row for row in inventory['stocks'] if row['ticker'] == 'SHUL4']
    # Run the full historical audit once per replay; subsequent callers still
    # verify the statute and every FCA archive against those real receipts.
    report = selected_identity_research.research(MARKET, as_of=CUTOFF.isoformat(), tickers=('SHUL4',))

    def replay(root, *, as_of, tickers):
        assert Path(root).resolve() == MARKET.resolve() and as_of == CUTOFF.isoformat()
        assert tickers == ('SHUL4',)
        return deepcopy(report)

    monkeypatch.setattr(selected_identity_research, 'research', replay)
    return inventory, stock, report


def test_actual_cached_registration_candidate_payments_and_native_EPS(cached_market):
    inventory, stock, report = cached_market
    original = deepcopy(stock)
    pointers = {name: (MARKET / name).read_bytes() for name in (
        'current.json', 'inventory/current.json', 'stock-selection/current.json')}
    proof = schulz.class_binding(MARKET, stock, CUTOFF)
    before = {source['file']: hashlib.sha256((MARKET / source['file']).read_bytes()).hexdigest()
              for source in proof['sources']}
    prepared, excluded, receipts = build_candidates(MARKET, {**inventory, 'stocks': [stock]}, CUTOFF)
    assert not excluded, excluded
    candidate, = prepared
    assert candidate['statement_spec'] == (14664, 'PNU', 5)
    assert candidate['share_class'] == 'PNU' and stock['share_class'] == 'PNU'
    assert candidate['checks']['security_registration']['share_class'] == 'PNU'
    assert candidate['checks']['exact_share_class_binding'] == proof
    assert candidate['ledger'] and candidate['independent_cash_matches'] and candidate['independent_price_matches']
    assert not candidate['financially_validated']
    assert candidate['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert not candidate['checks']['point_in_time_archive_verified']
    source_cache = _Cache(MARKET, inventory)
    _, codes = _identity(stock, source_cache, CUTOFF)
    native_rows = _cash_pages('SHUL4', codes, 14664, source_cache)
    native_copy = deepcopy(native_rows)
    view = schulz.cash_rows(native_rows, proof)
    assert any(row.get('typeStock') == 'PN' for row in native_rows)
    assert b3_evidence(native_rows, 'PN') == b3_evidence(view, 'PNU')
    calendar = cash_calendar_research.load_cvm_calendar(MARKET, stock, CUTOFF, view)
    assert calendar['share_class'] == 'PNU' and calendar['cvm'] == 14664
    assert not calendar['settlement_verified'] and not calendar['point_in_time_archive_verified']
    statements, _, sources = prepare_financials(MARKET, [stock], CUTOFF)
    native, _, _ = prepare_financials(MARKET, {'SHUL4': (14664, 'PN', 5)}, CUTOFF)
    features, latest = financial_features(statements, candidate['payments'], CUTOFF, candidate['statement_spec'])
    _, native_latest = financial_features(native, candidate['payments'], CUTOFF, (14664, 'PN', 5))
    assert latest['fiscal_end'] == native_latest['fiscal_end'] == '2025-12-31'
    assert latest['profit_per_share'] == native_latest['profit_per_share'] == features[2] == .84146
    assert latest['financial_method']['source_share_class'] == 'PN'
    assert latest['financial_method']['registered_share_class'] == 'PNU'
    assert all(record['fiscal_end'] >= '2021-01-01' or record['status'] == 'excluded' for record in statements)
    for source in proof['sources']:
        assert source['sha256'] == before[source['file']]
        assert any(all(receipt.get(key) == source.get(key) for key in ('file', 'sha256', 'url', 'retrieved_at'))
                   for receipt in receipts)
        assert source in sources
    assert all(row['security']['Sigla_Classe_Acao_Preferencial'] == 'PNU'
               for row in report['records'][0]['FCA_exact_code_records'])
    assert before == {name: hashlib.sha256((MARKET / name).read_bytes()).hexdigest() for name in before}
    assert pointers == {name: (MARKET / name).read_bytes() for name in pointers}
    assert stock == original and native_rows == native_copy
