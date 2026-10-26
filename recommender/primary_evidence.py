"""Reconcile primary research without promoting declarations into cash payments."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html import escape
import json
from pathlib import Path

from .collect_market_data import ROOT, write_json
from .market_inventory import load_inventory, market_root


def _amount(event):
    value = event.get('gross_per_unit_decimal', event.get('gross_per_share_decimal'))
    if value is None:
        value = event.get('gross_per_unit', event.get('gross_per_share'))
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _fact_key(event):
    return (event['cvm'], event['isin'], event.get('record_date'), event.get('approval_date'),
            event.get('payment_date'), _amount(event))


def declaration_facts(reports, as_of):
    documents = {}
    for report in reports:
        for document in report['results']:
            if not document.get('pages') or document['published_at'] > as_of:
                continue
            identity = document['url']
            previous = documents.get(identity)
            if previous and previous.get('sha256') != document.get('sha256'):
                raise ValueError('same_document_url_changed_content')
            documents[identity] = document
    grouped = {}
    for document in documents.values():
        for event in document['events']:
            if event['cvm'] != document['cvm'] or _amount(event) is None:
                raise ValueError('invalid_document_event_identity_or_amount')
            key = _fact_key(event)
            if key not in grouped:
                grouped[key] = {key: deepcopy(event.get(key)) for key in
                    ('cvm', 'isin', 'record_date', 'approval_date', 'payment_date')}
                grouped[key].update(gross_per_unit_decimal=str(_amount(event)),
                    amount_unit='BRL/ISIN-unit', declaration_evidence=[], label=None,
                    issues=deepcopy(event.get('issues', [])),
                    cash_model_eligible=False, is_settlement_confirmation=False)
            grouped[key]['declaration_evidence'].append({
                'url': document['url'], 'published_at': document['published_at'],
                'sequence': document.get('sequence'), 'version': document['version'],
                'sha256': document['sha256'], 'file': document['file'],
                'table': event['source_table'], 'row': event['source_row']})
    return list(documents.values()), list(grouped.values())


def match_declaration(event, stock, facts):
    """Only explicit gross, exact security identity and compatible dates can match."""
    if not stock.get('isin') or event.get('issues') or event.get('gross_per_share') is None:
        return []
    try:
        amount = Decimal(str(event['gross_per_share']))
    except InvalidOperation:
        return []
    if not amount.is_finite() or not event.get('payment_date'):
        return []
    matches = []
    for fact in facts:
        if fact.get('issues'):
            continue
        if (fact['cvm'], fact['isin'], fact['payment_date']) != (
                stock['cvm'], stock['isin'], event['payment_date']):
            continue
        if any(event.get(field) and fact.get(field) and event[field] != fact[field]
               for field in ('record_date', 'approval_date')):
            continue
        if abs(amount - Decimal(fact['gross_per_unit_decimal'])) > Decimal('0.000000001'):
            continue
        matches.append(fact)
    # Multiple facts are not collapsed into a made-up payment or a total.
    return matches if len(matches) == 1 else []


def build_registry(inventory, document_reports, operating, banks, fre, as_of):
    cutoff = date.fromisoformat(as_of).isoformat()
    documents, facts = declaration_facts(document_reports, cutoff)
    by_issuer, by_security = defaultdict(list), defaultdict(list)
    for document in documents:
        by_issuer[document['cvm']].append(document)
    for fact in facts:
        by_security[fact['cvm'], fact['isin']].append(fact)
    histories = defaultdict(list)
    for ticker, result in operating.get('instruments', {}).items():
        for event in result.get('events', []):
            histories[ticker].append(event)
    for result in banks.get('results', []):
        for event in result['events']:
            histories[event['ticker']].append(event)
    actions = [deepcopy(action) for result in operating.get('instruments', {}).values()
               for action in result.get('corporate_actions', [])]
    for result in banks.get('results', []):
        for action in result.get('actions', []):
            actions.append({**deepcopy(action), 'issuer_source': deepcopy(result.get('source', {}))})
    fre_stocks = {row['ticker']: row for row in fre.get('instruments', [])}
    rows, observations = [], []
    for stock in inventory['stocks']:
        ticker, cvm, isin = stock['ticker'], stock.get('cvm'), stock.get('isin')
        stock_facts = by_security.get((cvm, isin), []) if isin else []
        matched, paid, conflicts = 0, 0, 0
        for event in histories[ticker]:
            observed = deepcopy(event)
            matches = match_declaration(event, stock, stock_facts)
            observed['declaration_matches'] = matches
            observed['cash_model_eligible'] = False
            observed['history_complete'] = False
            observed['gross_reconstructed_from_net'] = False
            observed['settlement_inferred_from_date'] = False
            matched += bool(matches)
            conflicts += bool(event.get('issues'))
            paid += event.get('status') == 'paid_reported' and not event.get('issues')
            observations.append(observed)
        capital = fre_stocks.get(ticker, {})
        rows.append({'ticker': ticker, 'cvm': cvm, 'isin': isin,
            'issuer_document_count': len(by_issuer.get(cvm, [])), 'exact_isin_fact_count': len(stock_facts),
            'issuer_RI_observations': len(histories[ticker]),
            'explicit_paid_reported_observations': paid,
            'RI_events_matched_to_CVM_gross_declaration': matched,
            'RI_observations_with_issues': conflicts,
            'FRE_received_filing': capital.get('has_received_filing', False),
            'FRE_structured_capital': capital.get('has_structured_capital_evidence', False),
            'FRE_exact_share_class': capital.get('has_exact_share_class_evidence', False),
            'model_ready_before_research': stock.get('model_ready', False),
            'model_admission_changed': False, 'cash_history_complete': False,
            'remaining_work': ['Reconcile event revisions and payment confirmation with exact security identity.',
                'Validate share-scale adjustments and historical completeness before model admission.']})
    return {'schema_version': 1, 'as_of': cutoff,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'primary_evidence_not_model_admission', 'instruments': rows,
        'declaration_facts': facts, 'issuer_RI_observations': observations,
        'unmapped_installment_rows': [{**row, 'cvm': document['cvm'],
            'document_url': document['url'], 'document_file': document['file']}
            for document in documents for row in document.get('unmapped_installment_rows', [])],
        'issuer_corporate_action_evidence': actions,
        'FRE_class_snapshot_differences': fre.get('class_snapshot_differences', []),
        'source_acquisition_issues': banks.get('discoveries', []),
        'counts': {'inventory_instruments': len(rows),
            'valid_downloaded_CVM_documents': len(documents),
            'issuers_with_valid_CVM_documents': len(by_issuer),
            'raw_CVM_event_rows': sum(len(document['events']) for document in documents),
            'distinct_CVM_declaration_facts_not_cash_totals': len(facts),
            'instruments_with_exact_ISIN_facts': sum(bool(row['exact_isin_fact_count']) for row in rows),
            'issuer_RI_observations': len(observations),
            'explicit_paid_reported_observations': sum(row['explicit_paid_reported_observations'] for row in rows),
            'RI_events_matched_to_CVM_gross_declaration': sum(row['RI_events_matched_to_CVM_gross_declaration'] for row in rows),
            'issuer_corporate_action_observations': len(actions),
            'model_admissions': 0},
        'payment_states': dict(Counter(event['status'] for event in observations)),
        'FRE_counts': fre.get('counts', {}),
        'active_model_changed': False, 'active_inventory_changed': False,
        'limitations': [
            'Declared/scheduled payment is not a paid event; a paid issuer statement is not independent settlement proof.',
            'Different reports and versions can refer to one event. Row counts are never summed as cash.',
            'No dividend/JCP type, gross amount, record date or historical publication is guessed.',
            'Matching a declaration does not establish payment-status availability for a backtest.',
            'FRE capital participation tables do not establish dividend payments or effective split factors.']}


def _read(path):
    if path.is_symlink() or path.stat().st_size > 100_000_000:
        raise ValueError('unsafe_or_oversized_research_report')
    return json.loads(path.read_text(encoding='utf-8'))


def write_summary(report, output):
    counts = report['counts']
    table = ''.join('<tr>' + ''.join('<td>' + escape(str(value)) + '</td>' for value in (
        row['ticker'], row['issuer_document_count'], row['exact_isin_fact_count'],
        row['issuer_RI_observations'], row['explicit_paid_reported_observations'],
        'Mantido; sem nova certificacao' if row['model_ready_before_research'] else 'Pendente de validacao')) + '</tr>'
        for row in report['instruments'])
    summary = [
        ('PDFs da CVM com estrutura valida', counts['valid_downloaded_CVM_documents']),
        ('Emissores com PDFs baixados', counts['issuers_with_valid_CVM_documents']),
        ('Linhas de declaracoes CVM, antes da conciliacao', counts['raw_CVM_event_rows']),
        ('Observacoes de proventos nos sites de RI', counts['issuer_RI_observations']),
        ('Observacoes explicitamente descritas como pagas pelo RI', counts['explicit_paid_reported_observations']),
        ('Novos ativos liberados no motor nesta pesquisa', counts['model_admissions'])]
    metrics = ''.join('<tr><th>' + escape(label) + '</th><td>' + str(value) + '</td></tr>' for label, value in summary)
    content = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pesquisa de dados primarios</title>
<style>body{font:15px Arial,sans-serif;color:#202622;margin:32px auto;padding:0 20px;max-width:1120px}
h1{font-size:26px}h2{font-size:19px;margin-top:30px}p{line-height:1.6;max-width:90ch}
table{border-collapse:collapse;width:100%;margin:16px 0;font-size:13px}th,td{text-align:left;border-bottom:1px solid #d4dcd6;padding:10px}
thead{background:#eaf1ec}th{font-weight:600}a{color:#086342}.overflow{overflow-x:auto}
.note{border-left:4px solid #ad6d18;padding:4px 16px;background:#fcf6eb}footer{margin:28px 0;font-size:13px;color:#515c55}</style>
<h1>Pesquisa de dados primarios do RendaFutura</h1><p>Data de corte: ''' + escape(report['as_of']) + '''.</p>
<p class="note">Coleta e conciliacao de evidencias, sem alteracao do motor ou do documento academico.
Declaracao ou data prevista nao comprova pagamento. Revisoes de formularios nao sao somadas como renda.</p>
<h2>Resultados da coleta</h2><table>''' + metrics + '''</table>
<h2>O que foi corrigido</h2><p>A leitura dos formularios da CVM verifica a assinatura do arquivo PDF, mesmo quando
o servidor informa HTML. Valores quebrados entre linhas preservam as casas decimais. ISIN, empresa, data-com,
publicacao e pagamento permanecem separados. Parcelas sem vinculo explicito ao ISIN ficam pendentes.</p>
<h2>Fontes e limites</h2><p>Foram consultados os <a href="https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe">documentos IPE da CVM</a>,
os <a href="https://dados.cvm.gov.br/dataset/cia_aberta-doc-fre">formularios de referencia</a> e sites de RI dos emissores.
Os indices IPE de 2021 a 2026 localizaram documentos; isso nao significa que todos os documentos indexados foram baixados.</p>
<p>O RI do Itau retornou HTTP 403. Algumas consultas ao historico do Banco do Brasil falharam por conexao ou certificado.
Na BrasilAgro e na Alupar foram preservadas divergencias de datas entre arquivos. Nenhuma dessas falhas vira renda zero.</p>
<p>Os arquivos FRE recebidos identificam capital e classes, mas nao oferecem, nas tabelas baixadas, os fatores efetivos
de eventos societarios nem um historico individual de dividendos. Ainda e necessario conciliar escala por acao,
revisoes e cobertura temporal antes de liberar novos instrumentos para o modelo.</p>
<h2>Inventario por codigo</h2><div class="overflow"><table><thead><tr><th>Codigo</th><th>PDFs do emissor</th>
<th>Declaracoes com ISIN exato</th><th>Observacoes no RI</th><th>Pagos segundo RI</th><th>Motor</th></tr></thead>
<tbody>''' + table + '''</tbody></table></div><footer>As contagens representam evidencias, nao somas de pagamentos.
Arquivo detalhado: <a href="''' + escape(Path(output).with_suffix('.json').name) + '''">JSON com fontes e hashes</a>.</footer></html>'''
    Path(output).write_text(content, encoding='utf-8')


def consolidate(folder, as_of, output):
    folder = market_root(folder)
    root = folder / 'raw/primary-research'
    document_paths = sorted((root / 'cvm-documents').glob('documents-*.json'))
    current = root / 'cvm-documents/report.json'
    if current.is_file():
        document_paths.append(current)
    operating_paths = sorted((root / 'operating').glob('operating-sources-*.json'))
    bank_paths = sorted((root / 'banks').glob('ledger-*.json'), key=lambda path: path.stat().st_mtime)
    fre_path = root / ('fre/evidence-' + as_of + '.json')
    selected = document_paths + operating_paths[-1:] + bank_paths[-1:] + ([fre_path] if fre_path.is_file() else [])
    report = build_registry(load_inventory(folder), [_read(path) for path in document_paths],
        _read(operating_paths[-1]) if operating_paths else {},
        _read(bank_paths[-1]) if bank_paths else {}, _read(fre_path) if fre_path.is_file() else {}, as_of)
    report['input_reports'] = [{'file': path.relative_to(folder).as_posix(),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in selected]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    write_summary(report, output.with_suffix('.html'))
    print(json.dumps(report['counts']), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    consolidate(args.data_dir, args.as_of, args.output)
