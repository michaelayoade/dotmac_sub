const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync('static/js/customer-bulk-send.js', 'utf8');
const payload = {channel: 'email', template_id: 'template-1', confirmed: true,
    selection: {mode: 'selected', ids: ['customer-1'], expected_count: 1, expected_scope_token: 'scope'},
    expected_impact_token: 'impact', template_variables: {}};
const status = id => ({request_id: id, accepted: true, materialization_status: 'accepted',
    matched_count: 1, planned_queued_count: 1, planned_suppressed_count: 0, skipped_count: 0,
    delivered_count: 0, submitted_count: 0, pending_count: 0, failed_count: 0, canceled_count: 0,
    error: null, status_url: `/admin/customers/bulk/send-message/${id}`});
const response = (code, body) => ({ok: code < 400, status: code, json: async () => body});
function runtime(fetch, storage = new Map()) {
    const callbacks = {};
    const context = {
        window: {}, fetch, crypto: webcrypto, TextEncoder, AbortController,
        setTimeout: () => 1, clearTimeout: () => {},
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        document: {getElementById: () => null, querySelector: () => null,
            addEventListener: (name, handler) => {callbacks[name] = handler;}},
    };
    vm.runInNewContext(source, context);
    return context.window.DotmacCustomerBulkSend;
}

test('a lost POST response recovers the durable acceptance without another POST', async () => {
    let id, posts = 0, gets = 0;
    const client = runtime(async (url, options) => {
        if (options.method === 'POST') {
            posts++; id = JSON.parse(options.body).request_id;
            throw new Error('connection lost after acceptance');
        }
        gets++;
        assert.equal(url, `/admin/customers/bulk/send-message/${id}`);
        return response(200, status(id));
    });
    const result = await client.send(payload, {});
    assert.equal(result.request_id, id);
    assert.equal(posts, 1);
    assert.equal(gets, 1);
});

test('an uncertain send keeps its UUID across reload and explicit retry', async () => {
    const storage = new Map();
    const ids = [];
    const fetch = async (_url, options) => {
        if (options.method === 'POST') { ids.push(JSON.parse(options.body).request_id); throw new Error('lost response'); }
        return response(404, {detail: 'not found'});
    };
    await assert.rejects(runtime(fetch, storage).send(payload, {}), /Could not confirm/);
    const next = runtime(fetch, storage);
    next.startPreview();
    await assert.rejects(next.send(payload, {}), /Could not confirm/);
    assert.equal(ids.length, 2);
    assert.equal(ids[0], ids[1]);
    assert.ok(!JSON.stringify([...storage.values()]).includes('customer-1'));
});

test('changed inputs cannot create a new send while the preceding outcome is unknown', async () => {
    let posts = 0;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') { posts++; throw new Error('lost response'); }
        return response(404, {detail: 'not found'});
    });
    await assert.rejects(client.send(payload, {}));
    client.startPreview();
    await assert.rejects(client.send({...payload, expected_impact_token: 'different'}, {}));
    assert.equal(posts, 1);
});

test('double-click submission is refused while the first request is in flight', async () => {
    let resolvePost;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') return new Promise(resolve => {
            resolvePost = () => resolve(response(202, status(JSON.parse(options.body).request_id)));
        });
        throw new Error('unexpected status request');
    });
    const first = client.send(payload, {});
    await assert.rejects(client.send(payload, {}), /already being submitted/);
    while (!resolvePost) await new Promise(resolve => setImmediate(resolve));
    resolvePost();
    await first;
});

test('definite rejection shows its reason and allows a corrected interaction', async () => {
    const ids = [];
    const client = runtime(async (_url, options) => {
        ids.push(JSON.parse(options.body).request_id);
        if (ids.length === 1) return response(409, {detail: 'Preview changed'});
        return response(202, status(ids[1]));
    });
    await assert.rejects(client.send(payload, {}), /Preview changed/);
    const result = await client.send({...payload, expected_impact_token: 'updated'}, {});
    assert.notEqual(ids[0], ids[1]);
    assert.equal(result.accepted, true);
});

test('a malformed successful response checks the receipt instead of claiming failure', async () => {
    let id;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') {
            id = JSON.parse(options.body).request_id;
            return {ok: true, status: 202, json: async () => {throw new Error('invalid JSON');}};
        }
        return response(200, status(id));
    });
    assert.equal((await client.send(payload, {})).accepted, true);
});
