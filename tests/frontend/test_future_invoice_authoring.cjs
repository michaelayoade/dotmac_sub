const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const source = fs.readFileSync('static/js/future-invoice-authoring.js', 'utf8');
const context = {window: {}, crypto, Intl, Date, JSON, URL, Number, Object, Error};
vm.createContext(context); vm.runInContext(source, context);
function editor() { return context.window.futureInvoiceAuthoring({accountId:'account-1',currency:'NGN'}); }
function line(kind='monthly') {
    return {description:'Unlimited Elite', quantity:'1', unit_price:'100', tax_rate_id:null,
        total:'100',tax_total:'0',subscription_id:'sub-1',starts_at:kind==='monthly'?'2030-01-02T00:00:00Z':null,
        period_kind:kind,selection:{query:{item_kind:'offer',item_id:'offer-1'},fingerprint:'server-preview'}};
}
test('submission retains owner selection identity and actual service identity', () => {
    const ui=editor(); ui.lines=[line()]; const data=JSON.parse(ui.getLineItemsJSON());
    assert.equal(data[0].catalogue_selection.fingerprint,'server-preview');
    assert.equal(data[0].subscription_id,'sub-1');
});
test('same monthly period cannot be added twice to the draft', async () => {
    const ui=editor(); ui.selectedItem={item_kind:'offer',item_id:'offer-1',price_id:'price-1',price_type:'recurring'};
    ui.subscriptionId='sub-1';ui.periodStart='2030-01-02T00:00:00Z';ui.read=async()=>line();
    await ui.addLine();ui.periodStart='2030-01-02T00:00:00Z';await ui.addLine();
    assert.equal(ui.lines.length,1);assert.match(ui.error,/already in this draft/);
});
test('one pending capped renewal cannot be added twice', async () => {
    const ui=editor();ui.selectedItem={item_kind:'offer',item_id:'offer-1',price_type:'recurring'};
    ui.subscriptionId='sub-1';ui.periods=[{period_kind:'capped_pending_renewal'}];ui.read=async()=>line('capped_pending_renewal');
    await ui.addLine();await ui.addLine();assert.equal(ui.lines.length,1);
});
test('unresolved existing lines block review until retried or explicitly removed', async () => {
    const ui=editor();ui.read=async(path,query)=>{if(query.item_id==='bad')throw new Error('price changed');return line();};
    await ui.restoreSelections([{line_id:'bad',selection:{query:{item_id:'bad'}}},{line_id:'good',selection:{query:{item_id:'good'}}}]);
    assert.equal(ui.lines.length,1);assert.equal(ui.unresolvedSelections.length,1);
    assert.equal(ui.validateReview(),false);assert.match(ui.error,/unresolved/);
    ui.read=async()=>line();await ui.retrySelection(0);
    assert.equal(ui.unresolvedSelections.length,0);assert.equal(ui.lines.length,2);assert.equal(ui.validateReview(),true);
});
test('catalogue component tolerates an empty selection event', async () => {
    const ui=editor();await ui.chooseItem(undefined);assert.equal(ui.selectedItem,null);
});
test('calendar accepts only available server-owned service dates', () => {
    const ui=editor();ui.periods=[{available:true,starts_at:'2030-01-02T00:00:00Z',starts_on:'2030-01-02'}];
    ui.calendarStart='2030-01-03';ui.pickCalendarDate();assert.equal(ui.periodStart,'');
    ui.calendarStart='2030-01-02';ui.pickCalendarDate();assert.equal(ui.periodStart,'2030-01-02T00:00:00Z');
});
test('review requires customer and at least one successfully previewed line', () => {
    const ui=editor();assert.equal(ui.validateReview(),false);ui.lines=[line()];assert.equal(ui.validateReview(),true);
});
