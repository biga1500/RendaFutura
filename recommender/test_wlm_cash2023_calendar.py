"""Missing WLM August 2023 group: primary proof and unchanged parent gates."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import inventory_candidates as parent
from . import share_basis
from . import wlm_cash2023_calendar as new
from . import wlm_native_calendar as old
from .market_inventory import load_inventory


ROOT = Path(__file__).parent / 'data/market'
STOCKS = (old.STOCK, {'ticker':'WLMM4','cvm':11070,'share_class':'PNA','isin':'BRWLMMACNPR3'})


@pytest.fixture(scope='module')
def proofs():
    return {s['ticker']:new.verified_cash2023_group(ROOT,s) for s in STOCKS}


@pytest.fixture(scope='module')
def input_pages(proofs):
    reader=old.EvidenceReader(ROOT)
    originals=new.read_originals(reader,proofs['WLMM3']['documents'])
    return {'pages':old.pdf_pages(originals['notice']['body'],1)[0],
        'tables':old.pdf_pages(originals['form']['body'],1)[1]}


@pytest.fixture(scope='module')
def previous_repaired():
    values={}
    for s in STOCKS:
        parsed=old.inspector.inspect_payment_page((ROOT/f"raw/expanded/{s['ticker']}-payments.html").read_bytes(),
            s['ticker'],old.CUTOFF,datetime(2026,10,24,tzinfo=timezone.utc))
        values[s['ticker']]=old.repair_full_inspected_calendar(ROOT,s,old.CUTOFF,parsed)
    return values


@pytest.fixture(scope='module')
def real_trial():
    inv=load_inventory(ROOT)
    scoped={**inv,'stocks':[s for s in inv['stocks'] if s['ticker'] in ('WLMM3','WLMM4')]}
    before=(ROOT/'current.json').read_bytes()
    def forbidden(*a,**k): raise AssertionError('offline_verification_no_network')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket,'socket',forbidden)
        result=parent.build_candidates(ROOT,scoped,old.CUTOFF)
    assert (ROOT/'current.json').read_bytes()==before
    return result


def test_exact_primary_latest_notice_and_native_form_confirm_whole_class_schedule(proofs):
    for ticker,proof in proofs.items():
        assert proof['notice']['label']=='JCP'
        assert proof['notice']['record_date']=='2023-08-08'
        assert proof['notice']['payment_date']=='2023-08-25'
        assert proof['notice']['tax_document_deadline']=='2023-07-14'
        assert proof['notice']['gross_per_share_by_source_class']=={'ON':'0.20833868','PN':'0.22917255'}
        assert proof['native_form']['native_protocol']=='1117946'
        assert proof['native_form']['native_sent_date']=='2023-06-29'
        assert proof['native_form']['actual_index_publication']=='2023-07-04'
        assert proof['documents']['notice']['version']==2
        assert proof['documents']['form']['version']==1
        assert proof['documents']['approval_index']['version']==2
        assert not proof['notice']['gross_derived_from_net_or_class_ratio']
        assert not proof['settlement_verified'] and not proof['point_in_time_archive_verified']
        assert proof['separate_followon_acquisition_requests']==2
        assert len(proof['sources'])==(17 if ticker=='WLMM3' else 18)
        assert len(proof['independent_whole_class_group'])==2


@pytest.mark.parametrize('ticker',['WLMM3','WLMM4'])
def test_augmentation_creates_fresh_entire_missing_group_not_nearby_row_reuse(proofs,previous_repaired,ticker):
    original=deepcopy(previous_repaired[ticker]); before=deepcopy(original)
    out=new.augment_missing_group(original,proofs[ticker])
    assert original==before and len(original['events'])==46 and len(out['events'])==47
    assert out['events'][:-1]==original['events']
    assert out['report']['issues']==original['report']['issues']
    event=out['events'][-1]
    assert event['record_date']=='2023-08-08' and event['payment_date']=='2023-08-25'
    assert event['published_at']=='2023-07-04' and event['approval_date']=='2023-06-28'
    assert event['label']=='JCP' and event['quoted_quantity']==1
    assert event['isin']==proofs[ticker]['stock']['isin']
    assert event['source_row'].startswith('primary:cvm644048v1:')
    assert event['status']=='past_scheduled' and event['reported_status'] is None
    assert not event['financially_confirmed'] and not event['full_settlement_on_date_verified']
    assert len(event['primary_evidence'])==2
    audit=out['wlm_full_calendar_repair']['cash2023_missing_group_augmentation']
    assert audit['original_target_group']==[] and audit['added_primary_group']==[event]
    assert not audit['closest_vendor_rows_used_as_replacement']
    assert not audit['existing_dates_amounts_or_labels_changed']
    assert not audit['gross_from_class_ratio_or_tax_rescaling']
    assert len(out['wlm_full_calendar_repair']['evidence_sources'])==(45 if ticker=='WLMM3' else 46)
    nearest=[e for e in original['events'] if e['record_date']=='2023-11-10']
    assert nearest and all(e in out['events'] for e in nearest)


@pytest.mark.parametrize('fault',['whole_existing','partial_existing','conflicting_existing','other_type_existing'])
def test_missing_group_augmentation_never_overwrites_any_existing_group(proofs,previous_repaired,fault):
    parsed=deepcopy(previous_repaired['WLMM3'])
    event={'ticker':'WLMM3','record_date':'2023-08-08','label':'JCP','gross_per_share':.20833868}
    if fault=='partial_existing': event.pop('gross_per_share')
    elif fault=='conflicting_existing': event['gross_per_share']=.01
    elif fault=='other_type_existing': event['label']='DIVIDENDO'
    parsed['events'].append(event); before=deepcopy(parsed)
    with pytest.raises(new.ProofError,match='missing_group_already_present'):
        new.augment_missing_group(parsed,proofs['WLMM3'])
    assert parsed==before


@pytest.mark.parametrize('fault',['v1','wrong_CNPJ','typo_CNPJ','wrong_ticker_name','wrong_kind','missing_PN',
    'net_header','wrong_record','wrong_payment','wrong_tax_role','wrong_approval','missing_signature','partial_body'])
def test_full_notice_body_rejects_partial_wrong_identity_kind_or_date_roles(proofs,input_pages,fault):
    pages=deepcopy(input_pages['pages']); doc=deepcopy(proofs['WLMM3']['documents']['notice'])
    if fault=='v1': doc['version']=1
    elif fault=='wrong_CNPJ': pages[0]=pages[0].replace('33.228.024','11.111.111')
    elif fault=='typo_CNPJ': pages[0]=pages[0].replace('33.228.024','33.229.024')
    elif fault=='wrong_ticker_name': pages[0]=pages[0].replace('WLM','OTHER')
    elif fault=='wrong_kind': pages[0]=pages[0].replace('JUROS SOBRE CAPITAL','DIVIDENDOS SOBRE CAPITAL')
    elif fault=='missing_PN': pages[0]=pages[0].replace('R$0,22917255','')
    elif fault=='net_header': pages[0]=pages[0].replace('Valor bruto','Valor liquido')
    elif fault=='wrong_record': pages[0]=pages[0].replace('08 de agosto','14 de julho')
    elif fault=='wrong_payment': pages[0]=pages[0].replace('25 de agosto','27 de agosto')
    elif fault=='wrong_tax_role': pages[0]=pages[0].replace('14 de julho','08 de agosto')
    elif fault=='wrong_approval': pages[0]=pages[0].replace('28/06/2023','27/06/2023')
    elif fault=='missing_signature': pages[0]=pages[0].replace('Leandro Cardoso Massa','')
    elif fault=='partial_body': pages=[]
    with pytest.raises(new.ProofError): new.parse_notice(pages,doc)


@pytest.mark.parametrize('fault',['drop_ON','drop_PN','duplicate','cross_ISIN','wrong_currency','net_header',
    'wrong_unit','zero','negative','wrong_gross','wrong_year','wrong_period','update','installments',
    'wrong_payment','wrong_record','wrong_protocol','wrong_IPE','wrong_native_version','wrong_approval_index','wrong_publication'])
def test_native_form_requires_complete_latest_two_class_gross_schedule(proofs,input_pages,fault):
    tables=deepcopy(input_pages['tables']); proof=deepcopy(proofs['WLMM3'])
    doc=proof['documents']['form']; approval=proof['documents']['approval_index']
    if fault=='drop_ON': tables[1].pop(2)
    elif fault=='drop_PN': tables[1].pop(3)
    elif fault=='duplicate': tables[1][3]=deepcopy(tables[1][2])
    elif fault=='cross_ISIN': tables[1][2][0]='BROTHERCNOR1'
    elif fault=='wrong_currency': tables[1][1][1]='Valor Bruto (USD/Unidade)'
    elif fault=='net_header': tables[1][1][1]='Valor Liquido (R$/Unidade)'
    elif fault=='wrong_unit': tables[1][1][1]='Valor Bruto (R$/Lote)'
    elif fault=='zero': tables[1][2][1]='0,00000000000'
    elif fault=='negative': tables[1][2][1]='-0,20833868000'
    elif fault=='wrong_gross': tables[1][2][1]='0,20833900000'
    elif fault=='wrong_year': tables[1][2][3]='2024'
    elif fault=='wrong_period': tables[1][2][2]='Trimestral'
    elif fault=='update': tables[1][2][4]='Sim'
    elif fault=='installments': tables[2].append(['25/08/2023','0,01'])
    elif fault=='wrong_payment': tables[1][2][6]='27/08/2023'
    elif fault=='wrong_record': tables[0][5][1]='14/07/2023'
    elif fault=='wrong_protocol': tables[0][7][0]='1117953'
    elif fault=='wrong_IPE': tables[0][1][1]=tables[0][1][1].replace('28062023','29062023')
    elif fault=='wrong_native_version': tables[0][7][1]='2'
    elif fault=='wrong_approval_index': approval['version']=1
    elif fault=='wrong_publication': doc['published_at']='2023-06-29'
    with pytest.raises(new.ProofError): new.parse_form(tables,doc,proof['notice'],approval)


@pytest.mark.parametrize('fault',['drop_class','duplicate','wrong_class','wrong_kind','net_amount','wrong_quote_unit',
    'wrong_ratio','wrong_approval','wrong_CNPJ','wrong_ISIN','pagination'])
def test_complete_independent_group_cannot_be_cross_class_net_or_type_inferred(proofs,fault):
    proof=proofs['WLMM3']; reader=old.EvidenceReader(ROOT); payloads,_=old.cached_sources(reader)
    detail=deepcopy(payloads['expanded/b3-detail-11070.json']); page=deepcopy(payloads['expanded/WLMM4-b3-cash-1.json'])
    rows=[r for r in page['results'] if r['lastDatePriorEx']=='08/08/2023']
    if fault=='drop_class': page['results'].remove(rows[0]); page['page']['totalRecords']-=1
    elif fault=='duplicate': page['results'].append(deepcopy(rows[0])); page['page']['totalRecords']+=1
    elif fault=='wrong_class': rows[0]['typeStock']='PNB'
    elif fault=='wrong_kind': rows[0]['corporateAction']='DIVIDENDO'
    elif fault=='net_amount': rows[0]['valueCash']='0,177087878'
    elif fault=='wrong_quote_unit': rows[0]['quotedPerShares']='1000'
    elif fault=='wrong_ratio': rows[0]['ratio']='0,85'
    elif fault=='wrong_approval': rows[0]['dateApproval']='27/06/2023'
    elif fault=='wrong_CNPJ': detail['cnpj']='33229024000151'
    elif fault=='wrong_ISIN': detail['otherCodes'][0]['isin']='BROTHERCNOR1'
    elif fault=='pagination': page['page']['totalPages']=2
    with pytest.raises(new.ProofError): new.verify_independent_group(proof['notice'],proof['native_form'],detail,page)


@pytest.mark.parametrize('role',['notice','form','approval_index'])
@pytest.mark.parametrize('mutation',['new_version','changed_reference','duplicate_latest'])
def test_full_six_indices_no_fallback_for_later_or_ambiguous_revisions(proofs,role,mutation):
    docs,_=old.indexed_documents(old.EvidenceReader(ROOT),old.CUTOFF)
    seed=deepcopy(proofs['WLMM3']['documents'][role]); altered=deepcopy(seed)
    if mutation!='duplicate_latest':
        altered.update(version=seed['version']+1,sequence='999999',published_at='2023-07-05')
        altered['url']=seed['url'].replace('numSequencia='+seed['sequence'],'numSequencia=999999').replace(
            'numVersao='+str(seed['version']),'numVersao='+str(altered['version']))
        altered['index_evidence']['raw_row']['Link_Download']=altered['url']
        altered['index_evidence']['raw_row']['Protocolo_Entrega']=revised=altered['index_evidence']['raw_row']['Protocolo_Entrega']
        if revised:
            prefix=revised[:17]; altered['index_evidence']['raw_row']['Protocolo_Entrega']=prefix+'03'+revised[19:]
        if mutation=='changed_reference': altered['reference_date']='2023-07-05'
    docs.append(altered)
    with pytest.raises(new.ProofError): new.select_documents(docs)


@pytest.mark.parametrize('role',['notice','form'])
@pytest.mark.parametrize('source_kind',['body','receipt','request'])
def test_actual_original_request_receipt_and_body_hash_corruption_fails_closed(monkeypatch,tmp_path,role,source_kind):
    seq,version,digest,receipt_sha,request_sha=new.PINS[role]
    stem=f'cash2023-{seq}-v{version}'
    name=old.CACHE+'/'+({'body':stem+'-'+digest+'.pdf','receipt':'response-'+stem+'-'+receipt_sha+'.json','request':'request-'+stem+'.json'}[source_kind])
    corrupt=tmp_path/'bad.bin'; corrupt.write_bytes((ROOT/name).read_bytes()+b' ')
    safe=old.shared._safe
    monkeypatch.setattr(old.shared,'_safe',lambda root,p:corrupt if p==name else safe(root,p))
    with pytest.raises(ValueError,match='original_content_hash_mismatch'):
        new.verified_cash2023_group(ROOT,old.STOCK)


def test_public_source_repair_never_accepts_modified_original_inspection(proofs,monkeypatch):
    parsed=old.inspector.inspect_payment_page((ROOT/old.PAYMENTS).read_bytes(),'WLMM3',old.CUTOFF,
                                             datetime(2026,10,24,tzinfo=timezone.utc))
    parsed['events'][0]['gross_per_share']=999.
    with pytest.raises(old.ProofError,match='full_repair_requires_exact_original_inspector_output'):
        new.repair_full_inspected_calendar(ROOT,old.STOCK,old.CUTOFF,parsed)


def test_real_parent_candidates_use_all_normal_cash_price_and_share_checks(real_trial):
    candidates,pending,_=real_trial
    assert pending==[] and {r['ticker'] for r in candidates}=={'WLMM3','WLMM4'}
    assert parent.reconcile_prices is share_basis.reconcile_prices
    assert parent.reconcile_payments is share_basis.reconcile_payments
    for row in candidates:
        assert row['checks']['independent_cash_matches']==22
        assert row['checks']['independent_price_matches']==22
        assert row['share_class']==('ON' if row['ticker']=='WLMM3' else 'PNA')
        assert not row['financially_validated']
        group=[r for r in row['ledger'] if r['record_date']=='2023-08-08']
        assert len(group)==1 and group[0]['payment_date']=='2023-08-25'
        assert group[0]['published_at']=='2023-07-04'
        assert group[0]['label']=='JCP' and group[0]['gross_per_unit'] in (.20833868,.22917255)
        assert not group[0]['point_in_time_archive_verified']
        assert row['checks']['wlm_native_calendar_repair']['cash2023_missing_group_augmentation']


def test_parent_registers_every_proof_source_URL_actual_bytes_and_receipt(real_trial):
    candidates,_,sources=real_trial
    registered={(s['file'],s['sha256'],s.get('url')):s for s in sources}
    assert len(registered)==len(sources)==50
    for row in candidates:
        for source in row['checks']['wlm_native_calendar_repair']['evidence_sources']:
            key=source['file'],source['sha256'],source.get('url')
            assert key in registered
            assert registered[key]['retrieved_at']==source['retrieved_at']
    for source in sources:
        body=(ROOT/source['file']).read_bytes()
        assert len(body)==source['bytes'] and hashlib.sha256(body).hexdigest()==source['sha256']


@pytest.mark.parametrize('ticker',['WLMM3','WLMM4'])
def test_new_primary_hash_failure_blocks_parent_without_alternate_admission(monkeypatch,tmp_path,ticker):
    inv=load_inventory(ROOT); scoped={**inv,'stocks':[s for s in inv['stocks'] if s['ticker']==ticker]}
    seq,version,digest,_,_=new.PINS['notice']
    name=old.CACHE+f'/cash2023-{seq}-v{version}-{digest}.pdf'
    corrupt=tmp_path/'wrong.pdf'; corrupt.write_bytes((ROOT/name).read_bytes()+b' ')
    safe=old.shared._safe
    monkeypatch.setattr(old.shared,'_safe',lambda root,p:corrupt if p==name else safe(root,p))
    candidates,pending,_=parent.build_candidates(ROOT,scoped,old.CUTOFF)
    assert candidates==[] and pending[0]['reason']=='original_content_hash_mismatch'
    assert 'alternate_route_reason' not in pending[0]['checks']


@pytest.mark.parametrize('key,value',[('ticker','OTHER3'),('cvm','11070'),('share_class','PN'),('isin','BRWLMMACNOR6')])
def test_PNA_is_exact_identity_not_suffix_or_financial_alias(key,value):
    with pytest.raises(new.ProofError,match='cash2023_exact_verified_security_required'):
        new.verified_cash2023_group(ROOT,{**STOCKS[1],key:value})


def test_frozen_v2_runtime_and_all_old_report_files_remain_unchanged():
    manifest=json.loads((ROOT/old.CACHE/'report-f7f50454c4aa6735be654e949e9d80615024ed4a784205cba61f83a278d2f0de.json').read_bytes())
    expected=next(s for s in manifest['owned_files'] if s['workspace_file']=='recommender/wlm_native_calendar.py')
    assert hashlib.sha256(Path(expected['workspace_file']).read_bytes()).hexdigest()==expected['sha256']
    for p in (ROOT/old.CACHE).glob('report-*.json'):
        assert hashlib.sha256(p.read_bytes()).hexdigest()==p.stem.removeprefix('report-')


def test_new_report_content_addressed_immutable_owned_scope(tmp_path):
    report={'source':'bounded primary schedule','admission':False}
    name=new.save_report(tmp_path,report); body=(tmp_path/name).read_bytes()
    assert name==new.save_report(tmp_path,report)
    assert hashlib.sha256(body).hexdigest() in name and name.startswith(new.CACHE+'/')
