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
  const metrics = (glyph, small) => {
    const outer = glyph.band === 1 ? (small ? 36 : 46) : (small ? 56 : 72);
    const width = small ? 16 : 20;
    return {cx:small ? 180 : 380, cy:small ? 184 : 218, outer, radius:outer-width/2, width};
  };
  function draw({ringLayer, graph, glyph, small, contributions, controls, score, name, label = name, toneLabel, centerAttributes, points, percent, wireControl, overview}) {
    ringLayer.replaceChildren();
    const {cx, cy, outer, radius, width} = metrics(glyph, small);
    Object.assign(ringLayer.dataset, {band:String(glyph.band),sentiment:glyph.sentiment,risk:glyph.risk,mix:glyph.mix});
    graph.dataset.orbitCenter = `${cx},${cy}`;
    ringLayer.style.setProperty("--map-band-stroke", String(width));
    ringLayer.append(svg('circle', {cx, cy, r:radius, class:'map-score-track', 'stroke-width':width}));
    let angle = -Math.PI / 2;
    contributions.forEach(part => {
      const sweep = part.share * Math.PI * 2, end = angle + sweep;
      const solid = glyph.band === 3, r = solid ? outer : radius;
      const arc = `M ${cx + r*Math.cos(angle)} ${cy + r*Math.sin(angle)} A ${r} ${r} 0 ${sweep > Math.PI ? 1 : 0} 1 ${cx + r*Math.cos(end)} ${cy + r*Math.sin(end)}`;
      const d = solid ? `M ${cx} ${cy} L ${arc.slice(2)} Z` : arc;
      const attrs = {class:'map-score-segment', 'aria-label':`${part.label}: ${points(part.value)}, ${percent(part)}`, 'stroke-width':solid ? 0 : width};
      const segment = contributions.length === 1 ? svg('circle', {...attrs,cx,cy,r}) : svg('path', {...attrs,d});
      segment.style[solid ? 'fill' : 'stroke'] = color(part); angle = end;
      segment.append(svg('title', {}, `${part.label}: ${points(part.value)} · ${percent(part)}`));
      wireControl(segment,part); ringLayer.append(segment);
    });
    const sentiment = svg('circle', {cx,cy,r:outer+6,class:'map-glyph-sentiment','aria-label':`${toneLabel}: ${titleCase(glyph.sentiment)}. Show evidence tone.`});
    wireControl(sentiment,controls.find(part => part.key === 'sentiment')); ringLayer.append(sentiment);
    const center = svg('g', {role:'button',tabindex:0,class:'map-score-center',...centerAttributes,'aria-label':`${name}, attention ${score === '—' ? 'unavailable' : score + ' points'}. Show attention overview.`});
    // Keep the decorative hole outside the overview button. Combining it with
    // the labels creates a disjoint hit target, with an untappable bounding-box
    // center on small rings. The label rectangle is one contiguous target.
    // High attention is a real solid pie, not a thick donut.
    if (glyph.band !== 3 || !contributions.length) ringLayer.append(svg('circle', {cx,cy,r:radius-width/2-3,class:'map-glyph-hole','pointer-events':'none'}));
    center.append(svg('rect', {x:cx-60,y:cy+outer+12,width:120,height:42,fill:'transparent'}), svg('text', {x:cx,y:cy+outer+27,'text-anchor':'middle',class:'map-center-text'},label), svg('text', {x:cx,y:cy+outer+48,'text-anchor':'middle',class:'map-center-score'},score));
    center.addEventListener('click',overview);
    center.addEventListener('keydown',event => {if (['Enter',' '].includes(event.key)) {event.preventDefault(); overview();}});
    ringLayer.append(center);
    if (glyph.risk !== 'low') {
      const risk = svg('g', {class:'map-glyph-risk','aria-label':`Risk: ${titleCase(glyph.risk)}. Show risk assessment.`});
      risk.append(svg('circle', {cx,cy,r:12,class:'map-risk-target'}),svg('circle', {cx,cy,r:small ? 7 : 9,class:'map-risk-dot'}));
      if (glyph.risk === 'unknown') risk.append(svg('text', {x:cx,y:cy,'text-anchor':'middle','dominant-baseline':'central',class:'map-risk-unknown'},'?'));
      wireControl(risk,controls.find(part => part.key === 'risk')); ringLayer.append(risk);
    }
  }
  window.RatiRingGlyph = {metrics, draw};
})();
