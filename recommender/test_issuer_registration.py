import csv
from datetime import date
import hashlib
import io
import json

import pytest

from .issuer_registration import FIELDS, SOURCE_URL, attach_registration, load_registration, parse_registration, registration_status


def content(*, status='ATIVO', since='2020-01-01', cancelled='', reason=''):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=[*FIELDS, 'EMAIL_RESP'], delimiter=';')
    writer.writeheader()
    writer.writerow(dict(zip(FIELDS, ('123', 'Companhia', status, since, cancelled, reason)), EMAIL_RESP='not-used@example.test'))
    return stream.getvalue().encode('latin1')


def status(body, when='2026-10-25'):
    return registration_status(parse_registration(body), 123, when, observed_on='2026-10-25')


def test_active_is_not_exchange_membership_or_purchase_confirmation():
    result = status(content())
    assert result['status'] == 'active_issuer_registration_not_trading_confirmation'
    assert not result['registration_blocks_current_purchase'] and not result['exchange_trading_verified']
    assert 'EMAIL_RESP' not in parse_registration(content())[123]


def test_cancelled_and_suspended_dates_need_exact_evidence():
    result = status(content(status='CANCELADA', since='2024-01-01', cancelled='2024-01-01', reason='INCORPORACAO'))
    assert result['status'] == 'cancelled_issuer_registration' and result['registration_blocks_current_purchase']
    assert result['cancellation_reason'] == 'INCORPORACAO'
    suspended = status(content(status='SUSPENSO(A) - DECISAO ADM', since='2026-01-01'))
    assert suspended['status'] == 'suspended_issuer_registration' and suspended['registration_blocks_current_purchase']
    assert status(content(status='CANCELADA'))['status'] == 'unresolved'


def test_future_status_cannot_be_applied_to_historical_reference():
    body = content(status='CANCELADA', since='2026-10-10', cancelled='2026-10-10')
    result = status(body, '2026-09-30')
    assert result['status'] == 'current_status_not_established_at_reference'
    assert not result['registration_blocks_current_purchase']


def test_unknown_identity_and_future_reference_are_not_inferred():
    assert registration_status({}, 123, date(2026, 10, 25), observed_on=date(2026, 10, 25))['status'] == 'unresolved'
    with pytest.raises(ValueError, match='reference'):
        registration_status({}, 123, '2026-10-26', observed_on='2026-10-25')


@pytest.mark.parametrize('body', [b'', b'CD_CVM;SIT\n123;ATIVO', content().replace(b'123;', b'bad;'),
                                content().replace(b'2020-01-01', b'2020-1-1')])
def test_bad_source_is_not_admitted(body):
    with pytest.raises(ValueError):
        parse_registration(body)


def test_duplicate_conflicting_rows_do_not_select_one():
    first, second = content(), content(status='CANCELADA')
    with pytest.raises(ValueError, match='conflicting'):
        parse_registration(first + second.split(b'\n', 1)[1])


def test_immutable_source_integrity_and_path(tmp_path):
    folder = tmp_path / 'raw/primary-research/registration'
    folder.mkdir(parents=True)
    body = content()
    digest = hashlib.sha256(body).hexdigest()
    path = folder / ('cadastro-' + digest + '.csv')
    path.write_bytes(body)
    manifest = folder / 'current.json'
    receipt = {'url': SOURCE_URL, 'sha256': digest, 'file': path.relative_to(tmp_path).as_posix()}
    manifest.write_text(json.dumps(receipt), encoding='utf-8')
    assert load_registration(tmp_path)[0][123]['SIT'] == 'ATIVO'
    path.write_bytes(body + b'\n')
    with pytest.raises(ValueError, match='integrity'):
        load_registration(tmp_path)
    manifest.write_text(json.dumps({**receipt, 'file': '../outside.csv'}), encoding='utf-8')
    with pytest.raises(ValueError, match='unsafe'):
        load_registration(tmp_path)


def test_attachment_preserves_every_code_and_other_components():
    inventory = {'as_of': '2026-10-24', 'counts': {'stocks': 2}, 'sources': [], 'stocks': [
        {'ticker': 'TEST3', 'cvm': 123, 'quote': {'value': 10}}, {'ticker': 'MISS3', 'cvm': 456}]}
    receipt = {'role': 'current_issuer_registration_not_exchange_trading', 'retrieved_at': '2026-10-25T12:00:00+00:00'}
    result = attach_registration(inventory, parse_registration(content()), receipt)
    assert len(result['stocks']) == result['counts']['stocks'] == 2
    assert result['stocks'][0]['quote'] == {'value': 10}
    assert result['stocks'][1]['issuer_registration']['status'] == 'unresolved'
    assert 'issuer_registration' not in inventory['stocks'][0]
