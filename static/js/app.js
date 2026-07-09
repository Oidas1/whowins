const CATEGORY_ICONS = {
  'skills & style':  '⚡',
  'career résumé':   '🏆',
  'mental toughness':'🧠',
  'current form':    '📈',
  'situational edge':'🎯',
  'the x-factor':    '⭐',
};

const LOADING_MSGS = [
  'Pulling career stats and head-to-head data…',
  'Evaluating performance against common opponents…',
  'Analyzing clutch moments and high-stakes records…',
  'Scoring situational edge and mental toughness…',
  'Computing win probability…',
  'Finalizing breakdown…',
];

let fullText = '';
let currentSport = '', currentComp1 = '', currentComp2 = '';

// ── Form ──────────────────────────────────────────

document.getElementById('matchupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  currentSport = document.getElementById('sport').value.trim();
  currentComp1 = document.getElementById('comp1').value.trim();
  currentComp2 = document.getElementById('comp2').value.trim();
  const context = document.getElementById('context').value.trim();
  if (!currentSport || !currentComp1 || !currentComp2) return;

  fullText = '';
  showLoading(currentComp1, currentComp2);

  const body = new FormData();
  body.append('sport', currentSport);
  body.append('comp1', currentComp1);
  body.append('comp2', currentComp2);
  body.append('context', context);

  try {
    const res = await fetch('/analyze', { method: 'POST', body });
    if (!res.ok) { showError(await res.text()); return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let msgIdx = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      fullText += decoder.decode(value, { stream: true });
      const newIdx = Math.min(Math.floor(fullText.length / 300), LOADING_MSGS.length - 1);
      if (newIdx !== msgIdx) {
        msgIdx = newIdx;
        document.getElementById('loadingMsg').textContent = LOADING_MSGS[msgIdx];
      }
    }

    if (fullText.startsWith('ERROR')) { showError(fullText); return; }

    const data = parseAnalysis(fullText);
    renderResult(data);
    saveToJournal(currentSport, currentComp1, currentComp2, fullText);

  } catch (err) {
    showError('Something went wrong. Please try again.');
  }
});

// ── Parser ────────────────────────────────────────

function parseAnalysis(text) {
  const get = (key) => {
    const m = text.match(new RegExp(`^${key}:\\s*(.+)$`, 'm'));
    return m ? m[1].trim() : '';
  };

  const categories = [];
  const catBlocks = text.split(/^CATEGORY:/m).slice(1);
  catBlocks.forEach(block => {
    const lines = block.trim().split('\n');
    const name    = lines[0].trim();
    const aScore  = parseFloat((block.match(/^A_SCORE:\s*(.+)$/m) || [])[1]) || 0;
    const bScore  = parseFloat((block.match(/^B_SCORE:\s*(.+)$/m) || [])[1]) || 0;
    const aNote   = ((block.match(/^A_NOTE:\s*(.+)$/m) || [])[1] || '').trim();
    const bNote   = ((block.match(/^B_NOTE:\s*(.+)$/m) || [])[1] || '').trim();
    categories.push({ name, aScore, bScore, aNote, bNote });
  });

  return {
    compA:      get('COMP_A') || currentComp1,
    compB:      get('COMP_B') || currentComp2,
    aPct:       parseInt(get('A_PCT')) || 50,
    bPct:       parseInt(get('B_PCT')) || 50,
    winner:     get('WINNER'),
    confidence: get('CONFIDENCE') || 'Medium',
    verdict:    get('VERDICT'),
    categories,
  };
}

// ── Render ────────────────────────────────────────

function renderResult(data) {
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('resultSection').style.display  = 'block';

  renderProbBar(data);
  renderCategories(data);
  renderVerdict(data);

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderProbBar(data) {
  document.getElementById('probNameA').textContent = data.compA;
  document.getElementById('probNameB').textContent = data.compB;

  animateCount('probPctA', data.aPct, '%');
  animateCount('probPctB', data.bPct, '%');

  setTimeout(() => {
    document.getElementById('probFillA').style.width = data.aPct + '%';
    document.getElementById('probFillB').style.width = data.bPct + '%';
  }, 100);

  const aWins = data.winner && (
    data.winner.toLowerCase().includes(data.compA.split(' ')[0].toLowerCase()) ||
    data.compA.toLowerCase().includes(data.winner.split(' ')[0].toLowerCase())
  );
  document.getElementById('probA').classList.toggle('prob-winner', aWins);
  document.getElementById('probB').classList.toggle('prob-winner', !aWins);

  const conf = data.confidence;
  const emoji = conf === 'High' ? '🔥' : conf === 'Medium' ? '⚡' : '🎲';
  const badge = document.getElementById('probWinnerBadge');
  badge.textContent = `${emoji} ${data.winner} — ${conf} Confidence`;
  badge.className = 'prob-winner-badge conf-' + conf.toLowerCase();
  badge.style.display = 'block';
}

function renderCategories(data) {
  const grid = document.getElementById('sectionsGrid');
  grid.innerHTML = '';

  data.categories.forEach(cat => {
    const icon = CATEGORY_ICONS[cat.name.toLowerCase()] || '📊';
    const aWins = cat.aScore >= cat.bScore;

    const card = document.createElement('div');
    card.className = 'section-card';
    card.innerHTML = `
      <div class="section-header">
        <span class="section-icon">${icon}</span>
        <span class="section-title">${cat.name}</span>
      </div>
      <div class="score-row">
        <span class="score-name ${aWins ? 'score-winner' : ''}">${shortName(data.compA)}</span>
        <div class="score-bar-wrap">
          <div class="score-bar">
            <div class="score-fill ${aWins ? 'fill-winner' : 'fill-loser'}" style="width:0%" data-w="${(cat.aScore/10*100).toFixed(0)}%"></div>
          </div>
        </div>
        <span class="score-num ${aWins ? 'score-winner' : ''}">${cat.aScore.toFixed(1)}</span>
      </div>
      <div class="score-row">
        <span class="score-name ${!aWins ? 'score-winner' : ''}">${shortName(data.compB)}</span>
        <div class="score-bar-wrap">
          <div class="score-bar">
            <div class="score-fill ${!aWins ? 'fill-winner' : 'fill-loser'}" style="width:0%" data-w="${(cat.bScore/10*100).toFixed(0)}%"></div>
          </div>
        </div>
        <span class="score-num ${!aWins ? 'score-winner' : ''}">${cat.bScore.toFixed(1)}</span>
      </div>
      ${cat.aNote || cat.bNote ? `
      <div class="score-notes">
        ${cat.aNote ? `<div class="score-note"><span class="note-label">${shortName(data.compA)}:</span> ${cat.aNote}</div>` : ''}
        ${cat.bNote ? `<div class="score-note"><span class="note-label">${shortName(data.compB)}:</span> ${cat.bNote}</div>` : ''}
      </div>` : ''}
    `;
    grid.appendChild(card);
  });

  // Animate bars after paint
  requestAnimationFrame(() => {
    setTimeout(() => {
      document.querySelectorAll('.score-fill').forEach(el => {
        el.style.width = el.dataset.w;
      });
    }, 150);
  });
}

function renderVerdict(data) {
  const el = document.getElementById('verdictCard');
  if (!el) return;
  el.style.display = data.verdict ? 'block' : 'none';
  document.getElementById('verdictText').textContent = data.verdict || '';
}

function shortName(name) {
  const parts = name.trim().split(' ');
  return parts.length > 1 ? parts[parts.length - 1] : name;
}

function animateCount(elId, target, suffix = '') {
  const el = document.getElementById(elId);
  let current = 0;
  const step = target / 40;
  const timer = setInterval(() => {
    current = Math.min(current + step, target);
    el.textContent = Math.round(current) + suffix;
    if (current >= target) clearInterval(timer);
  }, 25);
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
  alert('Error: ' + msg);
}

function newSearch() {
  document.getElementById('resultSection').style.display = 'none';
  document.getElementById('formSection').style.display   = 'block';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function copyResult() {
  navigator.clipboard.writeText(fullText).then(() => {
    const msg = document.getElementById('copiedMsg');
    msg.classList.add('show');
    setTimeout(() => msg.classList.remove('show'), 2000);
  });
}

async function saveToJournal(sport, comp1, comp2, analysis) {
  if (!analysis || analysis.startsWith('ERROR')) return;
  try {
    await fetch('/journal/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sport, comp1, comp2, analysis }),
    });
  } catch (_) {}
}
