"""Read-only candidate bridge for EVERY stock in a supplied market inventory.

build_candidates(folder, inventory, cutoff) -> (candidates, excluded, sources).
The first two lists partition inventory['stocks'] by inventory_index; candidate
keys match broad_stocks.fetch_one, including date-keyed prices, DividendHistory,
ledger, training_since and financial_basis_start. This is SOURCE screening, not
financial/model approval: parent must apply exact (cvm, share_class, sector_code)
annual financial_features/stock_observations to old and new instruments alike.
sources is a list of actual read receipts with folder-relative raw/... filenames
and hashes. No cache creation, network, collector, pointer, or snapshot writes.

FCA identity supplied by the caller is authoritative for CVM/class; absent B3
detail is classified as missing independent corporate evidence, not FCA failure.
An available cached B3 detail/ISIN must agree before reconciliation can pass.
Classes are never guessed from suffixes; units remain unsupported. All cash
pages must exist. Same-issuer class caches can be reused ONLY through the exact
cached detail.otherCodes or supplied verified FCA/CVM mapping. Monthly quote-summary data and document-only
references are never promoted into historical daily prices or paid cash.

Semantic schedules retain their dates and amounts for reconciliation, including
future installments. A past scheduled date is NOT settlement confirmation.
Matched historic calendars can support retrospective modelling under the same
explicit qualification for every instrument. Unknown publication stays unknown;
payment-date observation is not labelled point-in-time publication. Inspection
cash_model_eligible flags do not approve or reject a row by themselves. No eight-event or 72-month reading requirement;
the actual 60-return window needs 61 monthly anchors, full cached pagination,
pre-window payment/right evidence and no unresolved in-window cash. Existing
independent matching and share-regime gates are retained unchanged. Source
reported PAGO is not a bank statement or an audited point-in-time archive.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from .broad_stocks import (br_day, check_b3_prices, check_cash_amounts,
                           split_price_history)
from .collect_market_data import DividendHistory, month_end, normalize, shift_month
from .payment_sources import (SOURCE_DISCOVERIES, inspect_payment_page,
                              inspect_petrobras_workbook)
from .share_basis import b3_evidence, reconcile_payments, reconcile_prices, reconcile_native_prices


class _Pending(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Pending(reason)


class _Cache:
    def safe_path(self, path):
        return (path.resolve().is_relative_to(self.folder)
                and all(not node.is_symlink() and not getattr(node, 'is_junction', lambda: False)()
                        for node in (path, *path.parents) if node.is_relative_to(self.folder)))

    def __init__(self, folder, inventory):
        self.folder = Path(folder).resolve()
        self.receipts = {}
        self.bodies = {}
        self.parsed = {}
        self.primary = None
        self.revised_calendars = {}
        self.older_calendars = {}
        self.exact_cash_calendars = {}
        self.pnvl_native_calendars = {}
        self.research_evidence_receipts = {}
        self.exact_share_class_bindings = {}
        self.older_cash_evidence = None
        self.registration = None
        self.security_registry = None
        self.registration_reference = inventory.get('as_of')
        from .security_registration import FCA_URL, SecurityRegistry
        if any(isinstance(source.get('url'), str) and re.fullmatch(
                re.escape(FCA_URL) + r'fca_cia_aberta_\d{4}\.zip', source['url'])
                for source in inventory.get('sources', [])):
            self.security_registry = SecurityRegistry(self.folder, inventory=inventory)
        registration = self.folder / 'raw/primary-research/registration/current.json'
        if registration.is_file():
            from .issuer_registration import load_registration
            self.registration = load_registration(self.folder)
        if (self.folder / 'raw/primary-research/cvm-documents/report.json').is_file():
            from .primary_cash_bridge import PrimaryCashBridge
            self.primary = PrimaryCashBridge(self.folder)
        self.known = {row['file']: row for row in inventory.get('sources', [])
                      if isinstance(row, dict) and isinstance(row.get('file'), str)}
        pointer_file = self.folder / 'current.json'
        if pointer_file.is_file():
            try:
                pointer = json.loads(pointer_file.read_text(encoding='utf-8'))
                relative = pointer.get('snapshot', '')
                if re.fullmatch(r'snapshots/[A-Za-z0-9-]+', relative):
                    metadata_file = self.folder / relative / 'assets.json'
                    if (metadata_file.resolve().is_relative_to(self.folder) and not metadata_file.is_symlink()
                            and metadata_file.stat().st_size <= 25_000_000):
                        metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
                        for source in metadata.get('sources', []):
                            name = source.get('file')
                            if isinstance(name, str):
                                name = name if name.startswith(('raw/', 'stock-selection/')) else 'raw/' + name
                                self.known.setdefault(name, {**source, 'file': name})
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.issuer_codes = defaultdict(set)
        for row in inventory['stocks']:
            ticker = row.get('ticker')
            if (type(row.get('cvm')) is int and row['cvm'] > 0
                    and row.get('identity_status') in ('official_cvm_fca', 'verified_cached_registry')
                    and isinstance(ticker, str) and re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}B?', ticker)):
                self.issuer_codes[row['cvm']].add(ticker)
        summary = 'raw/inventory/daily-summary.json'
        if (self.folder / summary).is_file():
            try:
                data = self.json(summary, 'daily_price_summary')
                for result in data.get('instruments', {}).values():
                    source = result.get('source', {})
                    relative = source.get('file')
                    if isinstance(relative, str):
                        self.known.setdefault(relative, source)
            except (OSError, ValueError, TypeError, AttributeError):
                # The parent may still be finishing its summary; individual
                # atomic daily cache files remain independently hashable.
                pass

    def read(self, relative, role):
        if relative in self.bodies:
            return self.bodies[relative]
        path = self.folder / relative
        _require(self.safe_path(path), 'unsafe_cache_path')
        _require(path.is_file(), role + '_cache_missing')
        _require(path.stat().st_size <= 25_000_000, 'cache_file_too_large')
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        known = self.known.get(relative, {})
        if known.get('sha256'):
            _require(known['sha256'] == digest, 'inventory_source_hash_mismatch')
        imported_observation = None
        sidecar = path.with_name(path.name + '.source.json')
        if sidecar.is_file() and re.fullmatch(r'raw/expanded/[A-Z0-9]+-payments\.html', relative):
            _require(self.safe_path(sidecar) and sidecar.stat().st_size <= 16_000, 'unsafe_payment_import_receipt')
            receipt = json.loads(sidecar.read_bytes())
            ticker = path.name.removesuffix('-payments.html')
            _require(receipt.get('file') == relative and receipt.get('sha256') == digest
                     and receipt.get('bytes') == len(body) and receipt.get('status') == 200
                     and receipt.get('error') is None
                     and receipt.get('url') == f'https://investidor10.com.br/acoes/{ticker.lower()}/',
                     'payment_import_receipt_disagrees')
            imported_observation = datetime.fromisoformat(receipt['retrieved_at'])
            _require(imported_observation.utcoffset() is not None
                     and imported_observation <= datetime.now(timezone.utc), 'invalid_payment_import_retrieval')
            original = receipt.get('source_original', '')
            _require(re.fullmatch(r'raw/primary-research/pending-recheck/body-[a-f0-9]{64}\.bin', original),
                     'unsafe_payment_import_original')
            source_path = self.folder / original
            _require(self.safe_path(source_path)
                     and source_path.stat().st_size == len(body)
                     and hashlib.sha256(source_path.read_bytes()).hexdigest() == digest,
                     'payment_import_original_changed')
            known = {**receipt, 'receipt_basis': 'retained_public_HTTP_response'}
        if known.get('sha256'):
            _require(known['sha256'] == digest, 'inventory_source_hash_mismatch')
        file_observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        observed = imported_observation or file_observed
        self.receipts[relative] = {'file': relative, 'sha256': digest, 'bytes': len(body),
                                   'retrieved_at': known.get('retrieved_at'), 'url': known.get('url'),
                                   'observed_at': file_observed.isoformat(),
                                   'observed_at_basis': 'local_file_mtime_not_network_retrieval',
                                   'role': role, 'access': 'existing_cache_read_only'}
        if imported_observation:
            self.receipts[relative].update(receipt_basis=known['receipt_basis'],
                source_original=known['source_original'], publication_inferred=False)
        self.bodies[relative] = body, observed
        return self.bodies[relative]

    def json(self, relative, role):
        if relative not in self.parsed:
            self.parsed[relative] = json.loads(self.read(relative, role)[0])
        return self.parsed[relative]

    def register_research_evidence(self, source):
        body, observed = self.read(source['file'], 'native_calendar_evidence')
        if hashlib.sha256(body).hexdigest() != source['sha256']:
            raise ValueError('pnvl_native_evidence_hash_mismatch')
        receipt = {**self.receipts[source['file']], **source,
            'retrieved_at': source.get('retrieved_at'),
            'observed_at': source.get('observed_at', observed.isoformat()),
            'observed_at_basis': source.get('observed_at_basis', 'local_file_mtime_not_network_retrieval')}
        key = (receipt['file'], receipt['sha256'], receipt.get('url'), receipt.get('retrieved_at'))
        self.research_evidence_receipts[key] = receipt
        if self.receipts[source['file']].get('url') in (None, source.get('url')):
            self.receipts[source['file']].update(receipt)

    def all_receipts(self):
        # Two documentary proofs may bind the same bytes/URL to different
        # retrieval observations, including an explicitly unknown date.
        proven = {key[:3] for key in self.research_evidence_receipts}
        receipts = {(r['file'], r['sha256'], r.get('url'), r.get('retrieved_at')): r
                    for r in self.receipts.values()
                    if (r['file'], r['sha256'], r.get('url')) not in proven}
        receipts.update(self.research_evidence_receipts)
        return sorted(receipts.values(), key=lambda r: (r['file'], r.get('url') or '', r.get('retrieved_at') or ''))


def enrich_calendar(parsed, primary, *, verify_source=None):
    """Attach exact primary declarations; never fill a payment date from an ex-date."""
    matched = 0
    declarations = [event for event in primary.get('events', [])
        if event['kind'] == 'cvm_declaration' and not event['issues']
        and not any(code.startswith('unresolved_') for code in event['blockers'])
        and event.get('gross_per_unit_decimal') is not None]
    for event in parsed['events']:
        amount = event.get('gross_per_share')
        if amount is None:
            continue
        matches = [declaration for declaration in declarations
            if declaration['record_date'] == event.get('record_date')
            and declaration['payment_date'] == event.get('payment_date')
            and declaration['record_date'] is not None and declaration['payment_date'] is not None
            and math.isclose(float(declaration['gross_per_unit_decimal']), amount, rel_tol=1e-9, abs_tol=1e-9)]
        if len(matches) != 1:
            continue
        declaration = matches[0]
        if verify_source is not None and not all(verify_source(ref) for ref in declaration['evidence']):
            continue
        matched += 1
        event['primary_declaration_id'] = declaration['id']
        event['primary_evidence'] = declaration['evidence']
        if event.get('published_at') is None and declaration['published_at']:
            event.update(published_at=declaration['published_at'],
                available_as_of=declaration['available_as_of'], publication_basis='exact_CVM_ISIN_amount_calendar_match')
    return matched


def _verify_primary_source(reference, cache):
    relative, expected = reference.get('file'), reference.get('sha256')
    if not isinstance(relative, str) or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        return False
    body, _ = cache.read(relative, 'primary_document')
    _require(hashlib.sha256(body).hexdigest() == expected, 'primary_document_hash_mismatch')
    receipt = cache.receipts[relative]
    if receipt.get('url') is None:
        receipt['url'] = reference.get('url')
    return True


def _identity(row, cache, cutoff):
    ticker, cvm, share = row.get('ticker'), row.get('cvm'), row.get('share_class')
    _require(isinstance(ticker, str) and re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}B?', ticker), 'unsupported_ticker_identity')
    _require(not ticker.endswith('11') and share is not None and row.get('unit_composition') is None,
             'unit_or_other_share_structure_not_reconciled')
    if share == 'PNU':
        from .schulz_share_class import class_binding
        proof = class_binding(cache.folder, row, cutoff)
        cache.exact_share_class_bindings[ticker] = proof
        for source in proof['sources']:
            cache.register_research_evidence(source)
    else:
        _require(share in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'), 'unsupported_exact_share_class')
    _require(row.get('identity_status') in ('official_cvm_fca', 'verified_cached_registry'), 'exact_issuer_identity_unresolved')
    _require(type(cvm) is int and cvm > 0, 'exact_CVM_code_unavailable')
    if row.get('listing_start'):
        _require(date.fromisoformat(row['listing_start']) <= cutoff, 'listing_started_after_cutoff')
    _require(type(row.get('sector_code')) is int and 0 <= row['sector_code'] <= 10, 'sector_code_unavailable')
    if (row['identity_status'] == 'official_cvm_fca'
            and not (cache.folder / f'raw/expanded/b3-detail-{cvm}.json').is_file()):
        return None, {}
    detail = cache.json(f'raw/expanded/b3-detail-{cvm}.json', 'identity_detail')
    _require(str(detail.get('codeCVM', '')).isdigit() and int(detail['codeCVM']) == cvm,
             'cached_B3_issuer_identity_mismatch')
    codes = {item['code']: item['isin'] for item in detail.get('otherCodes', [])}
    _require(ticker in codes and isinstance(codes[ticker], str) and re.fullmatch(r'BR[A-Z0-9]{9}\d', codes[ticker]),
             'exact_cached_B3_ISIN_unavailable')
    _require(row.get('isin') in (None, codes[ticker]), 'FCA_and_cached_B3_ISIN_disagree')
    from .preferred_class_binding import IDENTITIES as PREFERRED_IDENTITIES, class_binding as preferred_binding
    if ticker in PREFERRED_IDENTITIES:
        proof = preferred_binding(cache.folder, {**row, 'isin': codes[ticker]}, cutoff)
        cache.exact_share_class_bindings[ticker] = proof
        for source in proof['sources']:
            cache.register_research_evidence(source)
    return detail, codes


def _cash_pages(ticker, codes, cvm, cache):
    # Both mappings are exact issuer identities, never a ticker-prefix guess.
    related = set(codes) | cache.issuer_codes[cvm]
    alternate = next((code for code in [ticker, *sorted(related)]
                      if re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}B?', code)
                      and (cache.folder / f'raw/expanded/{code}-b3-cash-1.json').is_file()), None)
    _require(alternate is not None, 'independent_cash_cache_missing')
    first = cache.json(f'raw/expanded/{alternate}-b3-cash-1.json', 'independent_cash')
    pages = first.get('page', {}).get('totalPages')
    total = first.get('page', {}).get('totalRecords')
    _require(type(pages) is int and 1 <= pages <= 30 and type(total) is int and total >= 0,
             'cached_B3_cash_pagination_invalid')
    rows = []
    for page in range(1, pages + 1):
        payload = first if page == 1 else cache.json(f'raw/expanded/{alternate}-b3-cash-{page}.json', 'independent_cash')
        meta = payload.get('page', {})
        _require(meta.get('pageNumber') == page and meta.get('totalPages') == pages
                 and meta.get('totalRecords') == total and isinstance(payload.get('results'), list),
                 'cached_B3_cash_pagination_inconsistent')
        rows.extend(payload['results'])
    _require(len(rows) == total, 'cached_B3_cash_pagination_incomplete')
    return rows


def _reviewed_cash_view(identity, cache, cutoff, rows, checks):
    if identity['ticker'] == 'WLMM4' and cutoff == date(2026, 9, 30):
        from .wlm_cash2023_calendar import ProofError, canonical_independent_cash_rows
        try:
            proof = canonical_independent_cash_rows(cache.folder, identity, cutoff, rows)
        except ProofError as error:
            checks['wlm_source_class_review'] = {'status': 'blocked', 'reason': str(error)}
            raise ValueError(str(error)) from error
        for source in proof['evidence_sources']:
            cache.register_research_evidence(source)
        checks['wlm_source_class_review'] = {
            'status': 'verified_source_label_view_only', 'class_crosswalk': proof['class_crosswalk'],
            'original_cash_rows': proof['original_cash_rows'], 'cash_rows': proof['cash_rows'],
            'evidence_sources': proof['evidence_sources'], 'raw_files_changed': False,
            'normal_independent_cash_price_and_annual_checks_required': True}
        return proof['cash_rows']
    from .price_basis_research import CUTOFF, REVIEWS, reviewed_inputs
    if identity['ticker'] not in REVIEWS or cutoff != CUTOFF:
        return rows
    proof = reviewed_inputs(cache.folder, identity, cutoff)
    _require(rows == proof['original_cash_rows'] or rows == proof['cash_rows'],
             'reviewed_cash_original_rows_mismatch')
    # All later consumers must see the same typed view; original bytes and
    # their receipts remain unchanged, including unavailable-date sentinels.
    offset = 0
    for relative in proof['cash_files']:
        payload = cache.json(relative, 'independent_cash')
        count = len(payload['results'])
        cache.parsed[relative] = {**payload, 'results': proof['cash_rows'][offset:offset + count]}
        offset += count
    for receipt in proof['receipts']:
        cache.read(receipt['file'], 'reviewed_price_basis_evidence')
        if receipt.get('url'):
            cache.receipts[receipt['file']]['url'] = receipt['url']
    checks['price_source_interpretation'] = {
        'corrections': proof['corrections'], 'class_crosswalk': proof['class_proof'],
        'original_files_modified': False, 'missing_prices_created': False,
        'normal_reconciliation_required': True}
    return proof['cash_rows']


def _payments(ticker, cache, cutoff, primary=None, *, coverage_start=None, identity=None):
    relative = ('raw/income/petr-history.xlsx' if ticker in ('PETR3', 'PETR4')
                else f'raw/expanded/{ticker}-payments.html')
    body, observed = cache.read(relative, 'payments')
    _require(observed.date() >= cutoff, 'payment_source_observation_precedes_cutoff')
    inspector = inspect_petrobras_workbook if relative.endswith('.xlsx') else inspect_payment_page
    parsed = inspector(body, ticker, cutoff, observed)
    if ticker == 'SANB4':
        from .sanb4_cash_repair import apply_exact_repairs
        _require(isinstance(identity, dict), 'sanb4_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker == 'RADL3':
        from .radl_cash_repair import apply_exact_repairs
        _require(isinstance(identity, dict), 'radl3_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker == 'PLPL3':
        from .round2_plpl_cash import apply_exact_repairs
        _require(isinstance(identity, dict), 'plpl3_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker == 'BMEB4':
        from .round2_bmeb_cash import apply_exact_repairs
        _require(isinstance(identity, dict), 'bmeb4_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker == 'ALLD3':
        from .round2_cash import apply_exact_repairs
        _require(isinstance(identity, dict), 'alld3_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker == 'ETER3':
        from .round3_eter_cash import apply_exact_repairs
        _require(isinstance(identity, dict), 'eter3_exact_verified_identity_required')
        parsed = apply_exact_repairs(parsed, identity, cutoff, cache)
    if ticker in ('WLMM3', 'WLMM4') and cutoff == date(2026, 9, 30):
        from .wlm_cash2023_calendar import ProofError, repair_full_inspected_calendar
        _require(isinstance(identity, dict) and identity.get('ticker') == ticker,
                 'wlm_exact_verified_identity_required')
        try:
            parsed = repair_full_inspected_calendar(cache.folder, identity, cutoff, parsed)
        except ProofError as error:
            raise ValueError(str(error)) from error
        for source in parsed['wlm_full_calendar_repair']['evidence_sources']:
            cache.register_research_evidence(source)
    parsed['primary_declaration_matches'] = enrich_calendar(parsed, primary or {},
        verify_source=lambda reference: _verify_primary_source(reference, cache))
    _require(parsed['status'] != 'invalid', 'payment_source_invalid')
    _require(bool(parsed['events']), 'payment_calendar_not_found_no_zero_inferred')
    start = coverage_start or shift_month(cutoff, -60)
    history = DividendHistory(cutoff, len(parsed['events']), basis='payment')
    if parsed.get('exact_cash_repairs'):
        history.exact_cash_repairs = deepcopy(parsed['exact_cash_repairs'])
    if 'wlm_full_calendar_repair' in parsed:
        history.wlm_full_calendar_repair = deepcopy(parsed['wlm_full_calendar_repair'])
        history.wlm_full_calendar_repair['april2024_date_repair'] = deepcopy(parsed.get('native_calendar_repair'))
    history.installments, history.semantic_events = [], []
    # Reconcile the whole declaration even when an earlier installment precedes
    # the scenario window. Only the ledger, not the declaration sum, is truncated.
    relevant_records = {item['record_date'] for item in parsed['events']
        if item.get('record_date') and item.get('payment_date')
        and (start <= date.fromisoformat(item['record_date']) <= cutoff
             or start <= date.fromisoformat(item['payment_date']) <= cutoff)}
    oldest = None
    for item in parsed['events']:
        payment, record = item.get('payment_date'), item.get('record_date')
        _require(payment is not None, 'payment_date_unresolved_may_overlap_window')
        payment_day = date.fromisoformat(payment)
        oldest = min(oldest or payment_day, payment_day)
        if payment_day < start and (record is None or date.fromisoformat(record) < start) and record not in relevant_records:
            continue
        if not record:
            raise _Pending('payment_record_date_unresolved')
        record_day = date.fromisoformat(record)
        if record_day > cutoff:
            continue
        # Issues outside the actual return window do not create arbitrary
        # 72-month/history-count gates, but cannot silently cross its boundary.
        if (not payment or item.get('gross_per_share') is None or not item.get('date_order_valid')):
            _require(record_day < start, 'in_window_payment_date_or_amount_unresolved')
            continue
        source_row = item['source_row']
        _require(not any(issue['blocking'] and issue.get('row') == source_row
                         and issue['code'] != 'conflicting_event_amount' for issue in parsed['report']['issues']),
                 'in_window_payment_source_issue')
        history.installments.append({'ticker': ticker, 'record_date': record, 'payment_date': payment,
                                      'gross_per_unit': item['gross_per_share'], 'label': item['label'],
                                      'status': 'pending_reconciliation', 'source_row': source_row,
                                      'reported_status': item['reported_status'],
                                      'published_at': item['published_at'], 'observed_at': observed.isoformat(),
                                      'primary_evidence': item.get('primary_evidence', []),
                                      'primary_declaration_id': item.get('primary_declaration_id')})
        if item.get('income_label_normalization'):
            history.installments[-1].update(reported_income_label=item['reported_income_label'],
                income_label_normalization=item['income_label_normalization'])
        history.semantic_events.append(item)
    _require(oldest is not None and oldest <= start, 'payments_do_not_cover_common_return_window')
    _require(bool(history.installments), 'in_window_payment_calendar_unavailable')
    history.coverage_start = oldest
    history.source_observed_at = observed.isoformat()
    history.source_issues = parsed['report']['issues']
    history.primary_declaration_matches = parsed['primary_declaration_matches']
    url = cache.receipts[relative]['url']
    if url is None and relative.endswith('.xlsx') and cache.receipts[relative]['sha256'] == SOURCE_DISCOVERIES[-1]['sha256']:
        url = SOURCE_DISCOVERIES[-1]['url']
        cache.receipts[relative]['url'] = url
    return history, url


def _qualify_history(paid, ledger, original, cutoff, start):
    _require(bool(paid), 'no_verified_in_window_paid_cash')
    availability, states = {}, {}
    for event in original.semantic_events:
        payment = date.fromisoformat(event['payment_date'])
        key = event['record_date'], event['payment_date'], event['label']
        if payment <= cutoff:
            _require(event['status'] in ('paid_reported', 'past_scheduled'), 'payment_status_unusable')
            if event['published_at'] is not None:
                _require(event['available_as_of'] is True, 'payment_publication_after_cutoff')
        availability[key] = max(payment, date.fromisoformat(event['published_at'])) if event['published_at'] else payment
        states[key] = event['status']
    paid.clear()
    for item in ledger:
        key = item['record_date'], item['payment_date'], item['label']
        known = availability[key]
        _require(known <= cutoff, 'payment_availability_after_cutoff')
        item.update(status='included', observed_at=original.source_observed_at,
            reported_payment_status=states[key],
            financial_confirmation='reported_calendar_independently_amount_reconciled_not_settlement_confirmed',
            availability_basis='reported_publication_and_payment' if item.get('published_at')
                else 'payment_date_retrospective_observation_not_publication',
            publication_date_reported=item.get('published_at') is not None,
            publication_verified=bool(item.get('primary_declaration_id')), point_in_time_archive_verified=False)
        paid.append((date.fromisoformat(item['payment_date']), known, item['gross_per_unit']))
        paid.observed_payment_availability.append({'record_date': item['record_date'], 'payment_date': item['payment_date'],
            'available_at': known.isoformat(), 'observed_at': original.source_observed_at})
    paid.issues.append({'event_index': -1, 'interval_start': date.min.isoformat(),
        'interval_end': start.isoformat(), 'reason': 'outside_reconciled_payment_coverage'})
    paid.installments, paid.reconciled_events, paid.source_issues = original.installments, ledger, original.source_issues


def _one(row, cache, cutoff, checks):
    ticker, share = row['ticker'], row['share_class']
    from .unit_candidates import build_unit_candidate
    unit = build_unit_candidate(row, cache, cutoff)
    if unit is not None:
        checks.update(unit['checks'])
        return unit
    from .unit_composition import UNIT_COMPOSITIONS
    known_security = UNIT_COMPOSITIONS.get(ticker)
    _require(not known_security or known_security.status != 'not_a_unit',
             'subscription_warrant_outside_equity_income_scope')
    if cache.registration is not None:
        from .issuer_registration import registration_status
        records, receipt = cache.registration
        observed = datetime.fromisoformat(receipt['retrieved_at']).date()
        status = registration_status(records, row.get('cvm'), cache.registration_reference or cutoff,
                                     observed_on=observed)
        checks['issuer_registration'] = status
        cache.read(receipt['file'], 'current_issuer_registration')
        cache.receipts[receipt['file']]['url'] = receipt['url']
        _require(not status['registration_blocks_current_purchase'], status['status'])
    detail, codes = _identity(row, cache, cutoff)
    checks['identity'] = ('primary_FCA_exact_CVM_class' if row['identity_status'] == 'official_cvm_fca'
                          else 'verified_exact_CVM_class_ISIN')
    legacy = f'raw/expanded/{ticker}-prices.json'
    price_relative = legacy if (cache.folder / legacy).is_file() else f'raw/inventory/daily-prices/{ticker}.json'
    price_content, _ = cache.read(price_relative, 'daily_prices')
    if price_relative != legacy and not cache.receipts[price_relative]['url']:
        cache.receipts[price_relative].update(
            url=f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval=1d&events=div%2Csplits',
            url_basis='documented_collect_inventory_daily_request_template')
    payload = json.loads(price_content)['chart']['result'][0]
    timestamps = payload.get('timestamp', [])
    days = [datetime.fromtimestamp(stamp, timezone.utc).date() for stamp in timestamps]
    _require(len(set(days)) == len(days), 'duplicate_daily_price_dates')
    prices, actions = split_price_history(price_content, ticker, cutoff, allow_recent_actions=True)
    checks['prices'] = '61_consecutive_monthly_anchors'
    _require(detail is not None, 'independent_corporate_evidence_cache_missing')
    identity = {**row, 'isin': codes[ticker]}
    alternate = None
    try:
        rows = _cash_pages(ticker, codes, row['cvm'], cache)
    except _Pending as error:
        if str(error) != 'independent_cash_cache_missing':
            raise
        from .issuer_independent_evidence import load_evidence
        alternate = load_evidence(cache.folder, identity, cutoff)
        if alternate is None:
            raise
        rows = None
        for source in alternate['sources']:
            cache.register_research_evidence(source)
        checks['independent_issuer_evidence'] = alternate['checks']
    if rows is not None:
        if ticker in cache.exact_share_class_bindings:
            proof = cache.exact_share_class_bindings[ticker]
            if proof['registered_class'] == 'PNA':
                from .preferred_class_binding import cash_rows
            else:
                from .schulz_share_class import cash_rows
            rows = cash_rows(rows, proof)
            if proof['registered_class'] == 'PNA':
                for name, payload in list(cache.parsed.items()):
                    match = (re.fullmatch(r'raw/expanded/([A-Z0-9]+)-b3-cash-\d+\.json', name)
                             if isinstance(name, str) else None)
                    if match and match[1] in codes and isinstance(payload, dict) and isinstance(payload.get('results'), list):
                        cache.parsed[name] = {**payload, 'results': cash_rows(payload['results'], proof)}
            checks['exact_share_class_binding'] = proof
        rows = _reviewed_cash_view(identity, cache, cutoff, rows, checks)
    from .cash_calendar_research import CACHE as CALENDAR_CACHE, apply_research_calendar, deduplicate_independent_rows
    research_available = (cache.folder / CALENDAR_CACHE).is_dir()
    if research_available and rows is not None:
        rows, corrections = deduplicate_independent_rows(identity, cache, cutoff, rows)
        if corrections:
            checks['independent_cash_duplicate_corrections'] = corrections
    expected, quotes = ((alternate['expected_cash'], alternate['raw_quotes']) if alternate
                        else b3_evidence(rows, share))
    provider = alternate['provider'] if alternate else 'B3'
    start = shift_month(cutoff, -60)
    _require(any(day <= start for day in expected), 'independent_cash_evidence_does_not_cover_window_start')
    checks['cash_pagination'] = (alternate.get('pagination_basis', 'full_native_issuer_table')
                                  if alternate else 'complete_cached_pages')
    primary = cache.primary.primary_events({**row, 'isin': codes[ticker]}, cutoff) if cache.primary else {}
    original, payment_url = _payments(ticker, cache, cutoff, primary, identity=identity)
    if getattr(original, 'exact_cash_repairs', None):
        checks['exact_cash_repairs'] = deepcopy(original.exact_cash_repairs)
    if hasattr(original, 'wlm_full_calendar_repair'):
        checks['wlm_native_calendar_repair'] = deepcopy(original.wlm_full_calendar_repair)
    if research_available and rows is not None:
        original = apply_research_calendar(identity, cache, cutoff, original, rows, actions)
        checks['cash_calendar_research'] = {
            'sources': getattr(original, 'research_sources', []),
            'group_corrections': getattr(original, 'research_group_corrections', []),
            'settlement_confirmed': False, 'original_conflicts_retained_in_audit': True}
        native_contract = getattr(original, 'native_calendar_contract', None)
        if native_contract is not None:
            checks['cash_calendar_research'].update(
                native_calendar_contract=native_contract,
                source_conflicts=deepcopy(native_contract['source_conflicts']),
                modelling_priority_rule=deepcopy(native_contract['modelling_priority_rule']),
                source_conflict_warnings=deepcopy(original.source_conflict_warnings))
    if ticker == 'PNVL3':
        from .cash_calendar_research import validate_native_calendar_checks
        validate_native_calendar_checks(checks.get('cash_calendar_research', {}))
    checks['primary_evidence'] = primary.get('counts', {})
    checks['primary_declaration_matches'] = original.primary_declaration_matches
    checks['payment_calendar'] = 'window_spanning_reported_dates'
    recent = [action for action in actions if date.fromisoformat(action['date']) > shift_month(cutoff, -61)]
    supplement_file = f'raw/expanded/b3-supplement-{ticker[:4]}.json'
    if actions and (cache.folder / supplement_file).is_file():
        supplement = cache.json(supplement_file, 'share_actions')
        if isinstance(supplement, list):
            _require(len(supplement) == 1, 'cached_supplement_identity_ambiguous')
            supplement = supplement[0]
        for event in supplement.get('stockDividends', []):
            if event.get('isinCode') == codes[ticker]:
                day = br_day(event.get('lastDatePrior'))
                if start < day <= cutoff:
                    _require(normalize(event.get('label', '')) in ('desdobramento', 'grupamento', 'bonificacao'),
                             'non_cash_corporate_action_requires_reconciliation')
    # Always detect the raw/split-adjusted regime against independent anchors;
    # Yahoo adjusted-close/total-return fields are never used as raw prices.
    native_prices = None
    price_provider = provider
    if ticker == 'PSSA3' and cutoff == date(2026, 9, 30):
        from .issuer_quote_extension_research import get_pssa_independent_raw_quotes
        evidence = get_pssa_independent_raw_quotes(
            cache.folder, identity, cutoff, actions, quotes, price_content=price_content)
        for source in evidence['sources']:
            quote_bytes, _ = cache.read(source['file'], source['role'])
            _require(hashlib.sha256(quote_bytes).hexdigest() == source['sha256'],
                     'independent_quote_source_hash_mismatch')
            receipt = cache.receipts[source['file']]
            _require(receipt.get('url') in (None, source['url']),
                     'independent_quote_source_URL_mismatch')
            observed = receipt.get('observed_at')
            receipt.update(url=source['url'], retrieved_at=source.get('retrieved_at'),
                           observed_at=source.get('observed_at', observed),
                           observed_at_basis=source.get('observed_at_basis',
                               'local_file_mtime_not_network_retrieval'))
            for key in ('authority', 'provider', 'http_date', 'last_modified'):
                if key in source:
                    receipt[key] = source[key]
        quotes = evidence['quotes']
        checks['independent_raw_quote_evidence'] = evidence['proof']
        checks['primary_share_action_evidence'] = evidence['primary_actions']
    if ticker == 'EQTL3':
        from .round2_native_prices import load_evidence as native_price_evidence
        native_prices = native_price_evidence(cache.folder, identity, cutoff, actions, quotes, price_content)
        for source in native_prices['sources']:
            cache.register_research_evidence(source)
        checks['native_price_source'] = native_prices['proof']
    if ticker == 'BMEB4':
        from .round2_share_actions import load_evidence as action_quote_evidence
        evidence = action_quote_evidence(cache.folder, identity, cutoff, actions, quotes, price_content)
        for source in evidence['sources']:
            cache.register_research_evidence(source)
        checks['primary_share_action_evidence'] = evidence['primary_actions']
        checks['independent_raw_quote_evidence'] = evidence['checks']
        quotes, price_provider = evidence['raw_quotes'], evidence['provider']
    if ticker in ('DEXP3', 'DEXP4'):
        from .round2_dexp_prices import load_evidence as dexp_quote_evidence
        evidence = dexp_quote_evidence(cache.folder, identity, cutoff, actions, quotes, price_content)
        for source in evidence['sources']:
            cache.register_research_evidence(source)
        checks['primary_share_action_evidence'] = evidence['primary_actions']
        checks['independent_raw_quote_evidence'] = evidence['proof']
        quotes, price_provider = evidence['raw_quotes'], evidence['provider']
    if native_prices:
        prices, price_matches, price_mode = reconcile_native_prices(
            native_prices['daily'], actions, quotes, cutoff, evidence_provider=provider)
    else:
        prices, price_matches, price_mode = reconcile_prices(
            price_content, ticker, actions, quotes, cutoff, evidence_provider=price_provider)
    if alternate:
        from .issuer_independent_evidence import validate_primary_actions
        action_proof = validate_primary_actions(cache.folder, identity, cutoff, actions)
        for source in action_proof['sources']:
            cache.register_research_evidence(source)
        checks['independent_issuer_evidence']['primary_actions'] = action_proof
    comparable = defaultdict(float)
    for event in original.installments:
        comparable[date.fromisoformat(event['record_date'])] += event['gross_per_unit']
    if not recent and rows is not None:
        check_cash_amounts(rows, comparable, cutoff, share)
        if not native_prices:
            check_b3_prices(price_content, rows, cutoff, share)
    paid, ledger, adjustments = reconcile_payments(
        ticker, original, expected, actions, cutoff, evidence_provider=provider)
    checks.update(independent_cash_matches=len(adjustments), independent_price_matches=len(price_matches),
                  share_regime=price_mode,
                  corporate_action_validation='independent_cash_and_raw_price_regimes',
                  cash_history_qualification='reported_calendar_crosschecked_not_settlement_confirmed',
                  same_date_amount_ambiguities_resolved=sum(issue['code'] == 'conflicting_event_amount'
                      for issue in original.source_issues),
                  point_in_time_archive_verified=False)
    states = Counter(item['status'] for item in original.semantic_events)
    checks['payment_states'] = dict(states)
    _qualify_history(paid, ledger, original, cutoff, start)
    training_start = start
    earlier = max(min(prices), month_end(min(expected).year, min(expected).month))
    if earlier < start:
        try:
            # These recovered quote proofs cover only the required five-year window.
            _require(ticker not in ('BMEB4', 'DEXP3', 'DEXP4'), 'round2_earlier_native_quote_history_not_verified')
            extended, _ = _payments(ticker, cache, cutoff, primary, coverage_start=earlier, identity=identity)
            if research_available and rows is not None:
                extended = apply_research_calendar(identity, cache, cutoff, extended, rows, actions)
            extended_prices, extended_matches, extended_mode = reconcile_prices(
                price_content, ticker, actions, quotes, cutoff, coverage_start=earlier, evidence_provider=provider)
            extended_paid, extended_ledger, extended_adjustments = reconcile_payments(
                ticker, extended, expected, actions, cutoff, coverage_start=earlier, evidence_provider=provider)
            _qualify_history(extended_paid, extended_ledger, extended, cutoff, earlier)
            prices, paid, ledger, adjustments = extended_prices, extended_paid, extended_ledger, extended_adjustments
            price_matches, price_mode, training_start = extended_matches, extended_mode, earlier
            checks['extended_training_history'] = {'status': 'independently_reconciled', 'start': earlier.isoformat()}
        except (ValueError, KeyError, TypeError, IndexError) as error:
            reason = str(error) if re.fullmatch(r'[A-Za-z0-9_]+', str(error)) else 'earlier_history_unresolved'
            checks['extended_training_history'] = {'status': 'not_used', 'reason': reason}
    latest = max((date.fromisoformat(action['date']) for action in actions), default=date(2014, 1, 1))
    normalization = {'share_basis_date': cutoff.isoformat(), 'price_source_basis': price_mode,
                     'price_output_basis': 'cutoff_share_units_no_dividend_reinvestment',
                     'payments': adjustments, 'actions': actions, 'calendar_basis': 'reported_payment_dates_not_ex_dates'}
    return {'ticker': ticker, 'name': row.get('name') or detail['companyName'], 'cvm': row['cvm'],
            'share_class': share, 'isin': codes[ticker], 'sector_code': row['sector_code'],
            'industry_classification': detail.get('industryClassification'), 'prices': prices,
            'payments': paid, 'ledger': ledger, 'independent_cash_matches': [item['record_date'] for item in adjustments],
            'independent_price_matches': price_matches, 'share_actions': actions, 'share_normalization': normalization,
            'payment_coverage_start': start.isoformat(), 'financial_basis_start': latest.isoformat(),
            'training_since': shift_month(training_start, 12),
            'payment_url': payment_url,
            'price_url': native_prices['price_url'] if native_prices else cache.receipts[price_relative]['url'],
            'price_source_file': native_prices['price_source_file'] if native_prices else price_relative,
            **({'current_quote_source_file': price_relative} if native_prices else {}),
            'statement_spec': (row['cvm'], share, row['sector_code']), 'checks': checks,
            'readiness_scope': 'reconciled_sources_only_financial_checks_required',
            'financially_validated': False}


def build_candidates(folder, inventory, cutoff):
    """Partition all stock records; never consult model_ready as an approval."""
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    if type(cutoff) is not date or cutoff != month_end(cutoff.year, cutoff.month) or cutoff > date.today():
        raise ValueError('cutoff_must_be_past_or_current_month_end')
    if not isinstance(inventory, dict) or inventory.get('schema_version') != 1 or not isinstance(inventory.get('stocks'), list):
        raise ValueError('invalid_inventory_contract')
    stocks = inventory['stocks']
    if len(stocks) > 2000 or any(not isinstance(row, dict) for row in stocks):
        raise ValueError('invalid_inventory_stock_records')
    cache, candidates, excluded = _Cache(folder, inventory), [], []
    duplicates = Counter(row.get('ticker') if isinstance(row.get('ticker'), str) else None for row in stocks)
    for index, row in enumerate(stocks):
        checks = {}
        try:
            key = row.get('ticker') if isinstance(row.get('ticker'), str) else None
            _require(duplicates[key] == 1, 'duplicate_inventory_ticker')
            if cache.security_registry is not None:
                status = cache.security_registry.status(row, cache.registration_reference or cutoff)
                checks['security_registration'] = {field: status[field] for field in (
                    'status', 'security_type', 'type_confidence', 'cvm', 'share_class', 'isin',
                    'cvm_basis', 'isin_basis', 'market', 'listing_status', 'issues',
                    'blocks_current_purchase', 'block_reasons', 'exchange_trading_verified',
                    'whole_security_delisting_verified', 'model_admission_granted')}
                references = [item['source'] for item in status['evidence']]
                references.extend(status.get('reviewed_registry', {}).get('evidence', []))
                for source in references:
                    cache.read(source['file'], 'exact_security_registration')
                _require(status['status'] != 'conflicting_primary_or_exact_identity_evidence',
                         'conflicting_primary_security_identity')
                _require(status['share_class'] is None or row.get('share_class') == status['share_class'],
                         'conflicting_primary_security_identity')
                _require(status['security_type'] not in ('subscription_warrant', 'subscription_right'),
                         'subscription_warrant_outside_equity_income_scope')
                for reason in status['block_reasons']:
                    _require(False, reason)
            candidate = _one(row, cache, cutoff, checks)
            candidates.append({**candidate, 'inventory_index': index, 'verdict': 'candidate'})
        except _Pending as error:
            reason = str(error)
            alternative = None
            if duplicates[key] == 1 and reason not in ('cancelled_issuer_registration', 'suspended_issuer_registration',
                    'conflicting_primary_security_identity', 'subscription_warrant_outside_equity_income_scope'):
                from .primary_model_candidates import build_primary_candidate
                try:
                    alternative = build_primary_candidate(row, cache, cutoff)
                except (OSError, ValueError, KeyError, TypeError, IndexError) as alternative_error:
                    checks['primary_format_pending_reason'] = (str(alternative_error)
                        if re.fullmatch(r'[A-Za-z0-9_]+', str(alternative_error)) else 'primary_format_unresolved')
            if alternative is not None:
                alternative['checks']['alternate_route_reason'] = reason
                candidates.append({**alternative, 'inventory_index': index, 'verdict': 'candidate'})
            else:
                excluded.append({'ticker': row.get('ticker'), 'inventory_index': index, 'verdict': 'pending',
                                 'reason': reason, 'checks': checks})
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
            reason = str(error) if type(error) is ValueError and re.fullmatch(r'[A-Za-z0-9_]+', str(error)) else 'invalid_cached_source'
            excluded.append({'ticker': row.get('ticker'), 'inventory_index': index, 'verdict': 'pending',
                             'reason': reason, 'checks': checks})
    return candidates, excluded, cache.all_receipts()
