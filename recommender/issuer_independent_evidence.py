"""Typed issuer cash and independent quote evidence, not synthetic B3 responses."""

from collections import defaultdict
from datetime import date
import json
from pathlib import Path

from .alternate_issuer_history_research import validate_cash_events
from .cash_calendar_research import load_calendar
from .published_issuer_quotes import CACHE, quote_rows, read_original, request_proof


IDENTITIES = {'ITSA3': (7617, 'ON', 'BRITSAACNOR0'), 'ITSA4': (7617, 'PN', 'BRITSAACNPR7')}


def load_evidence(folder, identity, cutoff):
    folder = Path(folder).resolve()
    ticker = identity['ticker']
    if ticker == 'PLPL3':
        from .round2_plpl_cash import load_evidence as plpl_evidence
        return plpl_evidence(folder, identity, cutoff)
    if ticker in ('ALLD3', 'JSLG3'):
        from .round2_cash import load_evidence as recovered_cash
        return recovered_cash(folder, identity, cutoff)
    from .pending_b_independent import TARGETS as PENDING_B_TARGETS, load_evidence as pending_b_evidence
    if ticker in PENDING_B_TARGETS:
        return pending_b_evidence(folder, identity, cutoff)
    if ticker not in IDENTITIES or cutoff != date(2026, 9, 30):
        return None
    if tuple(identity.get(key) for key in ('cvm', 'share_class', 'isin')) != IDENTITIES[ticker]:
        raise ValueError('issuer_independent_exact_security_mismatch')
    from .dividend_scope import allowed_tickers
    if ticker not in (allowed_tickers(folder) or ()):
        raise ValueError('issuer_independent_outside_selected_scope')
    calendar = load_calendar(folder, ticker, cutoff)
    events = [{**event, 'isin': identity['isin'], 'payment_date_role': 'payment',
               'record_date_role': 'entitlement_record'} for event in calendar['events']]
    validated = validate_cash_events(identity, events, cutoff)
    expected = defaultdict(float)
    for event in events:
        expected[date.fromisoformat(event['record_date'])] += float(event['gross_per_share_decimal'])
    receipts = [json.loads(path.read_bytes()) for path in sorted((folder / CACHE).glob('response-*.json'))]
    receipts = [row for row in receipts if row.get('ticker') == ticker]
    if len(receipts) != 1:
        raise ValueError('issuer_independent_quote_cache_missing')
    receipt, = receipts
    proof = request_proof(folder, ticker, require_fresh_robots=False)
    if (any(receipt.get(key) != value for key, value in proof.items())
            or receipt['status'] != 200 or receipt['error']):
        raise ValueError('issuer_independent_quote_contract_changed')
    quotes = quote_rows(read_original(folder, receipt), ticker)
    sources = calendar['sources'] + [{key: receipt[key] for key in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}]
    configuration = proof['issuer_configuration']
    sources.append({**configuration, 'bytes': (folder / configuration['file']).stat().st_size,
                    'retrieved_at': None, 'authority': 'cached_issuer_configuration',
                    'unknown_retrieval_not_promoted_to_mtime': True})
    template = proof['public_provider_template']
    sources.append({key: template[key] for key in ('file', 'sha256', 'bytes', 'url')}
                   | {'retrieved_at': template['completed_at'], 'authority': 'independent_public_provider_template'})
    return {'provider': 'issuer_calendar_and_Enfoque', 'expected_cash': dict(expected), 'raw_quotes': quotes,
            'sources': sources, 'calendar': calendar, 'checks': {
                'provider': 'issuer_calendar_and_Enfoque', 'cash_rows': len(events),
                'window_cash_rows': len(validated['events']), 'future_cash_rows': len(validated['future_events']),
                'pre_window_cash_rows': len(validated['pre_window_events']), 'independent_daily_prices': len(quotes),
                'record_dates_derived_from_ex_dates': False, 'net_to_gross_inference': False,
                'synthetic_exchange_rows_created': False, 'settlement_verified': False,
                'point_in_time_archive_verified': False,
                'normal_cash_price_share_and_financial_gates_required': True}}


def validate_primary_actions(folder, identity, cutoff, observed_actions):
    if identity['ticker'] == 'PLPL3':
        from .round2_plpl_cash import validate_primary_actions as plpl_actions
        return plpl_actions(folder, identity, cutoff, observed_actions)
    if identity['ticker'] in ('ALLD3', 'JSLG3'):
        from .round2_cash import validate_primary_actions as recovered_actions
        return recovered_actions(folder, identity, cutoff, observed_actions)
    from .pending_b_independent import TARGETS as PENDING_B_TARGETS, validate_primary_actions as pending_b_actions
    if identity['ticker'] in PENDING_B_TARGETS:
        return pending_b_actions(folder, identity, cutoff, observed_actions)
    from .itausa_primary_actions import primary_actions
    proof = primary_actions(folder, identity, cutoff)
    if proof.get('blocked') or proof.get('missing_documents'):
        raise ValueError('issuer_primary_share_action_document_missing')
    expected = {(row['date'], row['ratio']) for row in observed_actions
                if '2021-09-01' <= row['date'] <= cutoff.isoformat()}
    native = {(row['date'], row['ratio']) for row in proof['actions']}
    if expected != native:
        raise ValueError('issuer_primary_share_actions_disagree')
    return proof
