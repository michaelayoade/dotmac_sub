const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync(__dirname + '/../../docs/runbooks/website/fiber/coverage-handoff.js', 'utf8');
function indexedDatabase(memory) {
  return {open() {
    const request = {};
    queueMicrotask(() => {
      request.result = {createObjectStore() {}, close() {}, transaction() {
        const tx = {objectStore() {
          const operation = (kind, key, value) => {
            const result = {};
            queueMicrotask(() => {
              if (kind === 'put') memory.set(key, value);
              if (kind === 'delete') memory.delete(key);
              result.result = kind === 'get' ? memory.get(key) : undefined;
              result.onsuccess();
              queueMicrotask(() => tx.oncomplete());
            });
            return result;
          };
          return {get: key => operation('get', key), put: (value, key) => operation('put', key, value), delete: key => operation('delete', key)};
        }};
        return tx;
      }};
      if (request.onupgradeneeded) request.onupgradeneeded();
      request.onsuccess();
    });
    return request;
  }};
}
function client(memory, server) {
  const window = {location: {search: '?utm_source=facebook&utm_campaign=abuja&ad_id=456', pathname: '/coverage/'}};
  vm.runInNewContext(source, {window, indexedDB: indexedDatabase(memory),
    navigator: {locks: {request: async (_key, action) => action()}},
    crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array, URLSearchParams,
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      if (url.endsWith('/attribution')) return {ok: true};
      server.requests.push({url, body});
      if (url.endsWith('/submission-status')) {
        const receipt = server.receipts.get(body.submission_id);
        return {status: receipt ? 200 : 404, ok: Boolean(receipt), json: async () => receipt};
      }
      if (server.validationFailure) return {status: 422, ok: false, json: async () => ({code: 'fiber_invalid_fields', message: 'Check your address'})};
      if (server.malformed) return {status: 200, ok: true, json: async () => ({enquiry_status: 'received', reference: 'Pending'})};
      if (server.confirm) {
        const receipt = {enquiry_status: 'received', reference: 'FWA-' + 'A'.repeat(24)};
        server.receipts.set(body.submission_id, receipt);
        if (server.lostResponse) throw new Error('Simulated lost response after save');
        return {status: 200, ok: true, json: async () => receipt};
      }
      return {status: 202, ok: true, json: async () => ({enquiry_status: 'pending_confirmation'})};
    }});
  return window.DotmacWebsiteHandoff;
}
const details = {name: 'TEST Prospect', phone: '08031234567', address: 'TEST 12 Example Street', area: 'Abuja'};
const state = () => ({requests: [], receipts: new Map(), confirm: false});
(async () => {
  const memory = new Map(), server = state();
  let api = client(memory, server);
  let result = await api.submit(details);
  assert.equal(result.state, 'pending');
  assert.equal(result.destination, undefined, 'No WhatsApp handoff before confirmation');
  assert.ok(memory.get('pending').cipher instanceof ArrayBuffer, 'Pending details are stored as ciphertext');
  const first = server.requests.find(item => item.url.endsWith('/whatsapp-enquiry')).body;
  assert.equal(first.attribution.ad_id, '456');
  assert.equal(first.attribution.utm_campaign, 'abuja');
  server.confirm = true;
  api = client(memory, server); // Reload with the same durable browser storage.
  result = await api.submit({...details, address: 'CHANGED ADDRESS'});
  const attempts = server.requests.filter(item => item.url.endsWith('/whatsapp-enquiry'));
  assert.deepEqual(attempts[1].body, first, 'Retry preserves exact submission content and identity');
  assert.equal(result.state, 'received');
  assert.match(decodeURIComponent(result.destination), /FWA-A{24}/);
  assert.match(decodeURIComponent(result.destination), /TEST 12 Example Street/);
  assert.ok(!memory.has('pending'));

  const lostServer = {...state(), confirm: true, lostResponse: true}, lostMemory = new Map();
  api = client(lostMemory, lostServer);
  assert.equal((await api.submit(details)).state, 'pending');
  api = client(lostMemory, lostServer);
  assert.equal((await api.submit(details)).state, 'received');
  assert.equal(lostServer.requests.filter(item => item.url.endsWith('/whatsapp-enquiry')).length, 1, 'A lost response is recovered through receipt lookup');

  api = client(new Map(), {...state(), malformed: true});
  assert.equal((await api.submit(details)).state, 'pending', 'Malformed receipts never open WhatsApp');
  const invalidMemory = new Map();
  api = client(invalidMemory, {...state(), validationFailure: true});
  await assert.rejects(() => api.submit(details), /Check your address/);
  assert.ok(!invalidMemory.has('pending'), 'Known pre-save validation errors allow correction');
  console.log('Passed pending/confirmed handoff, encrypted reload recovery, stable retry, attribution, lost response, malformed receipt and validation correction. No network calls or customer messages.');
})().catch(error => {console.error(error); process.exitCode = 1;});
