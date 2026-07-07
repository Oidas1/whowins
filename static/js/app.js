const form       = document.getElementById('matchupForm');
const resultWrap = document.getElementById('resultWrap');
const resultDiv  = document.getElementById('result');
const winnerBar  = document.getElementById('winner-bar');
const analyzeBtn = document.getElementById('analyzeBtn');
const btnText    = document.getElementById('btnText');
const btnLoader  = document.getElementById('btnLoader');

let fullText = '';

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  const sport   = document.getElementById('sport').value.trim();
  const comp1   = document.getElementById('comp1').value.trim();
  const comp2   = document.getElementById('comp2').value.trim();
  const context = document.getElementById('context').value.trim();

  if (!sport || !comp1 || !comp2) return;

  // reset
  fullText = '';
  resultDiv.innerHTML = '';
  winnerBar.style.display = 'none';
  winnerBar.textContent = '';
  resultWrap.style.display = 'block';
  analyzeBtn.disabled = true;
  btnText.style.display = 'none';
  btnLoader.style.display = 'inline';

  const body = new FormData();
  body.append('sport', sport);
  body.append('comp1', comp1);
  body.append('comp2', comp2);
  body.append('context', context);

  try {
    const res = await fetch('/analyze', { method: 'POST', body });

    if (!res.ok) {
      resultDiv.textContent = 'Error: ' + (await res.text());
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      fullText += chunk;
      resultDiv.innerHTML = renderMarkdown(fullText);
      resultDiv.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }

    extractWinner(fullText);

  } catch (err) {
    resultDiv.textContent = 'Something went wrong. Try again.';
  } finally {
    analyzeBtn.disabled = false;
    btnText.style.display = 'inline';
    btnLoader.style.display = 'none';
  }
});

function renderMarkdown(text) {
  return text
    .split('\n')
    .map(line => {
      if (line.startsWith('## '))
        return `<div class="md-h2">${line.slice(3)}</div>`;
      if (line.startsWith('### '))
        return `<div class="md-h3">${line.slice(4)}</div>`;
      // bold
      line = line.replace(/\*\*(.+?)\*\*/g, '<span class="md-bold">$1</span>');
      if (line.trim() === '') return '<br>';
      return `<div class="md-p">${line}</div>`;
    })
    .join('');
}

function extractWinner(text) {
  const match = text.match(/\*\*WINNER:\s*(.+?)\s*\|\s*Confidence:\s*(High|Medium|Low)\*\*/i);
  if (match) {
    const emoji = match[2].toLowerCase() === 'high' ? '🔥' : match[2].toLowerCase() === 'medium' ? '⚡' : '🎲';
    winnerBar.textContent = `${emoji}  WINNER: ${match[1].toUpperCase()}  —  Confidence: ${match[2]}`;
    winnerBar.style.display = 'block';
  }
}

function copyResult() {
  navigator.clipboard.writeText(fullText).then(() => {
    const btn = document.querySelector('.btn-copy');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 2000);
  });
}
