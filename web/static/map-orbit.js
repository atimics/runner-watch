/* Orbits a map's nodes around its centre while keeping their labels upright.

   The map scripts tag rotating shapes with data-orbit and nodes with
   data-orbit-anchor="x,y". For a ring layout the map also declares the track
   it places nodes on (data-orbit-track="rx,ry"), and each node travels along
   that fixed ellipse: its spoke grows and shrinks as it goes, so the ring
   itself stays put instead of rotating as a rigid shape. Without a track the
   nodes just revolve at a constant radius. Either way the labels never turn
   upside down. Clicking the map pauses the orbit; clicking again resumes it.
   Scenes that are already animating keep their own geometry, so the angle
   holds while data-phase is "moving". */
(() => {
  'use strict';
  const REVOLUTION_MS = 120000;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const numbers = value => String(value || '').split(/[\s,]+/).filter(Boolean).map(Number);
  const fading = element => ['opacity', 'stroke-opacity'].some(name => {
    const value = element.getAttribute(name);
    return value !== null && Number(value) < 1;
  });
  function apply(svg, angle) {
    const [cx, cy] = numbers(svg.dataset.orbitCenter);
    if (![cx, cy].every(Number.isFinite)) return;
    const [rx, ry] = numbers(svg.dataset.orbitTrack);
    const ring = [rx, ry].every(value => Number.isFinite(value) && value > 0);
    const radians = angle * Math.PI / 180;
    const cos = Math.cos(radians), sin = Math.sin(radians);
    // Affine map that rotates the ring track onto itself, about the centre.
    const track = `translate(${cx} ${cy}) matrix(${cos} ${ry / rx * sin} ${-rx / ry * sin} ${cos} 0 0) translate(${-cx} ${-cy})`;
    svg.querySelectorAll('[data-orbit]').forEach(shape => {
      if (!fading(shape)) shape.setAttribute('transform', ring ? track : `rotate(${angle} ${cx} ${cy})`);
    });
    svg.querySelectorAll('[data-orbit-anchor]').forEach(node => {
      if (fading(node)) return;
      const [x, y] = numbers(node.dataset.orbitAnchor);
      if (![x, y].every(Number.isFinite)) return;
      if (ring) {
        // Swing the node along the fixed track instead of around it.
        const start = Math.atan2((y - cy) / ry, (x - cx) / rx);
        const targetX = cx + rx * Math.cos(start + radians);
        const targetY = cy + ry * Math.sin(start + radians);
        node.setAttribute('transform', `translate(${targetX - x} ${targetY - y})`);
      } else {
        // No track: revolve at a constant radius with an upright label.
        node.setAttribute('transform', `rotate(${angle} ${cx} ${cy}) rotate(${-angle} ${x} ${y})`);
      }
    });
  }
  function attach(svg) {
    if (!svg || svg.dataset.orbitReady) return;
    svg.dataset.orbitReady = '1';
    svg.addEventListener('click', () => svg.classList.toggle('orbit-paused'));
    let angle = Number(svg.dataset.orbitAngle) || 0, previous = 0;
    function step(now) {
      const elapsed = previous ? now - previous : 0;
      previous = now;
      const idle = !reduce.matches && !svg.classList.contains('orbit-paused') && svg.dataset.phase !== 'moving';
      if (idle) {
        angle = (angle + elapsed / REVOLUTION_MS * 360) % 360;
        svg.dataset.orbitAngle = angle;
      }
      if (!reduce.matches) apply(svg, angle);
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  window.ratiOrbit = {attach};
})();