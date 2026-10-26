from datetime import date

import pytest

from .treasury_file_import import import_file, inspect


HEADER = 'Tipo Titulo;Vencimento do Titulo;Data Resgate;PU;Quantidade;Valor\n'


def csv_content(*rows):
    return (HEADER + '\n'.join(';'.join(row) for row in rows) + '\n').encode()


def event(day='01/07/2025', pu='10,00', quantity='2,00', total='20,00'):
    return ('Tesouro Prefixado com Juros Semestrais', '01/01/2031', day, pu, quantity, total)


@pytest.mark.parametrize('values', [('10,00', '-2,00', '-20,00'), ('NaN', '2,00', '20,00'),
                                   ('10,00', '0,00', '0,00'), ('10,00', '2,00', '21,00')])
def test_invalid_cash_values_are_rejected(values):
    with pytest.raises(ValueError):
        inspect(csv_content(event(pu=values[0], quantity=values[1], total=values[2])), date(2025, 9, 30))


def test_future_events_are_not_accepted_even_beyond_cutoff():
    with pytest.raises(ValueError, match='futura'):
        inspect(csv_content(event(day='01/07/2099')), date(2025, 9, 30))


def test_same_file_is_archived_but_never_replaces_active_cache(tmp_path):
    content = csv_content(event())
    original = tmp_path / 'source.csv'
    original.write_bytes(content)
    target = tmp_path / 'market/raw/income/treasury-coupons.csv'
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    receipt = import_file(original, tmp_path / 'market', apply=True, cutoff=date(2025, 9, 30))
    assert receipt['status'] == 'identical' and not receipt['activated']
    assert target.read_bytes() == original.read_bytes() == content


def test_missing_previous_payment_requires_manual_revision(tmp_path):
    original = tmp_path / 'source.csv'
    original.write_bytes(csv_content(event('01/07/2025')))
    target = tmp_path / 'market/raw/income/treasury-coupons.csv'
    target.parent.mkdir(parents=True)
    previous = csv_content(event('01/01/2025'), event('01/07/2025'))
    target.write_bytes(previous)
    receipt = import_file(original, tmp_path / 'market', apply=True, cutoff=date(2025, 9, 30))
    assert receipt['status'] == 'requires_revision_review' and not receipt['activated']
    assert target.read_bytes() == previous


def test_consistent_new_payment_is_activated_without_touching_input(tmp_path):
    original = tmp_path / 'source.csv'
    content = csv_content(event('01/01/2025'), event('01/07/2025'))
    original.write_bytes(content)
    target = tmp_path / 'market/raw/income/treasury-coupons.csv'
    target.parent.mkdir(parents=True)
    target.write_bytes(csv_content(event('01/01/2025')))
    receipt = import_file(original, tmp_path / 'market', apply=True, cutoff=date(2025, 9, 30))
    assert receipt['activated'] and target.read_bytes() == content
    assert original.read_bytes() == content
