/* Orbits a map's nodes around its centre while keeping their labels upright.

   The map scripts tag rotating shapes with data-orbit and nodes with
   data-orbit-anchor="x,y". Every frame this rotates those shapes about the map
   centre (data-orbit-center), then counter-rotates each anchored node about its
   own position so the text never turns upside down. Clicking the map pauses the
   orbit; clicking again resumes it. Scenes that are already animating keep
   their own geometry, so the angle holds while data-phase is "moving". */
(() => {
  'use strict';
  const REVOLUTION_MS = 120000;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const numbers = value => String(value || '').split(/[\s,]+/).filter(Boolean).map(Number);
  function apply(svg, angle) {
    const [cx, cy] = numbers(svg.dataset.orbitCenter);
    if (![cx, cy].every(Number.isFinite)) return;
    svg.querySelectorAll('[data-orbit]').forEach(shape => {
      shape.setAttribute('transform', `rotate(${angle} ${cx} ${cy})`);
    });
    svg.querySelectorAll('[data-orbit-anchor]').forEach(node => {
      const [x, y] = numbers(node.dataset.orbitAnchor);
      if (![x, y].every(Number.isFinite)) return;
      node.setAttribute('transform', `rotate(${angle} ${cx} ${cy}) rotate(${-angle} ${x} ${y})`);
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