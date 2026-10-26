"""Offline contracts for the exact-current round-two financial evidence."""

import builtins
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest
from pypdf import PdfReader

from . import inventory_financials as native
from . import round2_financial_readings as round2
from .collect_market_data import financial_features


@pytest.fixture(scope='module', autouse=True)
def offline():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('round2 verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
                            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(round2.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def annual():
    return round2.current_tables()


@pytest.fixture(scope='module')
def verified():
    return round2.get_round2_readings()


@pytest.fixture(scope='module')
def current_parser():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(native, '_caches', lambda folder, cutoff: [
            (('dfp', 2025), round2.ROOT / round2.ANNUAL)])
        return native.prepare_financials(round2.ROOT, round2.SPECS, round2.CUTOFF)


def _page(ticker, number):
    name = 'WLMM3' if ticker == 'WLMM4' else ticker
    proof = json.loads((round2.ROOT / round2.BASE / f'pages-{name}.json').read_bytes())
    pdf = round2.ROOT / proof['source']['file']
    assert round2._sha(pdf.read_bytes()) == proof['source']['sha256']
    return ' '.join(PdfReader(pdf).pages[number - 1].extract_text().split())


def _mutate_read(monkeypatch, relative, transform):
    target = round2.ROOT / relative
    original = Path.read_bytes

    def changed(path):
        body = original(path)
        return transform(body) if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)


def test_exact_current_masters_and_all_annual_flow_periods(annual):
    latest, tables = annual
    assert set(latest) == set(round2.SPECS)
    for ticker, master in latest.items():
        assert master['DT_REFER'] == '2025-12-31'
        assert tuple(master[k] for k in ('VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA')) == round2.EXPECTED[ticker]
    for suffix, cells in tables.items():
        if suffix.startswith(('_DRE_', '_DFC_')):
            assert all((r['DT_INI_EXERC'], r['DT_FIM_EXERC']) == (
                '2025-01-01', '2025-12-31') for r in cells)


def test_no_readings_authorized_no_WIZ_scope_switch_and_no_WLMM_clearance(verified):
    readings, findings, sources = verified
    assert readings == {}
    assert {row['ticker'] for row in findings} == set(round2.SPECS)
    assert all(row['status'] == 'pending_unchanged' and row['accounts_scope'] == 'consolidated'
               and row['applied_reading'] is None and not row['model_admission']
               and not row['EPS_computed'] and not row['numeric_values_changed'] for row in findings)
    assert all(row['missing_source_field'] for row in findings)
    assert len(sources) == 7
    assert all(row['point_in_time_archive_verified'] is False for row in sources)


@pytest.mark.parametrize('ticker', tuple(round2.PACKAGE_PINS))
def test_exact_original_ZIP_XML_PDF_and_transport_identity(ticker, annual):
    source = round2.verify_package(round2.ROOT, ticker, annual[0][ticker])
    assert source['exact_filing_verified'] is True
    assert source['retrieved_at'] and source['published_at'] == round2.EXPECTED[ticker][2]
    assert source['sha256'] == round2.PACKAGE_PINS[ticker][1]
    assert source['use'] == 'negative_evidence_only_no_account_override'


@pytest.mark.parametrize('ticker', tuple(round2.SPECS))
def test_normal_prepare_and_financial_features_do_not_create_recoveries(ticker, current_parser):
    records, diagnostics, _ = current_parser
    selected = financial_features(records, [], round2.CUTOFF, round2.SPECS[ticker])
    if ticker == 'WLMM3':
        assert selected is not None
        assert selected[1]['profit_per_share'] == 2.25219
        assert selected[1]['accounts_scope'] == 'consolidated'
        assert round2.ASSESSMENTS[ticker]['status'] == 'unresolved_exact_native_EPS_source_conflict'
    else:
        assert selected is None
    current = [r for r in records if (r['cvm'], r['share_class']) == round2.SPECS[ticker][:2]
               and r['fiscal_end'] == '2025-12-31'
               and r['version'] == round2.EXPECTED[ticker][0]]
    assert current and all(r['filing_id'] == round2.EXPECTED[ticker][1]
                           and r['version'] == round2.EXPECTED[ticker][0]
                           and r['accounts_scope'] == 'consolidated' for r in current)


def test_PINE_exact_v3_still_allocates_NCI_to_class_numerators():
    text = _page('PINE4', 106)
    assert '430.515' in text and '432.878' in text and '2.363' in text
    assert '221.909' in text and '210.969' in text
    assert '119.162.783' in text and '113.288.564' in text
    assert 'Ações preferenciais 1,86' in text
    assert Decimal('221909000') + Decimal('210969000') != Decimal('430515000')


def test_SIMH_placeholder_absence_does_not_resolve_the_native_profit_conflict():
    form, note = _page('SIMH3', 24), _page('SIMH3', 140)
    assert '39.471' in form and '215.250' in form and '173.170' in form
    assert '0,0931' in form and '3.99.01.02' not in form
    assert 'Controladora' in note and '39.741' in note and '426.797.758' in note
    assert Decimal('39471000') + Decimal('173170000') != Decimal('215250000')


def test_WLM_printed_class_EPS_conflict_without_computing_replacement_EPS():
    text = _page('WLMM3', 94)
    assert '16.571.220' in text and '19.843.450' in text
    assert '40.559.687,37' in text and '48.568.791,45' in text
    assert '1.204.364,13' in text and '1.442.183,47' in text
    on_net = Decimal('40559687.37') - Decimal('1204364.13')
    pn_net = Decimal('48568791.45') - Decimal('1442183.47')
    assert abs(Decimal('2.25219') * Decimal('16571220') - on_net) > Decimal('1000000')
    assert abs(Decimal('2.47741') * Decimal('19843450') - pn_net) > Decimal('1000000')


def test_WIZ_consolidated_na_is_not_an_empty_template_or_individual_permission():
    dre, note = _page('WIZC3', 56), _page('WIZC3', 102)
    assert 'Consolidado' in dre and '367.710' in dre and '201.076' in dre
    assert dre.count('n/a') >= 8
    assert 'Controladora' in note and '1,25745' in note and '159.907.282' in note


def test_EVEN_positive_net_capex_is_retained(annual):
    row, = [r for r in annual[1]['_DFC_MI_con']
             if int(r['CD_CVM']) == 20524 and r['CD_CONTA'] == '6.02.01']
    assert Decimal(row['VL_CONTA']) == Decimal('12618')
    assert round2.ASSESSMENTS['EVEN3']['status'] == 'missing_gross_cash_capex'


def test_WHRL_PN_is_not_invented_from_the_ON_leaf(annual):
    leaves = [r for r in annual[1]['_DRE_con']
              if int(r['CD_CVM']) == 14346 and r['CD_CONTA'].startswith('3.99.01.')]
    assert [(r['DS_CONTA'], Decimal(r['VL_CONTA'])) for r in leaves] == [
        ('ON', Decimal('0.2984629881'))]


@pytest.mark.parametrize('ticker', tuple(round2.PACKAGE_PINS))
def test_tampered_native_PDF_is_rejected(monkeypatch, annual, ticker):
    proof = json.loads((round2.ROOT / round2.BASE / f'package-{ticker}.json').read_bytes())
    pdf = next(m['file'] for m in proof['members'] if m['file'].endswith('.pdf'))
    _mutate_read(monkeypatch, pdf, lambda body: body + b'changed')
    with pytest.raises(ValueError, match='^round2_evidence_changed$'):
        round2.verify_package(round2.ROOT, ticker, annual[0][ticker])


@pytest.mark.parametrize(('key', 'value'), [
    ('filing_id', '154785'), ('version', '2'), ('fiscal_end', '2024-12-31'),
    ('published_at', '2026-04-01'), ('cnpj', '00.000.000/0001-00')])
def test_older_or_other_filing_identity_rejected(monkeypatch, annual, key, value):
    def changed(body):
        proof = json.loads(body)
        proof[key] = value
        return json.dumps(proof).encode()

    _mutate_read(monkeypatch, f'{round2.BASE}/package-PINE4.json', changed)
    with pytest.raises(ValueError, match='^round2_package_master_mismatch$'):
        round2.verify_package(round2.ROOT, 'PINE4', annual[0]['PINE4'])


def test_denied_or_partial_response_cannot_be_native_evidence(monkeypatch, annual):
    url = annual[0]['PINE4']['LINK_DOC'].replace('http://', 'https://', 1)

    def changed(body):
        proof = json.loads(body)
        proof.update(status=403, error='access_stop_no_retry')
        return json.dumps(proof).encode()

    _mutate_read(monkeypatch, f'{round2.BASE}/response-{round2._sha(url.encode())}.json', changed)
    with pytest.raises(ValueError, match='^round2_original_transport_changed$'):
        round2.verify_package(round2.ROOT, 'PINE4', annual[0]['PINE4'])


def test_annual_changed_bytes_and_unreviewed_cutoff_rejected(monkeypatch):
    _mutate_read(monkeypatch, round2.ANNUAL, lambda body: body + b'changed')
    with pytest.raises(ValueError, match='^round2_annual_archive_changed$'):
        round2.current_tables()
    with pytest.raises(ValueError, match='^round2_unreviewed_cutoff$'):
        round2.current_tables(cutoff=date(2026, 10, 25))


def test_WIZ_oversize_original_is_not_an_extracted_package():
    url = round2.PDF_URLS['WIZC3']
    response = json.loads((round2.ROOT / round2.BASE /
                          f'response-{round2._sha(url.encode())}.json').read_bytes())
    assert response['error'] == 'response_size_limit' and response['bytes'] == 16000001
    assert not (round2.ROOT / round2.BASE / 'package-WIZC3.json').exists()


def test_full_unmodified_offline_before_after_report():
    before = json.loads((round2.REPORTS / 'financial-before.json').read_bytes())
    after = json.loads((round2.REPORTS / 'financial-after.json').read_bytes())
    report = json.loads((round2.REPORTS / 'financial-handoff.json').read_bytes())
    assert before == after
    assert report['approved_readings'] == {} and report['new_admissions'] == []
    assert report['changed_financial_fields'] == []
    assert report['offline_verification']['normal_prepare_financials_and_financial_features'] is True
    assert all(row['parser_before_after']['unchanged'] for row in report['findings'])
