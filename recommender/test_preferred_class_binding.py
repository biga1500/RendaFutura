"""Independent PNA binding contracts; real evidence is strictly read-only.

Synthetic archives below live only in pytest's temporary directories. Real
positives replay all six annual FCA archives and compare actual reported EPS.
"""

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

from . import preferred_class_binding as binding
from . import selected_identity_research
from . import inventory_financials
from .collect_market_data import financial_features
from .inventory_candidates import _Cache, _cash_pages, _identity, _one, build_candidates
from .inventory_financials import prepare_financials
from .market_inventory import load_inventory
from .security_registration import FCA_URL
from .selected_identity_research import scan_fca
from .share_basis import b3_evidence
from .test_inventory_candidates import fixture as candidate_fixture, hashes, write
from .test_inventory_financials import annual, cache as financial_cache
from .test_security_registration import archive, filing, security


MARKET = Path(__file__).resolve().parent / 'data/market'
CUTOFF = date(2026, 9, 30)
TICKERS = ('ABCB4', 'CMIG4', 'DEXP4', 'RSUL4', 'TASA4')


@pytest.fixture(scope='module', autouse=True)
def offline_readonly():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('preferred binding tests cannot access the network')

        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guard(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(MARKET.resolve()):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache must remain read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guard(builtins.open))
        patch.setattr(io, 'open', guard(io.open))
        yield


def digest(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def binding_inputs(request, tmp_path, monkeypatch):
    ticker = getattr(request, 'param', 'TASA4')
    identity = {'ticker': ticker, **binding.IDENTITIES[ticker]}
    stock = {**identity, 'sector_code': 2 if ticker == 'ABCB4' else 5,
             'identity_status': 'official_cvm_fca'}
    entries, originals = [], {}
    for year in range(2021, 2027):
        master = filing(CD_CVM=str(identity['cvm']), ID_DOC=str(year),
            DT_REFER=f'{year}-01-01', DT_RECEB=f'{year}-04-01')
        preferred = security(Codigo_Negociacao=ticker, Valor_Mobiliario='Acoes Preferenciais',
            Sigla_Classe_Acao_Preferencial='A', Classe_Acao_Preferencial='Classe A',
            ID_Documento=master['ID_DOC'], Data_Referencia=master['DT_REFER'])
        body = archive([master], [preferred], year=year)
        path = write(tmp_path, f'raw/inventory/fca_cia_aberta_{year}.zip', body)
        source = {'file': path.relative_to(tmp_path).as_posix(), 'sha256': digest(body),
            'bytes': len(body), 'url': FCA_URL + f'fca_cia_aberta_{year}.zip',
            'retrieved_at': '2026-10-25T00:00:00+00:00'}
        entry, = scan_fca(body, year, (ticker,), CUTOFF.isoformat())['exact_security_records']
        entry['source'] = {**source, 'existing_receipts': [deepcopy(source)]}
        entries.append(entry)
        originals[year] = (master, preferred)
    detail = {'codeCVM': str(identity['cvm']), 'otherCodes': [{'code': ticker, 'isin': identity['isin']}]}
    detail_path = write(tmp_path, f'raw/expanded/b3-detail-{identity["cvm"]}.json', detail)
    mapping = {**identity, 'source': {'file': detail_path.relative_to(tmp_path).as_posix(),
        'sha256': digest(detail_path.read_bytes()), 'bytes': detail_path.stat().st_size}}
    record = {**identity, 'status': 'primary_exact_identity_evidence',
        'FCA_search_complete_for_2021_2026': True, 'FCA_exact_code_records': entries,
        'cached_exact_code_mappings': [mapping]}
    report = {'records': [record]}

    def research(root, *, as_of, tickers):
        assert Path(root).resolve() == tmp_path.resolve()
        assert as_of == CUTOFF.isoformat() and tickers == (ticker,)
        return deepcopy(report)

    monkeypatch.setattr(selected_identity_research, 'research', research)
    return {'stock': stock, 'record': record, 'report': report, 'originals': originals}


def replace_fca(tmp_path, inputs, *, master_change=None, child_change=None, extra=None,
                derive_scan=False):
    entry = inputs['record']['FCA_exact_code_records'][0]
    year = entry['year']
    master, child = deepcopy(inputs['originals'][year])
    master.update(master_change or {})
    child.update(child_change or {})
    body = archive([master], [child, *(extra or [])], year=year)
    source = entry['source']
    (tmp_path / source['file']).write_bytes(body)
    source.update(sha256=digest(body), bytes=len(body))
    for receipt in source['existing_receipts']:
        receipt.update(sha256=digest(body), bytes=len(body))
    if derive_scan:
        derived, = scan_fca(body, year, (inputs['stock']['ticker'],), CUTOFF.isoformat())['exact_security_records']
        entry.update(derived)


@pytest.mark.parametrize(('field', 'value'), [
    ('cvm', 123), ('cvm', '6173'), ('cvm', True), ('isin', 'BRTASAACNOR3'),
    ('isin', None), ('share_class', 'PN'), ('share_class', 'ON'), ('ticker', 'TASA3')])
def test_wrong_exact_identity_is_rejected_before_source_read(tmp_path, field, value):
    identity = {'ticker': 'TASA4', **binding.IDENTITIES['TASA4'], field: value}
    with pytest.raises(ValueError, match='preferred_class_exact_identity_required|unreviewed_preferred_class_binding'):
        binding.class_binding(tmp_path, identity, CUTOFF)


@pytest.mark.parametrize('when', ['2026-08-31', '2026-10-31'])
def test_unreviewed_cutoff_is_rejected(tmp_path, when):
    with pytest.raises(ValueError, match='unreviewed_preferred_class_binding'):
        binding.class_binding(tmp_path, {'ticker': 'TASA4', **binding.IDENTITIES['TASA4']}, when)


@pytest.mark.parametrize(('fault', 'reason'), [
    ('missing_year', 'preferred_class_history_incomplete'),
    ('ambiguous_records', 'preferred_class_registry_ambiguous'),
    ('incomplete_search', 'preferred_class_registry_conflict'),
    ('registry_isin', 'preferred_class_registry_conflict'),
    ('invalid_join', 'preferred_class_historical_identity_changed'),
    ('future', 'preferred_class_historical_identity_changed'),
    ('historical_class', 'preferred_class_historical_identity_changed'),
    ('missing_receipt', 'preferred_class_official_receipt_required'),
    ('wrong_receipt_url', 'preferred_class_official_receipt_required'),
    ('wrong_receipt_hash', 'preferred_class_official_receipt_required'),
    ('changed_hash', 'preferred_class_evidence_changed'),
    ('mapping_isin', 'preferred_class_exact_ISIN_mapping_required'),
    ('mapping_cvm', 'preferred_class_exact_ISIN_mapping_required'),
    ('missing_mapping', 'preferred_class_exact_ISIN_mapping_required')])
def test_incomplete_future_or_conflicting_registry_is_rejected(tmp_path, binding_inputs, fault, reason):
    record = binding_inputs['record']
    first = record['FCA_exact_code_records'][0]
    if fault == 'missing_year':
        record['FCA_exact_code_records'].pop()
    elif fault == 'ambiguous_records':
        binding_inputs['report']['records'].append(deepcopy(record))
    elif fault == 'incomplete_search':
        record['FCA_search_complete_for_2021_2026'] = False
    elif fault == 'registry_isin':
        record['isin'] = 'BRTASAACNOR3'
    elif fault == 'invalid_join':
        first['source_binding_valid'] = False
    elif fault == 'future':
        replace_fca(tmp_path, binding_inputs, master_change={'DT_RECEB': '2026-10-01'}, derive_scan=True)
    elif fault == 'historical_class':
        first['security']['Sigla_Classe_Acao_Preferencial'] = 'B'
    elif fault == 'missing_receipt':
        first['source']['existing_receipts'] = []
    elif fault == 'wrong_receipt_url':
        first['source']['existing_receipts'][0]['url'] = FCA_URL + 'fca_cia_aberta_2020.zip'
    elif fault == 'wrong_receipt_hash':
        first['source']['existing_receipts'][0]['sha256'] = '0' * 64
    elif fault == 'changed_hash':
        (tmp_path / first['source']['file']).write_bytes(b'changed synthetic archive')
    elif fault == 'missing_mapping':
        record['cached_exact_code_mappings'] = []
    else:
        record['cached_exact_code_mappings'][0]['isin' if fault == 'mapping_isin' else 'cvm'] = 'wrong'
    with pytest.raises(ValueError, match='^' + reason + '$'):
        binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)


@pytest.mark.parametrize('field,value', [('CNPJ_Companhia', '99.999.999/0001-99'),
    ('Data_Referencia', '2021-02-01'), ('Versao', '9')])
def test_actual_FCA_child_mismatch_cannot_be_hidden_by_report_flags(tmp_path, binding_inputs, field, value):
    replace_fca(tmp_path, binding_inputs, child_change={field: value})
    with pytest.raises(ValueError, match='preferred_class_child_identity_mismatch'):
        binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)


@pytest.mark.parametrize('extra_class,extra_code', [('B', 'TASA6'), ('A', 'OTHER4')])
def test_multiple_preferred_classes_or_codes_in_actual_FCA_block_binding(
        tmp_path, binding_inputs, extra_class, extra_code):
    first = binding_inputs['record']['FCA_exact_code_records'][0]
    _, child = binding_inputs['originals'][first['year']]
    extra = {**child, 'Codigo_Negociacao': extra_code,
             'Sigla_Classe_Acao_Preferencial': extra_class}
    replace_fca(tmp_path, binding_inputs, extra=[extra])
    with pytest.raises(ValueError, match='multiple_preferred_classes_or_codes_not_bound'):
        binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)


def test_cash_labels_change_without_altering_ON_dates_values_or_nested_data(tmp_path, binding_inputs):
    proof = binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)
    rows = [{'typeStock': share, 'valueCash': '0,123456789012', 'quotedPerShares': '1000',
        'corporateAction': 'DIVIDENDO', 'lastDatePriorEx': '20/09/2026',
        'dateClosingPricePriorExDate': '18/09/2026', 'closingPricePriorExDate': '12,34',
        'metadata': {'unchanged': [1, 2]}} for share in ('ON', 'PN')]
    original = deepcopy(rows)
    converted = binding.cash_rows(rows, proof)
    assert rows == original and converted[0] == original[0]
    assert {k: v for k, v in converted[1].items() if k not in
            ('typeStock', 'reported_typeStock', 'source_class_binding')} == {
        k: v for k, v in original[1].items() if k != 'typeStock'}
    assert converted[1]['typeStock'] == 'PNA' and converted[1]['reported_typeStock'] == 'PN'
    assert b3_evidence(converted, 'PNA') == b3_evidence(original, 'PN')
    assert b3_evidence(converted, 'ON') == b3_evidence(original, 'ON')
    converted[0]['metadata']['unchanged'].append(3)
    assert rows == original


@pytest.mark.parametrize('field', ['isin', 'isinCode'])
@pytest.mark.parametrize('share', ['PN', 'PNA'])
def test_conflicting_preferred_cash_ISIN_is_rejected(tmp_path, binding_inputs, field, share):
    proof = binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)
    with pytest.raises(ValueError, match='preferred_cash_ISIN_conflict'):
        binding.cash_rows([{'typeStock': share, field: 'BRTASAACNOR3'}], proof)


def test_two_cash_ISIN_fields_cannot_hide_a_conflict(tmp_path, binding_inputs):
    proof = binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)
    with pytest.raises(ValueError, match='preferred_cash_ISIN_conflict'):
        binding.cash_rows([{'typeStock': 'PN', 'isinCode': proof['identity']['isin'],
                            'isin': 'BRTASAACNOR3'}], proof)


@pytest.mark.parametrize('classes,reason', [(['PN', 'PNA'], 'ambiguous_double_preferred_cash_class'),
    (['ON', 'PNB'], 'unexpected_preferred_cash_class')])
def test_ambiguous_or_other_cash_classes_are_not_merged(tmp_path, binding_inputs, classes, reason):
    proof = binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)
    with pytest.raises(ValueError, match=reason):
        binding.cash_rows([{'typeStock': share} for share in classes], proof)


@pytest.mark.parametrize('key,value', [('id', 'wrong'), ('identity', {'ticker': 'CMIG4'})])
def test_cash_proof_for_other_identity_is_rejected(tmp_path, binding_inputs, key, value):
    proof = binding.class_binding(tmp_path, binding_inputs['stock'], CUTOFF)
    with pytest.raises(ValueError, match='preferred_cash_binding_required'):
        binding.cash_rows([{'typeStock': 'PN'}], {**proof, key: value})


@pytest.mark.parametrize('composite_key', [False, True])
def test_candidate_integration_preserves_native_cash_and_PNA_statement_spec(tmp_path, binding_inputs, monkeypatch, composite_key):
    stock = binding_inputs['stock']
    inventory = candidate_fixture(tmp_path, ticker=stock['ticker'])
    inventory['stocks'][0].update(stock)
    name = f'raw/expanded/{stock["ticker"]}-b3-cash-1.json'
    payload = json.loads((tmp_path / name).read_bytes())
    for row in payload['results']:
        row['typeStock'] = 'PN'
    write(tmp_path, name, payload)
    if composite_key:
        from . import inventory_candidates
        original_init = inventory_candidates._Cache.__init__

        def with_other_reader_cache(cache, *args, **kwargs):
            original_init(cache, *args, **kwargs)
            cache.parsed[('another_reader', 'issuer')] = {'results': [{'unrelated': True}]}

        monkeypatch.setattr(inventory_candidates._Cache, '__init__', with_other_reader_cache)
    before, original = hashes(tmp_path), deepcopy(inventory)
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded, excluded
    candidate, = candidates
    assert candidate['statement_spec'] == (stock['cvm'], 'PNA', stock['sector_code'])
    assert candidate['checks']['exact_share_class_binding']['id'] == binding.BINDING_ID
    assert candidate['ledger'] and all(r['gross_per_unit'] == 1 for r in candidate['ledger'])
    assert not candidate['financially_validated']
    assert inventory == original and hashes(tmp_path) == before
    assert all(s['sha256'] == before[s['file']] for s in sources)


def test_financial_label_binding_preserves_reported_EPS_and_entire_method(tmp_path, binding_inputs):
    stock = binding_inputs['stock']
    record = annual(cvm=stock['cvm'])
    next(r for r in record['tables']['DRE_con'] if r['DS_CONTA'] == 'PN')['VL_CONTA'] = '0.123456789'
    financial_cache(tmp_path, record)
    native, _, _ = prepare_financials(tmp_path, {'NATIVE4': (stock['cvm'], 'PN', stock['sector_code'])}, CUTOFF)
    before, original = hashes(tmp_path), deepcopy(stock)
    converted, diagnostics, _ = prepare_financials(tmp_path, [stock], CUTOFF)
    pn, = native
    pna, = converted
    assert pna['profit_per_share'] == pn['profit_per_share'] == .123456789
    assert {k: v for k, v in pna.items() if k not in ('share_class', 'financial_method')} == {
        k: v for k, v in pn.items() if k not in ('share_class', 'financial_method')}
    method = pna['financial_method']
    assert method['source_share_class'] == 'PN' and method['registered_share_class'] == 'PNA'
    assert method['share_class_binding']['id'] == binding.BINDING_ID
    assert {k: v for k, v in method.items() if k not in (
        'source_share_class', 'registered_share_class', 'share_class_binding')} == pn['financial_method']
    assert all(r['status'] == 'included' for r in diagnostics if r['kind'] == 'candidate')
    assert hashes(tmp_path) == before and stock == original


@pytest.mark.parametrize('fault', ['absent', 'zero', 'nonfinite', 'ambiguous'])
def test_binding_never_fabricates_EPS_from_ordinary_shares_or_profit(tmp_path, binding_inputs, fault):
    stock = binding_inputs['stock']
    record = annual(cvm=stock['cvm'])
    rows = record['tables']['DRE_con']
    preferred = next(r for r in rows if r['DS_CONTA'] == 'PN')
    if fault == 'absent':
        rows.remove(preferred)
    elif fault == 'ambiguous':
        rows.append({**preferred, 'CD_CONTA': '3.99.01.03', 'VL_CONTA': '9'})
    else:
        preferred['VL_CONTA'] = '0' if fault == 'zero' else 'NaN'
    financial_cache(tmp_path, record)
    statements, _, _ = prepare_financials(tmp_path, [stock], CUTOFF)
    assert financial_features(statements, [], CUTOFF, (stock['cvm'], 'PNA', stock['sector_code'])) is None
    assert all(r['profit_per_share'] is None for r in statements)


def test_conflicting_financial_PN_and_PNA_EPS_cannot_choose_one_label(tmp_path, binding_inputs):
    stock = binding_inputs['stock']
    record = annual(cvm=stock['cvm'])
    pn = next(r for r in record['tables']['DRE_con'] if r['DS_CONTA'] == 'PN')
    record['tables']['DRE_con'].append({**pn, 'CD_CONTA': '3.99.01.03', 'DS_CONTA': 'PNA', 'VL_CONTA': '99'})
    financial_cache(tmp_path, record)
    statements, _, _ = prepare_financials(tmp_path, [stock], CUTOFF)
    assert financial_features(statements, [], CUTOFF, (stock['cvm'], 'PNA', stock['sector_code'])) is None


@pytest.mark.parametrize('binding_inputs', ['ABCB4'], indirect=True)
def test_financial_sector_method_is_preserved_and_FCF_is_not_invented(tmp_path, binding_inputs):
    stock = binding_inputs['stock']
    financial_cache(tmp_path, annual(cvm=stock['cvm']))
    statements, _, _ = prepare_financials(tmp_path, [stock], CUTOFF)
    pna, = statements
    assert pna['status'] == 'included', pna
    assert pna['financial_method']['id'] == 'financial_no_industrial_fcf_v1'
    assert pna['free_cash_per_share'] is None and pna['profit_per_share'] == 4


def test_old_ON_financial_history_is_unchanged_when_preferred_binding_is_added(tmp_path, binding_inputs):
    stock = binding_inputs['stock']
    for year in (2020, 2021):
        financial_cache(tmp_path, annual(cvm=stock['cvm'], year=year,
            published=f'{year + 1}-03-01'), year=year)
    ordinary = {'ticker': 'TASA3', 'cvm': stock['cvm'], 'share_class': 'ON', 'sector_code': stock['sector_code']}
    native, _, _ = prepare_financials(tmp_path, [ordinary], CUTOFF)
    combined, diagnostics, _ = prepare_financials(tmp_path, [ordinary, stock], CUTOFF)
    assert [r for r in combined if r['share_class'] == 'ON'] == native
    as_of = date(2021, 9, 30)
    spec = (stock['cvm'], 'ON', stock['sector_code'])
    expected = financial_features(native, [], as_of, spec)
    assert expected is not None and financial_features(combined, [], as_of, spec) == expected
    assert not any(r['status'] == 'included' and r['share_class'] == 'PNA'
                   and r['fiscal_end'] < '2021-01-01' for r in combined)
    assert any('exact_class_binding_does_not_cover_financial_period' in r.get('reasons', []) for r in diagnostics)


@pytest.fixture(scope='module')
def real_market(offline_readonly):
    inventory = load_inventory(MARKET)
    stocks = {r['ticker']: r for r in inventory['stocks'] if r['ticker'] in TICKERS}
    assert set(stocks) == set(TICKERS)
    original_research = selected_identity_research.research
    reports = {}

    def replay(root, *, as_of, tickers):
        key = (Path(root).resolve(), as_of, tickers)
        if key not in reports:
            reports[key] = original_research(root, as_of=as_of, tickers=tickers)
        return deepcopy(reports[key])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(selected_identity_research, 'research', replay)
        proofs = {ticker: binding.class_binding(MARKET, stocks[ticker], CUTOFF) for ticker in TICKERS}
        before = {s['file']: digest((MARKET / s['file']).read_bytes())
                  for proof in proofs.values() for s in proof['sources']}
        yield inventory, stocks, proofs
        assert all(digest((MARKET / name).read_bytes()) == sha for name, sha in before.items())


@pytest.mark.parametrize('ticker', TICKERS)
def test_real_five_exact_PNA_bindings_cover_all_six_FCA_and_preserve_provenance(real_market, ticker):
    _, stock_by_ticker, proofs = real_market
    stock, proof = stock_by_ticker[ticker], proofs[ticker]
    assert proof['identity'] == {'ticker': ticker, **binding.IDENTITIES[ticker]}
    assert proof['registered_class'] == stock['share_class'] == 'PNA'
    assert {r['year'] for r in proof['historical_registry']} == set(range(2021, 2027))
    assert proof['only_preferred_class_in_each_received_FCA']
    for row in proof['historical_registry']:
        assert row['filing']['DT_RECEB'] <= CUTOFF.isoformat()
        assert row['filing']['DT_REFER'] <= CUTOFF.isoformat()
        assert row['sha256'] == digest((MARKET / row['file']).read_bytes())
        source, = [s for s in proof['sources'] if s['file'] == row['file']]
        assert source['url'] == FCA_URL + f'fca_cia_aberta_{row["year"]}.zip'
    cached_mapping, = [s for s in proof['sources'] if s.get('authority') ==
                       'existing_exact_identity_cache_not_legal_class_authority']
    assert cached_mapping['url'] is None and cached_mapping['retrieved_at'] is None
    for flag in ('EPS_computed', 'numeric_values_changed', 'original_files_modified',
                 'publication_date_inferred', 'point_in_time_archive_verified', 'model_admitted'):
        assert proof[flag] is False


@pytest.mark.parametrize('ticker', TICKERS)
def test_real_candidate_integration_keeps_cash_and_existing_price_blockers(real_market, ticker):
    inventory, stocks, proofs = real_market
    stock = stocks[ticker]
    original = deepcopy(stock)
    cache = _Cache(MARKET, inventory)
    _, codes = _identity(stock, cache, CUTOFF)
    assert cache.exact_share_class_bindings[ticker] == proofs[ticker]
    rows = _cash_pages(ticker, codes, stock['cvm'], cache)
    native = deepcopy(rows)
    converted = binding.cash_rows(rows, proofs[ticker])
    assert any(r.get('typeStock') == 'PN' for r in rows)
    assert b3_evidence(converted, 'PNA') == b3_evidence(rows, 'PN')
    assert b3_evidence(converted, 'ON') == b3_evidence(rows, 'ON')
    checks = {}
    blockers = {'ABCB4': 'historical_quote_or_share_scale_disagrees_with_B3',
                'DEXP4': 'historical_quote_or_share_scale_disagrees_with_independent_source'}
    if ticker in blockers:
        with pytest.raises(ValueError, match='^' + blockers[ticker] + '$'):
            _one(stock, cache, CUTOFF, checks)
    else:
        candidate = _one(stock, cache, CUTOFF, checks)
        assert candidate['statement_spec'] == (stock['cvm'], 'PNA', stock['sector_code'])
        assert candidate['ledger'] and not candidate['financially_validated']
    assert checks['exact_share_class_binding']['id'] == binding.BINDING_ID
    assert stock == original and rows == native


@pytest.fixture(scope='module')
def real_financials(real_market):
    _, stocks, _ = real_market
    native_specs = {ticker: (row['cvm'], 'PN', row['sector_code']) for ticker, row in stocks.items()}
    # Focus the real numeric comparison on the latest complete annual archive.
    # Synthetic 2020/2021 fixtures above separately test preservation of ON history.
    selected = [(key, path) for key, path in inventory_financials._caches(MARKET, CUTOFF)
                if key == ('dfp', 2025)]
    assert len(selected) == 1, 'real 2025 annual archive must be available'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(inventory_financials, '_caches', lambda *args: selected)
        native, native_diagnostics, _ = prepare_financials(MARKET, native_specs, CUTOFF)
        converted, diagnostics, _ = prepare_financials(MARKET, list(stocks.values()), CUTOFF)
    return stocks, native, converted, native_diagnostics, diagnostics


@pytest.mark.parametrize('ticker', TICKERS)
def test_real_financial_integration_reuses_reported_native_EPS_without_changing_method(real_financials, ticker):
    stocks, native, converted, native_diagnostics, diagnostics = real_financials
    stock = stocks[ticker]
    native_features = financial_features(native, [], CUTOFF, (stock['cvm'], 'PN', stock['sector_code']))
    bound_features = financial_features(converted, [], CUTOFF, (stock['cvm'], 'PNA', stock['sector_code']))
    if ticker == 'DEXP4':
        # The source CSV has no usable EPS; production now reads the audited note.
        assert native_features is None and bound_features is not None
        _, documented = bound_features
        original = max((row for row in native if row['cvm'] == stock['cvm']),
                       key=lambda row: (row['fiscal_end'], int(row['version'])))
        assert documented['profit_per_share'] == 1.3299
        assert str(documented['filing_id']) == str(original['filing_id']) == '155652'
        assert documented['financial_method']['basic_eps_account'] == 'note:2.5_basic_PN'
        assert documented['financial_method']['source_share_class'] == 'PN'
        assert documented['financial_method']['registered_share_class'] == 'PNA'
        assert documented['free_cash_per_share'] == pytest.approx(1.5315147396852138)
        assert documented['debt_ratio'] == pytest.approx(0.37911677296060636)
        assert documented['financial_method']['id'] == original['financial_method']['id']
        for key in ('published_at', 'fiscal_end', 'version'):
            assert documented[key] == original[key]
        return
    assert (bound_features is None) == (native_features is None), ticker
    if native_features is not None:
        native_values, pn = native_features
        values, pna = bound_features
        assert values == native_values
    else:
        def latest(rows):
            scoped = [r for r in rows if r['cvm'] == stock['cvm']]
            assert scoped, ticker
            return max(scoped, key=lambda r: (r['fiscal_end'], int(r['version'])))
        pn, pna = latest(native), latest(converted)
        assert pna['status'] == pn['status'] == 'excluded'
        assert pna['unavailable_reasons'] == pn['unavailable_reasons']
    native_candidate, = [r for r in native_diagnostics if r.get('kind') == 'candidate' and r['ticker'] == ticker]
    bound_candidate, = [r for r in diagnostics if r.get('kind') == 'candidate' and r['ticker'] == ticker]
    assert bound_candidate['status'] == native_candidate['status']
    assert bound_candidate['reasons'] == native_candidate['reasons']
    assert pna['profit_per_share'] == pn['profit_per_share']
    assert pna['free_cash_per_share'] == pn['free_cash_per_share']
    assert pna['debt_ratio'] == pn['debt_ratio']
    assert pna['published_at'] == pn['published_at'] and pna['filing_id'] == pn['filing_id']
    method = pna['financial_method']
    assert method['source_share_class'] == 'PN' and method['registered_share_class'] == 'PNA'
    assert {k: v for k, v in method.items() if k not in (
        'source_share_class', 'registered_share_class', 'share_class_binding')} == pn['financial_method']
    if ticker in ('CMIG4', 'TASA4'):
        assert bound_features is not None
        assert pna['profit_per_share'] == {'CMIG4': 1.71, 'TASA4': .13124}[ticker]
        assert pna['filing_id'] == {'CMIG4': '155515', 'TASA4': '155647'}[ticker]
    if ticker == 'RSUL4':
        assert bound_features is not None and pna['filing_id'] == '155874'
        assert pna['profit_per_share'] == 11.67921
        assert pna['financial_method']['profit_account'] == '3.11'
        assert pna['financial_method']['primary_evidence'][0]['pdf_page'] == 59
