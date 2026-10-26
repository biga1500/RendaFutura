from datetime import date
import csv
import hashlib
import io
import json
from pathlib import Path
import urllib.error
import zipfile

import pytest

from . import zero_distribution_research as research


def row(description='Dividendos e juros sobre capital pr\u00f3prio pagos', value='0.0000000000',
        code='6.03.05', scale='MIL', currency='REAL'):
    return {'CD_CONTA': code, 'DS_CONTA': description, 'VL_CONTA': value,
            'MOEDA': currency, 'ESCALA_MOEDA': scale}


def period(start='2026-01-01', end='2026-06-30', *, rows=None, filing='123',
           published='2026-08-10', version=1, scope='individual'):
    return {'ticker': 'TEST3', 'period_start': start, 'period_end': end,
            'published_at': published, 'document_reference': end, 'version': version,
            'filing_id': filing, 'scope': scope, 'table': 'itr_cia_aberta_DFC_MI_ind_2026.csv',
            **research.evaluate_period([row()] if rows is None else rows)}


@pytest.mark.parametrize('description,component', [
    ('Dividendos pagos', 'dividends'), ('Pagamento de dividendos', 'dividends'),
    ('Distribui\u00e7\u00e3o de dividendos', 'dividends'), ('Dividendos', 'dividends'),
    ('Juros sobre o capital pr\u00f3prio pagos', 'jcp'), ('JCP pagos', 'jcp'),
    ('Pagamento de dividendos e juros sobre capital', 'both'),
    ('Dividendos e juros sobre capital pr\u00f3prio', 'both'),
    ('Dividendos e  juros sobre capital pr\u00f3prio pagos', 'both'),
    ('Dividendos e JSCP pagos', 'both'),
])
def test_exact_financing_distribution_labels(description, component):
    assert research.cash_component('6.03.05', description) == component


@pytest.mark.parametrize('description,code', [
    ('Dividendos recebidos', '6.03.05'), ('Recebimento de Dividendos', '6.03.05'),
    ('Dividendos recebidos de controladas', '6.02.01'),
    ('Dividendos pagos para acionistas n\u00e3o controladores', '6.03.10'),
    ('Dividendos pagos aos minorit\u00e1rios', '6.03.10'),
    ('Dividendos e juros sobre capital pr\u00f3prio a pagar', '6.03.05'),
    ('Dividendos declarados', '6.03.05'), ('Proposta de dividendos', '6.03.05'),
    ('Juros sobre empr\u00e9stimos e financiamentos', '6.03.04'),
    ('Caixa L\u00edquido das Atividades de Financiamento', '6.03'),
    ('Dividendos e juros sobre capital pr\u00f3prio pagos', '6.01.01'),
    ('Dividendos e juros sobre capital pr\u00f3prio pagos', '6.02.01'),
    ('Revers\u00e3o de dividendos', '6.03.05'), ('Dividendos compensados', '6.03.05'),
])
def test_wrong_direction_scope_non_cash_or_general_totals_are_not_zero(description, code):
    result = research.evaluate_period([row(description, code=code)])
    assert result['status'] == 'pending'


def test_combined_and_separate_zeros_are_supported_but_never_events():
    for rows in ([row()], [row('Dividendos pagos'), row('JCP pagos', code='6.03.09')]):
        result = research.evaluate_period(rows)
        assert result['status'] == 'reported_zero_both_components'
        assert result['individual_events_generated'] == 0
        assert result['settlement_verified'] is False
        assert result['exact_unrounded_cash_zero_verified'] is False
        assert all(item['reporting_unit_brl'] == 1000 for item in result['rows'])


@pytest.mark.parametrize('description', ['Dividendos pagos', 'JCP pagos'])
def test_one_zero_component_never_implies_the_other(description):
    result = research.evaluate_period([row(description)])
    assert result['status'] == 'pending'
    assert result['reason'] == 'only_one_distribution_component_explicitly_reported'


@pytest.mark.parametrize('value,currency,scale', [
    ('NaN', 'REAL', 'MIL'), ('Infinity', 'REAL', 'MIL'), ('', 'REAL', 'MIL'),
    ('0', 'USD', 'MIL'), ('0', 'REAL', 'UNKNOWN'),
])
def test_invalid_values_and_scales_do_not_become_zero(value, currency, scale):
    assert research.evaluate_period([row(value=value, currency=currency, scale=scale)])['status'] == 'pending'


def test_nonzero_preserved_exactly_without_netting_or_grossing():
    result = research.evaluate_period([row(value='-15781.68543')])
    assert result['status'] == 'known_positive_reported_outflow'
    assert result['rows'][0]['reported_value_brl'] == '-15781685.43000'
    assert research.evaluate_period([row(value='10')])['status'] == 'pending'
    opposed = research.evaluate_period([row('Dividendos pagos', '-10'), row('JCP pagos', '10', code='6.03.09')])
    assert opposed['status'] == 'pending'
    assert opposed['reason'] == 'positive_sign_cash_out_requires_documentary_review'


def test_duplicate_account_cannot_be_used_as_independent_zero_confirmation():
    assert research.evaluate_period([row(), row()])['reason'] == 'duplicate_distribution_account'


def test_unknown_additional_outflow_blocks_zero_without_assigning_NCI_to_parent():
    result = research.evaluate_period([row(), row('Pagamento de dividendos extraordinarios', '-2', code='6.03.07')])
    assert result['reason'] == 'unresolved_additional_distribution_cashflow_label'
    assert research.evaluate_period([row(), row('Dividendos pagos para acionistas n\u00e3o controladores', '-2', code='6.03.10')])['status'] == 'reported_zero_both_components'
    assert research.evaluate_period([row(), row('JCP pagos', '-2', code='6.01.03.04')])['status'] == 'pending'


def test_missing_rows_are_not_a_zero():
    assert research.evaluate_period([])['reason'] == 'no_explicit_distributions_paid_accounts_not_zero'


def test_q2_does_not_fill_july_september_or_date_payments():
    result = research.summarize_periods([period()], date(2026, 1, 1), date(2026, 9, 30))
    assert result['zero_supported_full_month_count'] == 6
    assert result['unresolved_months'] == ['2026-07', '2026-08', '2026-09']
    assert not result['all_requested_months_reported_zero']
    assert result['status'] == 'pending'
    assert all(month['value_brl'] is None for month in result['months'])


def test_annual_zero_supports_subset_of_requested_year_without_events():
    result = research.summarize_periods([period('2021-01-01', '2021-12-31')], date(2021, 10, 1), date(2021, 12, 31))
    assert result['zero_supported_full_month_count'] == 3
    assert result['all_requested_months_reported_zero']
    assert result['individual_events_generated'] == 0


def test_latest_comparative_overrides_older_zero_and_preserves_nonzero():
    old = period('2025-01-01', '2025-12-31', published='2026-03-10', filing='old')
    new = period('2025-01-01', '2025-12-31', rows=[row(value='-1')], published='2026-08-10', filing='new')
    result = research.summarize_periods([old, new], date(2025, 1, 1), date(2025, 12, 31))
    assert result['zero_supported_full_month_count'] == 0
    assert result['status'] == 'known_positive'
    assert result['selected_individual_periods'][0]['filing_id'] == 'new'


def test_latest_missing_components_do_not_fall_back_to_old_zero():
    old = period(filing='old')
    new = period(rows=[], filing='new', published='2026-08-11')
    result = research.summarize_periods([old, new])
    assert result['zero_supported_full_month_count'] == 0


def test_consolidated_zero_is_not_assigned_to_parent_ticker():
    result = research.summarize_periods([period(scope='consolidated')])
    assert result['zero_supported_full_month_count'] == 0


def test_conflicting_positive_subperiod_blocks_zero_annual_period():
    annual = period('2025-01-01', '2025-12-31')
    interim = period('2025-01-01', '2025-06-30', rows=[row(value='-1')])
    result = research.summarize_periods([annual, interim], date(2025, 1, 1), date(2025, 12, 31))
    assert result['zero_supported_full_month_count'] == 0
    assert result['months'][0]['conflicting_filing_ids']


def test_positive_annual_does_not_disprove_zero_quarter_or_half_year():
    half = period('2025-01-01', '2025-06-30')
    annual = period('2025-01-01', '2025-12-31', rows=[row(value='-1')])
    result = research.summarize_periods([half, annual], date(2025, 1, 1), date(2025, 12, 31))
    assert result['zero_supported_full_month_count'] == 6
    assert result['status'] == 'known_positive'
    assert not result['months'][0]['conflicting_filing_ids']


def test_union_of_zero_subperiods_cannot_contradict_positive_total():
    first = period('2025-01-01', '2025-06-30', filing='first')
    second = period('2025-07-01', '2025-12-31', filing='second')
    annual = period('2025-01-01', '2025-12-31', rows=[row(value='-1')], filing='annual')
    result = research.summarize_periods([first, second, annual], date(2025, 1, 1), date(2025, 12, 31))
    assert result['zero_supported_full_month_count'] == 0
    assert result['months'][0]['conflicting_filing_ids'] == ['annual']


def test_positive_annual_aggregate_does_not_locate_month_or_window_subset():
    result = research.summarize_periods([period('2021-01-01', '2021-12-31', rows=[row(value='-1')])],
                                       date(2021, 10, 1), date(2021, 12, 31))
    assert result['status'] == 'pending'
    assert result['zero_supported_full_month_count'] == 0


def test_partial_month_and_ambiguous_methods_are_not_admitted():
    source = period()
    assert research.summarize_periods([source], date(2026, 1, 10), date(2026, 6, 30))['zero_supported_full_month_count'] == 5
    other = {**source, 'table': 'itr_cia_aberta_DFC_MD_ind_2026.csv'}
    assert research.summarize_periods([source, other])['zero_supported_full_month_count'] == 0
    with pytest.raises(ValueError):
        research.summarize_periods([], date(2026, 2, 1), date(2026, 1, 1))


def test_missing_or_empty_issuer_history_and_policy_do_not_prove_zero():
    receipt = {'source_id': 'amar-history', 'ticker': 'AMAR3'}
    result = research.inspect_issuer_source(b'<html><p>Politica: dividendos de 25%.</p><table><tr><td>2015</td></tr></table></html>', receipt)
    assert result['qualified_zero_periods'] == []
    assert result['empty_or_missing_history_is_not_zero']
    assert result['tables'][0]['rows']


def test_primary_calendar_preserves_explicit_date_and_keeps_deadline_unknown():
    receipt = {'source_id': 'ambp-history', 'ticker': 'AMBP3', 'sha256': 'test', 'url': 'https://ri.ambipar.com/remuneracao-aos-acionistas/'}
    tables = [{'table': 0, 'rows': [
        ['Provento', 'Evento-data', 'Montante', 'Valor por A\u00e7\u00e3o (R$)', 'Data do Pagamento', 'Payout Lucro L\u00edquido'],
        ['Dividendos', 'AGO 04/05/2022', '34.238.759,71', '0,3031707', '05/07/2022', '25%'],
        ['Dividendos', 'AGO 04/05/2023', '100,00', '0,1', 'A ser definida', '25%'],
        ['Dividendos', 'AGO 04/05/2023', '100,00', '0,1', 'At\u00e9 05/07/2023', '25%'],
    ]}]
    rows = research._calendar_disclosures(tables, receipt)
    assert len(rows) == 3
    assert rows[0]['calendar_payment_date'] == '2022-07-05'
    assert rows[0]['record_date'] is None and rows[0]['publication_date'] is None
    assert rows[0]['amount_per_share_as_reported'] == '0.3031707'
    assert rows[0]['amount_basis'] == 'unverified_no_net_to_gross_conversion'
    assert not rows[0]['cash_model_eligible'] and not rows[0]['settlement_verified']
    assert rows[1]['calendar_payment_date'] is rows[2]['calendar_payment_date'] is None
    assert not rows[1]['reported_in_requested_window']


def test_documentary_no_dividend_statement_does_not_prove_cash_zero_or_JCP():
    receipt = {'source_id': 'biom-proposal-2025', 'sha256': '0f64b9496eafe38f0e3772e293a7c2bca3be594352bb4fbd08823730b0ea5aa9'}
    pages = [{'pdf_page': 53, 'page_text_sha256': 'text-digest', 'text':
        'Nos exerc\u00edcios de 31 de dezembro de 2024 e 2023, a Companhia n\u00e3o apurou lucro e, portanto, n\u00e3o realizou a distribui\u00e7\u00e3o de dividendos.'}]
    result = research._documentary_readings(pages, receipt)
    assert len(result) == 1
    assert result[0]['qualified_zero_periods'] == [] and not result[0]['jcp_explicit']
    assert research._documentary_readings(pages, {**receipt, 'sha256': 'different-document'}) == []
    assert research._documentary_readings([{**pages[0], 'pdf_page': 52}], receipt) == []


@pytest.mark.parametrize('url', [
    'https://www.b3.com.br/whatever', 'https://sistemaswebb3-listados.b3.com.br/',
    'https://ri.blau.com/unknown', 'http://ri.blau.com/',
    'https://ri.blau.com@evil.test/', 'https://api.mziq.com/private',
])
def test_only_exact_preapproved_non_exchange_sources(url):
    assert not research._approved_url(url)


def test_acquisition_budget_and_duplicates_rejected_before_network(tmp_path):
    with pytest.raises(ValueError, match='budget'):
        research.acquire_sources(tmp_path, ['blau-history'] * 17)
    with pytest.raises(ValueError, match='duplicate'):
        research.acquire_sources(tmp_path, ['blau-history'] * 2)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_acquisition_stops_host_and_persists_error_without_retry(tmp_path, monkeypatch, status):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'blocked', {}, io.BytesIO(b''))

    monkeypatch.setattr(research.urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setitem(research.SOURCE_SPECS, 'same-host-second', ('AERI3', 'https://ri.aerisenergy.com.br/second'))
    receipts = research.acquire_sources(tmp_path, ['aeri-results', 'same-host-second'])
    assert len(calls) == 1
    assert receipts[0]['http_status'] == status
    assert receipts[1]['status'] == 'not_requested'
    assert research.acquire_sources(tmp_path, ['aeri-results'])[0]['http_status'] == status
    assert len(calls) == 1


def test_local_source_integrity_and_path_traversal(tmp_path):
    target = tmp_path / research.CACHE
    target.mkdir(parents=True)
    body = b'<html>source</html>'
    digest = hashlib.sha256(body).hexdigest()
    path = target / ('blau-history-' + digest + '.html')
    path.write_bytes(body)
    receipt = {'file': path.relative_to(tmp_path).as_posix(), 'sha256': digest, 'bytes': len(body)}
    assert research.read_acquired_source(tmp_path, receipt) == body
    path.write_bytes(body + b'corrupt')
    with pytest.raises(ValueError, match='integrity'):
        research.read_acquired_source(tmp_path, receipt)
    with pytest.raises(ValueError, match='reference'):
        research.read_acquired_source(tmp_path, {**receipt, 'file': '../outside.html'})


def test_immutable_conflict_rejected(tmp_path):
    target = tmp_path / 'receipt.json'
    research._immutable(target, b'original')
    research._immutable(target, b'original')
    with pytest.raises(ValueError, match='conflict'):
        research._immutable(target, b'replacement')


def test_receipt_content_address_protects_original_metadata(tmp_path):
    receipt = {'source_id': 'blau-history', 'url': research.SOURCE_SPECS['blau-history'][1], 'status': 'http_error', 'http_status': 403}
    body = research._json(receipt)
    path = tmp_path / ('blau-history-' + research._digest(body) + '-receipt.json')
    path.write_bytes(body)
    assert research._load_receipt(path) == receipt
    path.write_bytes(body + b' ')
    with pytest.raises(ValueError, match='integrity'):
        research._load_receipt(path)


def test_latest_filing_with_missing_individual_table_blocks_old_zero(tmp_path):
    directory = tmp_path / 'raw'
    directory.mkdir()
    metadata_fields = ['CD_CVM', 'CNPJ_CIA', 'DT_REFER', 'VERSAO', 'DT_RECEB', 'ID_DOC']
    source_fields = list(research.CSV_KEYS)

    def make_csv(fields, rows):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode('latin1')

    metadata = [{'CD_CVM': '1', 'CNPJ_CIA': '12', 'DT_REFER': '2025-12-31', 'VERSAO': '2', 'DT_RECEB': '2026-08-10', 'ID_DOC': 'new'},
                {'CD_CVM': '1', 'CNPJ_CIA': '12', 'DT_REFER': '2025-12-31', 'VERSAO': '1', 'DT_RECEB': '2026-03-10', 'ID_DOC': 'old'}]
    cells = {'CNPJ_CIA': '12', 'CD_CVM': '1', 'DT_REFER': '2025-12-31', 'VERSAO': '2',
             'ORDEM_EXERC': '\u00daLTIMO', 'DT_INI_EXERC': '2025-01-01', 'DT_FIM_EXERC': '2025-12-31', **row()}
    archive = directory / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('dfp_cia_aberta_2025.csv', make_csv(metadata_fields, metadata))
        output.writestr('dfp_cia_aberta_DFC_MI_con_2025.csv', make_csv(source_fields, [cells]))
        output.writestr('dfp_cia_aberta_DFC_MI_ind_2025.csv', make_csv(source_fields, [{**cells, 'VERSAO': '1'}]))
    rows, diagnostics, source = research._archive_periods(tmp_path, 'raw/test.zip', 'DFP', 2025,
        [{'cvm': 1, 'ticker': 'TEST3', 'cnpj': '12'}], date(2026, 10, 25), date(2021, 10, 1), date(2026, 9, 30))
    assert source['sha256'] == research._digest(archive.read_bytes())
    assert any(item['reason'] == 'latest_filing_has_no_individual_DFC_rows_no_older_fallback' for item in diagnostics)
    result = research.summarize_periods([period('2025-01-01', '2025-12-31', published='2026-03-10'), *rows])
    assert result['zero_supported_full_month_count'] == 0
    assert result['selected_individual_periods'][0]['filing_id'] == 'new'


def test_actual_cached_primary_evidence_without_active_pointer_changes():
    root = Path(__file__).parent / 'data/market'
    if not (root / 'raw/cvm-dfp-2025.zip').exists():
        pytest.skip('Optional immutable real-data fixture unavailable')
    pointers = [root / 'current.json', root / 'inventory/current.json']
    before = [path.read_bytes() for path in pointers]
    report, output = research.research(root)
    assert [path.read_bytes() for path in pointers] == before
    assert output.parent == root.resolve() / research.CACHE
    assert report['counts']['issuers'] == 9
    companies = {row['identity']['ticker']: row for row in report['companies']}
    assert companies['AALR3']['zero_supported_full_month_count'] == 51
    assert companies['LWSA3']['zero_supported_full_month_count'] == 21
    assert companies['ARML3']['zero_supported_full_month_count'] == 3
    assert companies['AERI3']['status'] == companies['BLAU3']['status'] == companies['ARML3']['status'] == 'known_positive'
    assert not companies['AMAR3']['identity']['identity_verified']
    assert companies['AMAR3']['identity']['issuer_identity_verified']
    assert companies['BIOM3']['zero_supported_full_month_count'] == 0
    assert companies['AMBP3']['zero_supported_full_month_count'] == 0
    assert companies['AMBP3']['status'] == 'known_positive_distribution_disclosure_not_settlement'
    assert len(companies['AMBP3']['positive_primary_calendar_disclosures']) == 2
    assert report['counts']['fully_reported_zero_issuers'] == 0
    assert report['individual_events_generated'] == 0
    assert not report['active_model_changed'] and not report['model_approval_performed']
    for company in companies.values():
        assert all(month in company['unresolved_months'] for month in ('2026-07', '2026-08', '2026-09'))
    saved = json.loads(output.read_bytes())
    assert saved == report
