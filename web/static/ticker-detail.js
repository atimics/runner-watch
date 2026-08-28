(() => {
  'use strict';

  const dataNode = document.getElementById('tickerPageData');
  if (!dataNode) return;

  let pageData;
  try {
    pageData = JSON.parse(dataNode.textContent);
  } catch (_) {
    return;
  }

  const ticker = pageData.ticker;
  const company = pageData.company;
  const initialPressure = pageData.initialPressure;
  const callData = Array.isArray(pageData.calls) ? pageData.calls : [];
  const chart = document.getElementById('priceChart');
  const chartStatus = document.getElementById('chartStatus');
  const chartEvents = document.getElementById('chartEvents');
  const annotationColors = {
    pulse: '#f5c66b',
    positive: '#57e389',
    risk: '#ff6b7a',
    media: '#75a7ff',
    neutral: '#a3ada8',
  };
  const chartModeNotes = {
    tape: 'OHLCV bars, volume, and session VWAP',
    gravity: 'Repeated prices, heavy volume, and session reference zones',
    astrology: 'Fixed-anchor Fibonacci crowd lore · astrology for the tape',
  };

  let allPoints = [];
  let chartAnnotations = [];
  let chartLevels = [];
  let chartFibonacci = null;
  let chartStructure = {};
  let chartMode = 'tape';
  let activeRange = '1D';

  function chartMoney(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return '$' + number.toFixed(number < 1 ? 4 : number < 10 ? 3 : 2);
  }

  function visibleAnnotations(points) {
    if (!points.length) return [];
    const start = new Date(points[0].time).getTime();
    const end = new Date(points.at(-1).time).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(end)) return [];
    return chartAnnotations.filter(item => {
      const stamp = new Date(item.time).getTime();
      return Number.isFinite(stamp) && stamp >= start && stamp <= end + 2 * 60 * 60 * 1000;
    });
  }

  function renderChartEvents(items) {
    chartEvents.replaceChildren();
    chartEvents.hidden = !items.length;
    items.forEach(item => {
      const safeTone = Object.hasOwn(annotationColors, item.tone) ? item.tone : 'neutral';
      const hasLink = typeof item.url === 'string' && /^https?:\/\//i.test(item.url);
      const chip = document.createElement(hasLink ? 'a' : 'span');
      chip.className = 'chart-event-chip tone-' + safeTone;
      if (hasLink) {
        chip.href = item.url;
        chip.target = '_blank';
        chip.rel = 'noopener';
      }

      const dot = document.createElement('i');
      const copy = document.createElement('span');
      const label = document.createElement('b');
      const time = document.createElement('small');
      label.textContent = item.label || item.category || 'Detected event';
      const stamp = new Date(item.time);
      time.textContent = Number.isFinite(stamp.getTime())
        ? stamp.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
          })
        : '';
      copy.append(label, time);
      chip.append(dot, copy);
      chartEvents.append(chip);
    });
  }

  function structureChip(label, value, note) {
    const chip = document.createElement('span');
    const name = document.createElement('small');
    const price = document.createElement('b');
    const copy = document.createElement('i');
    name.textContent = label;
    price.textContent = value;
    copy.textContent = note || '';
    chip.append(name, price, copy);
    return chip;
  }

  function renderStructureSummary() {
    const strip = document.getElementById('chartStructure');
    strip.replaceChildren();
    const support = chartStructure?.support;
    const resistance = chartStructure?.resistance;

    if (chartMode === 'tape') {
      if (chartStructure?.vwap) {
        strip.append(structureChip('VWAP', chartMoney(chartStructure.vwap), 'session average'));
      }
      if (chartStructure?.previous_close) {
        strip.append(
          structureChip('Previous', chartMoney(chartStructure.previous_close), 'last close')
        );
      }
      const opening = chartStructure?.opening_range;
      if (opening) {
        strip.append(
          structureChip(
            'Opening range',
            `${chartMoney(opening.low)}–${chartMoney(opening.high)}`,
            'first 30m'
          )
        );
      }
    } else if (chartMode === 'gravity') {
      if (support) {
        strip.append(
          structureChip(
            'Support zone',
            `${chartMoney(support.low)}–${chartMoney(support.high)}`,
            `${support.touches || 0} touches · ${Math.round(
              (support.strength || 0) * 100
            )} strength`
          )
        );
      }
      if (resistance) {
        strip.append(
          structureChip(
            'Resistance zone',
            `${chartMoney(resistance.low)}–${chartMoney(resistance.high)}`,
            `${resistance.touches || 0} touches · ${Math.round(
              (resistance.strength || 0) * 100
            )} strength`
          )
        );
      }
    } else if (chartFibonacci) {
      const nearest = (chartFibonacci.levels || []).find(
        item => Number(item.ratio) === Number(chartFibonacci.nearest_ratio)
      );
      if (nearest) {
        strip.append(
          structureChip(
            `${nearest.label} crowd line`,
            chartMoney(nearest.price),
            `${Number(chartFibonacci.nearest_distance_pct).toFixed(2)}% away`
          )
        );
      }
      strip.append(
        structureChip(
          'Current retrace',
          `${Number(chartFibonacci.retracement_pct).toFixed(1)}%`,
          `${chartFibonacci.direction} impulse`
        )
      );
    }
    strip.hidden = !strip.children.length;
  }

  function normalizeChartPoints(points) {
    return points
      .map(point => ({
        time: point.time,
        price: Number(point.price),
        open: Number(point.open ?? point.price),
        high: Number(point.high ?? point.price),
        low: Number(point.low ?? point.price),
        close: Number(point.close ?? point.price),
        volume: Number(point.volume || 0),
        vwap: Number(point.vwap),
        session: String(point.session || 'regular'),
      }))
      .filter(point => Number.isFinite(point.price));
  }

  function chartGrid(top, bottom) {
    return [0.25, 0.5, 0.75]
      .map(ratio => {
        const y = top + (bottom - top) * ratio;
        return `<line x1="0" y1="${y}" x2="600" y2="${y}" stroke="#26302c" stroke-width="1" opacity=".55" vector-effect="non-scaling-stroke"/>`;
      })
      .join('');
  }

  function chartSessionBands(points, xAt, top, bottom) {
    let bands = '';
    let sessionStart = 0;
    for (let index = 1; index <= points.length; index += 1) {
      if (index < points.length && points[index].session === points[sessionStart].session) {
        continue;
      }
      if (points[sessionStart].session !== 'regular') {
        const spacing = points.length > 1 ? 300 / (points.length - 1) : 0;
        const startX = Math.max(0, xAt(sessionStart) - spacing);
        const endX = Math.min(600, xAt(index - 1) + spacing);
        bands += `<rect x="${startX.toFixed(1)}" y="${top}" width="${Math.max(
          1,
          endX - startX
        ).toFixed(1)}" height="${bottom - top}" fill="#75a7ff" opacity=".035"/>`;
      }
      sessionStart = index;
    }
    return bands;
  }

  function chartVolumes(points, xAt, volumeTop, volumeBottom) {
    const maxVolume = Math.max(1, ...points.map(point => point.volume));
    const width = Math.max(1, Math.min(7, 520 / points.length));
    return points
      .map((point, index) => {
        const height = Math.max(
          1,
          (point.volume / maxVolume) * (volumeBottom - volumeTop)
        );
        const fill = point.close >= point.open ? '#57e389' : '#ff6b7a';
        return `<rect x="${(xAt(index) - width / 2).toFixed(1)}" y="${(
          volumeBottom - height
        ).toFixed(1)}" width="${width.toFixed(1)}" height="${height.toFixed(
          1
        )}" fill="${fill}" opacity=".34"/>`;
      })
      .join('');
  }

  function chartCandles(points, xAt, yAt, line, lineColor) {
    if (points.length > 150) {
      return `<path d="${line}" fill="none" stroke="${lineColor}" stroke-width="3" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`;
    }

    const width = Math.max(1.3, Math.min(8, 420 / points.length));
    return points
      .map((point, index) => {
        const x = xAt(index);
        const openY = yAt(point.open);
        const closeY = yAt(point.close);
        const highY = yAt(point.high);
        const lowY = yAt(point.low);
        const fill = point.close >= point.open ? '#57e389' : '#ff6b7a';
        const bodyY = Math.min(openY, closeY);
        const bodyHeight = Math.max(1.5, Math.abs(openY - closeY));
        return `<line x1="${x.toFixed(1)}" y1="${highY.toFixed(1)}" x2="${x.toFixed(
          1
        )}" y2="${lowY.toFixed(
          1
        )}" stroke="${fill}" stroke-width="1" opacity=".9" vector-effect="non-scaling-stroke"/><rect x="${(
          x -
          width / 2
        ).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${width.toFixed(
          1
        )}" height="${bodyHeight.toFixed(1)}" rx=".6" fill="${fill}"/>`;
      })
      .join('');
  }

  function chartVwapPath(points, xAt, yAt) {
    let path = '';
    let lastKey = '';
    points.forEach((point, index) => {
      if (!Number.isFinite(point.vwap)) return;
      const stamp = new Date(point.time);
      const key = `${stamp.getFullYear()}-${stamp.getMonth()}-${stamp.getDate()}-${
        point.session
      }`;
      const command = key === lastKey ? 'L' : 'M';
      path += `${command}${xAt(index).toFixed(1)} ${yAt(point.vwap).toFixed(1)} `;
      lastKey = key;
    });
    return path;
  }

  function chartReferences(yAt, top, bottom) {
    const references = [];
    if (chartMode === 'tape' && Number.isFinite(Number(chartStructure?.previous_close))) {
      const y = yAt(Number(chartStructure.previous_close));
      if (y >= top && y <= bottom) {
        references.push(
          `<line x1="0" y1="${y.toFixed(
            1
          )}" x2="600" y2="${y.toFixed(
            1
          )}" stroke="#78857f" stroke-width="1" stroke-dasharray="5 5" opacity=".7" vector-effect="non-scaling-stroke"/>`
        );
      }
    }
    if (chartMode === 'gravity') {
      chartLevels.forEach(level => {
        const zoneLow = Math.max(top, Math.min(bottom, yAt(Number(level.high))));
        const zoneHigh = Math.max(top, Math.min(bottom, yAt(Number(level.low))));
        if (zoneHigh < top || zoneLow > bottom) return;
        const fill =
          level.side === 'resistance'
            ? '#ff6b7a'
            : level.side === 'support'
              ? '#57e389'
              : '#f5c66b';
        references.push(
          `<rect x="0" y="${zoneLow.toFixed(1)}" width="600" height="${Math.max(
            2,
            zoneHigh - zoneLow
          ).toFixed(1)}" fill="${fill}" opacity="${(
            0.04 +
            (level.strength || 0) * 0.1
          ).toFixed(2)}"/><line x1="0" y1="${yAt(Number(level.price)).toFixed(
            1
          )}" x2="600" y2="${yAt(Number(level.price)).toFixed(
            1
          )}" stroke="${fill}" stroke-width="1" opacity=".55" vector-effect="non-scaling-stroke"/>`
        );
      });
    }
    if (chartMode === 'astrology' && chartFibonacci) {
      (chartFibonacci.levels || []).forEach(level => {
        const y = yAt(Number(level.price));
        if (y < top || y > bottom) return;
        references.push(
          `<line x1="0" y1="${y.toFixed(1)}" x2="600" y2="${y.toFixed(
            1
          )}" stroke="#b99aff" stroke-width="1.3" stroke-dasharray="3 5" opacity=".72" vector-effect="non-scaling-stroke"/>`
        );
      });
    }
    return references;
  }

  function chartMarkers(points, coordinates) {
    const annotations = visibleAnnotations(points);
    const markers = annotations
      .map((item, annotationIndex) => {
        const eventTime = new Date(item.time).getTime();
        let pointIndex = 0;
        points.forEach((point, index) => {
          const distance = Math.abs(new Date(point.time).getTime() - eventTime);
          const nearestDistance = Math.abs(
            new Date(points[pointIndex].time).getTime() - eventTime
          );
          if (distance < nearestDistance) pointIndex = index;
        });

        const [x, y] = coordinates[pointIndex];
        const markerColor = annotationColors[item.tone] || annotationColors.neutral;
        if (item.type === 'pulse_entry') {
          return `<line x1="${x.toFixed(
            1
          )}" y1="8" x2="${x.toFixed(
            1
          )}" y2="178" stroke="${markerColor}" stroke-width="1.5" stroke-dasharray="4 4" opacity=".72" vector-effect="non-scaling-stroke"/><circle cx="${x.toFixed(
            1
          )}" cy="${y.toFixed(
            1
          )}" r="6" fill="#090b0b" stroke="${markerColor}" stroke-width="3" vector-effect="non-scaling-stroke"/>`;
        }

        const markerY = Math.max(9, y - 10 - (annotationIndex % 3) * 8);
        const size = 5;
        return `<line x1="${x.toFixed(1)}" y1="${markerY.toFixed(
          1
        )}" x2="${x.toFixed(1)}" y2="${y.toFixed(
          1
        )}" stroke="${markerColor}" stroke-width="1" opacity=".65" vector-effect="non-scaling-stroke"/><path d="M${x.toFixed(
          1
        )} ${(markerY - size).toFixed(1)} L${(x + size).toFixed(1)} ${markerY.toFixed(
          1
        )} L${x.toFixed(1)} ${(markerY + size).toFixed(1)} L${(x - size).toFixed(
          1
        )} ${markerY.toFixed(
          1
        )} Z" fill="${markerColor}" stroke="#090b0b" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
      })
      .join('');
    return { annotations, markers };
  }

  function showChartUnavailable() {
    chartStatus.textContent = 'Chart not available';
    chartStatus.classList.add('visible');
    renderChartEvents([]);
  }

  function draw(points) {
    if (!points.length) {
      showChartUnavailable();
      return;
    }

    const clean = normalizeChartPoints(points);
    const values = clean.flatMap(point => [point.low, point.high]).filter(Number.isFinite);
    if (values.length < 2) {
      showChartUnavailable();
      return;
    }

    const low = Math.min(...values);
    const high = Math.max(...values);
    const spread = Math.max(high - low, high * 0.002, 0.001);
    const top = 13;
    const bottom = 178;
    const volumeTop = 195;
    const volumeBottom = 242;
    const xAt = index => (clean.length === 1 ? 300 : (index / (clean.length - 1)) * 600);
    const yAt = value => bottom - ((value - low) / spread) * (bottom - top);
    const coordinates = clean.map((point, index) => [xAt(index), yAt(point.close)]);
    const line = coordinates
      .map(([x, y], index) => `${index ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`)
      .join(' ');
    const rising = clean.at(-1).close >= clean[0].open;
    const lineColor = rising ? '#57e389' : '#ff6b7a';
    const sessionBands = chartSessionBands(clean, xAt, top, bottom);
    const grid = chartGrid(top, bottom);
    const volumes = chartVolumes(clean, xAt, volumeTop, volumeBottom);
    const candles = chartCandles(clean, xAt, yAt, line, lineColor);
    const vwapPath = chartVwapPath(clean, xAt, yAt);
    const references = chartReferences(yAt, top, bottom);
    const { annotations, markers } = chartMarkers(clean, coordinates);
    const vwap =
      vwapPath && chartMode !== 'astrology'
        ? `<path d="${vwapPath}" fill="none" stroke="#f5c66b" stroke-width="1.5" stroke-dasharray="2 3" opacity=".9" vector-effect="non-scaling-stroke"/>`
        : '';

    chart.innerHTML = `${sessionBands}${grid}${references.join(
      ''
    )}${volumes}${candles}${vwap}${markers}`;
    chartStatus.hidden = true;
    renderChartEvents(annotations);
    renderStructureSummary();
    chart.setAttribute(
      'aria-label',
      `${chartMode} price and volume chart with ${annotations.length} detected event${
        annotations.length === 1 ? '' : 's'
      }`
    );
    document.getElementById('chartLow').textContent = '$' + low.toFixed(low < 1 ? 4 : 2);
    document.getElementById('chartHigh').textContent = '$' + high.toFixed(high < 1 ? 4 : 2);
  }

  function visibleChartPoints() {
    if (activeRange === '5D') return allPoints;
    const latest = allPoints.length ? new Date(allPoints.at(-1).time).getTime() : 0;
    const points = allPoints.filter(
      point => new Date(point.time).getTime() >= latest - 24 * 60 * 60 * 1000
    );
    return points.length > 1 ? points : allPoints.slice(-30);
  }

  function selectRange(range) {
    activeRange = range;
    document.querySelectorAll('[data-range]').forEach(button => {
      button.classList.toggle('active', button.dataset.range === range);
    });
    draw(visibleChartPoints());
  }

  function callChartAnnotations() {
    return callData.flatMap(call => {
      const items = [
        {
          type: 'call_entry',
          tone: 'positive',
          time: call.entry_at,
          label: `Call · ${chartMoney(call.entry_price)}`,
        },
      ];
      if (call.exit_at) {
        items.push({
          type: 'call_exit',
          tone: 'media',
          time: call.exit_at,
          label: `Call closed · ${chartMoney(call.exit_price)}`,
        });
      }
      return items;
    });
  }

  const chartRequest = fetch(`/api/t/${encodeURIComponent(ticker)}/chart`)
    .then(response => response.json())
    .then(data => {
      allPoints = data.points || [];
      chartAnnotations = [...(data.annotations || []), ...callChartAnnotations()];
      chartLevels = data.levels || [];
      chartFibonacci = data.fibonacci || null;
      chartStructure = data.structure || {};
      selectRange('1D');
    })
    .catch(() => draw([]));

  document.querySelectorAll('[data-range]').forEach(button => {
    button.addEventListener('click', () => selectRange(button.dataset.range));
  });
  document.querySelectorAll('[data-chart-mode]').forEach(button => {
    button.addEventListener('click', () => {
      chartMode = button.dataset.chartMode;
      document.querySelectorAll('[data-chart-mode]').forEach(item => {
        item.classList.toggle('active', item === button);
      });
      document.getElementById('chartModeNote').textContent = chartModeNotes[chartMode];
      draw(visibleChartPoints());
    });
  });

  document.getElementById('shareButton').addEventListener('click', async () => {
    const share = { title: ticker, text: `${ticker} · ${company}`, url: location.href };
    try {
      if (navigator.share) {
        await navigator.share(share);
      } else {
        await navigator.clipboard.writeText(location.href);
        document.getElementById('shareButton').textContent = '✓';
      }
    } catch (_) {}
  });

  document.querySelectorAll('[data-call-time]').forEach(item => {
    const stamp = new Date(item.dateTime);
    if (Number.isFinite(stamp.getTime())) {
      item.textContent = stamp.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    }
  });

  const callStatus = document.getElementById('callStatus');

  function setTickerAction(button, label, detail) {
    if (!button) return;
    const title = document.createElement('strong');
    title.textContent = label;
    const meta = document.createElement('span');
    meta.textContent = detail;
    button.replaceChildren(title, meta);
  }

  document.getElementById('makeCallButton')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    setTickerAction(button, 'Make Call', 'Stamping quote…');
    if (callStatus) callStatus.textContent = '';
    try {
      const response = await fetch(`/api/calls/${encodeURIComponent(ticker)}`, {
        method: 'POST',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not make Call');
      location.reload();
    } catch (error) {
      if (callStatus) callStatus.textContent = error.message || 'Could not make Call';
      setTickerAction(button, 'Make Call', 'Public · stamped');
      button.disabled = false;
    }
  });

  document.getElementById('closeCallButton')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    setTickerAction(button, 'Close Call', 'Stamping exit…');
    try {
      const response = await fetch(
        `/api/calls/${encodeURIComponent(button.dataset.callId)}/close`,
        { method: 'POST' }
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not close Call');
      const reward = Number(result.reward) || 0;
      if (reward > 0) {
        setTickerAction(button, 'Call won', `+${reward} Flash`);
        if (callStatus) callStatus.textContent = `Balance: ${result.balance} Flash`;
        setTimeout(() => location.reload(), 700);
      } else {
        location.reload();
      }
    } catch (error) {
      if (callStatus) callStatus.textContent = error.message || 'Could not close Call';
      setTickerAction(button, 'Close Call', 'Try again');
      button.disabled = false;
    }
  });

  const commissionButton = document.getElementById('commissionButton');
  const commissionStatus = document.getElementById('commissionStatus');

  async function pollFlash(jobId) {
    const response = await fetch(`/api/research/jobs/${encodeURIComponent(jobId)}`);
    const result = await response.json().catch(() => ({}));
    if (result.status === 'complete' && result.url) {
      location.href = result.url;
      return;
    }
    if (result.status === 'failed') {
      commissionButton.disabled = false;
      setTickerAction(commissionButton, 'Retry Flash', '100 Flash');
      commissionStatus.textContent = result.error || 'Flash failed. You can retry.';
      return;
    }
    commissionStatus.textContent =
      'Flash is filling its context and writing your private report…';
    setTimeout(() => pollFlash(jobId), 2500);
  }

  if (commissionButton?.dataset.jobId) pollFlash(commissionButton.dataset.jobId);
  commissionButton?.addEventListener('click', async () => {
    commissionButton.disabled = true;
    setTickerAction(commissionButton, 'Daily Flash', 'Starting…');
    commissionStatus.textContent = 'Sending the report to the queue…';
    try {
      const response = await fetch(`/api/research/${encodeURIComponent(ticker)}`, {
        method: 'POST',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(result.detail || result.error || 'Could not start Flash');
      }
      if (Number.isFinite(Number(result.balance))) {
        document.querySelectorAll('[data-flash-balance]').forEach(item => {
          item.textContent = result.balance;
        });
      }
      if (result.status === 'complete' && result.url) {
        location.href = result.url;
        return;
      }
      setTickerAction(commissionButton, 'Daily Flash', 'Researching…');
      pollFlash(result.job_id);
    } catch (error) {
      commissionButton.disabled = false;
      setTickerAction(commissionButton, 'Retry Flash', '100 Flash');
      commissionStatus.textContent = error.message || 'Could not start Flash';
    }
  });

  const generateComment = document.getElementById('generateComment');
  const commentStatus = document.getElementById('commentStatus');
  const commentList = document.getElementById('commentList');

  function relativeCommentTime(value) {
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (!Number.isFinite(seconds)) return '';
    if (seconds < 60) return 'now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    if (seconds < 604800) return Math.floor(seconds / 86400) + 'd ago';
    return new Date(value).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function refreshCommentTimes() {
    document.querySelectorAll('[data-comment-time]').forEach(item => {
      item.textContent = relativeCommentTime(item.dateTime);
    });
  }

  function renderComment(comment) {
    const item = document.createElement('li');
    item.dataset.commentId = comment.id;

    const avatarData = comment.avatar || {};
    const avatarVariant = (key, maximum) => {
      const value = Number(avatarData[key]);
      return Number.isInteger(value) && value >= 0 && value < maximum ? value : 0;
    };
    const avatar = document.createElement('span');
    avatar.className = [
      'comment-avatar',
      'comment-avatar-ai',
      `avatar-tone-${avatarVariant('tone', 12)}`,
      `avatar-frame-${avatarVariant('frame', 6)}`,
      `avatar-eyes-${avatarVariant('eyes', 6)}`,
      `avatar-signal-${avatarVariant('signal', 6)}`,
    ].join(' ');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.append(document.createElement('i'));

    const copy = document.createElement('div');
    copy.className = 'comment-copy';
    const head = document.createElement('header');
    const author = document.createElement('strong');
    author.textContent = avatarData.name || comment.alias || 'Unknown Signal';
    head.append(author);
    const ability = document.createElement('span');
    ability.className = 'comment-ability';
    ability.textContent = avatarData.ability || 'Research Lens';
    if (avatarData.ability_description) ability.title = avatarData.ability_description;
    head.append(ability);
    if (comment.is_owner) {
      const owner = document.createElement('span');
      owner.className = 'comment-owner';
      owner.textContent = 'You';
      head.append(owner);
    }
    const meta = document.createElement('small');
    meta.className = 'comment-meta';
    const stamp = document.createElement('time');
    stamp.dateTime = comment.created_at;
    stamp.dataset.commentTime = '';
    stamp.textContent = relativeCommentTime(comment.created_at);
    meta.append(stamp);
    if (comment.ai_generated) {
      meta.prepend(document.createTextNode('Flash drafted · '));
    }
    if (comment.is_owner) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.deleteComment = comment.id;
      remove.textContent = 'delete';
      meta.append(document.createTextNode(' · '), remove);
    }

    const body = document.createElement('p');
    body.textContent = comment.body;
    copy.append(head, meta, body);
    item.append(avatar, copy);
    return item;
  }

  refreshCommentTimes();
  generateComment?.addEventListener('click', async () => {
    generateComment.disabled = true;
    commentStatus.textContent = 'Flash is drafting your comment…';
    try {
      const response = await fetch(`/api/comments/${encodeURIComponent(ticker)}`, {
        method: 'POST',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not post');
      commentList.prepend(renderComment(result.comment));
      commentStatus.textContent = `Posted · ${result.balance} Flash left`;
      document.querySelectorAll('[data-flash-balance]').forEach(item => {
        item.textContent = result.balance;
      });
      document.getElementById('discussionCount').textContent = result.count;
      document.getElementById('commentEmpty').hidden = true;
    } catch (error) {
      commentStatus.textContent = error.message || 'Could not post';
    } finally {
      generateComment.disabled = false;
    }
  });

  commentList?.addEventListener('click', async event => {
    const button = event.target.closest('[data-delete-comment]');
    if (!button) return;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/comments/${encodeURIComponent(button.dataset.deleteComment)}`,
        { method: 'DELETE' }
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not delete');
      button.closest('li')?.remove();
      const count = document.getElementById('discussionCount');
      count.textContent = Math.max(0, Number(count.textContent || 0) - 1);
      document.getElementById('commentEmpty').hidden = Boolean(commentList.children.length);
    } catch (error) {
      commentStatus.textContent = error.message || 'Could not delete';
      button.disabled = false;
    }
  });

  function compactVolume(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    if (Math.abs(number) >= 1e6) return (number / 1e6).toFixed(1) + 'm';
    if (Math.abs(number) >= 1e3) return (number / 1e3).toFixed(0) + 'k';
    return number.toFixed(0);
  }

  function renderPressure(pressure) {
    if (!pressure?.available) {
      document.getElementById('pressureNote').textContent = 'No bars yet.';
      return;
    }
    document.getElementById('pressureLabel').textContent = pressure.label;
    document.getElementById('buyPressure').textContent =
      Number(pressure.buy_pressure_pct).toFixed(0) +
      '% · ' +
      compactVolume(pressure.estimated_buy_volume);
    document.getElementById('sellPressure').textContent =
      Number(pressure.sell_pressure_pct).toFixed(0) +
      '% · ' +
      compactVolume(pressure.estimated_sell_volume);
    document.getElementById('volumeBurst').textContent =
      pressure.volume_burst == null ? '—' : Number(pressure.volume_burst).toFixed(1) + '×';
    document.getElementById('buyPressureBar').style.width = pressure.buy_pressure_pct + '%';
    document.getElementById('sellPressureBar').style.width = pressure.sell_pressure_pct + '%';
    document.getElementById('pressureNote').textContent = pressure.bar_count + ' bars';
  }

  function renderGate(gate) {
    if (!gate) return;
    const root = document.getElementById('evidenceGate');
    if (!root) return;
    root.className = 'evidence-gate gate-' + gate.state;
    document.getElementById('gateSummary').textContent = gate.summary || 'Watching';
    document.getElementById('gateCount').textContent = gate.count + '/' + gate.threshold;
    const gateNotes =
      (gate.blockers && gate.blockers.length ? gate.blockers : gate.checks) || [];
    document.getElementById('gateChecks').textContent =
      gateNotes.join(' · ') || 'Waiting for more evidence.';
    document.getElementById('gateBaseline').textContent =
      gate.baseline_summary || 'Building a same-time historical baseline.';
    document.querySelectorAll('#gateMeter i').forEach((item, index) => {
      item.classList.toggle('hit', index < gate.count);
    });
  }

  renderPressure(initialPressure);
  chartRequest.finally(async () => {
    try {
      const response = await fetch('/api/t/' + encodeURIComponent(ticker) + '/pressure');
      if (!response.ok) return;
      const data = await response.json();
      renderPressure(data.pressure);
      renderGate(data.evidence_gate);
    } catch (_) {}
  });
})();
