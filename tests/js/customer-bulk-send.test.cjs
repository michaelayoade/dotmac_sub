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
const response = (code, body) => ({ok: code < 400, status: code,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'application/json; charset=utf-8' : null},
    json: async () => body});
const htmlResponse = (code, body = '<!doctype html><html></html>') => ({ok: code < 400, status: code,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'text/html; charset=utf-8' : null},
    text: async () => body});
function runtime(fetch, storage = new Map(), panel = null) {
    const callbacks = {};
    const context = {
        window: {}, fetch, crypto: webcrypto, TextEncoder, AbortController,
        setTimeout: () => 1, clearTimeout: () => {},
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        document: {getElementById: () => panel, querySelector: () => null,
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
            assert.equal(options.headers.Accept, 'application/json');
            throw new Error('connection lost after acceptance');
        }
        gets++;
        assert.equal(options.headers.Accept, 'application/json');
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
        throw new Error('status unavailable');
    };
    await assert.rejects(runtime(fetch, storage).send(payload, {}), /status unavailable/);
    const next = runtime(fetch, storage);
    next.startPreview();
    await assert.rejects(next.send(payload, {}), /status unavailable/);
    assert.equal(ids.length, 2);
    assert.equal(ids[0], ids[1]);
    assert.ok(!JSON.stringify([...storage.values()]).includes('customer-1'));
});

test('changed inputs cannot create a new send while the preceding outcome is unknown', async () => {
    let posts = 0;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') { posts++; throw new Error('lost response'); }
        throw new Error('status unavailable');
    });
    await assert.rejects(client.send(payload, {}), /status unavailable/);
    client.startPreview();
    await assert.rejects(client.send({...payload, expected_impact_token: 'different'}, {}), /status unavailable/);
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
        if (ids.length === 1) return response(400, {code: 'http_400', message: 'Preview changed'});
        return response(202, status(ids[1]));
    });
    await assert.rejects(client.send(payload, {}), /Preview changed/);
    const result = await client.send({...payload, expected_impact_token: 'updated'}, {});
    assert.notEqual(ids[0], ids[1]);
    assert.equal(result.accepted, true);
});

test('a conflict checks the existing receipt before allowing another send', async () => {
    let id, posts = 0;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') {
            posts++;
            id = JSON.parse(options.body).request_id;
            return response(409, {code: 'http_409', message: 'Send conflict.'});
        }
        return response(200, status(id));
    });
    const result = await client.send(payload, {});
    assert.equal(result.request_id, id);
    assert.equal(posts, 1);
});

test('API error envelopes show the server reason without an unnecessary status lookup', async () => {
    let gets = 0;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') {
            return response(400, {code: 'http_400', message: 'Invalid send reference.'});
        }
        gets++;
        return response(404, {code: 'http_404', message: 'Not found.'});
    });
    await assert.rejects(client.send(payload, {}), /Invalid send reference/);
    assert.equal(gets, 0);
});

test('an authentication failure preserves the send reference and explains the next step', async () => {
    const storage = new Map();
    let id;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') {
            id = JSON.parse(options.body).request_id;
            throw new Error('confirmation response unavailable');
        }
        assert.equal(options.headers.Accept, 'application/json');
        return response(401, {code: 'http_401', message: 'Unauthorized'});
    }, storage);
    await assert.rejects(client.send(payload, {}), /Sign in again/);
    assert.ok([...storage.values()].some(value => value.includes(id)));
});

test('a temporarily missing receipt stays unresolved and blocks a second message', async () => {
    const storage = new Map();
    let posts = 0;
    const fetch = async (_url, options) => {
        if (options.method === 'POST') {
            posts++;
            return response(503, {code: 'http_503', message: 'Temporarily unavailable.'});
        }
        return response(404, {code: 'http_404', message: 'Not found.'});
    };
    const first = runtime(fetch, storage);
    await assert.rejects(first.send(payload, {}), /No saved send record is available yet/);
    const second = runtime(fetch, storage);
    second.startPreview();
    await assert.rejects(second.send({...payload, expected_impact_token: 'changed'}, {}), /No saved send record is available yet/);
    assert.equal(posts, 1);
});

test('an HTML confirmation failure checks the saved reference without exposing a parse error or retrying', async () => {
    const storage = new Map();
    let id, posts = 0, gets = 0;
    const client = runtime(async (url, options) => {
        if (options.method === 'POST') {
            posts++;
            id = JSON.parse(options.body).request_id;
            return htmlResponse(502);
        }
        gets++;
        assert.equal(url, `/admin/customers/bulk/send-message/${id}`);
        return response(404, {code: 'http_404', message: 'Not found.'});
    }, storage);
    await assert.rejects(client.send(payload, {}), /No saved send record is available yet/);
    assert.equal(posts, 1);
    assert.equal(gets, 1);
    assert.ok([...storage.values()].some(value => value.includes(id)));
});

test('an HTML status response keeps the reference and displays a safe message', async () => {
    const storage = new Map();
    let id;
    const client = runtime(async (_url, options) => {
        if (options.method === 'POST') {
            id = JSON.parse(options.body).request_id;
            throw new Error('connection lost');
        }
        return htmlResponse(502);
    }, storage);
    await assert.rejects(client.send(payload, {}), /webpage instead of send status/);
    assert.ok([...storage.values()].some(value => value.includes(id)));
});

test('a JSON CSRF rejection keeps the reference and explains how to recover', async () => {
    const storage = new Map();
    let id, gets = 0;
    const client = runtime(async (_url, options) => {
        id = JSON.parse(options.body).request_id;
        if (options.method === 'POST') {
            return response(403, {code: 'csrf_validation_failed', message: 'Refresh the page and try again.'});
        }
        gets++;
        return response(404, {code: 'http_404', message: 'Not found.'});
    }, storage);
    await assert.rejects(client.send(payload, {}), /Refresh the page and try again/);
    assert.equal(gets, 0);
    assert.ok([...storage.values()].some(value => value.includes(id)));
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

test('the status panel renders matched, suppressed and skipped counts from the receipt', async () => {
    const message = {textContent: ''};
    const reference = {textContent: ''};
    const panel = {hidden: true, querySelector: selector => selector === '[data-send-message]' ? message : reference};
    const client = runtime(async (_url, options) => {
        const id = JSON.parse(options.body).request_id;
        return response(202, {...status(id), matched_count: 10,
            planned_queued_count: 5, planned_suppressed_count: 2, skipped_count: 3});
    }, new Map(), panel);
    const result = await client.send(payload, {});
    assert.equal(panel.hidden, false);
    assert.match(message.textContent, /Matched 10 customer\(s\)/);
    assert.match(message.textContent, /5 delivery requests; 2 suppressed; 3 skipped/);
    assert.equal(reference.textContent, result.request_id);
});
