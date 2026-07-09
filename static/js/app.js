const LOADING_MSGS = [
  'Analyzing…',
  'Pulling stats and records…',
  'Evaluating head-to-head data…',
  'Weighing situational factors…',
  'Computing win probability…',
  'Almost done…',
];

let fullText = '';
let currentSport = '', currentComp1 = '', currentComp2 = '';
let resultData = null;

// ── Init ──────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  loadAccuracy();
  loadTrending();
  loadEvents();
});

// ── Form ──────────────────────────────────────────

function toTitleCase(str) {
  return str.trim().replace(/\b\w/g, c => c.toUpperCase());
}

document.getElementById('matchupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  currentSport = toTitleCase(document.getElementById('sport').value);
  currentComp1 = toTitleCase(document.getElementById('comp1').value);
  currentComp2 = toTitleCase(document.getElementById('comp2').value);
  const context = document.getElementById('context').value.trim();

  document.getElementById('sport').value = currentSport;
  document.getElementById('comp1').value = currentComp1;
  document.getElementById('comp2').value = currentComp2;

  if (!currentSport || !currentComp1 || !currentComp2) return;

  fullText = '';
  resultData = null;
  showLoading(currentComp1, currentComp2);

  const body = new FormData();
  body.append('sport', currentSport);
  body.append('comp1', currentComp1);
  body.append('comp2', currentComp2);
  body.append('context', context);

  try {
    const res = await fetch('/analyze', { method: 'POST', body });
    if (!res.ok) { showError(await res.text()); return; }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let msgIdx = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      fullText += decoder.decode(value, { stream: true });
      const newIdx = Math.min(Math.floor(fullText.length / 60), LOADING_MSGS.length - 1);
      if (newIdx !== msgIdx) {
        msgIdx = newIdx;
        document.getElementById('loadingMsg').textContent = LOADING_MSGS[msgIdx];
      }
    }

    if (fullText.startsWith('ERROR')) { showError(fullText); return; }

    resultData = parse(fullText);
    render(resultData);

    // Fetch odds in parallel with saving
    const oddsData = await loadOdds(currentSport, currentComp1, currentComp2);
    if (oddsData && oddsData.found) {
      renderOdds(oddsData, resultData);
    }
    saveToJournal(currentSport, currentComp1, currentComp2, fullText, resultData, oddsData);

  } catch (err) {
    showError('Something went wrong. Please try again.');
  }
});

// ── Parser ────────────────────────────────────────

function parse(text) {
  const get = (key) => {
    const m = text.match(new RegExp(`^${key}:\\s*(.+)$`, 'm'));
    return m ? m[1].trim() : '';
  };
  const getMultiline = (key) => {
    const m = text.match(new RegExp(`^${key}:\\s*(.+?)(?=\\n[A-Z_]+:|$)`, 'ms'));
    return m ? m[1].trim() : get(key);
  };
  return {
    aPct:       parseInt(get('A_PCT'))  || 50,
    bPct:       parseInt(get('B_PCT'))  || 50,
    winner:     get('WINNER'),
    confidence: get('CONFIDENCE') || 'Medium',
    edge:       parseInt(get('EDGE')) || null,
    knowA:      get('KNOW_A'),
    knowB:      get('KNOW_B'),
    reason:     getMultiline('REASON'),
  };
}

// ── Render result ─────────────────────────────────

function render(d) {
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('resultSection').style.display  = 'block';
  document.getElementById('sharePreview').style.display   = 'none';

  document.getElementById('compNameA').textContent = currentComp1;
  document.getElementById('compNameB').textContent = currentComp2;

  const aWins = d.winner && (
    d.winner.toLowerCase().includes(currentComp1.split(' ')[0].toLowerCase()) ||
    currentComp1.toLowerCase().includes(d.winner.split(' ')[0].toLowerCase())
  );

  document.getElementById('compBlockA').className = 'comp-block' + (aWins  ? ' comp-winner' : '');
  document.getElementById('compBlockB').className = 'comp-block' + (!aWins ? ' comp-winner' : '');

  animateCount('compPctA', d.aPct, '%');
  animateCount('compPctB', d.bPct, '%');

  setTimeout(() => {
    document.getElementById('probFillA').style.width = d.aPct + '%';
    document.getElementById('probFillB').style.width = d.bPct + '%';
  }, 100);

  const conf  = d.confidence;
  const emoji = conf === 'High' ? '🔥' : conf === 'Medium' ? '⚡' : '🎲';
  const badge = document.getElementById('confBadge');
  badge.textContent = `${emoji} ${conf} Confidence`;
  badge.className   = 'conf-badge-big conf-' + conf.toLowerCase();
  document.getElementById('confRow').style.display = 'flex';

  // Knowledge cards
  const existing = document.getElementById('knowCards');
  if (existing) existing.remove();
  if (d.knowA || d.knowB) {
    const knowDiv = document.createElement('div');
    knowDiv.id = 'knowCards';
    knowDiv.className = 'know-cards';
    if (d.knowA) knowDiv.innerHTML += `<div class="know-card"><span class="know-name">${currentComp1}</span><span class="know-text">${d.knowA}</span></div>`;
    if (d.knowB) knowDiv.innerHTML += `<div class="know-card"><span class="know-name">${currentComp2}</span><span class="know-text">${d.knowB}</span></div>`;
    document.getElementById('resultSection').querySelector('.result-card').appendChild(knowDiv);
  }

  const reasonEl = document.getElementById('reasonText');
  if (d.reason) {
    reasonEl.innerHTML = d.reason
      .split(/\n+/)
      .filter(s => s.trim())
      .map(s => `<span class="reason-line">${s.trim()}</span>`)
      .join('');
    reasonEl.style.display = 'block';
  } else {
    reasonEl.style.display = 'none';
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function animateCount(elId, target, suffix = '') {
  const el   = document.getElementById(elId);
  let current = 0;
  const step  = target / 40;
  const timer = setInterval(() => {
    current = Math.min(current + step, target);
    el.textContent = Math.round(current) + suffix;
    if (current >= target) clearInterval(timer);
  }, 25);
}

// ── Share image ───────────────────────────────────

async function shareImage() {
  if (!resultData) return;
  const btn = document.getElementById('shareBtn');
  const orig = btn.textContent;
  btn.textContent = '⏳ Generating…';
  btn.disabled = true;
  btn.style.opacity = '0.6';

  try {
    const res = await fetch('/share/image', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        comp1: currentComp1, comp2: currentComp2,
        sport: currentSport,
        a_pct: resultData.aPct, b_pct: resultData.bPct,
        winner: resultData.winner, confidence: resultData.confidence,
        reason: resultData.reason || '',
      }),
    });
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);

    // Try native share (iOS/Android) — supports Instagram Stories
    if (navigator.canShare && navigator.canShare({ files: [new File([blob], 'whowins.png', { type: 'image/png' })] })) {
      const file = new File([blob], 'whowins-prediction.png', { type: 'image/png' });
      await navigator.share({
        files: [file],
        title: `${currentComp1} vs ${currentComp2}`,
        text: `${currentComp1} ${resultData.aPct}% vs ${currentComp2} ${resultData.bPct}% — via WhoWins`,
      });
    } else {
      // Fallback: show preview + download
      const preview = document.getElementById('sharePreview');
      document.getElementById('shareImg').src = url;
      document.getElementById('shareDownload').href = url;
      preview.style.display = 'block';
      preview.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      alert('Could not generate share card. Try again.');
    }
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
    btn.style.opacity = '';
  }
}

// ── Trending ──────────────────────────────────────

async function loadTrending() {
  try {
    const res  = await fetch('/api/trending');
    const data = await res.json();
    if (!data.length) return;
    const list = document.getElementById('trendingList');
    list.innerHTML = '';
    data.forEach(item => {
      const el = document.createElement('button');
      el.className = 'trending-pill';
      el.innerHTML = `<span class="trending-sport">${item.sport}</span> ${item.comp_a} vs ${item.comp_b}`;
      el.onclick = () => {
        document.getElementById('sport').value = item.sport;
        document.getElementById('comp1').value = item.comp_a;
        document.getElementById('comp2').value = item.comp_b;
        document.getElementById('matchupForm').dispatchEvent(new Event('submit'));
      };
      list.appendChild(el);
    });
    document.getElementById('trendingSection').style.display = 'block';
  } catch (_) {}
}

// ── Events feed ───────────────────────────────────

async function loadEvents() {
  try {
    const res  = await fetch('/api/events');
    const data = await res.json();
    if (!data.length) return;
    const grid = document.getElementById('eventsGrid');
    grid.innerHTML = '';
    data.forEach(ev => {
      if (!ev.comp_a || !ev.comp_b) return;
      const el = document.createElement('button');
      el.className = 'event-card';
      el.innerHTML = `
        <div class="event-sport">${ev.sport}${ev.date ? ' · ' + ev.date : ''}</div>
        <div class="event-matchup">${ev.comp_a} <span>vs</span> ${ev.comp_b}</div>`;
      el.onclick = () => {
        document.getElementById('sport').value = ev.sport;
        document.getElementById('comp1').value = ev.comp_a;
        document.getElementById('comp2').value = ev.comp_b;
        document.getElementById('matchupForm').dispatchEvent(new Event('submit'));
      };
      grid.appendChild(el);
    });
    document.getElementById('eventsSection').style.display = 'block';
  } catch (_) {}
}

// ── Accuracy stat ─────────────────────────────────

async function loadAccuracy() {
  try {
    const res  = await fetch('/api/accuracy');
    const data = await res.json();
    if (data.rate === null || data.total < 5) return;
    const bar = document.getElementById('accuracyBar');
    document.getElementById('accuracyPct').textContent = data.rate + '%';
    document.getElementById('accuracySub').textContent = `(${data.total} settled picks)`;
    bar.style.display = 'flex';
    setTimeout(() => {
      document.getElementById('accuracyFill').style.width = data.rate + '%';
    }, 200);
  } catch (_) {}
}

// ── Helpers ───────────────────────────────────────

function showLoading(comp1, comp2) {
  document.getElementById('formSection').style.display    = 'none';
  document.getElementById('resultSection').style.display  = 'none';
  document.getElementById('loadingSection').style.display = 'block';
  document.getElementById('loadName1').textContent = comp1;
  document.getElementById('loadName2').textContent = comp2;
  document.getElementById('loadingMsg').textContent = LOADING_MSGS[0];
}

function showError(msg) {
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('formSection').style.display    = 'block';
  const el = document.getElementById('inlineError');
  if (el) {
    el.textContent = '⚠️ ' + msg.replace('ERROR: ', '');
    el.style.display = 'block';
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => { el.style.display = 'none'; }, 6000);
  }
}

function newSearch() {
  document.getElementById('resultSection').style.display = 'none';
  document.getElementById('formSection').style.display   = 'block';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function copyResult() {
  if (!resultData) return;
  const text = `${currentComp1}: ${resultData.aPct}% vs ${currentComp2}: ${resultData.bPct}%\nWinner: ${resultData.winner} (${resultData.confidence} confidence)\nSport: ${currentSport}\nvia WhoWins`;
  navigator.clipboard.writeText(text).then(() => {
    const msg = document.getElementById('copiedMsg');
    msg.classList.add('show');
    setTimeout(() => msg.classList.remove('show'), 2000);
  });
}

// ── Odds ──────────────────────────────────────────

async function loadOdds(sport, comp1, comp2) {
  try {
    const params = new URLSearchParams({ sport, comp1, comp2 });
    const res = await fetch('/api/odds?' + params);
    return await res.json();
  } catch (_) { return null; }
}

function renderOdds(odds, prediction) {
  const existing = document.getElementById('oddsSection');
  if (existing) existing.remove();

  const aWins = prediction.winner && (
    prediction.winner.toLowerCase().includes(currentComp1.split(' ')[0].toLowerCase()) ||
    currentComp1.toLowerCase().includes(prediction.winner.split(' ')[0].toLowerCase())
  );

  // Upset alert: AI backs the underdog
  const aiPct    = aWins ? prediction.aPct : prediction.bPct;
  const vegasPct = aWins ? odds.a_pct : odds.b_pct;
  const isUpset  = aiPct >= 55 && vegasPct <= 40;

  const div = document.createElement('div');
  div.id = 'oddsSection';
  div.className = 'odds-card' + (isUpset ? ' odds-upset' : '');
  div.innerHTML = `
    ${isUpset ? '<div class="upset-alert">⚡ UPSET ALERT — AI backs the underdog</div>' : ''}
    <div class="odds-header">
      <span class="odds-label">Vegas Odds</span>
      <span class="odds-sub">vs AI Prediction</span>
    </div>
    <div class="odds-row">
      <div class="odds-col">
        <div class="odds-name">${currentComp1}</div>
        <div class="odds-compare">
          <span class="odds-ai">${prediction.aPct}% <span class="odds-tag">AI</span></span>
          <span class="odds-sep">·</span>
          <span class="odds-veg">${odds.a_pct}% <span class="odds-tag">Vegas</span></span>
        </div>
      </div>
      <div class="odds-col">
        <div class="odds-name">${currentComp2}</div>
        <div class="odds-compare">
          <span class="odds-ai">${prediction.bPct}% <span class="odds-tag">AI</span></span>
          <span class="odds-sep">·</span>
          <span class="odds-veg">${odds.b_pct}% <span class="odds-tag">Vegas</span></span>
        </div>
      </div>
    </div>
  `;

  const actions = document.querySelector('.result-actions');
  actions.parentNode.insertBefore(div, actions);
}

async function saveToJournal(sport, comp1, comp2, analysis, prediction, odds) {
  if (!analysis || analysis.startsWith('ERROR')) return;
  try {
    const payload = {
      sport, comp1, comp2, analysis,
      a_pct: prediction?.aPct,
      b_pct: prediction?.bPct,
      a_odds_pct: (odds?.found && odds?.a_pct) ? odds.a_pct : null,
      b_odds_pct: (odds?.found && odds?.b_pct) ? odds.b_pct : null,
    };
    await fetch('/journal/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (_) {}
}
