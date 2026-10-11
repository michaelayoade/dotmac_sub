const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const html = fs.readFileSync(`${__dirname}/fiber-coverage-page.html`, 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.includes('const plans={home:'));
const storage = new Map();
// Isolated async storage adapter: exercises the actual browser owner's code.
// This is not browser IndexedDB persistence or real WordPress/Selfcare acceptance.
const indexedDB = { open() {
  const request = {};
  queueMicrotask(() => { request.result = { close() {}, transaction() {
    const tx = { objectStore() { return {
      get(key) { return operation(() => storage.get(key)); },
      put(value, key) { return operation(() => storage.set(key, value)); },
      delete(key) { return operation(() => storage.delete(key)); }
    }; }};
    function operation(action) { const req = {}; queueMicrotask(() => { req.result = action(); req.onsuccess?.(); queueMicrotask(() => tx.oncomplete?.()); }); return req; }
    return tx;
  }}; request.onsuccess(); }); return request;
}};
let lock = Promise.resolve();
const locks = { request(name, callback) { const next = lock.then(callback); lock = next.catch(() => {}); return next; }};
async function settle() { for (let i=0;i<30;i++) await new Promise(r=>setImmediate(r)); }
async function waitFor(predicate) {
  const deadline=Date.now()+10000;
  while(!predicate()) {
    if(Date.now()>deadline)throw new Error('Browser fixture timed out awaiting recovery');
    await new Promise(resolve=>setTimeout(resolve,10));
  }
}
async function browser(receiver) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, {value:'',hidden:false,checked:false,disabled:false,textContent:'',innerHTML:'',classList:{toggle(name,value){this[name]=value;}},handlers:{},addEventListener(event,fn){this.handlers[event]=fn;},focus(){}}); return elements.get(id); };
  get('address').value='Synthetic fixture address'; get('full-name').value='Synthetic Fixture'; get('phone').value='+2348000000000';
  const sent=[];
  const context={URLSearchParams,TextEncoder,TextDecoder,Uint8Array,AbortController,Date,crypto:webcrypto,indexedDB,navigator:{locks},document:{referrer:'',getElementById:get,querySelectorAll:()=>[]},location:{search:'',pathname:'/coverage/',hostname:'fixture.example.test'},setTimeout:()=>1,clearTimeout(){},fetch:async(url,args)=>{if(url.endsWith('/attribution'))return {};sent.push({url,body:args.body});return receiver(url,args);}};
  vm.runInNewContext(script,context);
  await waitFor(()=>get('submit-button').disabled===false||sent.length>0);
  return {get,sent,async submit(){await get('coverage-form').handlers.submit({preventDefault(){}});await settle();}};
}
function response(body,status=200){return {ok:status>=200&&status<300,status,json:async()=>body};}
function receipt(status='manual_review'){return {enquiry_status:'received',reference:'FBR-FIXTURE',coverage:{status,summary:'Synthetic fixture summary.'}};}
(async()=>{
  const first=await browser(async()=>{throw new Error('lost response');});
  await first.submit();
  assert.equal(first.get('enquiry-title').textContent,'Pending confirmation');
  assert.equal(first.get('another-check').disabled,true);
  const original=first.sent.find(s=>s.url.endsWith('/quote-request')).body;
  assert(storage.has('request'));
  assert(!JSON.stringify(storage.get('request')).includes('Synthetic fixture address'));
  assert.equal(storage.get('key').extractable,false);
  const restart=await browser(async url=>url.endsWith('/submission-status')?response({},404):response(receipt('technical_error')));
  await waitFor(()=>restart.get('enquiry-title').textContent==='Your request has been received.'&&!storage.has('request'));
  assert.equal(restart.sent.find(s=>s.url.endsWith('/quote-request')).body,original);
  assert.equal(restart.get('enquiry-title').textContent,'Your request has been received.');
  assert.equal(restart.get('result-status').textContent,'Coverage calculation failed');
  assert(!storage.has('request'));
  const malformed=await browser(async()=>response({success:true,reference:'FBR-FALSE'}));
  await malformed.submit();
  assert.equal(malformed.get('enquiry-title').textContent,'Pending confirmation');
  assert(storage.has('request'));
  const pending=await browser(async()=>response({enquiry_status:'pending_confirmation',delivery_state:'attention',message:'Pending operator review'},202));
  assert.equal(pending.get('enquiry-title').textContent,'Pending confirmation');
  storage.delete('request');
  for(const status of ['covered','survey_required','out_of_area','manual_review','technical_error']){
    const page=await browser(async()=>response(receipt(status)));await page.submit();
    assert.equal(page.get('result-reference').textContent,'FBR-FIXTURE');
    assert.equal(page.get('coverage-result-card').classList['result-card--error'],status==='technical_error');
  }
  console.log('PASS: encrypted browser pending storage, nonextractable key, closure recovery, exact replay bytes, malformed receipt, pending/received separation, five coverage states');
})().catch(error=>{console.error(error);process.exitCode=1;});
