"""Offline bank EPS evidence, existing-validator and ordinary-parser regressions."""

import builtins
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
import urllib.request
from urllib.parse import urlsplit

import pytest
from pypdf import PdfReader

from . import inventory_financials as native
from . import round2_bank_eps as banks


@pytest.fixture(scope='module', autouse=True)
def offline():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('bank EPS verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(banks.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'original cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def annual():
    return banks.current_tables()


@pytest.fixture(scope='module')
def review():
    return banks.review_bank_eps()


@pytest.fixture(scope='module')
def parser():
    return banks.parser_snapshot()


def page(label, number):
    proof = json.loads(banks.read(banks.ROOT, f'{banks.BASE}/pages-{label}.json',
                                  banks.REVIEW_PINS[label]))
    body = banks.read(banks.ROOT, proof['source_file'], proof['source_sha256'])
    return PdfReader(io.BytesIO(body)).pages[number - 1].extract_text()


def mutate_read(monkeypatch, relative, transform):
    original = Path.read_bytes
    target = banks.ROOT / relative

    def changed(path):
        body = original(path)
        return transform(body) if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)


def test_latest_exact_current_filing_scope_and_full_DRE(annual):
    masters, tables = annual
    assert set(masters) == {'BPAC3', 'PINE4'}
    for ticker, master in masters.items():
        assert tuple(master[k] for k in ('VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA')) == banks.EXPECTED[ticker]
        assert master['DT_REFER'] == '2025-12-31'
        assert native._dre_fingerprint(tables[ticker]['DRE_con'], full=True) == banks.DRE_PINS[ticker]
        assert all((r['DT_INI_EXERC'], r['DT_FIM_EXERC']) == ('2025-01-01', '2025-12-31')
                   for r in tables[ticker]['DRE_con'])


def test_PDF_EPS_is_present_and_never_classified_as_genuine_absence(review):
    assert review['native_EPS_present_in_both_PDFs'] and review['CSV_absence_is_not_PDF_absence']
    by_ticker = {r['ticker']: r for r in review['findings']}
    assert by_ticker['BPAC3']['observations']['printed_basic_EPS'] == '2.30'
    assert by_ticker['PINE4']['observations']['printed_basic_EPS'] == '1.86'
    assert all(r['observations']['EPS_pdf_present'] and not r['observations']['genuine_PDF_EPS_absence']
               for r in by_ticker.values())


@pytest.mark.parametrize('ticker', ('BPAC3', 'PINE4'))
def test_normal_prepare_financials_and_financial_features_without_gate_changes(parser, ticker):
    before = json.loads((banks.REPORTS / 'financial-bank-before.json').read_bytes())
    assert parser[ticker] == before[ticker]
    assert parser[ticker]['features'] is None and parser[ticker]['record'] is None
    version, filing, _, _ = banks.EXPECTED[ticker]
    current, = [r for r in parser[ticker]['latest_records'] if r['version'] == version]
    assert current['filing_id'] == filing and current['accounts_scope'] == 'consolidated'
    assert current['profit_per_share'] is None and current['free_cash_per_share'] is None
    assert current['financial_method']['id'] == 'financial_no_industrial_fcf_v1'
    assert 'basic_eps_absent_ambiguous_or_zero' in current['unavailable_reasons']


def test_pine_original_container_XML_PDF_transport_and_clock_are_preserved(review):
    finding, = [r for r in review['findings'] if r['ticker'] == 'PINE4']
    source = finding['source']
    assert source['exact_filing_verified'] is True and source['published_at'] == '2026-03-31'
    assert source['retrieved_at'] and source['original_zip_member'].startswith('155965_020567_')
    assert source['sha256'] == '7213f0114f7c328dfaba561dc31db0a509fc1ddf39a665322c73ecb90af79a2d'
    assert not source['point_in_time_archive_verified']


def test_pine_native_DRE_prints_PN_basic_EPS_numerator_and_weighted_shares():
    text = ' '.join(page('PINE-native', 106).split())
    assert 'Lucro b\u00e1sico e dilu\u00eddo por a\u00e7\u00e3o do exerc\u00edcio' in text
    assert 'A\u00e7\u00f5es preferenciais 1,86' in text
    assert all(value in text for value in ('210.969', '113.288.564', '221.909', '119.162.783'))
    assert all(value in text for value in ('430.515', '2.363', '432.878'))


def test_pine_policy_and_DMPL_independently_keep_NCI_outside_parent():
    policy = ' '.join(page('PINE-native', 117).split())
    dmpl = ' '.join(page('PINE-native', 108).split())
    literal = 'atribu\u00edvel aos portadores de a\u00e7\u00f5es ordin\u00e1rias e preferenciais do Pine'
    assert ''.join(literal.split()) in ''.join(policy.split())
    assert all(value in dmpl for value in ('430.515', '2.363', '432.878'))
    assert Decimal('221909000') + Decimal('210969000') == Decimal('432878000')
    assert Decimal('221909000') + Decimal('210969000') != Decimal('430515000')


@pytest.mark.parametrize(('ticker', 'error'), [
    ('BPAC3', 'documented_eps_numerator_mismatch'),
    ('PINE4', 'documented_class_allocations_do_not_reconcile')])
def test_printed_candidates_are_rejected_by_existing_native_validator(annual, ticker, error):
    candidate = banks.unapproved_native_candidates()[ticker]
    with pytest.raises(ValueError, match='^' + error + '$'):
        native._checked_note_eps(annual[1][ticker]['DRE_con'], candidate)


def test_no_reported_class_EPS_is_reconstructed_from_form_aggregate(annual):
    for ticker in banks.SPECS:
        leaves = [r for r in annual[1][ticker]['DRE_con'] if r['CD_CONTA'].startswith('3.99.01.')]
        assert leaves == []
    text = page('PINE-native', 21)
    assert '186.223' in text and '372.446' in text
    assert 'A\u00e7\u00f5es preferenciais' not in text


def test_bpac_current_official_catalogue_identifies_same_cached_PDF_but_not_native_ZIP(review):
    finding, = [r for r in review['findings'] if r['ticker'] == 'BPAC3']
    source = finding['source']
    assert source['published_at'] == '2026-03-25T00:22:00Z'
    assert source['catalogue_metadata']['refDate'][:10] == '2025-12-31'
    assert source['catalogue_retrieved_at'] and source['retrieved_at'] is None
    assert source['exact_filing_verified'] is False
    assert source['PDF_text_limitation'].startswith('Pypdf and PDFium')


def test_bpac_broken_font_text_is_not_evidence_that_EPS_is_absent(review):
    assert '/i255' in page('BPAC-support', 66)
    finding, = [r for r in review['findings'] if r['ticker'] == 'BPAC3']
    assert finding['observations']['printed_basic_EPS'] == '2.30'
    assert finding['observations']['weighted_open_ON_shares'] == '7244313000'
    assert finding['observations']['treasury_not_arbitrarily_subtracted']


def test_bpac_native_audit_date_conflict_is_preserved_literally(review):
    assert 'S\u00e3o Paulo, 17 de mar\u00e7o de 2025' in page('BPAC-audit-end', 11)
    finding, = [r for r in review['findings'] if r['ticker'] == 'BPAC3']
    assert finding['source']['native_audit_date_literal'] == '2025-03-17'
    assert finding['source']['native_audit_date_not_silently_corrected']


def test_no_approved_readings_no_computed_EPS_no_scope_change(review, monkeypatch):
    assert review['approved_readings'] == []
    assert all(r['applied_reading'] is None and not r['EPS_computed'] and not r['model_admission']
               and not r['gates_changed'] and r['accounts_scope'] == 'consolidated'
               for r in review['findings'])
    monkeypatch.setattr(banks, 'review_bank_eps', lambda *args: review)
    readings, sources = banks.get_bank_eps_readings()
    assert readings == [] and len(sources) == 2


@pytest.mark.parametrize('relative', [banks.ANNUAL,
    banks.BASE + '/cached-BPAC3-ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f.pdf',
    banks.BASE + '/native-PINE4-7213f0114f7c328dfaba561dc31db0a509fc1ddf39a665322c73ecb90af79a2d.pdf',
    banks.BASE + '/pages-BPAC-support.json'])
def test_changed_original_or_visual_proof_is_rejected(monkeypatch, relative):
    mutate_read(monkeypatch, relative, lambda body: body + b'changed')
    with pytest.raises(ValueError, match='bank_eps_source_bytes_changed'):
        if relative == banks.ANNUAL:
            banks.current_tables()
        elif 'PINE4' in relative:
            banks.verify_pine()
        else:
            banks.verify_bpac()


def test_unreviewed_cutoff_does_not_fall_back_to_other_annual_filing():
    with pytest.raises(ValueError, match='bank_eps_unreviewed_cutoff'):
        banks.review_bank_eps(cutoff=date(2025, 9, 30))


def test_forbidden_MZiQ_rejected_before_any_collector_use(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('collector must not be constructed for prohibited MZiQ URL')

    monkeypatch.setattr(banks, 'PublicSourceCollector', forbidden)
    with pytest.raises(ValueError, match='bank_eps_MZiQ_network_forbidden'):
        banks.fetch('https://api.mziq.com/mzfilemanager/v2/d/example', {})


def test_network_receipts_have_no_B3_MZiQ_auth_or_retry_after_new_403():
    from .bulk_pending_sources import approved

    folder = banks.ROOT / banks.BASE
    requests = [json.loads(p.read_bytes()) for p in folder.glob('request-*.json')]
    receipts = [json.loads(p.read_bytes()) for p in folder.glob('response-*.json')]
    assert all(approved(r['url']) and urlsplit(r['url']).hostname != 'api.mziq.com'
               and r['retries'] == 0 and not r.get('headers') for r in requests)
    stopped, = [r for r in receipts if r['status'] == 403]
    assert stopped['url'] == 'https://d169uzu5o4xu1k.cloudfront.net/robots.txt'
    assert len([r for r in requests if urlsplit(r['url']).hostname
                == 'd169uzu5o4xu1k.cloudfront.net']) == 1
    assert not any('155965' in r['url'] for r in requests)  # PINE bytes reused, no refetch.
