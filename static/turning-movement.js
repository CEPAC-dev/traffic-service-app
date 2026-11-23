/* Turning-movement studies: the intersection diagram plus the behaviour of the entry form and the result page.
   Plain browser JavaScript, no libraries. */
(function () {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var C = 200, ROAD = 34, LANE = 17, REACH = 128, LABEL_AT = 172;
  var STEP_KIND = ['u_turn', 'right', 'through', 'left'];   // by (to - from) mod 4, traffic driving on the right
  var KIND_LABEL = {u_turn: 'U-turn', left: 'Left', through: 'Through', right: 'Right'};
  // Legs are numbered counter-clockwise: 0 = right side of the drawing, 1 = top, 2 = left, 3 = bottom.
  var U = [[1, 0], [0, -1], [-1, 0], [0, 1]];
  var RIGHT = U.map(function (u) { return [u[1], -u[0]]; });   // right-hand side of a vehicle driving towards the centre

  function el(name, attrs, parent) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }
  function num(v) { var n = typeof v === 'number' ? v : parseFloat(String(v == null ? '' : v).replace(/,/g, '')); return isFinite(n) ? n : null; }
  function fmt(n) { return n == null ? '' : Math.round(n).toLocaleString('en-US'); }
  function short(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function inPoint(i, r) { return [C + r * U[i][0] + LANE * RIGHT[i][0], C + r * U[i][1] + LANE * RIGHT[i][1]]; }
  function outPoint(i, r) { return [C + r * U[i][0] - LANE * RIGHT[i][0], C + r * U[i][1] - LANE * RIGHT[i][1]]; }
  function pt(p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }

  function arrowHead(parent, tip, dir, size, cls) {
    var len = Math.hypot(dir[0], dir[1]) || 1, d = [dir[0] / len, dir[1] / len], n = [-d[1], d[0]];
    var a = [tip[0] - d[0] * size + n[0] * size * 0.6, tip[1] - d[1] * size + n[1] * size * 0.6];
    var b = [tip[0] - d[0] * size - n[0] * size * 0.6, tip[1] - d[1] * size - n[1] * size * 0.6];
    el('polygon', {points: pt(tip) + ',' + pt(a) + ',' + pt(b), 'class': 'tm-arrow ' + cls}, parent);
  }

  /* Draws the intersection.
     d = {legs: [4 names], inflows: [4], outflows: [4], movements: null | [{u_turn,left,through,right} x4],
          hint: text shown when there is nothing to draw yet, animate: bool}                                   */
  function render(svg, d) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var legs = d.legs || ['1', '2', '3', '4'], ins = (d.inflows || []).map(num), outs = (d.outflows || []).map(num);
    el('rect', {x: 0, y: C - ROAD, width: 400, height: ROAD * 2, 'class': 'tm-road'}, svg);
    el('rect', {x: C - ROAD, y: 0, width: ROAD * 2, height: 400, 'class': 'tm-road'}, svg);
    for (var i = 0; i < 4; i++) {
      var a = [C + U[i][0] * (REACH + 10), C + U[i][1] * (REACH + 10)], b = [C + U[i][0] * (ROAD + 6), C + U[i][1] * (ROAD + 6)];
      el('path', {d: 'M' + pt(a) + 'L' + pt(b), 'class': 'tm-center'}, svg);
    }
    var moves = d.movements, drew = false, flows = [];
    if (moves) {
      for (var f = 0; f < 4; f++) for (var t = 0; t < 4; t++) {
        var kind = STEP_KIND[(t - f + 4) % 4];
        var v = num(moves[f] && moves[f][kind]);
        if (v > 0) flows.push({from: f, to: t, kind: kind, v: v});
      }
      var vmax = Math.max.apply(null, flows.map(function (x) { return x.v; }).concat([1]));
      flows.sort(function (p, q) { return q.v - p.v; }).forEach(function (fl) {
        var w = 2 + 15 * fl.v / vmax, s = inPoint(fl.from, REACH), e = outPoint(fl.to, REACH), c1, c2;
        if (fl.from === fl.to) { c1 = inPoint(fl.from, 78); c2 = outPoint(fl.from, 78); }
        else { c1 = inPoint(fl.from, REACH * 0.45); c2 = outPoint(fl.to, REACH * 0.45); }
        var path = el('path', {d: 'M' + pt(s) + 'C' + pt(c1) + ',' + pt(c2) + ',' + pt(e), pathLength: 1,
          'class': 'tm-flow tm-f-' + fl.kind, 'stroke-width': w.toFixed(1)}, svg);
        el('title', {}, path).textContent = legs[fl.from] + ' to ' + legs[fl.to] + ' (' + KIND_LABEL[fl.kind] + '): ' + fmt(fl.v) + ' vehicles';
        arrowHead(svg, e, [e[0] - c2[0], e[1] - c2[1]], Math.max(7, w * 0.55), 'tm-a-' + fl.kind);
        drew = true;
      });
    } else {
      var vals = ins.concat(outs).filter(function (x) { return x != null && x > 0; });
      var top = Math.max.apply(null, vals.concat([1]));
      for (var k = 0; k < 4; k++) {
        [['in', ins[k], inPoint(k, REACH), inPoint(k, ROAD + 10)], ['out', outs[k], outPoint(k, ROAD + 10), outPoint(k, REACH)]].forEach(function (bar) {
          if (!(bar[1] > 0)) return;
          var w = 4 + 14 * bar[1] / top;
          el('path', {d: 'M' + pt(bar[2]) + 'L' + pt(bar[3]), 'class': 'tm-flow tm-f-' + bar[0], 'stroke-width': w.toFixed(1)}, svg);
          arrowHead(svg, bar[3], [bar[3][0] - bar[2][0], bar[3][1] - bar[2][1]], Math.max(7, w * 0.55), 'tm-a-' + bar[0]);
          drew = true;
        });
      }
    }
    for (var m = 0; m < 4; m++) {
      var x = C + U[m][0] * LABEL_AT, y = C + U[m][1] * LABEL_AT;
      var g = el('g', {'text-anchor': 'middle'}, svg);
      el('text', {x: x, y: y - 6, 'class': 'tm-leg-name'}, g).textContent = short(legs[m], 9);
      el('text', {x: x, y: y + 9, 'class': 'tm-leg-num'}, g).textContent = ins[m] != null ? 'in ' + fmt(ins[m]) : '';
      el('text', {x: x, y: y + 22, 'class': 'tm-leg-num'}, g).textContent = outs[m] != null ? 'out ' + fmt(outs[m]) : '';
    }
    if (!drew && d.hint) el('text', {x: C, y: C + 5, 'text-anchor': 'middle', 'class': 'tm-hint-text'}, svg).textContent = d.hint;
    svg.setAttribute('aria-label', (moves ? 'Turning movements' : 'Vehicles entering and leaving') + ' at the intersection of ' +
      legs.join(', ') + (d.label ? ', ' + d.label : ''));
    if (d.animate) { svg.classList.add('tm-draw'); window.setTimeout(function () { svg.classList.remove('tm-draw'); }, 1800); }
  }
  window.TMDiagram = {render: render};

  /* ------------------------------------------------------------------ result page */
  function initView(dataNode) {
    var data = JSON.parse(dataNode.textContent), svg = document.getElementById('tmStage');
    var sections = [].slice.call(document.querySelectorAll('[data-period]')), tabs = document.getElementById('tmTabs');
    var caption = document.getElementById('tmStageLabel'), first = true;
    function show(k) {
      var p = data.periods[k];
      sections.forEach(function (s) { s.hidden = Number(s.getAttribute('data-period')) !== k; });
      if (tabs) [].slice.call(tabs.children).forEach(function (b, i) { b.setAttribute('aria-selected', String(i === k)); });
      if (caption) caption.textContent = p.label;
      render(svg, {legs: data.legs, inflows: p.inflows, outflows: p.outflows, movements: p.movements, label: p.label, animate: first});
      first = false;
    }
    if (tabs && data.periods.length > 1) {
      data.periods.forEach(function (p, k) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'tm-seg-item'; b.setAttribute('role', 'tab');
        b.textContent = p.label; b.addEventListener('click', function () { show(k); }); tabs.appendChild(b);
      });
    } else if (tabs) { tabs.hidden = true; }
    show(0);
  }

  /* ------------------------------------------------------------------ entry form */
  function initForm(form) {
    var periods = document.getElementById('tmPeriods'), tpl = document.getElementById('tmPeriodTemplate');
    var svg = document.getElementById('tmStage'), caption = document.getElementById('tmStageLabel');
    var active = periods.querySelector('.tm-period'), next = 0;
    [].forEach.call(periods.querySelectorAll('.tm-period'), function (p) { next = Math.max(next, Number(p.getAttribute('data-k')) + 1); });

    function legNames() { return [0, 1, 2, 3].map(function (i) { var f = form.elements['leg_name_' + i]; return (f && f.value.trim()) || String(i + 1); }); }
    function values(p, prefix) { var k = p.getAttribute('data-k'); return [0, 1, 2, 3].map(function (i) { return form.elements[prefix + '_' + k + '_' + i].value; }); }
    function sum(list) { var t = 0, any = false; list.forEach(function (v) { var n = num(v); if (n != null) { t += n; any = true; } }); return any ? t : null; }
    function refresh() {
      var names = legNames();
      [].forEach.call(document.querySelectorAll('.tm-legname'), function (c) { c.textContent = names[Number(c.getAttribute('data-i'))]; });
      [].forEach.call(periods.querySelectorAll('.tm-period'), function (p) {
        var a = sum(values(p, 'in')), b = sum(values(p, 'out'));
        p.querySelector('[data-total-in]').textContent = a == null ? '' : fmt(a);
        p.querySelector('[data-total-out]').textContent = b == null ? '' : fmt(b);
        p.classList.toggle('is-active', p === active);
      });
      if (!active) return;
      var label = form.elements['period_label_' + active.getAttribute('data-k')].value.trim();
      if (caption) caption.textContent = label || 'This period';
      render(svg, {legs: names, inflows: values(active, 'in'), outflows: values(active, 'out'), movements: null,
        label: label, hint: 'Enter the counts to see them here'});
    }
    form.addEventListener('input', refresh);
    form.addEventListener('focusin', function (e) { var p = e.target.closest && e.target.closest('.tm-period'); if (p && p !== active) { active = p; refresh(); } });

    // Enter moves down the column instead of sending the form, like a spreadsheet.
    form.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || !e.target.matches || !e.target.matches('.tm-counts input')) return;
      e.preventDefault();
      var all = [].slice.call(form.querySelectorAll('.tm-counts input')), i = all.indexOf(e.target), col = e.target.name.split('_')[0];
      var nextInput = all.slice(i + 1).filter(function (x) { return x.name.split('_')[0] === col; })[0] ||
                      all.slice(i + 1)[0];
      if (nextInput) { nextInput.focus(); nextInput.select(); }
    });

    document.getElementById('tmAdd').addEventListener('click', function () {
      if (periods.querySelectorAll('.tm-period').length >= 24) { alert('A study can have at most 24 periods.'); return; }
      var holder = document.createElement('div');
      holder.innerHTML = tpl.innerHTML.split('__K__').join(String(next++));
      var fresh = holder.firstElementChild; periods.appendChild(fresh); active = fresh;
      fresh.querySelector('input[name^="period_label_"]').focus(); refresh();
    });
    periods.addEventListener('click', function (e) {
      if (!e.target.hasAttribute('data-remove')) return;
      var all = periods.querySelectorAll('.tm-period');
      if (all.length <= 1) { alert('A study needs at least one period.'); return; }
      var gone = e.target.closest('.tm-period'); if (gone === active) active = all[0] === gone ? all[1] : all[0];
      gone.remove(); refresh();
    });
    var custom = document.getElementById('tmClasses');
    [].forEach.call(form.querySelectorAll('input[name=preset]'), function (r) {
      r.addEventListener('change', function () { custom.classList.toggle('on', form.elements.preset.value === 'custom'); });
    });
    refresh();
  }

  /* ------------------------------------------------------------------ printable report */
  function initReport(dataNode) {
    var data = JSON.parse(dataNode.textContent);
    [].forEach.call(document.querySelectorAll('[data-period-svg]'), function (svg) {
      var p = data.periods[Number(svg.getAttribute('data-period-svg'))];
      render(svg, {legs: data.legs, inflows: p.inflows, outflows: p.outflows, movements: p.movements, label: p.label});
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var data = document.getElementById('tm-data'), form = document.getElementById('tmForm');
    if (form) initForm(form); else if (data && document.getElementById('tmReport')) initReport(data); else if (data) initView(data);
  });
})();
