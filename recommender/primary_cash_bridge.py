"""Offline primary evidence, complementary to the parent's independent B3 checks.

primary_events(folder, stock, cutoff) reads a market-data folder, not pointers.
For inventory builds, instantiate PrimaryCashBridge(folder) ONCE and call
loader.primary_events(stock, cutoff). JSON bytes are read/hashed once, indexed
by CVM; there are no per-stock file reads or PDF reads. Each loader is a frozen
build snapshot; construct a new instance (or call refresh) for a new build.
stock supplies exact ticker/CVM and optionally ISIN/share_class; neither is
inferred. cutoff is an ISO day or datetime.date. Future publications are omitted,
future schedules retained, and unknown publication/availability stays None.

The JSON-compatible result contains events, sources (actual JSON read receipts),
diagnostics, conflicts and unmapped_installment_rows. Amounts are Decimal strings
in the units reported by each source. CVM ISIN-unit amounts are never converted
to per-share amounts. Declaration and RI event lists are NOT additive cash.
Evidence retains document versions, original fields and report-asserted hashes;
these hashes do not claim this reader verified the original PDF/HTML bytes.

The registry confirms ticker/CVM for RI reports without CVM. It is read from
folder/primary-evidence.json, or the project's sibling
work/primary_data_deep_20261024/primary-evidence.json for the canonical
<project>/recommender/data/market layout. Its consolidated facts/observations
are audit context, never additional events or an identity/ISIN fallback.
There are no imports of collectors, network calls, writes or model approvals.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re


_PRIMARY = Path('raw/primary-research')
_DATES = ('record_date', 'approval_date', 'payment_date', 'ex_date')
_AMOUNTS = ('gross_per_unit_decimal', 'gross_per_share', 'net_per_share',
            'reported_per_share', 'reported_per_unit')
_MAX_BYTES = 100_000_000


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def _id(prefix, value):
    return prefix + ':' + hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return format(amount, 'f') if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _cvm(value):
    return value if type(value) is int and value > 0 else None


def _codes(issues):
    return [item if isinstance(item, str) else item.get('code', 'source_issue')
            for item in (issues or []) if isinstance(item, (str, dict))]


def _unique(items):
    return list({_json(item): item for item in items}.values())


def _validate_report(payload, role):
    def objects(container, field):
        rows = container.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('invalid_report_rows:' + field)
        return rows

    if role == 'primary_registry':
        objects(payload, 'instruments')
    elif role == 'cvm_declarations':
        for document in objects(payload, 'results'):
            objects(document, 'events')
            objects(document, 'unmapped_installment_rows')
    else:
        objects(payload, 'sources')
        if role == 'issuer_bank_RI':
            results = objects(payload, 'results') + objects(payload, 'declaration_documents')
        else:
            instruments = payload.get('instruments', {})
            if not isinstance(instruments, dict) or any(not isinstance(r, dict) for r in instruments.values()):
                raise ValueError('invalid_report_instruments')
            results = list(instruments.values())
        for result in results:
            objects(result, 'events')
            alternatives = result.get('alternative_observations', {})
            if not isinstance(alternatives, dict) or any(not isinstance(r, dict) for r in alternatives.values()):
                raise ValueError('invalid_report_alternatives')
            for alternative in alternatives.values():
                objects(alternative, 'events')


class _Reader:
    def __init__(self, folder):
        self.folder = folder
        self.sources = []
        self.diagnostics = []

    def issue(self, code, **context):
        self.diagnostics.append({'code': code, **context})

    def read(self, path, role, *, external=False):
        relative = path.as_posix() if external else path.relative_to(self.folder).as_posix()
        try:
            if path.is_symlink() or (not external and not path.resolve().is_relative_to(self.folder)):
                raise ValueError('unsafe_report_path')
            if path.stat().st_size > _MAX_BYTES:
                raise ValueError('oversized_report')
            body = path.read_bytes()
            receipt = {'file': relative, 'sha256': hashlib.sha256(body).hexdigest(),
                       'bytes': len(body), 'role': role, 'access': 'existing_cache_read_only'}
            self.sources.append(receipt)
            payload = json.loads(body, parse_constant=self._invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError('report_object_required')
            _validate_report(payload, role)
            return payload, receipt
        except (OSError, ValueError) as error:
            self.issue('report_unavailable_or_invalid', file=relative, role=role,
                       error=type(error).__name__)
            return {}, None

    @staticmethod
    def _invalid_constant(value):
        raise ValueError('nonfinite_json_number:' + value)


def _registry_path(folder):
    local = folder / 'primary-evidence.json'
    if local.exists():
        return local
    if folder.parent.name == 'data' and folder.parent.parent.name == 'recommender':
        return folder.parents[3] / 'work/primary_data_deep_20261024/primary-evidence.json'
    return local


def _reference(source, receipt, row, *, declaration=False, alternative=None):
    """Hashes on references are assertions in a hashed report, not byte checks."""
    return {'report_file': receipt['file'], 'report_sha256': receipt['sha256'],
            'source_ref': row.get('source_ref', source.get('id')),
            'url': row.get('source_url') or source.get('url'), 'file': source.get('file'),
            'sha256': source.get('sha256'), 'hash_basis': 'report_assertion',
            'published_at': _day(row.get('published_at') or source.get('published_at')),
            'retrieved_at': source.get('retrieved_at'),
            'sequence': source.get('sequence'), 'version': source.get('version'),
            'locator': deepcopy(row.get('source_locator') or {
                'table': row.get('source_table'), 'row': row.get('source_row')}),
            'alternative': alternative, 'declaration_only': declaration,
            'raw_event': deepcopy(row)}


def _normalize(row, stock, kind, reference, cutoff):
    issues = _codes(row.get('issues', []))
    item = {'kind': kind, 'ticker': stock['ticker'], 'cvm': stock['cvm'],
            # A ticker/CVM RI link does not establish an ISIN or a class.
            'isin': row.get('isin'), 'share_class': row.get('share_class'),
            'label': row.get('label'), 'amount_unit': row.get('amount_unit') or
                ('BRL/ISIN-unit' if kind == 'cvm_declaration' else 'BRL/share'),
            'amount_tax_basis': row.get('amount_tax_basis') or
                ('explicit_gross' if kind == 'cvm_declaration' or row.get('gross_per_share') is not None
                 else 'explicit_net' if row.get('net_per_share') is not None else 'unspecified_by_table'),
            'published_at': reference['published_at'], 'publication_basis': None,
            'payment_status_observed_at': row.get('payment_status_observed_at') or row.get('observed_at'),
            'payment_claim_basis': row.get('payment_claim_basis'),
            'share_basis': row.get('share_basis'), 'share_basis_date': _day(row.get('share_basis_date')),
            'issues': issues, 'evidence': [reference], 'declaration_matches': [],
            'cash_model_eligible': False, 'history_complete': False,
            'is_settlement_confirmation': False, 'paid_status_as_of_verified': False,
            'payment_status_available_as_of': None,
            'gross_reconstructed_from_net': False}
    for field in _DATES:
        item[field] = _day(row.get(field))
        if row.get(field) is not None and item[field] is None:
            issues.append('invalid_' + field)
    for field in _AMOUNTS:
        value = row.get(field)
        if field == 'gross_per_unit_decimal' and value is None:
            value = row.get('gross_per_unit')
        item[field] = _decimal(value)
        if value is not None and item[field] is None:
            issues.append('invalid_' + field)
    if kind == 'cvm_declaration':
        item['gross_per_share'] = None
    if 'net' in item['amount_tax_basis'] and 'gross' not in item['amount_tax_basis']:
        if item['gross_per_share'] is not None:
            issues.append('gross_conflicts_with_explicit_net_basis')
        item['gross_per_share'] = None
    payment = item['payment_date']
    reported_paid = (kind == 'issuer_ri_history' and not reference['declaration_only']
                     and row.get('status') == 'paid_reported')
    item['status'] = ('paid_reported' if reported_paid else 'invalid' if row.get('status') == 'invalid'
                      else 'announced' if payment and payment > cutoff
                      else 'past_scheduled' if payment else 'date_unresolved')
    if reported_paid and payment and payment > cutoff:
        issues.append('paid_claim_for_future_payment')
    if reference['published_at']:
        item['publication_basis'] = 'source_publication'
    # A previously enriched bank calendar must be rematched to the actual notice.
    if row.get('declaration_source_ref'):
        item['published_at'] = item['publication_basis'] = None
    item['available_as_of'] = True if item['published_at'] else None
    item['availability_date'] = item['published_at']
    item['publication_verified'] = item['published_at'] is not None
    item['blockers'] = sorted(set(issues + ['independent_validation_required', 'share_basis_unverified']
        + ([] if payment else ['payment_date_unresolved'])
        + ([] if any(item[field] is not None for field in _AMOUNTS) else ['amount_unresolved'])
        + ([] if item['published_at'] else ['publication_unresolved'])
        + (['payment_status_historical_availability_unverified'] if reported_paid
           else ['payment_not_confirmed'])))
    return item


def _event_key(event):
    fields = ('kind', 'ticker', 'cvm', 'isin', 'share_class', 'label', *_DATES,
              *_AMOUNTS, 'amount_unit', 'amount_tax_basis')
    # Decimal scale is metadata on evidence, not a second economic fact.
    key = {field: event[field] for field in fields}
    for field in _AMOUNTS:
        value = key[field]
        if value is not None:
            # Decimal.normalize() rounds beyond the active context's precision.
            key[field] = '0' if Decimal(value) == 0 else value.rstrip('0').rstrip('.') if '.' in value else value
    return key


def _aggregate(events):
    grouped = {}
    for event in events:
        identity = _id('primary', _event_key(event))
        if identity not in grouped:
            grouped[identity] = {**event, 'id': identity}
            continue
        previous = grouped[identity]
        previous['evidence'] = _unique(previous['evidence'] + event['evidence'])
        previous['issues'] = sorted(set(previous['issues'] + event['issues']))
        previous['blockers'] = sorted(set(previous['blockers'] + event['blockers']))
        # Keep every original status in evidence; the scalar cannot manufacture paid.
        if previous['status'] != event['status']:
            previous['issues'] = sorted(set(previous['issues'] + ['conflicting_source_status']))
            previous['blockers'] = sorted(set(previous['blockers'] + ['conflicting_source_status']))
            previous['status'] = 'unresolved'
        publications = [value for value in (previous['published_at'], event['published_at']) if value]
        previous['published_at'] = min(publications, default=None)
        previous['available_as_of'] = True if previous['published_at'] else None
        previous['availability_date'] = previous['published_at']
        previous['publication_verified'] = previous['published_at'] is not None
        if previous['published_at']:
            previous['publication_basis'] = 'source_publication'
            previous['blockers'] = [code for code in previous['blockers'] if code != 'publication_unresolved']
    return sorted(grouped.values(), key=lambda event: event['id'])


def _conflicts(events):
    """Candidate conflicts are blockers, never an automatic revision selection."""
    groups = defaultdict(list)
    for event in events:
        security = (event['kind'], event['isin'], event['share_class'], event['label'])
        if event['approval_date'] or event['record_date']:
            key = (*security, 'rights', event['approval_date'], event['record_date'])
            groups[key].append(event)
        # Two equal dates can anchor a revision that changes the third date.
        for left, right in (('approval_date', 'payment_date'), ('record_date', 'payment_date')):
            if event[left] and event[right]:
                groups[(*security, left, right, event[left], event[right])].append(event)
    conflicts = []
    for group in groups.values():
        if len(group) < 2:
            continue
        fields = (*_DATES, *_AMOUNTS)
        differing = [field for field in fields if len({_json(_event_key(e)[field]) for e in group}) > 1]
        if not differing:
            continue
        conflict = {'code': 'unresolved_declaration_variants' if group[0]['kind'] == 'cvm_declaration'
                    else 'unresolved_RI_variants', 'fields': differing,
                    'event_ids': sorted(event['id'] for event in group)}
        conflict['id'] = _id('conflict', conflict)
        conflicts.append(conflict)
        for event in group:
            event['blockers'] = sorted(set(event['blockers'] + [conflict['code']]))
    return _unique(conflicts)


def _compatible(history, declaration, stock):
    if history['issues'] or declaration['issues'] or any(
            code.startswith('unresolved_') for code in declaration['blockers']):
        return False
    if (not history['payment_date'] or history['payment_date'] != declaration['payment_date']
            or history['cvm'] != declaration['cvm']):
        return False
    if any(history[field] and declaration[field] and history[field] != declaration[field]
           for field in ('record_date', 'approval_date', 'ex_date')):
        return False
    if declaration['kind'] == 'cvm_declaration':
        # ISIN units and shares are comparable only for an explicitly identified share.
        if (not stock.get('isin') or declaration['isin'] != stock['isin']
                or stock.get('share_class') not in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND')
                or stock.get('unit_composition') is not None
                or history['gross_per_share'] is None):
            return False
        amount = declaration['gross_per_unit_decimal']
        reported = history['gross_per_share']
    else:
        if (history['ticker'] != declaration['ticker'] or not history['record_date']
                or history['record_date'] != declaration['record_date'] or not history['label']
                or history['label'] != declaration['label']
                or history['share_class'] != declaration['share_class']
                or 'net' in history['amount_tax_basis'] and 'gross' not in history['amount_tax_basis']):
            return False
        amount = declaration['gross_per_share']
        reported = history['gross_per_share'] or history['reported_per_share']
    return amount is not None and reported is not None and Decimal(amount) == Decimal(reported)


def _publications(events, stock, reader):
    declarations = [event for event in events if event['kind'] in ('cvm_declaration', 'issuer_ri_declaration')]
    for event in events:
        if event['kind'] != 'issuer_ri_history' or event['published_at'] is not None:
            continue
        matches = [candidate for candidate in declarations if candidate['published_at']
                   and _compatible(event, candidate, stock)]
        # The same notice in several ledgers is one aggregated declaration.
        if len(matches) != 1:
            if len(matches) > 1:
                event['blockers'] = sorted(set(event['blockers'] + ['ambiguous_publication_match']))
                reader.issue('ambiguous_publication_match', event_id=event['id'],
                             candidates=[candidate['id'] for candidate in matches])
            continue
        matched = matches[0]
        event.update(published_at=matched['published_at'], available_as_of=True,
                     availability_date=matched['published_at'], publication_verified=True,
                     publication_basis='unique_primary_declaration_match',
                     declaration_matches=[matched['id']])
        event['blockers'] = [code for code in event['blockers'] if code != 'publication_unresolved']


class PrimaryCashBridge:
    """One read-only, explicitly scoped snapshot per build, with no global cache.

    sources records the hash of every JSON actually read. refresh() starts a new
    snapshot and recomputes hashes; an existing build cannot mix file revisions.
    """

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.refresh()

    def refresh(self):
        reader = _Reader(self.folder)
        self.registry, _ = reader.read(_registry_path(self.folder), 'primary_registry', external=True)
        report, _ = reader.read(self.folder / _PRIMARY / 'cvm-documents/report.json', 'cvm_declarations')
        self.report_receipt = next((s for s in reader.sources if s['role'] == 'cvm_declarations'), None)
        self.documents_by_cvm = defaultdict(list)
        documents = report.get('results', [])
        for document in documents:
            self.documents_by_cvm[document.get('cvm')].append(document)
        self.document_count = len(documents)
        self.event_row_count = sum(len(doc.get('events', [])) for doc in documents)
        self.registry_by_ticker = defaultdict(list)
        for row in self.registry.get('instruments', []):
            self.registry_by_ticker[row.get('ticker')].append(row)
        self.histories = []
        for subdir, pattern, role in (
                ('operating', 'operating-sources-*.json', 'issuer_operating_RI'),
                ('banks', 'ledger-*.json', 'issuer_bank_RI')):
            for path in sorted((self.folder / _PRIMARY / subdir).glob(pattern)):
                payload, receipt = reader.read(path, role)
                if receipt is None or not payload:
                    continue
                sources = {}
                for source in payload.get('sources', []):
                    for key in (source.get('id'), source.get('sha256')):
                        if key:
                            sources[key] = source
                by_cvm = defaultdict(list)
                if subdir == 'banks':
                    declarations = payload.get('declaration_documents', [])
                    for result, declaration in ([(r, False) for r in payload.get('results', [])]
                                                 + [(r, True) for r in declarations]):
                        source = result.get('source', {})
                        if source.get('sha256'):
                            sources[source['sha256']] = source
                        by_cvm[result.get('cvm')].append((result, declaration))
                self.histories.append((role, payload.get('instruments', {}), by_cvm, sources, receipt))
        self.sources = reader.sources
        self.input_diagnostics = reader.diagnostics
        return self

    def primary_events(self, stock, cutoff):
        return _primary_events(self, stock, cutoff)


def primary_events(folder, stock, cutoff, *, loader=None):
    """Convenience API; supply loader=PrimaryCashBridge(folder) to reuse a build."""
    if loader is None:
        loader = PrimaryCashBridge(folder)
    elif not isinstance(loader, PrimaryCashBridge) or loader.folder != Path(folder).resolve():
        raise ValueError('loader_folder_mismatch')
    return loader.primary_events(stock, cutoff)


def _primary_events(loader, stock, cutoff):
    """Return JSON-compatible primary evidence for one exact security, read only.

    Missing/bad inputs are diagnostics/blockers; malformed API arguments raise
    ValueError. No source observation date substitutes for historical publication.
    All events retain cash_model_eligible=False, including issuer paid reports.
    """
    if isinstance(cutoff, str):
        cutoff = _day(cutoff)
    elif type(cutoff) is date:
        cutoff = cutoff.isoformat()
    else:
        cutoff = None
    if not cutoff:
        raise ValueError('cutoff_must_be_ISO_day_or_date')
    if not isinstance(stock, dict) or not isinstance(stock.get('ticker'), str):
        raise ValueError('stock_with_exact_ticker_required')
    stock = deepcopy(stock)
    folder = loader.folder
    reader = _Reader(folder)
    reader.sources = deepcopy(loader.sources)
    reader.diagnostics = deepcopy(loader.input_diagnostics)
    events, installments, source_issues = [], [], []
    registry = loader.registry
    identities = loader.registry_by_ticker.get(stock['ticker'], [])
    registry_cvms = {_cvm(row.get('cvm')) for row in identities}
    exact_registry = registry_cvms == {stock.get('cvm')} and _cvm(stock.get('cvm')) is not None
    if not _cvm(stock.get('cvm')):
        reader.issue('exact_CVM_unavailable')
    if identities and not exact_registry:
        reader.issue('registry_ticker_CVM_conflict')
    if not stock.get('isin'):
        reader.issue('exact_ISIN_unavailable')

    receipt = loader.report_receipt
    documents = loader.documents_by_cvm.get(stock.get('cvm'), [])
    seen_urls = defaultdict(set)
    for document in documents:
        if _cvm(stock.get('cvm')) is None or _cvm(document.get('cvm')) != stock['cvm']:
            continue
        published = _day(document.get('published_at'))
        if published and published > cutoff:
            reader.issue('publication_after_cutoff', url=document.get('url'), published_at=published)
            continue
        if not document.get('pages'):
            reader.issue('CVM_document_without_valid_pages', url=document.get('url'))
            continue
        seen_urls[document.get('url')].add(document.get('sha256'))
        for row in document.get('events', []):
            if _cvm(row.get('cvm')) != stock['cvm']:
                reader.issue('CVM_document_event_identity_conflict', url=document.get('url'))
                continue
            if not stock.get('isin') or row.get('isin') != stock['isin']:
                continue
            reference = _reference(document, receipt, row, declaration=True)
            if published and reference['published_at'] and published != reference['published_at']:
                reader.issue('CVM_document_event_publication_conflict', url=document.get('url'))
                reference['published_at'] = None
            if reference['published_at'] and reference['published_at'] > cutoff:
                continue
            event = _normalize(row, stock, 'cvm_declaration', reference, cutoff)
            event['identity_basis'] = 'exact_CVM_ISIN'
            if published is None:
                event['blockers'].append('document_publication_unresolved')
            events.append(event)
        for row in document.get('unmapped_installment_rows', []):
            installments.append({**deepcopy(row), 'cvm': stock['cvm'], 'isin': row.get('isin'),
                'code': 'unmapped_installment_blocks_cash', 'cash_model_eligible': False,
                'evidence': [_reference(document, receipt, row, declaration=True)]})
    for url, hashes in seen_urls.items():
        if len(hashes) > 1:
            reader.issue('same_document_url_changed_content', url=url, sha256=sorted(h for h in hashes if h))
            for event in events:
                if any(ref['url'] == url for ref in event['evidence']):
                    event['issues'].append('same_document_url_changed_content')
                    event['blockers'].append('same_document_url_changed_content')

    def add_ri(result, role, receipt, sources, *, declaration=False, alternative=None):
        result_cvm = result.get('cvm')
        if result_cvm is not None and result_cvm != stock.get('cvm'):
            return
        for row in result.get('events', []):
            if row.get('ticker') != stock['ticker'] or _cvm(stock.get('cvm')) is None:
                continue
            asserted = [value for value in (result_cvm, row.get('cvm')) if value is not None]
            if (any(_cvm(value) != stock['cvm'] for value in asserted)
                    or (not asserted and not exact_registry)):
                reader.issue('RI_exact_ticker_CVM_unresolved', role=role)
                continue
            if (row.get('isin') is not None and row['isin'] != stock.get('isin')
                    or row.get('share_class') and stock.get('share_class')
                    and row['share_class'] != stock['share_class']):
                reader.issue('RI_security_identity_conflict', role=role)
                continue
            source = sources.get(row.get('source_ref')) or result.get('source', {})
            reference = _reference(source, receipt, row, declaration=declaration or row.get('declaration_only', False),
                                   alternative=alternative)
            if reference['published_at'] and reference['published_at'] > cutoff:
                reader.issue('publication_after_cutoff', url=reference['url'],
                             published_at=reference['published_at'])
                continue
            kind = 'issuer_ri_declaration' if reference['declaration_only'] else 'issuer_ri_history'
            event = _normalize(row, stock, kind, reference, cutoff)
            event['identity_basis'] = ('exact_ticker_CVM_in_source' if asserted
                                       else 'exact_ticker_CVM_in_registry')
            events.append(event)
        source_issues.extend({'code': 'RI_source_issue', 'role': role,
                             'report_file': receipt['file'], 'issue': deepcopy(issue)}
                             for issue in result.get('issues', []))

    for role, instruments, by_cvm, sources, receipt in loader.histories:
        if role == 'issuer_operating_RI':
            result = instruments.get(stock['ticker'], {})
            if result.get('ticker') not in (None, stock['ticker']):
                reader.issue('RI_instrument_ticker_conflict', file=receipt['file'])
                continue
            add_ri(result, role, receipt, sources)
            for name, alternative in result.get('alternative_observations', {}).items():
                add_ri(alternative, role, receipt, sources, alternative=name)
        else:
            for result, declaration in by_cvm.get(stock.get('cvm'), []):
                add_ri(result, role, receipt, sources, declaration=declaration)

    events = _aggregate(events)
    conflicts = _conflicts(events)
    _publications(events, stock, reader)
    # Installments remain issuer-scoped blockers even when this stock has no ISIN.
    grouped_installments = {}
    for row in installments:
        key = _id('installment', {k: v for k, v in row.items() if k != 'evidence'})
        if key not in grouped_installments:
            grouped_installments[key] = {**row, 'id': key}
        else:
            grouped_installments[key]['evidence'].extend(row['evidence'])
    installments = list(grouped_installments.values())
    kinds = Counter(event['kind'] for event in events)
    counts = {'events': len(events), **{kind: kinds[kind] for kind in (
                  'cvm_declaration', 'issuer_ri_history', 'issuer_ri_declaration')},
              'CVM_report_documents': loader.document_count,
              'CVM_report_event_rows': loader.event_row_count,
              'issuer_documents': len(documents),
              'read_reports': len(reader.sources), 'conflicts': len(conflicts),
              'unmapped_installments': len(installments),
              'publication_recovered': sum(e['publication_basis'] == 'unique_primary_declaration_match' for e in events),
              'paid_reported': sum(e['status'] == 'paid_reported' for e in events),
              'model_admissions': 0}
    blockers = _unique(reader.diagnostics + conflicts + source_issues + [
        {'code': 'unmapped_installment_blocks_cash', 'installment_id': row['id'], 'cvm': row['cvm']}
        for row in installments])
    return {'schema_version': 1, 'ticker': stock['ticker'], 'cvm': stock.get('cvm'),
            'isin': stock.get('isin'), 'cutoff': cutoff, 'scope': 'primary_validation_complement',
            'events': events, 'sources': reader.sources, 'diagnostics': _unique(reader.diagnostics),
            'conflicts': conflicts, 'source_issues': _unique(source_issues),
            'unmapped_installment_rows': installments, 'blockers': blockers, 'counts': counts,
            'registry_counts': deepcopy(registry.get('counts', {})),
            'cash_model_eligible': False, 'history_complete': False,
            'independent_validation_required': True}
