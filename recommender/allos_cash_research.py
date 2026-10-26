"""ALLOS documentary cash sidecar. No parent, financial or admission writes.

Offline analysis is separate from explicit bounded public-source acquisition.
Thirteen requests include robots, redirects and failures. No proxy, exchange,
BRAPI, denied-source retry or automatic redirect is permitted.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import parse_qs, urlsplit, urljoin

from lxml import html

from . import revised_cash_research as revised


shared = revised.shared
Reader = shared.Reader
CACHE = 'raw/primary-research/allos-cash'
MAX_REQUESTS = 13
MAX_BYTES = 12_000_000
TARGET = {'ticker': 'ALOS3', **revised.TARGETS['ALOS3']}
RI_HOSTS = {'ri.allos.co', 'ri.allos.com.br', 'www.allos.com.br', 'allos.com.br',
            'allos.co', 'www.allos.co'}


def save_body(folder, name, body):
    path = shared._safe(Path(folder).resolve(), CACHE + '/' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_allos_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(Path(folder).resolve()).as_posix()


def save(folder, prefix, payload):
    body = shared._dump(payload)
    return save_body(folder, prefix + '-' + shared._sha(body) + '.json', body)


def links(body, url):
    tree = html.fromstring(body)
    return [{'url': urljoin(url, item.get('href')), 'text': ' '.join(item.itertext()).strip()}
            for item in tree.xpath('//a[@href]')]


def approved(url, discoveries=()):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return False
        if set(parse_qs(parts.query)) & {'proxy', 'url', 'target', 'forward'}:
            return False
        if parts.hostname in RI_HOSTS:
            return True
        if parts.hostname == 'www.rad.cvm.gov.br':
            return url == shared.ROBOT_URL or bool(revised.cvm.document_id(url))
        if parts.hostname == 'api.mziq.com' and (parts.path.startswith('/mzfilemanager/') or parts.path == '/robots.txt'):
            return any((row['url'] == url or parts.path == '/robots.txt')
                       and urlsplit(row['parent_url']).hostname in RI_HOSTS
                       and urlsplit(row['url']).hostname == 'api.mziq.com'
                       for row in discoveries)
        return False
    except (ValueError, TypeError):
        return False


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class Fetcher:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoFollow())

    def ledger(self, prefix):
        reader = Reader(self.folder)
        return [reader.json(p.relative_to(self.folder).as_posix(), 'allos_network_' + prefix)[0]
                for p in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def _get(self, url):
        if not approved(url, self.ledger('discovery')):
            raise ValueError('unapproved_URL_no_request_sent')
        attempts, responses = self.ledger('request'), self.ledger('response')
        if any(r.get('http_status') in (401, 403, 429) for r in responses):
            raise ValueError('access_stop_no_request_sent')
        if any(r['url'] == url for r in attempts):
            raise ValueError('URL_already_attempted_no_retry')
        if len(attempts) >= MAX_REQUESTS:
            raise ValueError('allos_request_budget_exhausted')
        attempt = {'url': url, 'ordinal': len(attempts) + 1,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'proxy': False, 'automatic_redirects': False, 'retries': 0}
        reservation = shared._safe(self.folder, f'{CACHE}/request-{len(attempts) + 1:02d}.json')
        reservation.parent.mkdir(parents=True, exist_ok=True)
        with reservation.open('xb') as stream:
            stream.write(shared._dump(attempt))
        body = b''
        result = {**attempt, 'http_status': None, 'status': 'failed'}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': shared.USER_AGENT,
                'Accept': 'text/html,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/plain',
                'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                result.update(http_status=response.status, content_type=response.headers.get('Content-Type'))
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('response_size_limit')
            result['status'] = 'received'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, location=error.headers.get('Location'),
                          content_type=error.headers.get('Content-Type'), error=type(error).__name__)
            body = error.read(MAX_BYTES + 1)[:MAX_BYTES]
        except (OSError, ValueError) as error:
            result['error'] = str(error)
            body = body[:MAX_BYTES]
        digest = shared._sha(body)
        result.update(sha256=digest, bytes=len(body), retrieved_at=datetime.now(timezone.utc).isoformat())
        suffix = 'pdf' if body.lstrip().startswith(b'%PDF-') else 'xlsx' if body.startswith(b'PK\x03\x04') else 'bin'
        result['file'] = save_body(self.folder, f'body-{digest}.{suffix}', body)
        save(self.folder, 'response', result)
        return body, result

    def acquire(self, url):
        if not approved(url, self.ledger('discovery')):
            raise ValueError('unapproved_URL_no_request_sent')
        if any(r.get('http_status') in (401, 403, 429) for r in self.ledger('response')):
            raise ValueError('access_stop_no_request_sent')
        if urlsplit(url).hostname == 'www.rad.cvm.gov.br':
            documents, _, _ = allos_index(Reader(self.folder))
            if not any(document['url'] == url for document in documents):
                raise ValueError('exact_ALLOS_index_document_required')
        robot_url = urlsplit(url)._replace(path='/robots.txt', query='', fragment='').geturl()
        robots = next((row for row in self.ledger('response') if row['url'] == robot_url), None)
        if robots is None:
            body, robots = self._get(robot_url)
        else:
            body, _ = Reader(self.folder).read(robots['file'], 'robots', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(robot_url)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(shared.USER_AGENT, url):
                raise ValueError('robots_disallow_no_document_request_sent')
            if parser.crawl_delay(shared.USER_AGENT) or parser.request_rate(shared.USER_AGENT):
                raise ValueError('robots_rate_requires_spacing')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_access_denied')
        body, receipt = self._get(url)
        if receipt['status'] == 'received' and 'html' in (receipt.get('content_type') or ''):
            for item in links(body, url):
                if urlsplit(item['url']).hostname in RI_HOSTS | {'api.mziq.com'}:
                    save(self.folder, 'discovery', {**item, 'parent_url': url,
                         'parent_file': receipt['file'], 'parent_sha256': receipt['sha256']})
        return receipt


def allos_index(reader):
    documents, periods, issues = shared.scan_indices(reader, [TARGET])
    if issues:
        raise ValueError('native_ALLOS_IPE_scan_incomplete:' + str(issues))
    return documents, periods, issues


def original_facts(reader, documents):
    review, receipt = reader.json(revised.CACHE + '/reviewed-notices.json', 'existing_review_read_only')
    if receipt['sha256'] != revised.REVIEW_SHA256:
        raise ValueError('parent_review_manifest_hash_mismatch')
    facts = {}
    for sequence in ('841501', '841533', '842767'):
        choices = [d for d in documents if d['sequence'] == sequence and d['cvm'] == TARGET['cvm']]
        reviews = [r for r in review['documents'] if r['sequence'] == sequence]
        if len(choices) != 1 or len(reviews) != 1:
            raise ValueError('exact_ALLOS_document_and_review_required')
        document = choices[0]
        body, source = revised.document_body(reader, document)
        if body is None:
            raise ValueError('existing_primary_ALLOS_original_missing')
        facts[sequence] = revised.reviewed_facts(revised.pdf_pages(body), reviews[0], document, source, TARGET)
    return facts


def documentary_relationship(facts):
    """Link a unique JCP installment by issuer, publication, class and period.

    No market comparator or future amount is accepted as a linking argument.
    The amended value and the incompatible printed year remain source facts.
    """
    board, notice, amendment = (facts[key] for key in ('841501', '841533', '842767'))
    for observation in (board, notice, amendment):
        row = observation['source']['index_evidence']['raw_row']
        if (int(row['Codigo_CVM']) != TARGET['cvm']
                or re.sub(r'\D', '', row['CNPJ_Companhia']) != TARGET['cnpj']):
            raise ValueError('exact_ALLOS_CVM_CNPJ_relationship_required')
    base, initial, revised_fields = board['fields'], notice['fields'], amendment['fields']
    if (base['record_date'] != TARGET['record_date'] or base['period_end'] != TARGET['period_end']
            or base['share_class'] != TARGET['share_class']
            or any(row['label'] != 'JCP' for row in (base, initial, revised_fields))):
        raise ValueError('exact_JCP_class_record_and_period_required')
    if (notice['published_at'] != board['published_at']
            or revised_fields.get('revision_of_publication') != notice['published_at']
            or amendment['published_at'] <= notice['published_at']
            or not revised_fields.get('revision_language')):
        raise ValueError('explicit_notice_revision_relationship_required')
    if (any(initial[k] != base[k] for k in ('record_date', 'payment_date'))
            or Decimal(initial['amount']) != Decimal(base['amount'])):
        raise ValueError('base_board_and_notice_disagree')
    return {'cvm': TARGET['cvm'], 'cnpj': TARGET['cnpj'], 'share_class': base['share_class'],
        'record_date': base['record_date'], 'period_end': base['period_end'], 'label': 'JCP',
        'base_publication': notice['published_at'], 'amendment_publication': amendment['published_at'],
        'base_gross_per_share_decimal': base['amount'],
        'amended_gross_per_share_decimal': revised_fields['amount'],
        'board_scheduled_payment_date': base['payment_date'],
        'amendment_printed_payment_date': revised_fields['payment_date'],
        'year_conflict': base['payment_date'] != revised_fields['payment_date'],
        'linking_basis': 'explicit_named_base_notice_single_JCP_clause_and_primary_board_period',
        'selection_uses_market_amount': False, 'date_values_repaired': False,
        'ISIN_printed_in_prose_notices': False}


def identity_evidence(reader):
    """FCA class evidence plus explicitly labelled existing exact-code ISIN cache."""
    from .security_registration import parse_fca_registry
    pointer, _ = reader.json('inventory/current.json', 'identity_inventory_pointer_read_only')
    inventory, original = reader.json('inventory/' + pointer['snapshot'] + '/market.json', 'identity_inventory')
    if original['sha256'] != pointer['sha256']:
        raise ValueError('inventory_pointer_hash_mismatch')
    stocks = [row for row in inventory['stocks'] if row['ticker'] == TARGET['ticker']]
    if len(stocks) != 1 or any(stocks[0].get(k) != TARGET[k] for k in ('cvm', 'isin', 'share_class')):
        raise ValueError('exact_ALLOS_inventory_identity_required')
    fca = [row for row in inventory['sources'] if row.get('url') ==
           'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip']
    if len(fca) != 1:
        raise ValueError('exact_primary_FCA_source_required')
    body, source = reader.read(fca[0]['file'], 'primary_FCA_identity', fca[0]['sha256'], fca[0]['url'])
    masters, securities = parse_fca_registry(body, 2026)
    rows = [row for row in securities if row['Codigo_Negociacao'] == TARGET['ticker']
            and re.sub(r'\D', '', row['CNPJ_Companhia']) == TARGET['cnpj']]
    if len(rows) != 1 or revised.cvm.norm(rows[0]['Valor_Mobiliario']) != 'acoes ordinarias':
        raise ValueError('exact_ALLOS_FCA_ordinary_class_required')
    filings = [row for row in masters if row['ID_DOC'] == rows[0]['ID_Documento']]
    if (len(filings) != 1 or int(filings[0]['CD_CVM']) != TARGET['cvm']
            or filings[0]['DT_RECEB'] > revised.CUTOFF):
        raise ValueError('exact_ALLOS_FCA_CVM_required')
    detail, cached = reader.json('raw/expanded/b3-detail-22357.json', 'existing_identity_only_after_primary_selection')
    codes = [row for row in detail['otherCodes'] if row['code'] == TARGET['ticker']]
    if (str(detail['codeCVM']) != str(TARGET['cvm']) or detail['cnpj'] != TARGET['cnpj']
            or len(codes) != 1 or codes[0]['isin'] != TARGET['isin']):
        raise ValueError('exact_cached_ISIN_mapping_required')
    return {'primary_FCA_source': source, 'FCA_security_row': rows[0], 'FCA_master_row': filings[0],
        'ISIN': TARGET['isin'], 'ISIN_basis': 'existing_exact_code_cache_not_reported_in_FCA_or_prose',
        'cached_mapping': cached, 'contemporaneous_archive_verified': False}


def protected_state(folder):
    root = Path(folder).resolve()
    code = Path(__file__).parent
    paths = [root / 'current.json', root / 'inventory/current.json',
             code.parent / 'lib/recommendation-revision.ts',
             *(code / name for name in ('revised_cash_research.py', 'test_revised_cash_research.py',
                'cash_calendar_research.py', 'inventory_candidates.py', 'cvm_fundamentals.py',
                'issuer_bank_sources.py', 'market_inventory.py', 'prepare_inventory.py',
                'collect_market_data.py'))]
    return {str(path): shared._sha(path.read_bytes()) for path in paths if path.is_file()}


def documentary_sidecar(folder):
    """Pure offline result; an unavailable RI history is not zero cash evidence."""
    before = protected_state(folder)
    reader = Reader(folder)
    documents, periods, _ = allos_index(reader)
    facts = original_facts(reader, documents)
    relationship = documentary_relationship(facts)
    identity = identity_evidence(reader)
    fetcher = Fetcher(folder)
    attempts = sorted(fetcher.ledger('request'), key=lambda row: row['ordinal'])
    responses = sorted(fetcher.ledger('response'), key=lambda row: row['ordinal'])
    for response in responses:
        reader.read(response['file'], 'retrieval_response_not_cash_evidence', response['sha256'], response['url'])
    denied = [r for r in responses if r.get('http_status') in (401, 403, 429)]
    cached_later = []
    for document in documents:
        if document['published_at'] <= facts['842767']['published_at']:
            continue
        body, source = revised.document_body(reader, document)
        if body is None:
            continue
        result = {'sequence': document['sequence'], 'published_at': document['published_at'],
            'index_evidence': document['index_evidence'], 'source': source,
            'clarifies_target_event': False}
        if document['category'] == 'relatorio proventos':
            native = revised.native_observation(body, document, source, TARGET)
            result.update(record_date=native['record_date'], reference_period=native['reference_period'],
                          fiscal_year=native['fiscal_year'], issues=native['issues'])
            result['exclusion_reason'] = ('different_actual_record_not_IPE_reference' if
                native['record_date'] != TARGET['record_date'] else 'native_form_not_a_paid_history_or_clarifying_notice')
        else:
            result['exclusion_reason'] = 'no_reviewed_explicit_target_payment_clarification'
        cached_later.append(result)
    after = protected_state(folder)
    if before != after:
        raise ValueError('parent_state_changed_during_sidecar_read')
    return {'schema': 'allos-documentary-cash-sidecar-v1', 'target': TARGET,
        'created_at': datetime.now(timezone.utc).isoformat(), 'parent_reference_date': revised.CUTOFF,
        'network_observed_at_not_backdated': True,
        'research_status': 'stopped_access_denied' if denied else 'no_new_primary_history_verified',
        'relationship': relationship, 'identity': identity, 'original_facts': facts,
        'documentary_payment_date': None, 'documentary_payment_effected_verified': False,
        'adopted_new_primary_source': None, 'new_RI_payment_rows': None,
        'remaining_conflicts': [{'code': 'primary_payment_year_conflict',
            'record_date': TARGET['record_date'], 'board_sequence': '841501',
            'board_payment_date': relationship['board_scheduled_payment_date'],
            'amendment_sequence': '842767',
            'amendment_printed_payment_date': relationship['amendment_printed_payment_date'],
            'no_later_primary_history_or_clarification_verified': True}],
        'cash_amount_selection_completed_before_market_comparison': True,
        'market_cash_comparator_read': False,
        'index_periods': periods, 'later_ALLOS_index_candidates': [d for d in documents
            if d['published_at'] > facts['842767']['published_at']],
        'cached_index_selection_cutoff': revised.CUTOFF,
        'current_or_post_cutoff_CVM_coverage_asserted': False,
        'later_cached_originals': cached_later,
        'network': {'maximum': MAX_REQUESTS, 'attempts_used': len(attempts), 'attempts': attempts,
            'responses': responses, 'stopped_on_access_denial': bool(denied),
            'includes_robots_redirects_and_failures': True, 'proxies_used': False,
            'automatic_redirects_followed': False, 'retries': 0, 'TLS_verification_disabled': False,
            'denial_sources': [{'url': r['url'], 'http_status': r['http_status'],
                               'file': r['file'], 'sha256': r['sha256']} for r in denied]},
        'sources': list(reader.sources.values()),
        'implementation': {str(path): shared._sha(path.read_bytes()) for path in
            (Path(__file__), Path(__file__).with_name('test_allos_cash_research.py')) if path.is_file()},
        'protected_files': {'before': before, 'after': after, 'unchanged_during_execution': True},
        'parent_model_changed': False, 'financials_changed': False, 'cash_model_eligible': False,
        'settlement_verified': False, 'bank_settlement_certified': False,
        'point_in_time_archive_verified': False, 'history_complete': False,
        'missing_sources_are_not_zero_payments': True, 'ex_dates_used_as_payment': False}


def build_report(folder):
    result = documentary_sidecar(folder)
    return save(folder, 'report', result), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', help='One exact public URL; explicit opt-in network')
    parser.add_argument('--inspect', help='Inspect an acquired response by exact URL, offline')
    parser.add_argument('--network-status', action='store_true', help='Offline network ledger only')
    args = parser.parse_args()
    fetcher = Fetcher(args.data_dir)
    if args.acquire:
        print(json.dumps(fetcher.acquire(args.acquire)), flush=True)
    elif args.inspect:
        receipt = next(r for r in fetcher.ledger('response') if r['url'] == args.inspect)
        body, _ = Reader(args.data_dir).read(receipt['file'], 'inspection', receipt['sha256'])
        print(json.dumps({'response': receipt, 'links': links(body, args.inspect)}, ensure_ascii=True))
    elif args.network_status:
        print(json.dumps({'attempts': fetcher.ledger('response'), 'maximum': MAX_REQUESTS}))
    else:
        path, result = build_report(args.data_dir)
        print(json.dumps({'report': path, 'status': result['research_status'],
            'requests': result['network']['attempts_used'],
            'documentary_payment_date': result['documentary_payment_date'],
            'remaining_conflicts': result['remaining_conflicts']}))


if __name__ == '__main__':
    main()
