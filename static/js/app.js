// ── Section config (maps ### headers → display labels + icons) ───────────────
const SECTION_MAP = {
  'the breakdown': { icon: '⚡', label: 'The Breakdown' },
  'track record':  { icon: '🏆', label: 'Track Record'  },
  'the journey':   { icon: '🌱', label: 'The Journey'   },
  'mental makeup': { icon: '🧠', label: 'Mental Makeup' },
  'right now':     { icon: '📈', label: 'Right Now'     },
  'the verdict':   { icon: '⚖️', label: 'The Verdict', highlight: true },
};

const LOADING_MSGS = [
  'Pulling every stat, record, and detail we can find…',
  'Digging into career histories and head-to-head data…',
  'Analyzing performance patterns and mental records…',
  'Crunching the numbers on form, momentum, and edge…',
  'Comparing their biggest moments under pressure…',
  'Almost done — building your breakdown…',
];

let fullText = '';
let currentComp1 = '', currentComp2 = '', currentSport = '';
let msgInterval = null;

// ── Form submit ───────────────────────────────────────────────────────────────

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
      msgIdx = Math.min(Math.floor(fullText.length / 400), LOADING_MSGS.length - 1);
      document.getElementById('loadingMsg').textContent = LOADING_MSGS[msgIdx];
    }

    if (fullText.startsWith('ERROR')) {
      showError(fullText);
      return;
    }

    const verdict = extractVerdict(fullText);
    renderResult(currentComp1, currentComp2, fullText, verdict);
    saveToJournal(currentSport, currentComp1, currentComp2, fullText);

  } catch (err) {
    showError('Something went wrong. Please try again.');
  }
});

// ── Render ────────────────────────────────────────────────────────────────────

function renderResult(comp1, comp2, text, verdict) {
  clearInterval(msgInterval);
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('resultSection').style.display = 'block';

  // Clean verdict block from display text
  const cleanText = text.replace(/<<VERDICT:.*?>>/gs, '').trim();

  // Win probability bar
  renderProbBar(comp1, comp2, verdict);

  // Parse and render sections
  renderSections(cleanText, comp1, comp2);

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderProbBar(comp1, comp2, verdict) {
  document.getElementById('probNameA').textContent = comp1;
  document.getElementById('probNameB').textContent = comp2;

  if (!verdict) {
    document.getElementById('probPctA').textContent = '?';
    document.getElementById('probPctB').textContent = '?';
    return;
  }

  const aPct = verdict.a_pct || 50;
  const bPct = verdict.b_pct || 50;

  // Animate percentage count-up
  animateCount('probPctA', aPct, '%');
  animateCount('probPctB', bPct, '%');

  // Animate bar fill
  setTimeout(() => {
    document.getElementById('probFillA').style.width = aPct + '%';
    document.getElementById('probFillB').style.width = bPct + '%';
  }, 100);

  // Highlight winner side
  const winner = verdict.winner || '';
  const aWins = winner && comp1.toLowerCase().includes(winner.split(' ')[0].toLowerCase()) ||
                winner && winner.toLowerCase().includes(comp1.split(' ')[0].toLowerCase());

  document.getElementById('probA').classList.toggle('prob-winner', aWins);
  document.getElementById('probB').classList.toggle('prob-winner', !aWins);

  // Winner badge
  const badge = document.getElementById('probWinnerBadge');
  const conf = verdict.confidence || 'Medium';
  const confEmoji = conf === 'High' ? '🔥' : conf === 'Medium' ? '⚡' : '🎲';
  badge.textContent = `${confEmoji} ${winner} wins — ${conf} confidence`;
  badge.className = 'prob-winner-badge conf-' + conf.toLowerCase();
  badge.style.display = 'block';
}

function animateCount(elId, target, suffix = '') {
  const el = document.getElementById(elId);
  let current = 0;
  const step = target / 40;
  const timer = setInterval(() => {
    current = Math.min(current + step, target);
    el.textContent = Math.round(current) + suffix;
    if (current >= target) clearInterval(timer);
  }, 30);
}

function renderSections(text, comp1, comp2) {
  const grid = document.getElementById('sectionsGrid');
  grid.innerHTML = '';

  // Split by ### headers
  const parts = text.split(/^### /m).filter(Boolean);

  // Remove the ## title line from the first chunk
  const firstPart = parts[0] || '';
  const bodyStart = firstPart.indexOf('\n');
  if (bodyStart > -1) parts[0] = firstPart.slice(bodyStart + 1);

  parts.forEach(chunk => {
    const nlIdx = chunk.indexOf('\n');
    if (nlIdx === -1) return;
    const rawTitle = chunk.slice(0, nlIdx).trim();
    const body = chunk.slice(nlIdx + 1).trim();
    if (!body) return;

    const key = rawTitle.toLowerCase();
    const cfg = SECTION_MAP[key] || { icon: '📌', label: rawTitle };

    const card = document.createElement('div');
    card.className = 'section-card' + (cfg.highlight ? ' section-verdict' : '');

    card.innerHTML = `
      <div class="section-header">
        <span class="section-icon">${cfg.icon}</span>
        <span class="section-title">${cfg.label}</span>
      </div>
      <div class="section-body">${formatBody(body)}</div>`;

    grid.appendChild(card);
  });
}

function formatBody(text) {
  return text
    .split('\n\n')
    .filter(p => p.trim())
    .map(p => `<p>${p.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, ' ')}</p>`)
    .join('');
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractVerdict(text) {
  const match = text.match(/<<VERDICT:(\{.*?\})>>/s);
  if (match) {
    try { return JSON.parse(match[1]); } catch (_) {}
  }
  return null;
}

function showLoading(comp1, comp2) {
  document.getElementById('formSection').style.display = 'none';
  document.getElementById('resultSection').style.display = 'none';
  document.getElementById('loadingSection').style.display = 'block';
  document.getElementById('loadName1').textContent = comp1;
  document.getElementById('loadName2').textContent = comp2;
  document.getElementById('loadingMsg').textContent = LOADING_MSGS[0];
}

function showError(msg) {
  clearInterval(msgInterval);
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('formSection').style.display = 'block';
  const btn = document.getElementById('analyzeBtn');
  btn.disabled = false;
  document.getElementById('btnText').style.display = 'inline';
  document.getElementById('btnLoader').style.display = 'none';
  alert('Error: ' + msg);
}

function newSearch() {
  document.getElementById('resultSection').style.display = 'none';
  document.getElementById('formSection').style.display = 'block';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function copyResult() {
  navigator.clipboard.writeText(fullText.replace(/<<VERDICT:.*?>>/gs, '').trim()).then(() => {
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
