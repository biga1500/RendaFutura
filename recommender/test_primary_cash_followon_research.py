"""Offline safety/provenance checks for the separately bounded follow-on."""

from pathlib import Path
from copy import deepcopy
from decimal import Decimal
import json
import shutil
import urllib.error
import urllib.request

from lxml import html
import pytest

from . import primary_cash_followon_research as research


FOLDER = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def current_v3_report():
    return research.build_report(FOLDER)


def isolated_fetcher(tmp_path):
    fetcher = research.Fetcher.__new__(research.Fetcher)
    fetcher.folder = tmp_path
    fetcher.bootstrap = {'issuer_seeds': [{'url': 'https://ri.eztec.com.br/'},
                                         {'url': 'https://ri.grupopanvel.com.br/'}]}
    fetcher.bootstrap_file = 'owned-test-bootstrap'
    class NoNetwork:
        def open(self, *args, **kwargs):
            raise AssertionError('unexpected_real_network')
    fetcher.opener = NoNetwork()
    return fetcher


def isolated_v3_fetcher(tmp_path):
    phase = research.budget_v3(FOLDER)
    for name in (*phase['prior_files_sha256'], phase['file']):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FOLDER / name, target)
    return isolated_fetcher(tmp_path)


def test_cached_shell_inspection_is_read_only_and_classifies_unavailability():
    count = len(list((FOLDER / research.CACHE).glob('request-*.json')))
    report, _ = research.bootstrap(FOLDER)
    assert len(report['cached_shell_inspection']) == 3
    for shell in report['cached_shell_inspection']:
        assert not shell['published_links_or_frames']
        assert not shell['inline_scripts']
        assert shell['form_only_repeats_empty_download_URL']
        assert shell['classification'] == 'unavailable_document_body_not_authentication_or_robots_denial'
    assert len(list((FOLDER / research.CACHE).glob('request-*.json'))) == count


def test_html_base_is_respected_before_resolving_public_script_navigation():
    tree = html.fromstring('<html><head><base href="/pt/"></head><body><script src="main.js"></script></body></html>')
    assert research.document_base(tree, 'https://www.grupopanvel.com.br/pt') == 'https://www.grupopanvel.com.br/pt/'


def test_real_script_assignment_proves_canonical_RI_without_guessing():
    fetcher = research.Fetcher(FOLDER)
    proof = fetcher.provenance('https://ri.grupopanvel.com.br/')
    assert proof['kind'] == 'published_public_script_navigation'
    assert proof['source']['url'].endswith('/pt/main.39c0e830040ab98c.js')
    assert proof['assignment'] == 'investorRelationshipLink="https://ri.grupopanvel.com.br/"'
    assert proof['source']['ordinal'] == 11
    assert proof['assignment'].encode() in fetcher.body(proof['source'])
    # A later RI homepage contains the same destination. Its self-link must not
    # displace the earlier exact canonical navigation assignment.
    responses = fetcher.ledger('response')
    assert any(r['ordinal'] == 13 and r['url'] == 'https://ri.grupopanvel.com.br/' for r in responses)
    fetcher.ledger = lambda prefix: list(reversed(responses)) if prefix == 'response' else []
    assert fetcher.provenance('https://ri.grupopanvel.com.br/') == proof


@pytest.mark.parametrize('url', ['https://cdn-sites-assets.mziq.com/a.js', 'https://allos.com.br/x',
    'https://ri.azzas2154.com.br/x', 'https://www.b3.com.br/x', 'https://www.engie.com.br/x',
    'https://www.santander.com.br/ri/x', 'https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?numSequencia=741697',
    'https://ri.eztec.com.br/notice.docx', 'https://user:password@ri.eztec.com.br/',
    'http://ri.eztec.com.br/'])
def test_forbidden_origins_old_download_URLs_and_Word_never_requested(tmp_path, url):
    with pytest.raises(ValueError, match='forbidden'):
        isolated_fetcher(tmp_path).acquire(url)
    assert not list(tmp_path.rglob('request-*.json'))


def test_unpublished_issuer_path_cannot_be_guessed(tmp_path):
    with pytest.raises(ValueError, match='no_published_navigation'):
        isolated_fetcher(tmp_path).acquire('https://ri.eztec.com.br/api/document/509579')
    assert not list(tmp_path.rglob('request-*.json'))


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_is_persistent_across_other_paths_and_new_instances(tmp_path, status):
    fetcher = isolated_fetcher(tmp_path)
    class Denied:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, 'stop', {}, None)
    fetcher.opener = Denied()
    fetcher._get('https://ri.eztec.com.br/robots.txt', {'kind': 'owned-test'})
    with pytest.raises(ValueError, match='access_stop'):
        isolated_fetcher(tmp_path)._get('https://ri.grupopanvel.com.br/robots.txt', {'kind': 'owned-test'})
    assert len(list((tmp_path / research.CACHE).glob('request-*.json'))) == 1


def test_budget_includes_robots_and_rejects_thirteenth_request(tmp_path):
    for ordinal in range(1, 13):
        research.save(tmp_path, 'request', {'ordinal': ordinal, 'url': 'https://ri.eztec.com.br/' + str(ordinal)})
    with pytest.raises(ValueError, match='budget_exhausted'):
        isolated_fetcher(tmp_path)._get('https://ri.eztec.com.br/robots.txt', {'kind': 'owned-test'})
    assert len(list((tmp_path / research.CACHE).glob('request-*.json'))) == 12


def test_attempted_URL_cannot_be_retried(tmp_path):
    research.save(tmp_path, 'request', {'ordinal': 1, 'url': 'https://ri.eztec.com.br/'})
    with pytest.raises(ValueError, match='no_retry'):
        isolated_fetcher(tmp_path)._get('https://ri.eztec.com.br/', {'kind': 'owned-test'})


def test_robots_disallow_stops_without_document_request(tmp_path):
    fetcher = isolated_fetcher(tmp_path)
    body = b'User-agent: *\nDisallow: /\n'
    file = research.save(tmp_path, 'body', body)
    research.save(tmp_path, 'response', {'ordinal': 1, 'url': 'https://ri.eztec.com.br/robots.txt',
        'http_status': 200, 'access_stop': False, 'file': file, 'sha256': research.exact.shared._sha(body)})
    with pytest.raises(ValueError, match='robots_disallow'):
        fetcher.acquire('https://ri.eztec.com.br/')
    assert not list((tmp_path / research.CACHE).glob('request-*.json'))
    with pytest.raises(ValueError, match='access_stop'):
        fetcher.acquire('https://ri.grupopanvel.com.br/')


def test_redirect_handler_never_follows_automatically():
    assert research.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://ri.eztec.com.br/') is None


def test_final_report_counts_all_attempts_and_does_not_promote_older_bodies(record_property):
    record_property('ledger_scope', 'immutable_historical_v2_at_12_attempts_not_current_v3')
    record_property('expected_historical_request_count', 12)
    before = len(list((FOLDER / research.CACHE).glob('request-*.json')))
    report, path = research.historical_v2_report(FOLDER)
    assert path == research.V2_REPORT
    assert report['schema_version'] == 2
    assert report['request_count'] == report['maximum_requests'] == 12
    assert len({r['url'] for r in report['requests']}) == 12
    assert not report['access_denial_encountered']
    assert not report['earlier_versions_used_as_substitutes']
    assert not report['amounts_or_dates_inferred']
    assert not report['empty_CVM_URLs_retried']
    assert report['results'][0]['robots_can_fetch_RI_home']
    assert not report['results'][0]['RI_home_requested']
    assert not any(row['exact_bodies_obtained'] for row in report['results'])
    assert report['navigation_correction']['initial_redirect_response_preserved_and_counted']
    assert len(list((FOLDER / research.CACHE).glob('request-*.json'))) == before
    print('followon_report=' + path)


def test_current_v3_report_counts_the_same_cumulative_ledger_and_preserves_v2(current_v3_report, record_property):
    record_property('ledger_scope', 'current_cumulative_v3_with_explicit_12_to_20_phase')
    record_property('expected_current_request_count', 20)
    report, path = current_v3_report
    historical, _ = research.historical_v2_report(FOLDER)
    assert report['schema_version'] == 3
    assert report['report_scope'] == 'current_cumulative_v3_after_explicit_12_to_20_extension'
    assert report['request_count'] == report['maximum_requests'] == 20
    assert report['prior_request_count'] == report['historical_v2_snapshot']['request_count'] == 12
    assert report['additional_request_count'] == 8
    assert report['acquisition_frozen'] and not report['counter_reset']
    assert [r['ordinal'] for r in report['requests']] == list(range(1, 21))
    assert [r['ordinal'] for r in report['responses']] == list(range(1, 21))
    assert len({r['url'] for r in report['requests']}) == 20
    assert report['requests'][:12] == historical['requests']
    assert report['responses'][:12] == historical['responses']
    assert not report['access_denial_encountered']
    assert not report['earlier_versions_used_as_substitutes']
    assert not report['amounts_or_dates_inferred']
    assert not report['empty_CVM_URLs_retried']
    assert report['canonical_RI_script_proof']['source']['ordinal'] == 11
    for row in report['requests'][12:]:
        assert row['phase'] == 'PNVL_exact_revised_bodies_only'
        assert row['cumulative_limit'] == 20
        assert row['budget_audit_sha256'] == report['budget_change_audit']['sha256']
        assert research.pnvl_provenance(row['provenance'])
        assert 'eztec' not in row['url'] and research.approved(row['url'])
    print('current_followon_report=' + path)


def test_v3_phase_pins_every_old_request_receipt_body_and_snapshot(current_v3_report):
    report, _ = current_v3_report
    phase = research.budget_v3(FOLDER)
    assert (phase['previous_limit'], phase['cumulative_limit'], phase['maximum_additional_requests']) == (12, 20, 8)
    assert phase['prior_request_count'] == 12 and phase['prior_ordinals'] == list(range(1, 13))
    assert phase['targets'] == ['741697', '741696'] and phase['EZTC_stopped']
    assert not phase['counter_reset']
    assert phase['maximum_unselected_navigation_candidates'] == 3
    assert set(phase['denial_stops_unchanged']) == {401, 403, 429, 'robots_disallow', 'authentication', 'captcha', 'denied_CDN'}
    files = phase['prior_files_sha256']
    for prefix in ('request', 'response'):
        for path in (FOLDER / research.CACHE).glob(prefix + '-*.json'):
            if json.loads(path.read_bytes())['ordinal'] <= 12:
                assert path.relative_to(FOLDER).as_posix() in files
    assert research.V2_REPORT in files
    for response in report['responses'][:12]:
        assert files[response['file']] == response['sha256']
    for name, digest in files.items():
        assert research.exact.shared._sha((FOLDER / name).read_bytes()) == digest


def test_v3_phase_rejects_changed_baseline_before_network(tmp_path):
    fetcher = isolated_v3_fetcher(tmp_path)
    phase = research.budget_v3(tmp_path)
    # A hash-addressed new file cannot replace the phase's original pinned file.
    name = next(n for n in phase['prior_files_sha256'] if '/request-' in n)
    (tmp_path / name).unlink()
    with pytest.raises(FileNotFoundError):
        fetcher._get('https://ri.grupopanvel.com.br/new', {'source': {'url': 'https://ri.grupopanvel.com.br/'}})


def test_twenty_first_request_is_rejected_without_reset_or_network(tmp_path, current_v3_report):
    fetcher = isolated_v3_fetcher(tmp_path)
    for request in current_v3_report[0]['requests'][12:]:
        research.save(tmp_path, 'request', request)
    with pytest.raises(ValueError, match='budget_exhausted'):
        fetcher._get('https://ri.grupopanvel.com.br/new', {'source': {'url': 'https://ri.grupopanvel.com.br/'}})
    assert len(fetcher.ledger('request')) == 20


@pytest.mark.parametrize('status', [401, 403, 429])
def test_explicit_v3_budget_never_overrides_prior_status_denial(tmp_path, status):
    fetcher = isolated_v3_fetcher(tmp_path)
    research.save(tmp_path, 'response', {'ordinal': 13, 'url': 'https://ri.grupopanvel.com.br/blocked',
                                      'http_status': status, 'access_stop': True})
    with pytest.raises(ValueError, match='persistent_access_stop'):
        fetcher._get('https://ri.grupopanvel.com.br/new', {'source': {'url': 'https://ri.grupopanvel.com.br/'}})
    assert len(fetcher.ledger('request')) == 12


@pytest.mark.parametrize('reason', ['robots_disallow', 'authentication', 'captcha', 'denied_CDN'])
def test_explicit_v3_budget_never_overrides_persistent_policy_stop(tmp_path, reason):
    fetcher = isolated_v3_fetcher(tmp_path)
    research.save(tmp_path, 'stop', {'reason': reason})
    with pytest.raises(ValueError, match='persistent_access_stop'):
        fetcher._get('https://ri.grupopanvel.com.br/new', {'source': {'url': 'https://ri.grupopanvel.com.br/'}})
    assert len(fetcher.ledger('request')) == 12


def test_v3_cannot_resume_EZTC_even_with_claimed_PNVL_provenance(tmp_path):
    fetcher = isolated_v3_fetcher(tmp_path)
    with pytest.raises(ValueError, match='v3_PNVL_only'):
        fetcher._get('https://ri.eztec.com.br/another-path', {'source': {'url': 'https://ri.grupopanvel.com.br/'}})
    assert len(fetcher.ledger('request')) == 12


def test_v3_catalogue_request_is_exact_literal_read_only_payload(current_v3_report):
    report, _ = current_v3_report
    spec = research.pnvl_catalogue_spec(research.Fetcher(FOLDER))
    assert spec['request_json'] == {'year': '2024', 'categories': ['aviso_aos_acionistas', 'reuniao_administracao'],
                                  'language': 'pt_BR', 'published': True}
    assert spec['no_new_prohibited_CDN_request']
    assert spec['official_page_receipts'][0]['ordinal'] == 14
    assert spec['official_page_receipts'][1]['ordinal'] == 15
    assert spec['cached_transport_template_source']['sha256'] == 'd8d86b0e03f682b8797f951642a27c09258ba9e323c84057831f2fd9d443c6cb'
    assert report['catalogue_receipt']['ordinal'] == 17
    assert report['catalogue_receipt']['method'] == 'POST'
    assert report['catalogue_receipt']['request_json'] == spec['request_json']
    assert report['catalogue_receipt']['url'] == spec['url']


def test_v3_catalogue_cannot_accept_altered_payload_or_guessed_query(tmp_path, monkeypatch, current_v3_report):
    fetcher = isolated_v3_fetcher(tmp_path)
    catalogue = current_v3_report[0]['catalogue_receipt']
    spec = {'url': catalogue['url'], 'request_json': catalogue['request_json']}
    monkeypatch.setattr(research, 'pnvl_catalogue_spec', lambda unused: spec)
    payload = {**spec['request_json'], 'year': '2023'}
    with pytest.raises(ValueError, match='only_exact_literal'):
        fetcher._get(spec['url'], {'source': {'url': 'https://ri.grupopanvel.com.br/'}}, request_json=payload)
    assert len(fetcher.ledger('request')) == 12


def test_revised_selection_excludes_v1_and_is_not_amount_driven(current_v3_report):
    result = current_v3_report[0]['results'][0]
    fetcher = research.Fetcher(FOLDER)
    catalogue = current_v3_report[0]['catalogue_receipt']
    payload = json.loads(fetcher.body(catalogue))
    selected = research.select_pnvl_revised_records(payload, result['native_documents'])
    assert set(selected) == {'741697', '741696'}
    assert all(r['file_date'].startswith('2024-03-28') and r['file_published_date'].startswith('2024-04-02')
               for r in selected.values())
    older = [r for r in payload['data']['document_metas'] if r['file_date'].startswith('2024-03-28')
             and r['file_published_date'].startswith('2024-03-28')]
    assert len(older) == 2
    requested = {r['url'] for r in current_v3_report[0]['requests']}
    assert not requested.intersection(r['file_url'] for r in older)
    assert all(r['file_url'] in requested for r in selected.values())
    stripped = deepcopy(payload)
    stripped['data']['document_metas'] = [r for r in stripped['data']['document_metas'] if r['id'] not in
        {s['id'] for s in selected.values()}]
    with pytest.raises(ValueError, match='unique_exact_revised'):
        research.select_pnvl_revised_records(stripped, result['native_documents'])
    duplicated = deepcopy(payload)
    duplicated['data']['document_metas'].append(deepcopy(next(iter(selected.values()))))
    with pytest.raises(ValueError, match='unique_exact_revised'):
        research.select_pnvl_revised_records(duplicated, result['native_documents'])


def test_complete_revised_entitlement_retains_gross_tax_class_and_both_starts(current_v3_report):
    result = current_v3_report[0]['results'][0]
    assert result['complete_group_id'] == 'PNVL3:2024-04-05'
    assert result['identity']['isin'] == 'BRPNVLACNOR9'
    assert result['identity']['cvm'] == 9342
    assert result['approval_date'] == '2024-03-28' and result['record_date'] == '2024-04-05'
    assert result['kind'] == 'JCP'
    assert result['native_form']['native_protocol'] == '1216970'
    assert result['native_form']['version'] == 2
    assert result['amount_tax_basis'] == 'explicit_gross_notice_table_corroborated_by_native_gross_ISIN_row'
    assert result['net_total_per_share_decimal_not_used_as_gross'] == '0.06623569'
    installments = result['installments']
    assert len(installments) == 2
    assert [(i['scheduled_payment_date'], Decimal(i['gross_per_share_decimal'])) for i in installments] == [
        ('2024-08-30', Decimal('0.05911502')), ('2025-03-31', Decimal('0.01880932'))]
    assert sum(Decimal(i['gross_per_share_decimal']) for i in installments) == Decimal(result['gross_total_per_share_decimal'])
    for installment in installments:
        assert installment['isin'] == result['identity']['isin'] and installment['share_class'] == 'ON'
        assert installment['payment_date_basis'] == 'explicit_payment_start_date_not_settlement'
        assert not installment['full_settlement_on_date_verified']
        assert not installment['production_eligible']
    for sequence, original in result['originals'].items():
        assert original['source']['sha256'] == research.PNVL_REVISED[sequence][2]
        assert (FOLDER / original['readable_original_file']).read_bytes() == (FOLDER / original['source']['file']).read_bytes()
        assert original['catalogue_record']['file_size'] == str(original['source']['bytes'])
        assert not original['CVM_original_byte_identity_verified']
        assert original['reviewed_spans']


def test_board_prose_table_conflict_and_accounting_credit_cannot_be_promoted(current_v3_report):
    result = current_v3_report[0]['results'][0]
    assert result['conflicts'] == [{'source_sequence': '741696', 'page': 1,
        'board_narrative_second_payment_date': '2025-03-28',
        'board_table_notice_PT_and_EN_native_annex_second_payment_date': '2025-03-31',
        'unresolved': True, 'silent_typo_correction': False,
        'required_external_clarification': 'Issuer correction or unambiguous governing final document resolving board prose 28 March versus table 31 March 2025.'}]
    assert result['accounting_credit_date_not_cash_date'] == '2024-03-31'
    assert all(i['scheduled_payment_date'] != '2024-03-31' for i in result['installments'])
    assert not result['production_eligible'] and result['patches'] == []
    assert not result['settlement_verified']
    assert result['missing_later_issuer_body'] is None
    assert result['CVM_download_bodies_remain_empty_unretried'] == ['741697', '741696']
    assert current_v3_report[0]['results'][1]['exact_missing_sequences'] == ['509579']
    assert current_v3_report[0]['results'][1]['status'] == 'stopped_prohibited_loader_no_alternate_CDN_or_v3_request'


def test_v3_source_closure_hashes_all_actual_originals_indices_and_local_context(current_v3_report):
    report, _ = current_v3_report
    sources = {s['file']: s for s in report['source_receipts']}
    by_file_and_url = {(s['file'], s['url']): s for s in report['source_receipts']}
    actual_http = {s['acquisition_ordinal']: s for s in report['actual_HTTP_reader_receipts']}
    for response in report['responses']:
        source = by_file_and_url[(response['file'], response['url'])]
        assert source['sha256'] == response['sha256'] and source['url'] == response['url']
        assert actual_http[response['ordinal']]['url'] == response['url']
        assert actual_http[response['ordinal']]['sha256'] == response['sha256']
        assert actual_http[response['ordinal']]['retrieved_at'] == response['retrieved_at']
    assert len(actual_http) == 20
    # Ordinals 1 and 10 really returned the same bytes from different URLs.
    assert actual_http[1]['file'] == actual_http[10]['file']
    assert actual_http[1]['url'] != actual_http[10]['url']
    assert report['source_registration_identity'] == 'actual_file_SHA256_and_actual_URL_not_file_alone'
    for name, source in sources.items():
        assert research.exact.shared._sha((FOLDER / name).read_bytes()) == source['sha256']
        assert source['hash_basis'] == 'actual_bytes_read' and source['roles']
    assert sources[research.V2_REPORT]['url'] is None
    assert sources[report['budget_change_audit']['file']]['url'] is None
    assert sources[report['catalogue_spec_file']]['url'] is None
    assert sources[research.CACHE + '/cached-HTML-audit-81929b5e87c5ff0917afbd71e031370c6594786cee30b774ba0c31e0d2d7c6c2.json']['url'] is None
    native = report['results'][0]['native_form']
    assert sources[native['source']['file']]['url'] == native['document']['url']
    assert sources[native['document']['index_evidence']['file']]['url'].startswith('https://dados.cvm.gov.br/')
    assert report['cached_shells_are_unavailable_not_policy_denials']
    assert not report['production_files_changed'] and not report['pointers_written'] and not report['thresholds_changed']


def test_offline_report_dispatch_is_stable_and_never_changes_attempts(current_v3_report):
    before = {p.name: research.exact.shared._sha(p.read_bytes()) for prefix in ('request', 'response', 'phase-v3-budget')
              for p in (FOLDER / research.CACHE).glob(prefix + '-*.json')}
    again, path = research.build_report(FOLDER)
    assert (again, path) == current_v3_report
    after = {p.name: research.exact.shared._sha(p.read_bytes()) for prefix in ('request', 'response', 'phase-v3-budget')
             for p in (FOLDER / research.CACHE).glob(prefix + '-*.json')}
    assert after == before
