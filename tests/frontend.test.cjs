const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const source = readFileSync('static/app.js', 'utf8');
const ids = [...readFileSync('templates/index.html', 'utf8').matchAll(/id="([^"]+)"/g)].map(match => match[1]);
const id = '00000000-0000-4000-8000-000000000001';
const scope = {mode: 'policy_test', enrollment_ready: true, suspended: false, per_checkout_cap_minor: 10000, merchant_key: 'fixture-approved', market: 'SG', currency: 'SGD', budget: {available_minor: 20000, total_budget_minor: 20000, reserved_minor: 0, completed_minor: 0}};

// Minimal DOM doubles run the real controller; rendered layout is checked separately in the browser.
class Element {
  constructor() {
    this.children = []; this.listeners = {}; this.attributes = {}; this.style = {}; this.classes = new Set(); this.ownText = '';
    this.classList = {add: name => this.classes.add(name), remove: name => this.classes.delete(name), contains: name => this.classes.has(name), toggle: (name, enabled) => { const value = enabled ?? !this.classes.has(name); value ? this.classes.add(name) : this.classes.delete(name); return value; }};
  }
  set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focused = true; }
}

async function boot({record, readyScope = scope, failScope = false, failDiscovery = false, passport = {events: [], fixture_notice: 'POLICY TEST'}} = {}) {
  const nodes = Object.fromEntries(ids.map(name => [name, new Element()]));
  const rail = Array.from({length: 4}, () => new Element());
  const location = new URL(`http://127.0.0.1:8000/${record ? `?request_id=${id}` : ''}`);
  const calls = []; const store = new Map();
  const context = vm.createContext({URL, URLSearchParams, Intl, Date, console, location,
    window: {location}, history: {replaceState: (_, __, url) => { location.href = new URL(url, location.href).href; }},
    sessionStorage: {getItem: key => store.get(key), setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key)},
    document: {body: new Element(), cookie: 'spendpilot_csrf=fixture-token', getElementById: name => { assert.ok(nodes[name], `missing DOM hook ${name}`); return nodes[name]; }, querySelectorAll: () => rail, createElement: () => new Element(), createTextNode: value => { const node = new Element(); node.textContent = value; return node; }},
    fetch: async (path, options = {}) => {
      calls.push({path, method: options.method || 'GET'});
      if (path === '/api/scope') { if (failScope) throw new Error('network unavailable'); return {ok: true, json: async () => readyScope}; }
      if (path.endsWith('/passport')) return {ok: true, json: async () => passport};
      if (path.endsWith('/discover') && failDiscovery) throw new Error('network unavailable');
      if (path === '/api/requests') return {ok: true, json: async () => ({request_id: id, state: 'DRAFT'})};
      return {ok: true, json: async () => record};
    }});
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return {nodes, rail, calls, context, location};
}

function quoted(state = 'QUOTED', outcome = 'PASS') {
  return {state, source_mode: 'policy_test', candidate: {name: 'USB-C Hub', merchant_name: 'Fixture'}, quote: {total_minor: 7290, currency: 'SGD'}, rule_results: [{rule: 'run_budget', outcome}]};
}

test('restoring a quote uses GET only and leaves checkout explicitly selectable', async () => {
  const {nodes, calls} = await boot({record: quoted()});
  assert.ok(calls.every(call => call.method === 'GET'));
  assert.equal(nodes.composer.classList.contains('hidden'), true);
  assert.equal(nodes.checkout.classList.contains('hidden'), false);
  assert.equal(nodes.checkout.disabled, false);
});

test('historical passes never enable checkout for blocked or changed quotes', async () => {
  for (const state of ['BLOCKED', 'REVIEW_REQUIRED']) {
    const {nodes, rail} = await boot({record: quoted(state)});
    assert.equal(nodes.checkout.classList.contains('hidden'), true);
    assert.equal(rail[2].classList.contains('is-complete'), false);
  }
  const {nodes} = await boot({record: quoted('QUOTED', 'UNKNOWN')});
  assert.equal(nodes.checkout.classList.contains('hidden'), true);
});

test('unknown checkout holds the next action and never says no checkout happened', async () => {
  const {nodes} = await boot({record: {...quoted('UNKNOWN'), attempt: {checkout_status: 'UNKNOWN', settlement_state: 'HELD'}}});
  assert.match(nodes['checkout-status'].textContent, /reservation remains held/i);
  assert.doesNotMatch(nodes['checkout-status'].textContent, /no checkout|completed/i);
  assert.equal(nodes['new-request'].disabled, true);
  assert.equal(nodes.checkout.classList.contains('hidden'), true);
  assert.equal(nodes['resume-approval'].classList.contains('hidden'), true);
});

test('readiness failure cannot be bypassed by checking confirmation', async () => {
  for (const options of [{failScope: true}, {readyScope: {...scope, enrollment_ready: false}}, {readyScope: {...scope, suspended: true}}]) {
    const {nodes} = await boot(options);
    nodes['scope-confirm'].checked = true;
    nodes['scope-confirm'].listeners.change();
    assert.equal(nodes.start.disabled, true);
  }
});

test('discovery network errors remain visible with a saved recoverable request', async () => {
  const {nodes, calls} = await boot({failDiscovery: true});
  nodes['scope-confirm'].checked = true; nodes.request.value = 'Find one USB-C hub';
  nodes['scope-confirm'].listeners.change();
  await nodes.start.listeners.click();
  assert.equal(nodes.discovery.classList.contains('hidden'), false);
  assert.match(nodes['checkout-status'].textContent, /quote preparation stopped/i);
  assert.equal(nodes['new-request'].disabled, false);
  assert.ok(!calls.some(call => call.path.endsWith('/checkout')));
});

test('fixture completion never claims a real sandbox checkout completed', async () => {
  const {nodes} = await boot({record: {...quoted('COMPLETED'), attempt: {checkout_status: 'COMPLETED', settlement_state: 'SETTLED', final_amount_minor: 7290}}});
  assert.doesNotMatch(nodes['checkout-status'].textContent, /sandbox checkout completed/i);
});

test('starting a new request clears old callback selection without a POST', async () => {
  const {nodes, calls, location} = await boot({record: quoted('BLOCKED')});
  nodes['new-request'].listeners.click();
  assert.equal(location.search, '');
  assert.equal(nodes.composer.classList.contains('hidden'), false);
  assert.equal(nodes['scope-confirm'].checked, false);
  assert.equal(nodes['session-id'].textContent, 'SESSION ID: Awaiting request');
  assert.equal(nodes['check-count'].classList.contains('hidden'), true);
  assert.ok(calls.every(call => call.method === 'GET'));
});

test('command budget uses committed amount and policy display counts every outcome', async () => {
  const rules = [
    {rule: 'scope_confirmed', outcome: 'PASS'}, {rule: 'selected_product_binding', outcome: 'PASS'},
    {rule: 'per_checkout_cap', outcome: 'PASS'}, {rule: 'run_budget', outcome: 'UNKNOWN'}
  ];
  const commandScope = {...scope, budget: {total_budget_minor: 20000, available_minor: 12710, reserved_minor: 7290, completed_minor: 0}};
  const {nodes} = await boot({record: {...quoted('QUOTED'), rule_results: rules}, readyScope: commandScope});
  assert.match(nodes['budget-used'].textContent, /36\.5%/);
  assert.match(nodes['budget-headroom'].textContent, /63\.5%/);
  assert.match(nodes['rules-total'].textContent, /3 \/ 4/);
  assert.match(nodes['check-count'].textContent, /3\/4 checks review/);
  assert.equal(nodes.checkout.classList.contains('hidden'), true);
});

test('recorded JSON is a local safe view switch with no network call', async () => {
  const {nodes, calls} = await boot({record: quoted()});
  const before = calls.length;
  nodes['json-tab'].listeners.click();
  assert.equal(nodes.timeline.classList.contains('hidden'), true);
  assert.equal(nodes['passport-json'].classList.contains('hidden'), false);
  assert.equal(nodes['json-tab'].attributes['aria-pressed'], 'true');
  assert.equal(calls.length, before);
});

test('quote leaves missing quote metadata visibly unknown', async () => {
  const {nodes} = await boot({record: quoted()});
  assert.match(nodes.quote.textContent, /Delivery:\s+Unknown/);
  assert.match(nodes.quote.textContent, /Tax:\s+Unknown/);
  assert.match(nodes.quote.textContent, /remain unverified/);
});

test('audit timeline explains purchases without JSON and preserves the technical view', async () => {
  const passport = {mode: 'policy_test', events: [
    {source: 'USER_SCOPE', event_type: 'scope_confirmed', payload: {permitted_quantity: 1, approved_product_type: 'USB-C hub', business_label: 'Harbour Studio', per_checkout_cap_minor: 10000, total_budget_minor: 20000}},
    {source: 'POLICY_TEST', event_type: 'discovery', payload: {candidate: {name: 'USB-C Hub', merchant_name: 'Fixture Merchant'}, quote: {total_minor: 7290}}},
    {source: 'APP_POLICY', event_type: 'budget_reserved', payload: {reserved_minor: 7290}}
  ]};
  const {nodes} = await boot({record: quoted(), passport});
  assert.match(nodes.timeline.textContent, /You confirmed 1 USB-C hub for Harbour Studio/);
  assert.match(nodes.timeline.textContent, /Quoted total: S\$72\.90/);
  assert.match(nodes.timeline.textContent, /Delivery or tax details were not provided/);
  assert.match(nodes.timeline.textContent, /hold does not confirm payment/);
  assert.doesNotMatch(nodes.timeline.textContent, /reserved_minor|per_checkout_cap_minor|\{|View recorded evidence/);
  assert.deepEqual(JSON.parse(nodes['passport-json'].textContent).events, passport.events);
});

test('audit summaries distinguish test results, unknown payments, and failed checks', async () => {
  const {context} = await boot();
  const summarize = event => context.auditSummary(event);
  assert.match(summarize({source: 'POLICY_TEST', event_type: 'checkout_response', payload: {status: 'COMPLETED'}}), /No real payment/);
  assert.match(summarize({event_type: 'checkout_outcome_unknown'}), /Do not start another checkout/);
  assert.match(summarize({event_type: 'settlement_unverified'}), /budget remains held/);
  assert.match(summarize({event_type: 'checkout_evaluated', payload: {allowed: false, rules: [{rule: 'run_budget', outcome: 'UNKNOWN'}]}}), /0 of 1 spending checks passed.*cannot proceed.*Within remaining budget/);
});
