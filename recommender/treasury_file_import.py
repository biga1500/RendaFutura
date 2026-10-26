"""Validate a manually downloaded official coupon CSV without touching its source."""

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import uuid

from .collect_income_data import COUPON_URL, br_number, treasury_payments
from .collect_market_data import ROOT, month_end, shift_month, write_json

HEADERS = {'Tipo Titulo', 'Vencimento do Titulo', 'Data Resgate', 'PU', 'Quantidade', 'Valor'}


def inspect(content, cutoff):
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')), delimiter=';')
    if set(reader.fieldnames or []) != HEADERS:
        raise ValueError('Cabecalho diferente do CSV oficial de cupons do Tesouro.')
    rows = list(reader)
    if not rows:
        raise ValueError('Arquivo de cupons vazio.')
    for row in rows:
        if row['Tipo Titulo'] not in {'Tesouro Prefixado com Juros Semestrais',
                                     'Tesouro IPCA+ com Juros Semestrais', 'Tesouro IGPM+ com Juros Semestrais'}:
            raise ValueError('Tipo de titulo nao identificado como cupom de juros.')
        values = [br_number(row[key]) for key in ('PU', 'Quantidade', 'Valor')]
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError('PU, quantidade e valor devem ser positivos e finitos.')
    payments = treasury_payments(content, cutoff)
    days = [datetime.strptime(row['Data Resgate'], '%d/%m/%Y').date() for row in rows]
    if max(days) > date.today():
        raise ValueError('Historico oficial de pagamentos contem data futura.')
    return payments, {'rows': len(rows), 'last_reported_payment': max(days).isoformat(),
                      'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}


def import_file(path, folder, *, apply=False, cutoff=None):
    cutoff = cutoff or shift_month(month_end(date.today().year, date.today().month), -1)
    content = Path(path).read_bytes()
    payments, details = inspect(content, cutoff)
    target = folder / 'raw/income/treasury-coupons.csv'
    previous = target.read_bytes() if target.is_file() else None
    old_payments, old = inspect(previous, cutoff) if previous else ({}, {})
    missing, changed = [], []
    for key, events in old_payments.items():
        for day, value in events.items():
            if day not in payments.get(key, {}):
                missing.append([key[0], key[1].isoformat(), day.isoformat()])
            elif abs(payments[key][day] - value) > 1e-8:
                changed.append([key[0], key[1].isoformat(), day.isoformat()])
    status = ('identical' if previous == content else 'requires_revision_review' if missing or changed else
              'validated_additional_evidence')
    receipt = {**details, 'status': status, 'source_url': COUPON_URL,
               'input_filename': Path(path).name, 'retrieval_method': 'user_provided_official_download',
               'validated_at': datetime.now(timezone.utc).isoformat(), 'cutoff': cutoff.isoformat(),
               'previous_sha256': old.get('sha256'), 'missing_prior_payments': missing,
               'changed_prior_payments': changed, 'activated': False,
               'note': 'Integridade e consistencia nao comprovam atualidade, completude ou autenticidade independente.'}
    archive = folder / 'raw/manual-coupons' / details['sha256']
    archive.mkdir(parents=True, exist_ok=True)
    archived = archive / 'cupomjurostesourodireto.csv'
    if not archived.is_file():
        archived.write_bytes(content)
    if apply and status == 'validated_additional_evidence':
        target.parent.mkdir(parents=True, exist_ok=True)
        if previous:
            backup = archive / 'previous-coupons.csv'
            if not backup.exists():
                shutil.copy2(target, backup)
        temporary = target.parent / ('.coupons-' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(content)
        os.replace(temporary, target)
        receipt['activated'] = True
        write_json(target.parent / 'treasury-coupons.manual.json', receipt)
    write_json(archive / 'validation.json', receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(import_file(args.file, args.data_dir.resolve(), apply=args.apply), indent=2))
