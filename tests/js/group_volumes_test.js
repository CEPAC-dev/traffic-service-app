// Tests of static/group_volumes.js with a fake API: nothing real is ever called.
const assert = require('assert');
const GV = require('../../static/group_volumes.js');

const child = (id, no, extra) => Object.assign({
  id, link_no: no, period: 'AM', d1: '2026-10-24', start_time: '07:15:00', end_time: '08:15:00',
  coords: [[30.138239, 31.395698], [30.137423, 31.393946]], point_c: [30.129997, 31.383281], road_km: 0.19,
}, extra || {});
const children = [child(1, 1), child(2, 2), child(3, 3)];

function fakeApi(routes) {
  const calls = [];
  const request = async (method, url, body) => {
    calls.push({ method, url, body });
    const key = `${method} ${url}`;
    const handler = routes[key] || Object.entries(routes).find(([pattern]) => pattern.endsWith('*') && key.startsWith(pattern.slice(0, -1)));
    if (!handler) return { ok: false, status: 404, data: { error: `no route for ${key}` } };
    const value = typeof handler === 'function' ? handler(body, url) : handler[1] ? handler[1](body, url) : handler;
    return value;
  };
  return { request, calls };
}
const ok = data => ({ ok: true, status: 200, data });
const bad = (error, status) => ({ ok: false, status: status || 400, data: { error } });
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('plan: only links without a request get one', () => {
  const gv = GV.create(async () => bad('unused'));
  const states = [
    { id: 1, directions: null, volumes: { status: 'waiting_for_inputs', freeflow_accepted: false, freeflow_job_id: null } },
    { id: 2, directions: { status: 'running', job_status: 'running' }, volumes: { status: 'waiting_for_inputs', freeflow_accepted: false, freeflow_job_id: 'ff2', freeflow_job_status: 'running' } },
    { id: 3, directions: { status: 'ready', job_status: 'completed' }, volumes: { status: 'ready_for_review', freeflow_accepted: true } },
  ];
  const plan = gv.plan(children, states);
  assert.deepStrictEqual(plan.directions.map(c => c.id), [1]);
  assert.deepStrictEqual(plan.freeflow.map(c => c.id), [1]);
});

test('plan: a failed request can be sent again', () => {
  const gv = GV.create(async () => bad('unused'));
  const states = [{ id: 1, directions: { status: 'failed', job_status: 'failed' }, volumes: { status: 'waiting_for_inputs', freeflow_accepted: false, freeflow_job_id: 'old', freeflow_job_status: 'failed' } }];
  const plan = gv.plan([children[0]], states);
  assert.strictEqual(plan.directions.length, 1);
  assert.strictEqual(plan.freeflow.length, 1);
});

test('sendDirections builds the same A to C request as the single page and asks for approval each time', async () => {
  const api = fakeApi({
    'GET /api/requests/1/directions/location': () => ok({ location: { country_code: 'EG', country_name: 'Egypt', timezone: 'UTC' } }),
    'GET /api/requests/2/directions/location': () => ok({ location: { country_code: 'EG', country_name: 'Egypt', timezone: 'UTC' } }),
    'POST /api/outscraper/preview': body => ok({ id: `job-${body.trafficlens_request.source_request_id}`, approval_token: 'tok', configuration: body }),
    'POST /api/outscraper/*': () => ok({}),
  });
  const results = await GV.create(api.request).sendDirections([children[0], children[1]]);
  assert.deepStrictEqual(results.map(r => [r.id, r.ok, r.job]), [[1, true, 'job-1'], [2, true, 'job-2']]);
  const preview = api.calls.find(c => c.url === '/api/outscraper/preview').body;
  assert.deepStrictEqual(preview.routes, [{ origin: '30.138239, 31.395698', destination: '30.129997, 31.383281' }]);
  assert.strictEqual(preview.departure_time, Date.UTC(2026, 9, 24, 7, 15) / 1000);
  assert.strictEqual(preview.finish_time, Date.UTC(2026, 9, 24, 8, 15) / 1000);
  assert.strictEqual(preview.interval, 60);
  assert.strictEqual(preview.region, 'EG');
  assert.strictEqual(preview.trafficlens_request.source_request_id, 1);
  assert.strictEqual(preview.trafficlens_request.point_b, '30.137423, 31.393946');
  assert.strictEqual(preview.trafficlens_request.road_ab_meters, 190);
  const approvals = api.calls.filter(c => /\/approve$/.test(c.url));
  assert.strictEqual(approvals.length, 2);
  approvals.forEach(a => { assert.strictEqual(a.body.approval_token, 'tok'); assert.strictEqual(a.body.acknowledge_unknown_cost, true); });
});

test('a failing link does not stop the others and is reported with its reason', async () => {
  const api = fakeApi({
    'GET /api/requests/*': () => ok({ location: { country_code: 'EG', country_name: 'Egypt', timezone: 'UTC' } }),
    'POST /api/outscraper/preview': body => (body.trafficlens_request.source_request_id === 2 ? bad('Route not found.') : ok({ id: 'j', approval_token: 't', configuration: body })),
    'POST /api/outscraper/*': () => ok({}),
  });
  const results = await GV.create(api.request).sendDirections(children);
  assert.deepStrictEqual(results.map(r => r.ok), [true, false, true]);
  assert.strictEqual(results[1].error, 'Route not found.');
  assert.strictEqual(api.calls.filter(c => /\/approve$/.test(c.url)).length, 2);
});

test('an invalid time range is reported before anything is sent', async () => {
  const api = fakeApi({ 'GET /api/requests/1/directions/location': () => ok({ location: { country_code: 'EG', country_name: 'Egypt', timezone: 'UTC' } }) });
  const [r] = await GV.create(api.request).sendDirections([child(1, 1)], { start: '2026-10-24T08:00:00', end: '2026-10-24T07:00:00' });
  assert.strictEqual(r.ok, false);
  assert.ok(!api.calls.some(c => c.url === '/api/outscraper/preview'));
});

test('sendFreeflow sends the proposed configuration unchanged', async () => {
  const config = { routes: [{ origin: 'a', destination: 'c', departure_time: 5 }], departure_time: 5, finish_time: 9, interval: 60, trafficlens_context: { weekend_date: '2026-10-23' } };
  const api = fakeApi({
    'GET /api/requests/1/volumes/freeflow-preview': () => ok({ configuration: config }),
    'POST /api/outscraper/preview': body => ok({ id: 'ff', approval_token: 't', configuration: body }),
    'POST /api/outscraper/*': () => ok({}),
  });
  const [r] = await GV.create(api.request).sendFreeflow([child(1, 1)]);
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(api.calls.find(c => c.url === '/api/outscraper/preview').body, config);
});

test('saveLanes skips locked links and blank values and rejects bad numbers', async () => {
  const api = fakeApi({ 'POST /api/requests/1/volumes/lanes': () => ok({ ok: true }), 'POST /api/requests/3/volumes/lanes': () => ok({ ok: true }) });
  const states = [{ id: 1, volumes: { status: 'waiting_for_inputs' } }, { id: 2, volumes: { status: 'ready_for_review' } }, { id: 3, volumes: { status: 'failed' } }];
  const results = await GV.create(api.request).saveLanes(children, states, { 1: '3', 2: '2', 3: 4 });
  assert.deepStrictEqual(api.calls.map(c => [c.url, c.body.lanes]), [['/api/requests/1/volumes/lanes', 3], ['/api/requests/3/volumes/lanes', 4]]);
  assert.ok(results.every(r => r.ok));
  const bad2 = await GV.create(fakeApi({}).request).saveLanes([children[0]], [{ id: 1, volumes: { status: 'waiting_for_inputs' } }], { 1: '2.5' });
  assert.strictEqual(bad2[0].ok, false);
});

test('approveAll waits until every link is ready and sends nothing before that', async () => {
  const api = fakeApi({});
  const states = [{ id: 1, volumes: { status: 'ready_for_review' } }, { id: 2, volumes: { status: 'running' } }, { id: 3, volumes: { status: 'ready_for_review' } }];
  const outcome = await GV.create(api.request).approveAll(children, states);
  assert.strictEqual(outcome.ok, false);
  assert.deepStrictEqual(outcome.waiting, [2]);
  assert.strictEqual(api.calls.length, 0);
});

test('approveAll approves each ready link once and skips links already sent', async () => {
  const api = fakeApi({ 'POST /api/requests/1/volumes/confirm': () => ok({ ok: true }), 'POST /api/requests/3/volumes/confirm': () => bad('Already confirmed.', 409) });
  const states = [{ id: 1, volumes: { status: 'ready_for_review' } }, { id: 2, volumes: { status: 'sent' } }, { id: 3, volumes: { status: 'ready_for_review' } }];
  const outcome = await GV.create(api.request).approveAll(children, states);
  assert.deepStrictEqual(api.calls.map(c => c.url), ['/api/requests/1/volumes/confirm', '/api/requests/3/volumes/confirm']);
  assert.strictEqual(outcome.ok, false);
  assert.strictEqual(outcome.results[1].error, 'Already confirmed.');
});

test('loadStates reads both workflows of every link', async () => {
  const api = fakeApi({
    'GET /api/requests/1/volumes': () => ok({ workflow: { status: 'running' } }), 'GET /api/requests/1/directions': () => ok({ workflow: null }),
    'GET /api/requests/2/volumes': () => bad('This request does not include Traffic volumes.', 404), 'GET /api/requests/2/directions': () => ok({ workflow: null }),
  });
  const states = await GV.create(api.request).loadStates([children[0], children[1]]);
  assert.strictEqual(states[0].volumes.status, 'running');
  assert.strictEqual(states[1].volumes, null);
  assert.match(states[1].error, /include Traffic volumes/);
});

test('refreshRunning asks the provider only about jobs that are still running', async () => {
  const api = fakeApi({ 'POST /api/outscraper/*': () => ok({ ok: true }) });
  const states = [
    { id: 1, directions: { job_id: 'd1', job_status: 'running' }, volumes: { freeflow_job_id: 'f1', freeflow_job_status: 'running' } },
    { id: 2, directions: { job_id: 'd2', job_status: 'completed' }, volumes: { freeflow_job_id: 'f2', freeflow_job_status: 'completed' } },
  ];
  assert.strictEqual(await GV.create(api.request).refreshRunning(states), 2);
  assert.deepStrictEqual(api.calls.map(c => c.url).sort(), ['/api/outscraper/d1/refresh', '/api/outscraper/f1/refresh']);
});

test('stage gives one clear word per situation', () => {
  const s = GV.stage;
  assert.strictEqual(s({ volumes: null }).label, 'No volumes');
  assert.strictEqual(s({ volumes: { status: 'sent' } }).label, 'Sent');
  assert.strictEqual(s({ volumes: { status: 'ready_for_review' } }).label, 'Ready to approve');
  assert.strictEqual(s({ volumes: { status: 'waiting_for_inputs' }, directions: null }).label, 'Needs speed request');
  assert.strictEqual(s({ volumes: { status: 'waiting_for_inputs' }, directions: { status: 'running', job_status: 'running' } }).label, 'Waiting for speeds');
  assert.strictEqual(s({ volumes: { status: 'waiting_for_inputs' }, directions: { status: 'ready', job_status: 'completed' } }).label, 'Needs free-flow request');
  assert.strictEqual(s({ volumes: { status: 'waiting_for_inputs', freeflow_job_id: 'x', freeflow_accepted: true, lanes: null }, directions: { status: 'ready' } }).label, 'Needs lanes');
  assert.strictEqual(s({ volumes: { status: 'failed' } }).label, 'MATLAB failed');
});

test('local times are converted with the route time zone', () => {
  assert.strictEqual(GV.routeTimestamp('2026-10-24T07:15:00', 'UTC'), Date.UTC(2026, 9, 24, 7, 15) / 1000);
  assert.strictEqual(GV.routeTimestamp('2026-10-24T07:15:00', 'Asia/Kolkata'), Date.UTC(2026, 9, 24, 1, 45) / 1000);
  assert.strictEqual(GV.routeTimestamp('2026-10-24T07:15:00', null), null);
});

test('a period that ends after midnight ends on the next day', () => {
  assert.deepStrictEqual(GV.defaultWindow({ d1: '2026-10-24', start_time: '22:00:00', end_time: '01:00:00' }), { start: '2026-10-24T22:00:00', end: '2026-10-25T01:00:00' });
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('ok   ' + name); } catch (error) { failed++; console.log('FAIL ' + name + '\n     ' + error.message); }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
