/* Traffic volumes for a whole grouped order from one page.
 *
 * Every step calls the same per-request endpoints the single-request page uses, one link after another, so
 * the same checks apply. Nothing is sent to the paid Directions API before the admin has seen the list of
 * routes and acknowledged the cost once for all of them.
 *
 * `request(method, url, body)` must resolve to {ok, status, data}; it is injected so the module can be tested.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.GroupVolumes = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const ACTIVE_DIRECTIONS = ['submitting', 'running', 'unknown'];
  const DEAD_DIRECTIONS = ['failed', 'rejected', 'invalidated'];
  const LOCKED_VOLUMES = ['running', 'ready_for_review', 'sent'];

  const coordinate = p => p.map(value => Number(value).toFixed(6)).join(', ');
  const timeText = value => (value ? String(value).slice(0, 8) : '00:00:00');

  function routeTimestamp(input, timeZone) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(input);
    if (!match || !timeZone) return null;
    const [year, month, day, hour, minute, second] = match.slice(1).map(value => Number(value || 0));
    const wall = Date.UTC(year, month - 1, day, hour, minute, second);
    const formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    });
    const parts = ts => Object.fromEntries(formatter.formatToParts(new Date(ts)).filter(p => p.type !== 'literal').map(p => [p.type, Number(p.value)]));
    let candidate = wall;
    for (let attempt = 0; attempt < 5; attempt++) {
      const v = parts(candidate);
      const displayed = Date.UTC(v.year, v.month - 1, v.day, v.hour, v.minute, v.second);
      const next = wall - (displayed - candidate);
      if (next === candidate) break;
      candidate = next;
    }
    const check = parts(candidate);
    if (check.year !== year || check.month !== month || check.day !== day || check.hour !== hour || check.minute !== minute || check.second !== second) return null;
    return Math.floor(candidate / 1000);
  }

  /** The local start and end of a link's period, as the single-request page proposes them. */
  function defaultWindow(child) {
    const date = child.d1;
    const start = `${date}T${timeText(child.start_time)}`;
    let end = `${date}T${timeText(child.end_time)}`;
    if (end <= start) {
      const next = new Date(`${date}T00:00:00Z`);
      next.setUTCDate(next.getUTCDate() + 1);
      end = `${next.toISOString().slice(0, 10)}T${timeText(child.end_time)}`;
    }
    return { start, end };
  }

  const fail = (child, message) => ({ id: child.id, ok: false, error: message });
  const done = (child, extra) => Object.assign({ id: child.id, ok: true }, extra || {});

  function create(request) {
    const failure = (response, fallback) => (response && response.data && response.data.error) || fallback;

    /** Current state of every link: its Traffic volumes workflow and its Directions workflow. */
    async function loadStates(children) {
      return Promise.all(children.map(async child => {
        const [volumes, directions] = await Promise.all([
          request('GET', `/api/requests/${child.id}/volumes`),
          request('GET', `/api/requests/${child.id}/directions`),
        ]);
        return {
          id: child.id,
          volumes: volumes.ok ? volumes.data.workflow : null,
          directions: directions.ok ? directions.data.workflow : null,
          error: volumes.ok ? null : failure(volumes, 'Could not read the Traffic volumes status.'),
        };
      }));
    }

    /** Ask the provider about jobs that are still running (the single-request page does this every 15 seconds). */
    async function refreshRunning(states) {
      const jobs = [];
      states.forEach(state => {
        if (state.directions && ACTIVE_DIRECTIONS.includes(state.directions.job_status)) jobs.push(state.directions.job_id);
        const v = state.volumes;
        if (v && v.freeflow_job_id && ['running', 'unknown'].includes(v.freeflow_job_status)) jobs.push(v.freeflow_job_id);
      });
      await Promise.all(jobs.map(job => request('POST', `/api/outscraper/${encodeURIComponent(job)}/refresh`, {})));
      return jobs.length;
    }

    const needsDirections = state => !state.directions || DEAD_DIRECTIONS.includes(state.directions.status);
    const needsFreeflow = state => {
      const v = state.volumes;
      if (!v || v.freeflow_accepted || ['ready_for_review', 'sent'].includes(v.status)) return false;
      return !(v.freeflow_job_id && ['submitting', 'running', 'unknown'].includes(v.freeflow_job_status));
    };

    /** What would be sent: used for the one review and cost acknowledgement. */
    function plan(children, states) {
      const by = Object.fromEntries(states.map(s => [s.id, s]));
      return {
        directions: children.filter(c => by[c.id] && needsDirections(by[c.id])),
        freeflow: children.filter(c => by[c.id] && needsFreeflow(by[c.id])),
      };
    }

    async function submit(config, base) {
      const preview = await request('POST', '/api/outscraper/preview', config);
      if (!preview.ok) throw new Error(failure(preview, 'Could not prepare the Scraper request.'));
      const approval = await request('POST', `/api/outscraper/${encodeURIComponent(preview.data.id)}/approve`, {
        approval_token: preview.data.approval_token, configuration: preview.data.configuration, acknowledge_unknown_cost: true,
      });
      if (!approval.ok) throw new Error(failure(approval, 'The Scraper request was not approved.'));
      return preview.data.id;
    }

    /** Actual-speed Directions request for every link that has none. `window` overrides the default time window. */
    async function sendDirections(children, window) {
      const results = [];
      for (const child of children) {
        try {
          const located = await request('GET', `/api/requests/${child.id}/directions/location`);
          if (!located.ok) throw new Error(failure(located, 'Could not detect the route country.'));
          const location = located.data.location;
          const span = window || defaultWindow(child);
          const departure = routeTimestamp(span.start, location.timezone);
          const finish = routeTimestamp(span.end, location.timezone);
          if (!departure || !finish || finish <= departure) throw new Error('The period is not a valid local time range.');
          const interval = Math.min(Math.ceil((finish - departure) / 60), 60);
          const a = coordinate(child.coords[0]), b = coordinate(child.coords[child.coords.length - 1]), c = coordinate(child.point_c);
          const config = {
            routes: [{ origin: a, destination: c }], departure_time: departure, finish_time: finish, interval,
            travel_mode: 'car', language: 'en', region: location.country_code,
            trafficlens_request: {
              source_request_id: child.id, point_a: a, point_b: b, point_c: c, road_ab_meters: Number(child.road_km) * 1000,
              interval_minutes: interval, country_code: location.country_code, country_name: location.country_name, timezone: location.timezone,
            },
          };
          results.push(done(child, { job: await submit(config) }));
        } catch (error) { results.push(fail(child, error.message || 'Could not send the Directions request.')); }
      }
      return results;
    }

    /** Free-flow (weekly weekend reference) request for every link that needs one, with its proposed configuration. */
    async function sendFreeflow(children) {
      const results = [];
      for (const child of children) {
        try {
          const prepared = await request('GET', `/api/requests/${child.id}/volumes/freeflow-preview`);
          if (!prepared.ok) throw new Error(failure(prepared, 'Could not prepare the free-flow request.'));
          results.push(done(child, { job: await submit(prepared.data.configuration) }));
        } catch (error) { results.push(fail(child, error.message || 'Could not send the free-flow request.')); }
      }
      return results;
    }

    /** Save the lane counts that were typed and let MATLAB start for every link whose inputs are complete. */
    async function saveLanes(children, states, lanesById) {
      const by = Object.fromEntries(states.map(s => [s.id, s]));
      const results = [];
      for (const child of children) {
        const typed = lanesById[child.id];
        const state = by[child.id];
        if (typed === undefined || typed === '' || typed === null) continue;
        if (state && state.volumes && LOCKED_VOLUMES.includes(state.volumes.status)) continue;
        const lanes = Number(typed);
        if (!Number.isInteger(lanes) || lanes < 1) { results.push(fail(child, 'Enter a whole number of lanes.')); continue; }
        const response = await request('POST', `/api/requests/${child.id}/volumes/lanes`, { lanes });
        results.push(response.ok ? done(child) : fail(child, failure(response, 'Could not save the lane count.')));
      }
      return results;
    }

    /** Approve and send the result of every link whose MATLAB result is ready. Does nothing until all are ready. */
    async function approveAll(children, states) {
      const by = Object.fromEntries(states.map(s => [s.id, s]));
      const waiting = children.filter(c => !by[c.id] || !by[c.id].volumes || !['ready_for_review', 'sent'].includes(by[c.id].volumes.status));
      if (waiting.length) return { ok: false, waiting: waiting.map(c => c.id), results: [] };
      const results = [];
      for (const child of children) {
        if (by[child.id].volumes.status === 'sent') continue;
        const response = await request('POST', `/api/requests/${child.id}/volumes/confirm`, {});
        results.push(response.ok ? done(child) : fail(child, failure(response, 'Could not approve this result.')));
      }
      return { ok: results.every(r => r.ok), waiting: [], results };
    }

    /** Earlier completed API results each link could reuse. Only a list: the admin decides. */
    async function findReuse(children) {
      const found = {};
      await Promise.all(children.map(async child => {
        const response = await request('GET', `/api/requests/${child.id}/reuse-candidates`);
        const data = response.ok ? response.data : {};
        found[child.id] = { directions: data.directions || [], freeflow: data.freeflow || [] };
      }));
      return found;
    }

    async function reuse(children, kind, candidates) {
      const path = kind === 'directions' ? 'directions/reuse' : 'volumes/freeflow-reuse';
      const results = [];
      for (const child of children) {
        const best = ((candidates[child.id] || {})[kind] || [])[0];
        if (!best) { results.push(fail(child, 'No earlier result fits this link.')); continue; }
        const response = await request('POST', `/api/requests/${child.id}/${path}`, { source_job_id: best.source_job_id });
        results.push(response.ok ? done(child, { source_request_id: best.source_request_id, kind }) : fail(child, failure(response, 'Could not reuse the earlier result.')));
      }
      return results;
    }

    /**
     * Split a plan between "reuse an earlier result" and "send a new request".
     * Nothing is reused unless the admin chose it (`choices.speeds` / `choices.freeflow`).
     */
    function split(plan, candidates, choices) {
      const has = (child, kind) => ((candidates[child.id] || {})[kind] || []).length > 0;
      const directionsReuse = choices.speeds ? plan.directions.filter(c => has(c, 'directions')) : [];
      const freeflowReuse = choices.freeflow ? plan.freeflow.filter(c => has(c, 'freeflow')) : [];
      return {
        directionsReuse, freeflowReuse,
        directionsSend: plan.directions.filter(c => !directionsReuse.includes(c)),
        freeflowSend: plan.freeflow.filter(c => !freeflowReuse.includes(c)),
      };
    }

    return { loadStates, refreshRunning, plan, sendDirections, sendFreeflow, saveLanes, approveAll, findReuse, reuse, split };
  }

  /** One short word per link for the table. */
  function stage(state) {
    const v = state.volumes, d = state.directions;
    if (!v) return { label: 'No volumes', tone: 'bad' };
    if (v.status === 'sent') return { label: 'Sent', tone: 'good' };
    if (v.status === 'ready_for_review') return { label: 'Ready to approve', tone: 'good' };
    if (v.status === 'failed') return { label: 'MATLAB failed', tone: 'bad' };
    if (v.status === 'running') return { label: 'MATLAB running', tone: 'busy' };
    if (!d) return { label: 'Needs speed request', tone: 'wait' };
    if (ACTIVE_DIRECTIONS.includes(d.job_status)) return { label: 'Waiting for speeds', tone: 'busy' };
    if (DEAD_DIRECTIONS.includes(d.status)) return { label: 'Speed request failed', tone: 'bad' };
    if (!v.freeflow_job_id && !v.freeflow_accepted) return { label: 'Needs free-flow request', tone: 'wait' };
    if (v.freeflow_job_status && ['running', 'unknown', 'submitting'].includes(v.freeflow_job_status)) return { label: 'Waiting for free-flow', tone: 'busy' };
    if (!v.lanes) return { label: 'Needs lanes', tone: 'wait' };
    if (v.freeflow_validation && v.freeflow_validation.is_valid === false && !v.freeflow_accepted) return { label: 'Free-flow check failed', tone: 'bad' };
    return { label: 'Waiting for inputs', tone: 'wait' };
  }

  return { create, stage, routeTimestamp, defaultWindow, coordinate };
});
