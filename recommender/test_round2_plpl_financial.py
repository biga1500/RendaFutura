"""Offline, immutable evidence and ordinary parser checks for PLPL3."""

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
from . import round2_plpl_financial as plpl


@pytest.fixture(scope='module', autouse=True)
def offline():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('PLPL verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(plpl.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real source cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def annual():
    return plpl.current_tables()


@pytest.fixture(scope='module')
def assessment():
    return plpl.get_plpl_reading()


@pytest.fixture(scope='module')
def parser():
    return plpl.parser_snapshot()


def page(number):
    return ' '.join(PdfReader(io.BytesIO(plpl.read(plpl.ROOT, plpl.PUBLICATION,
        plpl.PUBLICATION_SHA))).pages[number - 1].extract_text().split())


def mutate_read(monkeypatch, relative, transform):
    original = Path.read_bytes
    target = plpl.ROOT / relative

    def changed(path):
        body = original(path)
        return transform(body) if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)


def test_exact_latest_identity_scope_period_and_literal_account(annual):
    master, tables = annual
    assert (master['ID_DOC'], master['VERSAO'], master['DT_RECEB']) == ('155293', '2', '2026-03-12')
    row, = [r for r in tables['_DFC_MI_con'] if r['CD_CONTA'] == '6.02.03']
    assert row['DS_CONTA'] == 'Acr\u00e9scimo/baixa do Imobilizado'
    assert Decimal(row['VL_CONTA']) == -26810
    assert row['ESCALA_MOEDA'] == 'MIL' and row['MOEDA'] == 'REAL'
    assert (row['DT_INI_EXERC'], row['DT_FIM_EXERC']) == ('2025-01-01', '2025-12-31')
    assert not native._capex_reading(row['DS_CONTA'])


def test_no_override_no_new_EPS_no_cash_gross_inference(assessment):
    reading, finding, sources = assessment
    assert reading is None and finding['applied_reading'] is None
    assert finding['status'] == 'pending_unchanged'
    assert not finding['model_admission'] and not finding['numeric_values_changed']
    assert not finding['EPS_computed'] and finding['accounts_scope'] == 'consolidated'
    assert len(finding['missing_source_fields']) == 2
    assert sources[0]['exact_filing_verified'] is False


def test_ordinary_prepare_financials_and_financial_features_unchanged(parser):
    assert parser == json.loads((plpl.REPORTS / 'financial-plpl-before.json').read_bytes())
    assert parser['features'] is None and parser['record'] is None
    latest, = [r for r in parser['latest_records'] if r['version'] == '2']
    assert latest['filing_id'] == '155293' and latest['unavailable_reasons'] == ['capex_not_identified']
    assert latest['accounts_scope'] == 'consolidated'
    assert latest['financial_method']['capex_accounts'] == []
    assert latest['profit_per_share'] is None and latest['free_cash_per_share'] is None


def test_native_DFC_is_mixed_PPE_and_intangibles_not_PPE_only():
    text = page(53)
    assert 'Acr\u00e9scimo/baixa do imobilizado e intang\u00edvel' in text
    assert '(26.810)' in text and '(76.132)' in text and 'Consolidado' in text
    assert '(16.077)' in text  # Different individual scope, not an alternative capex.


def test_asset_rollforwards_do_not_certify_gross_cash_paid():
    ppe, intangible = page(104), page(105)
    assert 'Aquisi\u00e7\u00f5es' in ppe and '17.997' in ppe and 'Baixas' in ppe and '(2)' in ppe
    assert '(+) Adi\u00e7\u00f5es' in intangible and '8.815' in intangible
    assert Decimal('17997') + Decimal('8815') - Decimal('2') == Decimal('26810')
    assert Decimal('17997') + Decimal('8815') != Decimal('26810')


def test_note_34_is_not_a_zero_disposal_cash_certificate(assessment):
    assert '34. Transa\u00e7\u00f5es que n\u00e3o afetam caixa' in page(133)
    assert 'Dividendos provisionados' in page(133)
    assert 'absence is not a declared zero' in assessment[1]['note_34_limit']


def test_retrieval_publication_and_revision_are_not_conflated(assessment):
    source, = assessment[2]
    assert source['published_at'] is None and source['retrieved_at'].startswith('2026-10-26T')
    assert source['audit_report_date'] == '2026-03-11'
    assert source['http_last_modified_is_not_publication_proof']
    assert source['use'] == 'negative_evidence_no_override'
    assert not source['bank_settlement_verified'] and not source['point_in_time_archive_verified']
    assert source['visually_inspected_pages'] == [53, 104, 105, 133]


@pytest.mark.parametrize('relative', (plpl.ANNUAL, plpl.PUBLICATION))
def test_changed_source_bytes_are_rejected(monkeypatch, relative):
    mutate_read(monkeypatch, relative, lambda b: b + b'changed')
    with pytest.raises(ValueError, match='plpl_evidence_changed'):
        plpl.get_plpl_reading()


@pytest.mark.parametrize(('key', 'value'), [('status', 403), ('error', 'access_stop_no_retry'),
    ('published_at', '2026-03-12'), ('scope', 'other'), ('sha256', '0' * 64)])
def test_changed_transport_or_invented_publication_rejected(monkeypatch, key, value):
    def transform(body):
        receipt = json.loads(body)
        receipt[key] = value
        return json.dumps(receipt).encode()

    mutate_read(monkeypatch, f'{plpl.BASE}/response-{plpl.sha(plpl.PUBLICATION_URL.encode())}.json', transform)
    with pytest.raises(ValueError, match='plpl_publication_transport_changed'):
        plpl.verify_publication()


def test_changed_extracted_text_is_rejected(monkeypatch):
    def transform(body):
        review = json.loads(body)
        review['pages'][2]['text'] = 'Gross acquisitions paid in cash 26810'
        return json.dumps(review).encode()

    mutate_read(monkeypatch, f'{plpl.BASE}/pages-publication.json', transform)
    with pytest.raises(ValueError, match='plpl_review_text_changed'):
        plpl.verify_publication()


def test_arbitrary_cutoff_is_not_approved():
    with pytest.raises(ValueError, match='plpl_unreviewed_cutoff'):
        plpl.get_plpl_reading(cutoff=date(2025, 9, 30))


def test_historical_discovery_cannot_supply_current_or_training_override():
    rows = plpl.historical_candidates()
    assert {r['year'] for r in rows} == set(range(2020, 2025))
    assert all(not r.get('approved') for r in rows)
    assert all('native_same_filing_review_pending' in r['status']
               or r['status'] == 'annual_archive_not_cached' for r in rows)
    assert all(r['source_file'].startswith('raw/cvm-dfp-') for r in rows)


def test_no_request_to_MZiQ_after_401_and_no_authentication_or_denied_CDN():
    from urllib.parse import urlsplit
    from .bulk_pending_sources import approved

    folder = plpl.ROOT / plpl.BASE
    receipts = [json.loads(p.read_bytes()) for p in folder.glob('response-*.json')]
    stop, = [r for r in receipts if urlsplit(r['url']).hostname == 'api.mziq.com'
             and r['status'] == 401]
    requests = [json.loads(p.read_bytes()) for p in folder.glob('request-*.json')]
    assert all(approved(r['url']) and r['retries'] == 0 for r in requests)
    assert all(r['started_at'] <= stop['retrieved_at'] for r in requests
               if urlsplit(r['url']).hostname == 'api.mziq.com')
    assert not any('/v2/d/' in r['url'] for r in requests)
    assert all(not r.get('headers') for r in requests)
