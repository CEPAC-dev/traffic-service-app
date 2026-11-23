const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const template = fs.readFileSync('templates/study.html', 'utf8');
const start = template.indexOf('  function refreshSavedPointCs() {');
const end = template.indexOf('  async function solve()', start);
assert(start >= 0 && end > start, 'Point C refresh helper was not found in study.html');
const helper = template.slice(start, end);

function harness(links, calculatePointC) {
  const context = {
    links,
    pointCRefreshPromise: null,
    pointCRefreshError: null,
    needsPointCForServices: () => true,
    calculatePointC,
    L: {
      latLng: (lat, lng) => ({
        lat,
        lng,
        distanceTo: () => 2000,
      }),
    },
    renderLinks: () => {},
    check: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${helper}\nglobalThis.refreshSavedPointCs = refreshSavedPointCs;`, context);
  return context;
}

(async () => {
  const links = [
    { coords: [[30, 31], [30.01, 31.01]], pointC: null },
    { coords: [[30.02, 31.02], [30.03, 31.03]], pointC: [30.04, 31.04] },
  ];
  let calls = 0;
  const success = harness(links, async (_a, _b, delta) => {
    calls += 1;
    assert.strictEqual(delta, 2);
    return { pointC: [30.05, 31.05], routeDistanceToC: 3200 };
  });
  success.refreshSavedPointCs();
  await success.pointCRefreshPromise;
  assert.strictEqual(calls, 1);
  assert.strictEqual(links[0].pointC[0], 30.05);
  assert.strictEqual(links[0].point_c_distance_km, 3.2);
  assert.strictEqual(links[0].coords[0][0], 30);
  assert.strictEqual(links[1].pointC[0], 30.04);

  const failedLinks = [{ coords: [[29, 48], [29.01, 48.01]], pointC: null }];
  const failure = harness(failedLinks, async () => {
    throw new Error('routing unavailable');
  });
  failure.refreshSavedPointCs();
  await failure.pointCRefreshPromise;
  assert.strictEqual(failedLinks[0].pointC, null);
  assert.match(failure.pointCRefreshError, /Link 1: routing unavailable/);

  console.log('Point C refresh tests passed.');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
