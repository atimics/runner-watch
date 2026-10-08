(() => {
  'use strict';
  const svg = (tag, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    if (text != null) element.textContent = text;
    return element;
  };
  const riskReading = value => ({significant:'1+ significant risk factors detected in saved checks',detected:'Risk factors detected in saved checks',none:'Detected risk factors: 0 in saved checks',unknown:'Risk factor checks unavailable'}[value] || 'Risk factor checks unavailable');
  const color = part => `var(--indicator-${part.key})`;
  let nextPattern = 0;
  function patterns(layer, scale = 1) {
    const prefix = `ring-pattern-${++nextPattern}`;
    const defs = svg('defs', {});
    const stripe = svg('pattern', {id:`${prefix}-stripe`,patternUnits:'userSpaceOnUse',width:7*scale,height:7*scale,patternTransform:'rotate(45)'});
    stripe.append(svg('path', {d:`M 0 0 V ${7*scale}`,class:'map-pattern-line','stroke-width':2.2*scale}));
    const dot = svg('pattern', {id:`${prefix}-dot`,patternUnits:'userSpaceOnUse',width:7*scale,height:7*scale});
    dot.append(svg('circle', {cx:3.5*scale,cy:3.5*scale,r:1.4*scale,class:'map-pattern-dot'}));
    const grid = svg('pattern', {id:`${prefix}-grid`,patternUnits:'userSpaceOnUse',width:8*scale,height:8*scale});
    grid.append(svg('path', {d:`M 0 0 H ${8*scale} M 0 0 V ${8*scale}`,class:'map-pattern-line','stroke-width':1.6*scale}));
    defs.append(stripe,dot,grid); layer.append(defs);
    return {evidence:`url(#${prefix}-stripe)`,social:`url(#${prefix}-dot)`,cluster:`url(#${prefix}-grid)`};
  }
  const readSentiment = value => {
    const mix = value || {};
    const valid = mix.state === 'available' && [mix.bullish,mix.bearish].every(n => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1) && Math.abs(mix.bullish + mix.bearish - 1) < 0.000001;
    const bullish = valid ? mix.bullish : null;
    const percent = valid ? Math.round(bullish * 100) : null;
    return {state:valid ? 'available' : 'unknown',bullish,bearish:valid ? 1-bullish : null,
      // The server's reading wins: filing counts read "1 of 3 filings", not "100% bearish".
      reading:valid ? (typeof mix.reading === 'string' ? mix.reading : `${percent}% bullish, ${100-percent}% bearish`) : 'bullish/bearish split unavailable',
      compact:valid ? (typeof mix.compact === 'string' ? mix.compact : `▲${percent}% / ▼${100-percent}%`) : '▲— / ▼—',
      basis:typeof mix.basis === 'string' ? mix.basis : 'Saved directional assessments'};
  };
  const metrics = (glyph, small) => {
    // The Well keeps one size; its frame's thickness carries attention.
    const outer = glyph.band === 1 && !glyph.liquidity ? (small ? 36 : 46) : (small ? 56 : 72);
    const width = small ? 16 : 20;
    return {cx:small ? 180 : 380, cy:small ? 184 : 218, outer, radius:outer-width/2, width};
  };
  // Shared face for detail-map controls and compact stock links on entity pages.
  function drawFace({ringLayer, glyph, geometry, small, contributions, controls = [], toneLabel, points, percent, wireControl}) {
    ringLayer.replaceChildren();
    const {cx, cy, outer, radius, width} = geometry;
    const sentimentMix = glyph.sentimentMix;
    const paint = patterns(ringLayer, geometry.patternScale || 1);
    Object.assign(ringLayer.dataset, {band:String(glyph.band),sentiment:glyph.sentiment,sentimentMix:sentimentMix.state,risk:glyph.risk,mix:glyph.mix});
    ringLayer.style.setProperty("--map-band-stroke", String(width));
    ringLayer.style.setProperty("--map-risk-font", `${12*(geometry.markerScale || 1)}px`);
    ringLayer.append(svg('circle', {cx, cy, r:radius, class:'map-score-track', 'stroke-width':width}));
    let angle = -Math.PI / 2;
    const boundaries = [];
    contributions.forEach(part => {
      const sweep = part.share * Math.PI * 2, end = angle + sweep;
      const solid = glyph.band === 3, r = solid ? outer : radius;
      const arc = `M ${cx + r*Math.cos(angle)} ${cy + r*Math.sin(angle)} A ${r} ${r} 0 ${sweep > Math.PI ? 1 : 0} 1 ${cx + r*Math.cos(end)} ${cy + r*Math.sin(end)}`;
      const d = solid ? `M ${cx} ${cy} L ${arc.slice(2)} Z` : arc;
      const attrs = {class:'map-score-segment', 'data-score-key':part.key, 'aria-label':`${part.label}: ${points(part.value)}, ${percent(part)}`, 'stroke-width':solid ? 0 : width};
      const segment = contributions.length === 1 ? svg('circle', {...attrs,cx,cy,r}) : svg('path', {...attrs,d});
      if (contributions.length > 1) {
        const inner = solid ? 0 : radius-width/2;
        boundaries.push(svg('path', {d:`M ${cx+inner*Math.cos(angle)} ${cy+inner*Math.sin(angle)} L ${cx+outer*Math.cos(angle)} ${cy+outer*Math.sin(angle)}`,class:'map-score-divider','aria-hidden':'true'}));
      }
      segment.style[solid ? 'fill' : 'stroke'] = color(part); angle = end;
      let pattern;
      if (paint[part.key]) {
        pattern = segment.cloneNode(false);
        pattern.setAttribute('class','map-score-pattern');
        pattern.setAttribute('data-pattern',part.key);
        pattern.setAttribute('aria-hidden','true');
        pattern.removeAttribute('data-score-key'); pattern.removeAttribute('aria-label');
        pattern.style[solid ? 'fill' : 'stroke'] = paint[part.key];
      }
      segment.append(svg('title', {}, `${part.label}: ${points(part.value)} · ${percent(part)}`));
      wireControl?.(segment,part); ringLayer.append(segment);
      if (pattern) ringLayer.append(pattern);
    });
    ringLayer.append(...boundaries);
    if (sentimentMix.state === 'available') {
      ['bullish','bearish'].forEach(side => {
        const share = sentimentMix[side];
        if (share <= 0) return;
        const arc = svg('circle', {cx,cy,r:outer+6,class:'map-sentiment-part',
          'data-sentiment-side':side,'data-share':share,'aria-hidden':'true',pathLength:100,
          'stroke-dasharray':`${share*100} ${100-share*100}`,
          'stroke-dashoffset':side === 'bullish' ? 0 : -sentimentMix.bullish*100,
          transform:`rotate(-90 ${cx} ${cy})`});
        ringLayer.append(arc);
        if (side === 'bearish') {
          const hatch = arc.cloneNode(false);
          hatch.setAttribute('class','map-sentiment-pattern');
          hatch.removeAttribute('data-sentiment-side');
          hatch.style.stroke = paint.evidence;
          ringLayer.append(hatch);
        }
      });
    }
    const sentiment = svg('circle', {cx,cy,r:outer+6,class:'map-glyph-sentiment','aria-label':`${toneLabel}: ${sentimentMix.reading}. Show sentiment.`});
    wireControl?.(sentiment,controls.find(part => part.key === 'sentiment')); ringLayer.append(sentiment);
    if (glyph.band !== 3 || !contributions.length) ringLayer.append(svg('circle', {cx,cy,r:radius-width/2-3,class:'map-glyph-hole','pointer-events':'none'}));
    if (glyph.risk !== 'none') {
      const risk = svg('g', {class:'map-glyph-risk','aria-label':`${riskReading(glyph.risk)}. Show risk factors.`});
      const size = (small ? 7 : 9)*(geometry.markerScale || 1);
      const marker = glyph.risk === 'detected'
        ? svg('path', {d:`M ${cx} ${cy-size-2} L ${cx+size+2} ${cy} L ${cx} ${cy+size+2} L ${cx-size-2} ${cy} Z`,class:'map-risk-dot','data-risk-shape':'diamond'})
        : svg('circle', {cx,cy,r:size,class:'map-risk-dot','data-risk-shape':glyph.risk === 'unknown' ? 'unknown' : 'circle'});
      risk.append(svg('circle', {cx,cy,r:12,class:'map-risk-target'}),marker);
      if (glyph.risk === 'unknown') risk.append(svg('text', {x:cx,y:cy,'text-anchor':'middle','dominant-baseline':'central',class:'map-risk-unknown'},'?'));
      wireControl?.(risk,controls.find(part => part.key === 'risk')); ringLayer.append(risk);
    }
  }
  function draw({ringLayer, graph, glyph, small, contributions, controls, score, name, label = name, toneLabel, centerAttributes, points, percent, wireControl, overview}) {
    ringLayer.replaceChildren();
    const {cx, cy, outer, radius, width} = metrics(glyph, small);
    graph.dataset.orbitCenter = `${cx},${cy}`;
    (glyph.liquidity ? drawWell : drawFace)({ringLayer, glyph, geometry:{cx,cy,outer,radius,width}, small, contributions, controls, toneLabel, points, percent, wireControl});
    const center = svg('g', {role:'button',tabindex:0,class:'map-score-center',...centerAttributes,'aria-label':`${name}, attention ${score === '—' ? 'unavailable' : score + ' of 100 points'}. Show attention overview.`});
    // Keep the decorative hole outside the overview button. Combining it with
    // the labels creates a disjoint hit target, with an untappable bounding-box
    // center on small rings. The label rectangle is one contiguous target.
    // High attention is a real solid pie, not a thick donut.
    const below = glyph.liquidity ? outer * 94 / 86 : outer; // clear the Well's hairline
    center.append(svg('rect', {x:cx-60,y:cy+below+12,width:120,height:42,fill:'transparent'}), svg('text', {x:cx,y:cy+below+27,'text-anchor':'middle',class:'map-center-text'},label), svg('text', {x:cx,y:cy+below+48,'text-anchor':'middle',class:'map-center-score'},score));
    center.addEventListener('click',overview);
    center.addEventListener('keydown',event => {if (['Enter',' '].includes(event.key)) {event.preventDefault(); overview();}});
    ringLayer.append(center);
  }
  // The Well: a memecoin's sigil, drawn around its liquidity. The server computes
  // every radius and state (memecoin_well.py) in units where the frame's outer
  // edge is 86; this only scales them. Rows draw the same marks from well_svg().
  const WELL_GAUGE = 9, WELL_STANDARDS_R = 73;
  const arcPath = (cx, cy, r, a0, a1) => `M ${cx + r*Math.cos(a0)} ${cy + r*Math.sin(a0)} A ${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${cx + r*Math.cos(a1)} ${cy + r*Math.sin(a1)}`;
  const ringShape = (cx, cy, r, a0, a1, attrs) => a1 - a0 >= Math.PI*2 - 1e-6 ? svg('circle', {...attrs, cx, cy, r}) : svg('path', {...attrs, d:arcPath(cx, cy, r, a0, a1)});
  function drawWell({ringLayer, glyph, geometry, contributions, controls = [], points, percent, wireControl}) {
    ringLayer.replaceChildren();
    const {cx, cy, outer} = geometry, u = outer / 86, well = glyph.liquidity, radii = well.radii || {};
    const flow = glyph.flow || {}, standards = glyph.standards;
    const paint = patterns(ringLayer, u);
    Object.assign(ringLayer.dataset, {band:String(glyph.band),sentiment:glyph.sentiment,sentimentMix:glyph.sentimentMix.state,risk:glyph.risk,mix:glyph.mix,well:'',lock:well.lock,depth:well.depth || 'unknown'});
    // Frame: an attention gauge, filled clockwise from the top to score/100,
    // split in the colors of where attention came from.
    const width = WELL_GAUGE * u, gauge = 86*u - width/2;
    ringLayer.style.setProperty('--map-band-stroke', String(width + 4*u));
    ringLayer.append(svg('circle', {cx, cy, r:gauge, class:'map-score-track', 'stroke-width':width}));
    const sweepAll = Math.min(100, Math.max(0, glyph.score || 0)) / 100 * Math.PI * 2;
    let angle = -Math.PI / 2;
    contributions.forEach(part => {
      const end = angle + part.share * sweepAll;
      const segment = ringShape(cx, cy, gauge, angle, end, {class:'map-score-segment', 'data-score-key':part.key, 'aria-label':`${part.label}: ${points(part.value)}, ${percent(part)}`, 'stroke-width':width});
      segment.style.stroke = color(part);
      segment.append(svg('title', {}, `${part.label}: ${points(part.value)} · ${percent(part)}`));
      wireControl?.(segment, part); ringLayer.append(segment);
      if (paint[part.key]) {
        const pattern = segment.cloneNode(false);
        pattern.setAttribute('class', 'map-score-pattern'); pattern.setAttribute('aria-hidden', 'true');
        ['data-score-key','aria-label','role','tabindex','aria-pressed'].forEach(name => pattern.removeAttribute(name));
        pattern.style.stroke = paint[part.key]; ringLayer.append(pattern);
      }
      angle = end;
    });
    // The last hour's buyers (green, from the top) against its sellers (dashed red).
    const hair = 93*u;
    if (flow.share != null) {
      const split = -Math.PI/2 + flow.share * Math.PI * 2;
      if (flow.share > 0) ringLayer.append(ringShape(cx, cy, hair, -Math.PI/2, split, {class:'well-bull', 'aria-hidden':'true'}));
      if (flow.share < 1) ringLayer.append(ringShape(cx, cy, hair, split, Math.PI*1.5, {class:'well-bear', 'aria-hidden':'true'}));
    }
    const trading = svg('circle', {cx, cy, r:hair, class:'map-glyph-sentiment well-flow-target', 'aria-label':`${flow.reading || 'No trades read in the last hour'}. Show trading.`});
    wireControl?.(trading, controls.find(part => part.key === 'sentiment')); ringLayer.append(trading);
    // Nine ticks, one per RATi standard: bright met, red failed, dim not checked yet.
    if (standards && standards.marks?.length) {
      const ticks = svg('g', {class:'well-standards', 'aria-label':`${standards.reading}. Show standards.`});
      const step = Math.PI * 2 / standards.marks.length, gap = Math.min(0.16, step * 0.3);
      standards.marks.forEach((mark, index) => {
        const a0 = -Math.PI/2 + index*step + gap/2;
        ticks.append(svg('path', {d:arcPath(cx, cy, WELL_STANDARDS_R*u, a0, a0 + step - gap), class:'well-standard', 'data-state':mark.state}));
      });
      ticks.append(svg('title', {}, standards.reading));
      wireControl?.(ticks, controls.find(part => part.key === 'standards')); ringLayer.append(ticks);
    }
    // The well itself is one control: it opens the liquidity reading.
    const depth = well.depth || 'unknown';
    const body = svg('g', {class:'well-body', 'aria-label':`${well.reading} Show liquidity.`});
    const chamber = radii.chamber * u, water = radii.real * u, ghost = radii.quoted * u, line = radii.line * u;
    body.append(svg('circle', {cx, cy, r:chamber, class:'well-chamber', 'data-depth':depth}));
    if (well.real != null && ghost > water + u) body.append(svg('circle', {cx, cy, r:(ghost + water)/2, class:'well-phantom', 'stroke-width':ghost - water}));
    if (water > 0) body.append(svg('circle', {cx, cy, r:water, class:'well-water', 'data-depth':depth}));
    // Several pools: one wedge each, busiest first from the top; every other one shaded.
    const shares = (well.pools || []).map(pool => pool.share).filter(share => share > 0);
    if (water > 0 && shares.length > 1) {
      const total = shares.reduce((sum, share) => sum + share, 0);
      const at = a => `${cx + water*Math.cos(a)} ${cy + water*Math.sin(a)}`;
      let a0 = -Math.PI / 2;
      const starts = shares.map((share, index) => {
        const a1 = a0 + share / total * Math.PI * 2, start = a0;
        if (index % 2) body.append(svg('path', {d:`M ${cx} ${cy} L ${at(a0)} A ${water} ${water} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${at(a1)} Z`, class:'well-wedge'}));
        a0 = a1; return start;
      });
      starts.forEach(a => body.append(svg('line', {x1:cx, y1:cy, x2:cx + water*Math.cos(a), y2:cy + water*Math.sin(a), class:'well-split'})));
    }
    if (ghost && Math.abs(ghost - water) > u) body.append(svg('circle', {cx, cy, r:ghost, class:`well-ghost${ghost < water ? ' well-ghost--inside' : ''}`}));
    if (water < line) body.append(svg('circle', {cx, cy, r:line, class:'well-line'}));
    if (well.real == null) body.append(svg('text', {x:cx, y:cy, 'text-anchor':'middle', 'dominant-baseline':'central', class:'well-unknown'}, '?'));
    if (well.lock === 'open') {
      const gap = Math.min(100, Math.max(5, well.lock_left_pct || 0)) / 100 * Math.PI * 2, bottom = Math.PI / 2;
      if (gap < Math.PI * 2 - 0.01) body.append(svg('path', {d:arcPath(cx, cy, chamber, bottom + gap/2, bottom - gap/2 + Math.PI*2), class:'well-wall', 'data-lock':'open'}));
    } else body.append(svg('circle', {cx, cy, r:chamber, class:'well-wall', 'data-lock':well.lock}));
    if (well.pulled) {
      const r = chamber + 4*u, h = 0.2, bottom = Math.PI / 2;
      body.append(svg('path', {d:`M ${cx} ${cy} L ${cx + r*Math.cos(bottom-h)} ${cy + r*Math.sin(bottom-h)} A ${r} ${r} 0 0 1 ${cx + r*Math.cos(bottom+h)} ${cy + r*Math.sin(bottom+h)} Z`, class:'well-cut'}));
      const zig = [[0,4*u],[5*u,chamber*.35],[-4*u,chamber*.6],[3*u,chamber*.85],[0,r]].map(([x,y]) => `${cx+x},${cy+y}`).join(' ');
      body.append(svg('polyline', {points:zig, class:'well-crack'}));
    }
    body.append(svg('title', {}, well.reading));
    wireControl?.(body, controls.find(part => part.key === 'liquidity')); ringLayer.append(body);
    // A risk factor notches the top; unknown risk draws nothing.
    if (glyph.risk === 'detected' || glyph.risk === 'significant') {
      const risk = svg('g', {class:'map-glyph-risk', 'aria-label':`${riskReading(glyph.risk)}. Show risk factors.`});
      // The ring's shapes: detected is a diamond, 1+ significant a circle.
      const notch = glyph.risk === 'detected'
        ? svg('path', {d:`M ${cx} ${cy - 101*u} L ${cx + 7*u} ${cy - 93*u} L ${cx} ${cy - 85*u} L ${cx - 7*u} ${cy - 93*u} Z`, class:'well-risk', 'data-risk':'detected', 'data-risk-shape':'diamond'})
        : svg('circle', {cx, cy:cy - 93*u, r:7*u, class:'well-risk', 'data-risk':'significant', 'data-risk-shape':'circle'});
      risk.append(svg('circle', {cx, cy:cy - 93*u, r:12, class:'map-risk-target'}), notch);
      wireControl?.(risk, controls.find(part => part.key === 'risk')); ringLayer.append(risk);
    }
  }
  // The attention index runs 0-100; the band gives the bare number a scale.
  const attentionReading = (score, band) => score === '—' || score === null || score === undefined
    ? 'Attention unavailable'
    : `Attention ${score} of 100 points (${band === 3 ? 'high, 70+' : band === 2 ? 'medium, 40–69' : 'low, under 40'})`;
  window.RatiRingGlyph = {metrics, draw, drawFace, drawWell, readSentiment, riskReading, attentionReading};
})();
