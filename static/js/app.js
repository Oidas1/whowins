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

document.getElementById('matchupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  currentSport = document.getElementById('sport').value.trim();
  currentComp1 = document.getElementById('comp1').value.trim();
  currentComp2 = document.getElementById('comp2').value.trim();
  const context = document.getElementById('context').value.trim();
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
    saveToJournal(currentSport, currentComp1, currentComp2, fullText);

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
  return {
    compA:      get('COMP_A') || currentComp1,
    compB:      get('COMP_B') || currentComp2,
    aPct:       parseInt(get('A_PCT'))  || 50,
    bPct:       parseInt(get('B_PCT'))  || 50,
    winner:     get('WINNER'),
    confidence: get('CONFIDENCE') || 'Medium',
    reason:     get('REASON') || '',
  };
}

// ── Render ────────────────────────────────────────

function render(d) {
  document.getElementById('loadingSection').style.display = 'none';
  document.getElementById('resultSection').style.display  = 'block';

  document.getElementById('compNameA').textContent = currentComp1;
  document.getElementById('compNameB').textContent = currentComp2;

  // Determine winner
  const aWins = d.winner && (
    d.winner.toLowerCase().includes(currentComp1.split(' ')[0].toLowerCase()) ||
    currentComp1.toLowerCase().includes(d.winner.split(' ')[0].toLowerCase())
  );

  document.getElementById('compBlockA').className = 'comp-block' + (aWins  ? ' comp-winner' : '');
  document.getElementById('compBlockB').className = 'comp-block' + (!aWins ? ' comp-winner' : '');

  // Animate percentages
  animateCount('compPctA', d.aPct, '%');
  animateCount('compPctB', d.bPct, '%');

  // Animate bar
  setTimeout(() => {
    document.getElementById('probFillA').style.width = d.aPct + '%';
    document.getElementById('probFillB').style.width = d.bPct + '%';
  }, 100);

  // Confidence badge
  const conf  = d.confidence;
  const emoji = conf === 'High' ? '🔥' : conf === 'Medium' ? '⚡' : '🎲';
  const badge = document.getElementById('confBadge');
  badge.textContent = `${emoji} ${conf} Confidence`;
  badge.className   = 'conf-badge-big conf-' + conf.toLowerCase();
  document.getElementById('confRow').style.display = 'flex';

  const reasonEl = document.getElementById('reasonText');
  if (d.reason) {
    reasonEl.textContent = d.reason;
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
  if (!resultData) return;
  const text = `${currentComp1}: ${resultData.aPct}% vs ${currentComp2}: ${resultData.bPct}%\nWinner: ${resultData.winner} (${resultData.confidence} confidence)\nSport: ${currentSport}\nvia WhoWins`;
  navigator.clipboard.writeText(text).then(() => {
    const msg = document.getElementById('copiedMsg');
    msg.classList.add('show');
    setTimeout(() => msg.classList.remove('show'), 2000);
  });
}

function copyRef() {
  const input = document.getElementById('refLink');
  if (!input) return;
  navigator.clipboard.writeText(input.value).then(() => {
    const btn = document.querySelector('.btn-copy-ref');
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy', 2000); }
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
