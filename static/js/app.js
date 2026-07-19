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
  loadIndexRankBar();
  loadDailyBestBet();
  if (localStorage.getItem('ww_disclaimer') === 'dismissed') {
    const b = document.getElementById('disclaimerBanner');
    if (b) b.style.display = 'none';
  }
});

function dismissDisclaimer() {
  localStorage.setItem('ww_disclaimer', 'dismissed');
  const b = document.getElementById('disclaimerBanner');
  if (b) { b.style.opacity = '0'; b.style.transform = 'translateY(-8px)'; setTimeout(() => b.style.display = 'none', 300); }
}

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
    verifyAnalysis(resultData); // fire-and-forget challenger pass

    // Fetch odds + prediction markets in parallel
    const [oddsData] = await Promise.all([
      loadOdds(currentSport, currentComp1, currentComp2),
      loadMarkets(currentSport, currentComp1, currentComp2, resultData.aPct, resultData.bPct),
    ]);
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
    aPct:          parseInt(get('A_PCT'))  || 50,
    bPct:          parseInt(get('B_PCT'))  || 50,
    winner:        get('WINNER'),
    confidence:    get('CONFIDENCE') || 'Medium',
    edge:          parseInt(get('EDGE')) || null,
    knowA:         get('KNOW_A'),
    knowB:         get('KNOW_B'),
    reason:        getMultiline('REASON'),
    comebackAlert: get('COMEBACK_ALERT'),
    scoutTip:      getMultiline('SCOUT_TIP'),
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

  // Comeback alert
  const oldCB = document.getElementById('comebackAlert');
  if (oldCB) oldCB.remove();
  const cbText = d.comebackAlert && d.comebackAlert !== '—' && d.comebackAlert.trim();
  if (cbText) {
    const cbEl = document.createElement('div');
    cbEl.id = 'comebackAlert';
    cbEl.className = 'comeback-alert';
    cbEl.innerHTML = `<span class="comeback-icon">🔥</span><span class="comeback-text">${cbText}</span>`;
    document.getElementById('resultSection').querySelector('.result-card').appendChild(cbEl);
  }

  // Scout tip
  const oldTip = document.getElementById('scoutTip');
  if (oldTip) oldTip.remove();
  if (d.scoutTip && d.scoutTip.trim()) {
    const tipEl = document.createElement('div');
    tipEl.id = 'scoutTip';
    tipEl.className = 'scout-tip';
    tipEl.innerHTML = `<span class="scout-tip-label">🎯 Scout's Eye</span><span class="scout-tip-text">${d.scoutTip}</span>`;
    document.getElementById('resultSection').querySelector('.result-card').appendChild(tipEl);
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

// ── Challenger verification pass ──────────────────

async function verifyAnalysis(d) {
  const card = document.getElementById('resultSection').querySelector('.result-card');
  if (!card) return;

  // Remove any prior badge
  const old = document.getElementById('verifyBadge');
  if (old) old.remove();

  const badge = document.createElement('div');
  badge.id = 'verifyBadge';
  badge.className = 'verify-checking';
  badge.textContent = '🔍 Cross-checking…';
  card.appendChild(badge);

  try {
    const res = await fetch('/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sport: currentSport, comp1: currentComp1, comp2: currentComp2,
        a_pct: d.aPct, b_pct: d.bPct,
        winner: d.winner, confidence: d.confidence,
      }),
    });
    const v = await res.json();

    if (v.verified || !v.real_adj) {
      badge.className = 'verify-passed';
      badge.textContent = '✓ Scout verified';
    } else {
      badge.className = 'verify-adjusted';
      badge.innerHTML = `⚡ Scout adjusted · <span class="verify-note">${v.note}</span>`;

      // Animate updated percentages
      if (v.new_a_pct !== d.aPct || v.new_b_pct !== d.bPct) {
        animateCount('compPctA', v.new_a_pct, '%');
        animateCount('compPctB', v.new_b_pct, '%');
        setTimeout(() => {
          const fa = document.getElementById('probFillA');
          const fb = document.getElementById('probFillB');
          if (fa) fa.style.width = v.new_a_pct + '%';
          if (fb) fb.style.width = v.new_b_pct + '%';
        }, 100);
        resultData.aPct = v.new_a_pct;
        resultData.bPct = v.new_b_pct;
      }
    }
  } catch (_) {
    const b = document.getElementById('verifyBadge');
    if (b) b.remove();
  }
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

// ── Mode switching ────────────────────────────────

function switchMode(mode) {
  const predict = document.getElementById('formSection');
  const search  = document.getElementById('searchSection');
  const result  = document.getElementById('resultSection');
  const loading = document.getElementById('loadingSection');
  document.getElementById('tabPredict').classList.toggle('active', mode === 'predict');
  document.getElementById('tabSearch').classList.toggle('active', mode === 'search');
  if (mode === 'predict') {
    predict.style.display = ''; search.style.display = 'none';
    result.style.display = 'none'; loading.style.display = 'none';
  } else {
    predict.style.display = 'none'; search.style.display = '';
    result.style.display = 'none'; loading.style.display = 'none';
    document.getElementById('marketSearchInput').focus();
  }
}

// ── Market Search ─────────────────────────────────

let searchTimer = null;

function onSearchInput(val) {
  clearTimeout(searchTimer);
  if (val.length < 2) return;
  searchTimer = setTimeout(() => runMarketSearch(), 500);
}

async function runMarketSearch() {
  const q = document.getElementById('marketSearchInput').value.trim();
  if (q.length < 2) return;

  const resultsWrap = document.getElementById('searchResults');
  const list        = document.getElementById('searchResultsList');
  const countEl     = document.getElementById('searchResultCount');
  const loadingEl   = document.getElementById('searchResultsLoading');

  resultsWrap.style.display = 'block';
  loadingEl.style.display   = 'inline';
  list.innerHTML = '';
  countEl.textContent = '';

  try {
    const res  = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    loadingEl.style.display = 'none';

    if (!data.results || !data.results.length) {
      list.innerHTML = '<div class="search-empty">No markets found for "' + q + '"</div>';
      return;
    }

    countEl.textContent = `${data.total} market${data.total !== 1 ? 's' : ''} found`;
    list.innerHTML = '';

    data.results.forEach(r => {
      const card = document.createElement('div');
      card.className = 'search-result-card';

      const pricesHtml = r.prices.slice(0,4).map(([outcome, pct]) =>
        `<span class="search-outcome"><span class="search-outcome-name">${outcome}</span><span class="search-outcome-pct">${pct}%</span></span>`
      ).join('');

      card.innerHTML = `
        <div class="search-result-top">
          <div class="search-badges">
            <span class="market-source-badge ${r.source==='Polymarket'?'badge-poly':'badge-kalshi'}">${r.source}</span>
            <span class="search-cat-badge">${r.category}</span>
          </div>
          <a href="${r.url}" target="_blank" class="market-link">Trade →</a>
        </div>
        <div class="search-question">${r.question}</div>
        <div class="search-prices">${pricesHtml}</div>
        <div class="search-vol">24h volume: $${r.volume24h.toLocaleString(undefined,{maximumFractionDigits:0})}</div>
      `;
      list.appendChild(card);
    });
  } catch (_) {
    loadingEl.style.display = 'none';
    list.innerHTML = '<div class="search-empty">Something went wrong. Try again.</div>';
  }
}

// ── Parlay Slip ───────────────────────────────────

let slip = [];   // [{sport, comp1, comp2, winner, pct, label}]
let slipOpen = false;

function addToSlip() {
  if (!resultData || !resultData.winner) return;

  const aWins = resultData.winner.toLowerCase().includes(currentComp1.split(' ')[0].toLowerCase()) ||
                currentComp1.toLowerCase().includes(resultData.winner.split(' ')[0].toLowerCase());

  const pick = {
    sport:   currentSport,
    comp1:   currentComp1,
    comp2:   currentComp2,
    winner:  resultData.winner,
    pct:     aWins ? resultData.aPct : resultData.bPct,
    label:   `${currentComp1} vs ${currentComp2}`,
  };

  // Avoid duplicate matchups
  const exists = slip.find(p => p.comp1 === pick.comp1 && p.comp2 === pick.comp2);
  if (exists) {
    const btn = document.getElementById('addSlipBtn');
    btn.textContent = '✓ Already in slip';
    setTimeout(() => { btn.textContent = '➕ Add to Slip'; }, 1500);
    return;
  }

  slip.push(pick);
  renderSlip();

  const fab = document.getElementById('slipFab');
  fab.style.display = 'flex';
  if (!slipOpen) {
    fab.classList.add('slip-fab-pulse');
    setTimeout(() => fab.classList.remove('slip-fab-pulse'), 600);
  }

  const btn = document.getElementById('addSlipBtn');
  btn.textContent = '✓ Added!';
  btn.classList.add('btn-added');
  setTimeout(() => { btn.textContent = '➕ Add to Slip'; btn.classList.remove('btn-added'); }, 1500);

  if (!slipOpen) openSlip();
}

function removeFromSlip(idx) {
  slip.splice(idx, 1);
  renderSlip();
  if (slip.length === 0) {
    document.getElementById('slipFab').style.display = 'none';
  }
}

function clearSlip() {
  slip = [];
  renderSlip();
  closeSlip();
  document.getElementById('slipFab').style.display = 'none';
}

function renderSlip() {
  const picksEl  = document.getElementById('slipPicks');
  const statsEl  = document.getElementById('slipStats');
  const lockBtn  = document.getElementById('lockSlipBtn');
  const countEl  = document.getElementById('slipCount');
  const fabCount = document.getElementById('slipFabCount');

  countEl.textContent  = `${slip.length} pick${slip.length !== 1 ? 's' : ''}`;
  fabCount.textContent = slip.length;

  picksEl.innerHTML = '';
  slip.forEach((p, i) => {
    const div = document.createElement('div');
    div.className = 'slip-pick';
    div.innerHTML = `
      <div class="slip-pick-info">
        <span class="slip-pick-sport">${p.sport}</span>
        <span class="slip-pick-match">${p.comp1} vs ${p.comp2}</span>
        <span class="slip-pick-winner">Pick: <strong>${p.winner}</strong> ${p.pct}%</span>
      </div>
      <button class="slip-pick-remove" onclick="removeFromSlip(${i})">✕</button>
    `;
    picksEl.appendChild(div);
  });

  if (slip.length >= 2) {
    const combined = slip.reduce((acc, p) => acc * (p.pct / 100), 1);
    const combinedPct  = (combined * 100).toFixed(1);
    const fairOdds = (1 / combined).toFixed(2);
    document.getElementById('slipPct').textContent  = `${combinedPct}%`;
    document.getElementById('slipOdds').textContent = `${fairOdds}x`;
    statsEl.style.display = 'flex';
    lockBtn.style.display = 'block';
  } else {
    statsEl.style.display = 'none';
    lockBtn.style.display = slip.length === 0 ? 'none' : 'none';
  }

  document.getElementById('slipSavedMsg').style.display = 'none';
}

async function lockSlip() {
  if (slip.length < 2) return;
  const btn = document.getElementById('lockSlipBtn');
  btn.textContent = 'Saving…';
  btn.disabled = true;

  try {
    const res = await fetch('/parlay/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ picks: slip }),
    });
    const data = await res.json();
    if (data.ok) {
      const msg = document.getElementById('slipSavedMsg');
      msg.style.display = 'block';
      btn.textContent = '✓ Saved!';
      setTimeout(() => {
        clearSlip();
        btn.textContent = '🔒 Lock Slip & Save to Journal';
        btn.disabled = false;
      }, 1800);
    }
  } catch (_) {
    btn.textContent = '🔒 Lock Slip & Save to Journal';
    btn.disabled = false;
  }
}

function openSlip()  { slipOpen = true;  document.getElementById('slipTray').style.display = 'flex'; }
function closeSlip() { slipOpen = false; document.getElementById('slipTray').style.display = 'none'; }
function toggleSlip() { slipOpen ? closeSlip() : openSlip(); }

// ── Prediction Markets ────────────────────────────

async function loadMarkets(sport, comp1, comp2, aiA, aiB) {
  try {
    const params = new URLSearchParams({ sport, comp1, comp2, ai_a: aiA, ai_b: aiB });
    const res = await fetch('/api/markets?' + params);
    const data = await res.json();
    if (data.markets && data.markets.length > 0) {
      renderMarkets(data, comp1, comp2, aiA, aiB);
    } else {
      document.getElementById('marketsSection').style.display = 'none';
    }
  } catch (_) {}
}

function renderMarkets(data, comp1, comp2, aiA, aiB) {
  const section = document.getElementById('marketsSection');
  const list    = document.getElementById('marketsList');
  const sub     = document.getElementById('marketsSub');
  const hasLive = data.has_live;

  sub.textContent = hasLive ? '🔴 Live markets found' : `${data.markets.length} market${data.markets.length > 1 ? 's' : ''} found`;
  if (hasLive) sub.classList.add('markets-live');

  list.innerHTML = '';
  data.markets.forEach(m => {
    const card = document.createElement('div');
    card.className = 'market-card' + (m.live ? ' market-live' : '');

    const aGap = m.a_gap ?? null;
    const bGap = m.b_gap ?? null;
    const dipA = m.dip_a;
    const dipB = m.dip_b;

    const aRow = m.a_price != null ? `
      <div class="market-row ${dipA ? 'market-dip' : ''}">
        <span class="market-name">${comp1}</span>
        <div class="market-prices">
          <span class="market-ai">AI ${aiA}%</span>
          <span class="market-sep">→</span>
          <span class="market-mkt">${m.source} ${m.a_price}%</span>
          ${aGap != null ? `<span class="market-gap ${aGap > 0 ? 'gap-pos' : 'gap-neg'}">${aGap > 0 ? '+' : ''}${aGap}</span>` : ''}
        </div>
        ${dipA ? '<span class="dip-badge">🔥 BUY THE DIP</span>' : ''}
      </div>` : '';

    const bRow = m.b_price != null ? `
      <div class="market-row ${dipB ? 'market-dip' : ''}">
        <span class="market-name">${comp2}</span>
        <div class="market-prices">
          <span class="market-ai">AI ${aiB}%</span>
          <span class="market-sep">→</span>
          <span class="market-mkt">${m.source} ${m.b_price}%</span>
          ${bGap != null ? `<span class="market-gap ${bGap > 0 ? 'gap-pos' : 'gap-neg'}">${bGap > 0 ? '+' : ''}${bGap}</span>` : ''}
        </div>
        ${dipB ? '<span class="dip-badge">🔥 BUY THE DIP</span>' : ''}
      </div>` : '';

    card.innerHTML = `
      <div class="market-card-top">
        <span class="market-source-badge ${m.source === 'Polymarket' ? 'badge-poly' : 'badge-kalshi'}">${m.source}</span>
        <span class="market-q">${m.question}</span>
        <a href="${m.url}" target="_blank" class="market-link">Trade →</a>
      </div>
      ${aRow}${bRow}
    `;
    list.appendChild(card);
  });

  section.style.display = 'block';
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

// ── Fade Scout ────────────────────────────────────

async function fadeScout() {
  if (!resultData || !resultData.winner) return;
  const btn = document.getElementById('fadeBtn');
  // Determine the fade target (the one Scout said would LOSE)
  const aWins = resultData.winner.toLowerCase().includes(currentComp1.split(' ')[0].toLowerCase()) ||
                currentComp1.toLowerCase().includes(resultData.winner.split(' ')[0].toLowerCase());
  const fadeWinner = aWins ? currentComp2 : currentComp1;
  const fadePct    = aWins ? resultData.bPct : resultData.aPct;

  btn.disabled = true;
  btn.textContent = '⏳ Saving fade…';
  try {
    const res = await fetch('/api/fade', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        sport: currentSport, comp1: currentComp1, comp2: currentComp2,
        fade_winner: fadeWinner, fade_pct: fadePct,
        a_pct: resultData.aPct, b_pct: resultData.bPct,
        confidence: resultData.confidence,
      }),
    });
    const d = await res.json();
    if (d.success) {
      btn.textContent = '✓ Fade saved';
      btn.style.color = 'var(--green)';
      const note = document.createElement('div');
      note.className = 'fade-confirm';
      note.textContent = `Fading Scout — backing ${fadeWinner} (${fadePct}%). Mark result in Journal.`;
      btn.parentNode.insertBefore(note, btn.nextSibling);
    }
  } catch (_) {
    btn.disabled = false;
    btn.textContent = '🔀 Fade Scout';
  }
}

// ── Index page rank bar + daily best bet ──────────

async function loadIndexRankBar() {
  try {
    const d = await (await fetch('/api/my-stats')).json();
    if (d.error) return;
    const TIER_COL = {legend:'#d4b464', elite:'#FF6B35', sharp:'#7eb8d4', rookie:'#8c8479'};
    const col = TIER_COL[d.rank?.tier] || '#8c8479';
    const streak = d.streak >= 3 ? ` &nbsp;🔥 ${d.streak}-streak` : '';
    const el = document.getElementById('myRankBar');
    if (!el) return;
    el.style.display = 'block';
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
                  background:var(--surface);border:1px solid var(--border);border-radius:12px;
                  padding:9px 14px;margin-bottom:12px;font-size:0.8rem;">
        <span>
          <span class="rank-badge rank-${d.rank?.tier}" style="color:${col}">${d.rank?.emoji} ${d.rank?.name}</span>
          <span style="color:var(--muted);margin-left:8px">${d.wins}W–${d.total-d.wins}L · ${d.rate}%${streak}</span>
        </span>
        <span style="display:flex;gap:8px">
          ${d.profile_url ? `<a href="${d.profile_url}" style="color:var(--accent);text-decoration:none;font-size:0.78rem">👤 Profile</a>` : ''}
          <a href="/journal" style="color:var(--muted);text-decoration:none;font-size:0.78rem">Journal →</a>
        </span>
      </div>`;
  } catch (_) {}
}

async function loadDailyBestBet() {
  try {
    const res  = await fetch('/api/plays?limit=1');
    const data = await res.json();
    const plays = data.plays || [];
    if (!plays.length) return;
    const top = plays[0];
    if (!top || !top.edge || Math.abs(top.edge) < 6) return;
    const el = document.getElementById('dailyBestBet');
    if (!el) return;
    const sign = top.edge > 0 ? '+' : '';
    el.style.display = 'block';
    el.innerHTML = `
      <div class="daily-best-bet">
        <span class="dbb-label">🎯 Scout's Best Bet</span>
        <span class="dbb-question">${top.question}</span>
        <span class="dbb-edge">${sign}${top.edge}pp edge</span>
        <a class="dbb-link" href="/plays">See all plays →</a>
      </div>`;
  } catch (_) {}
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
