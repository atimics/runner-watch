(() => {
  'use strict';
  const svg = (tag, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    if (text != null) element.textContent = text;
    return element;
  };
  const titleCase = value => value[0].toUpperCase() + value.slice(1);
  const color = part => `var(--indicator-${part.key})`;
  let nextPattern = 0;
  function patterns(layer, scale = 1) {
    const prefix = `ring-pattern-${++nextPattern}`;
    const defs = svg('defs', {});
    const stripe = svg('pattern', {id:`${prefix}-stripe`,patternUnits:'userSpaceOnUse',width:7*scale,height:7*scale,patternTransform:'rotate(45)'});
    stripe.append(svg('path', {d:`M 0 0 V ${7*scale}`,class:'map-pattern-line','stroke-width':2.2*scale}));
    const dot = svg('pattern', {id:`${prefix}-dot`,patternUnits:'userSpaceOnUse',width:7*scale,height:7*scale});
    dot.append(svg('circle', {cx:3.5*scale,cy:3.5*scale,r:1.4*scale,class:'map-pattern-dot'}));
    defs.append(stripe,dot); layer.append(defs);
    return {evidence:`url(#${prefix}-stripe)`,social:`url(#${prefix}-dot)`};
  }
  const readSentiment = value => {
    const mix = value || {};
    const valid = mix.state === 'available' && [mix.bullish,mix.bearish].every(n => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1) && Math.abs(mix.bullish + mix.bearish - 1) < 0.000001;
    const bullish = valid ? mix.bullish : null;
    const percent = valid ? Math.round(bullish * 100) : null;
    return {state:valid ? 'available' : 'unknown',bullish,bearish:valid ? 1-bullish : null,
      reading:valid ? `${percent}% bullish, ${100-percent}% bearish` : 'bullish/bearish split unavailable',
      compact:valid ? `▲${percent}% / ▼${100-percent}%` : '▲— / ▼—',
      basis:typeof mix.basis === 'string' ? mix.basis : 'Saved directional assessments'};
  };
  const metrics = (glyph, small) => {
    const outer = glyph.band === 1 ? (small ? 36 : 46) : (small ? 56 : 72);
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
    if (glyph.risk !== 'low') {
      const risk = svg('g', {class:'map-glyph-risk','aria-label':`Risk: ${titleCase(glyph.risk)}. Show risk assessment.`});
      const size = (small ? 7 : 9)*(geometry.markerScale || 1);
      const marker = glyph.risk === 'high'
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
    drawFace({ringLayer, glyph, geometry:{cx,cy,outer,radius,width}, small, contributions, controls, toneLabel, points, percent, wireControl});
    const center = svg('g', {role:'button',tabindex:0,class:'map-score-center',...centerAttributes,'aria-label':`${name}, attention ${score === '—' ? 'unavailable' : score + ' points'}. Show attention overview.`});
    // Keep the decorative hole outside the overview button. Combining it with
    // the labels creates a disjoint hit target, with an untappable bounding-box
    // center on small rings. The label rectangle is one contiguous target.
    // High attention is a real solid pie, not a thick donut.
    center.append(svg('rect', {x:cx-60,y:cy+outer+12,width:120,height:42,fill:'transparent'}), svg('text', {x:cx,y:cy+outer+27,'text-anchor':'middle',class:'map-center-text'},label), svg('text', {x:cx,y:cy+outer+48,'text-anchor':'middle',class:'map-center-score'},score));
    center.addEventListener('click',overview);
    center.addEventListener('keydown',event => {if (['Enter',' '].includes(event.key)) {event.preventDefault(); overview();}});
    ringLayer.append(center);
  }
  window.RatiRingGlyph = {metrics, draw, drawFace, readSentiment};
})();
