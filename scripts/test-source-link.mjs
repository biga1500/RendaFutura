import assert from 'node:assert/strict';
import { test } from 'node:test';
import { sourceLink } from '../lib/source-link.ts';

test('public source references retain their actual scheme and query',()=>{
  for(const scheme of ['https','http']){
    const href=`${scheme}://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?numSequencia=742120&numVersao=2`;
    assert.deepEqual(sourceLink(href),{href,label:'www.rad.cvm.gov.br'});
  }
});

test('local audit receipts and malformed references do not require a URL',()=>{
  for(const value of [null,undefined,'','   ','raw/review.json','not a URL',{},123])assert.equal(sourceLink(value),null);
});

test('source references never turn executable protocols or credentials into links',()=>{
  for(const value of ['javascript:alert(1)','data:text/html,test','file:///C:/audit.json','//example.test/a',
    'https://user:secret@example.test/a','https://user@example.test/a'])assert.equal(sourceLink(value),null);
});
