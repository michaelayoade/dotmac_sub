/** @typedef {{name: string, phone: string, address: string, area: string, latitude?: number, longitude?: number}} HandoffDetails */
/** @typedef {{submission_id: string, full_name: string, phone: string, address: string, area: string, latitude: string|null, longitude: string|null, attribution: Record<string,string|null>}} PendingEnquiry */
/** @typedef {{state:'received',reference:string,destination:string}|{state:'pending',message:string}} HandoffOutcome */
(function () {
  'use strict';
  const api = '/wp-json/dotmac-fiber/v1';
  const storeName = 'dotmac-whatsapp-enquiry-v1';
  let busy = false;
  class InputError extends Error {}
  /** @returns {Record<string,string|null>} */
  function attribution() {
    const query = new URLSearchParams(window.location.search);
    const values = {landing_path: window.location.pathname, captured_at: new Date().toISOString()};
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'campaign_id', 'adset_id', 'ad_id'].forEach(function (key) {
      values[key === 'adset_id' ? 'ad_set_id' : key] = (query.get(key) || '').trim().slice(0, 200) || null;
    });
    values.click_id = (query.get('fbclid') || query.get('gclid') || '').slice(0, 255) || null;
    return values;
  }
  const landing = attribution();
  // Same-origin attribution cookie is set by the existing server-side producer.
  fetch(api + '/attribution', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(landing)}).catch(function () {});

  function database() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(storeName, 1);
      request.onupgradeneeded = function () { request.result.createObjectStore('data'); };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(new Error('Pending enquiry storage is unavailable.')); };
    });
  }
  async function stored(operation, key, value) {
    const db = await database();
    try {
      return await new Promise(function (resolve, reject) {
        const tx = db.transaction('data', operation === 'get' ? 'readonly' : 'readwrite');
        const store = tx.objectStore('data');
        const request = operation === 'get' ? store.get(key) : operation === 'delete' ? store.delete(key) : store.put(value, key);
        let result;
        request.onsuccess = function () { result = request.result; };
        tx.oncomplete = function () { resolve(result); };
        tx.onerror = tx.onabort = function () { reject(new Error('Could not preserve the pending enquiry.')); };
      });
    } finally { db.close(); }
  }
  /** @returns {Promise<PendingEnquiry|null>} */
  async function read() {
    const record = await stored('get', 'pending');
    if (!record) return null;
    if (Date.now() - record.created >= 7 * 86400000) throw new Error('Your earlier enquiry needs staff review. Contact Sales before sending another.');
    const key = await stored('get', 'key');
    if (!key) throw new Error('Your pending enquiry could not be recovered. Contact Sales.');
    const bytes = await crypto.subtle.decrypt({name: 'AES-GCM', iv: record.iv}, key, record.cipher);
    return JSON.parse(new TextDecoder().decode(bytes));
  }
  /** @param {PendingEnquiry} value @returns {Promise<PendingEnquiry>} */
  async function preserve(value) {
    if (!navigator.locks) throw new Error('This browser cannot safely preserve an enquiry. Please contact Sales.');
    return navigator.locks.request(storeName, async function () {
      const existing = await read();
      if (existing) return existing;
      let key = await stored('get', 'key');
      if (!key) {
        key = await crypto.subtle.generateKey({name: 'AES-GCM', length: 256}, false, ['encrypt', 'decrypt']);
        await stored('put', 'key', key);
      }
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const cipher = await crypto.subtle.encrypt({name: 'AES-GCM', iv: iv}, key, new TextEncoder().encode(JSON.stringify(value)));
      await stored('put', 'pending', {iv: iv, cipher: cipher, created: Date.now()});
      return value;
    });
  }
  /** @param {PendingEnquiry} pending @param {boolean} statusOnly @returns {Promise<HandoffOutcome>} */
  async function send(pending, statusOnly) {
    const controller = new AbortController();
    const timeout = setTimeout(function () { controller.abort(); }, 25000);
    try {
      const response = await fetch(api + (statusOnly ? '/submission-status' : '/whatsapp-enquiry'), {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(statusOnly ? {submission_id: pending.submission_id} : pending)
      });
      if (statusOnly && response.status === 404) return send(pending, false);
      const receipt = await response.json();
      if (response.status === 422 && ['fiber_missing_field', 'fiber_invalid_fields', 'fiber_invalid_email', 'fiber_invalid_location'].includes(receipt.code)) {
        await navigator.locks.request(storeName, async function () {
          const current = await read();
          if (current && current.submission_id === pending.submission_id) await stored('delete', 'pending');
        });
        throw new InputError(receipt.message || 'Please check your enquiry details.');
      }
      if (!response.ok || receipt.enquiry_status !== 'received' || !/^FWA-[A-F0-9]{24}$/.test(receipt.reference || '')) {
        return {state: 'pending', message: 'Your enquiry is pending confirmation. Retry this saved enquiry; no new enquiry is needed.'};
      }
      await navigator.locks.request(storeName, async function () {
        const current = await read();
        if (current && current.submission_id === pending.submission_id) await stored('delete', 'pending');
      });
      const message = ['Hello Dotmac Sales, I would like a new internet connection.',
        'Enquiry reference: ' + receipt.reference, 'Name: ' + pending.full_name,
        'Phone: ' + pending.phone, 'Installation address: ' + pending.address,
        'City / area: ' + pending.area];
      if (pending.latitude !== null && pending.longitude !== null) message.push('Installation pin: https://www.google.com/maps?q=' + pending.latitude + ',' + pending.longitude);
      return {state: 'received', reference: receipt.reference, destination: 'https://wa.me/2348121179536?text=' + encodeURIComponent(message.join('\n'))};
    } catch (error) {
      if (error instanceof InputError) throw error;
      return {state: 'pending', message: 'Receipt is unconfirmed. Your enquiry is preserved; retry it before sending another.'};
    } finally { clearTimeout(timeout); }
  }
  /** @param {HandoffDetails} details @returns {Promise<HandoffOutcome>} */
  async function submit(details) {
    if (busy) return {state: 'pending', message: 'An enquiry is already being processed.'};
    busy = true;
    try {
      const pending = await preserve({submission_id: crypto.randomUUID(), full_name: details.name.trim(), phone: details.phone.trim(), address: details.address.trim(), area: details.area.trim(), latitude: details.latitude == null ? null : String(details.latitude), longitude: details.longitude == null ? null : String(details.longitude), attribution: landing});
      const outcome = await send(pending, true);
      if (outcome.state !== 'received') busy = false;
      return outcome;
    } catch (error) {
      busy = false;
      throw error;
    }
  }
  window.DotmacWebsiteHandoff = Object.freeze({submit: submit});
}());
