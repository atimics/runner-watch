(() => {
  'use strict';
  function attach(graph) {
    const root = graph.closest('.entity-map');
    const plus = root.querySelector('[data-entity-zoom-in]');
    const minus = root.querySelector('[data-entity-zoom-out]');
    const reset = root.querySelector('[data-entity-fit]');
    const label = root.querySelector('[data-entity-zoom]');
    let home, view, suppressClickUntil = 0;
    const pointers = new Map();
    const world = (x,y) => new DOMPoint(x,y).matrixTransform(graph.getScreenCTM().inverse());
    function paint() {
      graph.setAttribute('viewBox', `${view.x} ${view.y} ${view.width} ${view.height}`);
      const zoom = home.width/view.width;
      label.textContent = `${zoom.toFixed(1)}×`;
      graph.dataset.zoom = String(zoom);
      minus.disabled = zoom <= 1.001; plus.disabled = zoom >= 11.999;
    }
    function fit(bounds) {
      if (bounds) home = {...bounds};
      view = {...home}; paint();
    }
    function zoom(factor, anchor = {x:view.x+view.width/2,y:view.y+view.height/2}) {
      const target = Math.max(1,Math.min(12,home.width/view.width*factor));
      const width = home.width/target, ratio = width/view.width;
      view = {x:anchor.x-(anchor.x-view.x)*ratio,y:anchor.y-(anchor.y-view.y)*ratio,width,height:view.height*ratio};
      paint();
    }
    plus.addEventListener('click',()=>zoom(1.4));
    minus.addEventListener('click',()=>zoom(1/1.4));
    reset.addEventListener('click',()=>fit());
    graph.addEventListener('wheel',event=>{
      event.preventDefault();
      zoom(Math.exp(-Math.max(-100,Math.min(100,event.deltaY))*0.005),world(event.clientX,event.clientY));
    },{passive:false});
    graph.addEventListener('keydown',event=>{
      if (event.target !== graph) return;
      if (['+','=','-','0','Home','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key)) event.preventDefault();
      if (['+','='].includes(event.key)) zoom(1.4);
      else if (event.key === '-') zoom(1/1.4);
      else if (['0','Home'].includes(event.key)) fit();
      else if (event.key.startsWith('Arrow')) {
        view.x += event.key === 'ArrowRight' ? view.width/8 : event.key === 'ArrowLeft' ? -view.width/8 : 0;
        view.y += event.key === 'ArrowDown' ? view.height/8 : event.key === 'ArrowUp' ? -view.height/8 : 0;
        paint();
      }
    });
    graph.addEventListener('click',event=>{
      if (performance.now() < suppressClickUntil) {event.preventDefault();event.stopImmediatePropagation();}
    },true);
    graph.addEventListener('pointerdown',event=>{
      if (event.button !== 0 || pointers.size >= 2) return;
      if (!pointers.size) suppressClickUntil = 0;
      const tap = pointers.size === 0;
      if (!tap) pointers.forEach(pointer=>{pointer.tap=false;});
      pointers.set(event.pointerId,{
        x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,
        tap,at:performance.now(),link:event.target.closest('a[href]'),
      });
      graph.classList.add('orbit-interacting');
    });
    window.addEventListener('pointermove',event=>{
      const previous = pointers.get(event.pointerId);
      if (!previous) return;
      const before = [...pointers.values()];
      const next = {...previous,x:event.clientX,y:event.clientY};
      pointers.set(event.pointerId,next);
      if (pointers.size === 1 && Math.hypot(next.x-next.startX,next.y-next.startY) < 5) return;
      next.tap = false;
      graph.setPointerCapture(event.pointerId);
      suppressClickUntil = performance.now()+400;
      if (pointers.size === 2) {
        const after = [...pointers.values()];
        const midpoint = pair => ({x:(pair[0].x+pair[1].x)/2,y:(pair[0].y+pair[1].y)/2});
        const distance = pair => Math.hypot(pair[0].x-pair[1].x,pair[0].y-pair[1].y);
        const oldMid = midpoint(before), newMid = midpoint(after), anchor = world(oldMid.x,oldMid.y);
        if (distance(before) > 0) zoom(distance(after)/distance(before),anchor);
        const moved = world(newMid.x,newMid.y);
        view.x += anchor.x-moved.x; view.y += anchor.y-moved.y;
      } else {
        const from = world(previous.x,previous.y), to = world(next.x,next.y);
        view.x += from.x-to.x; view.y += from.y-to.y;
      }
      paint();
    });
    function release(event) {
      const pointer = pointers.get(event.pointerId);
      pointers.delete(event.pointerId);
      if (!pointers.size) graph.classList.remove('orbit-interacting');
      if (graph.hasPointerCapture(event.pointerId)) graph.releasePointerCapture(event.pointerId);
      // Some touch browsers withhold a compatibility click after a map gesture.
      // Activate a short, still touch once; keep the link's native keyboard path.
      if (event.type === 'pointerup' && event.pointerType === 'touch' && pointer?.tap &&
          performance.now()-pointer.at < 500 &&
          Math.hypot(event.clientX-pointer.startX,event.clientY-pointer.startY) < 5 &&
          pointer.link && graph.contains(pointer.link)) {
        suppressClickUntil = 0;
        pointer.link.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
        suppressClickUntil = performance.now()+400;
      }
    }
    window.addEventListener('pointerup',release);
    window.addEventListener('pointercancel',release);
    window.addEventListener('blur',()=>{pointers.clear();graph.classList.remove('orbit-interacting');});
    return {fit,zoom};
  }
  window.EntityMapNavigation = {attach};
})();
