import os
import re
import uuid
import hashlib
import traceback
import secrets
import threading
import time
import io
from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response, stream_with_context, session, redirect, url_for, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
import anthropic
import urllib.request
import urllib.parse
import json
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=60)

db_url = os.environ.get('DATABASE_URL', 'sqlite:///whowins.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
TAVILY_API_KEY    = os.environ.get('TAVILY_API_KEY', '')
ODDS_API_KEY      = os.environ.get('ODDS_API_KEY', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', 'adminkey123')
RENDER_API_KEY    = os.environ.get('RENDER_API_KEY', '')
RENDER_SERVICE_ID = os.environ.get('RENDER_SERVICE_ID', '')

_password_store = {'value': os.environ.get('SITE_PASSWORD', 'whowins2026')}

# ── Models ────────────────────────────────────────────────────────────────────

class UserProfile(db.Model):
    __tablename__ = 'ww_users'
    id          = db.Column(db.Integer, primary_key=True)
    user_uid    = db.Column(db.String(64), unique=True, nullable=False, index=True)
    referred_by = db.Column(db.String(64), nullable=True)   # uid of referrer
    first_seen  = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen   = db.Column(db.DateTime, default=datetime.utcnow)

class Query(db.Model):
    __tablename__ = 'ww_queries'
    id           = db.Column(db.Integer, primary_key=True)
    user_uid     = db.Column(db.String(64), nullable=False, index=True)
    sport        = db.Column(db.String(100))
    competitor_a = db.Column(db.String(100))
    competitor_b = db.Column(db.String(100))
    winner       = db.Column(db.String(100))
    confidence   = db.Column(db.String(20))
    analysis     = db.Column(db.Text)
    outcome        = db.Column(db.String(10), default='pending')  # pending / win / loss
    ai_a_pct       = db.Column(db.Integer, nullable=True)   # AI win % for comp_a
    ai_b_pct       = db.Column(db.Integer, nullable=True)   # AI win % for comp_b
    a_odds_pct     = db.Column(db.Float, nullable=True)     # Vegas implied % for comp_a
    b_odds_pct     = db.Column(db.Float, nullable=True)     # Vegas implied % for comp_b
    is_upset_alert = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Warning: could not create tables on startup: {e}")

# ── Display name helper ───────────────────────────────────────────────────────

_ADJS  = ['Sharp','Bold','Swift','Keen','Wise','Slick','Iron','Cold','Steel','Clutch']
_NOUNS = ['Hawk','Wolf','Eagle','Fox','Bear','Lion','Viper','Raven','Shark','Ghost']

def get_display_name(uid):
    h = int(hashlib.md5(uid.encode()).hexdigest()[:8], 16)
    adj  = _ADJS[h % len(_ADJS)]
    noun = _NOUNS[(h // len(_ADJS)) % len(_NOUNS)]
    num  = h % 9000 + 1000
    return f"{adj}{noun}#{num}"

# ── Events cache ──────────────────────────────────────────────────────────────

_events_cache = {'data': [], 'ts': 0}
_EVENTS_TTL   = 1800  # 30 min

# ── Password helpers ──────────────────────────────────────────────────────────

def get_password():
    return _password_store['value']

def rotate_password():
    new_pw = secrets.token_urlsafe(8)
    _password_store['value'] = new_pw
    threading.Thread(target=_sync_to_render, args=(new_pw,), daemon=True).start()
    return new_pw

def _sync_to_render(new_pw):
    """Read current Render env vars, update only SITE_PASSWORD, write back."""
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return
    try:
        base_url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars"
        headers  = {"Authorization": f"Bearer {RENDER_API_KEY}",
                    "Accept": "application/json", "Content-Type": "application/json"}

        # Fetch current list
        get_req  = urllib.request.Request(base_url, headers=headers)
        current  = json.loads(urllib.request.urlopen(get_req, timeout=8).read())
        env_list = [e['envVar'] for e in current]

        # Patch only SITE_PASSWORD
        patched = False
        for item in env_list:
            if item['key'] == 'SITE_PASSWORD':
                item['value'] = new_pw
                patched = True
        if not patched:
            env_list.append({'key': 'SITE_PASSWORD', 'value': new_pw})

        put_req = urllib.request.Request(
            base_url, data=json.dumps(env_list).encode(),
            headers=headers, method="PUT"
        )
        urllib.request.urlopen(put_req, timeout=10)
    except Exception:
        pass

# ── Session helpers ───────────────────────────────────────────────────────────

def is_authed():
    return session.get('authed') is True

def get_user_uid():
    if 'user_uid' not in session:
        session['user_uid'] = str(uuid.uuid4())
    uid = session['user_uid']
    profile = UserProfile.query.filter_by(user_uid=uid).first()
    if profile is None:
        profile = UserProfile(
            user_uid=uid,
            referred_by=session.get('ref')
        )
        db.session.add(profile)
        db.session.commit()
    else:
        profile.last_seen = datetime.utcnow()
        db.session.commit()
    return uid

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Store referral code from URL before login
    ref = request.args.get('ref') or request.form.get('ref')
    if ref:
        session['ref'] = ref

    error = None
    if request.method == 'POST':
        if request.form.get('password') == get_password():
            session.permanent = True
            session['authed'] = True
            get_user_uid()
            rotate_password()
            return redirect(url_for('index'))
        error = 'Wrong password. Try again.'
    return render_template('login.html', error=error, ref=ref or session.get('ref', ''))

@app.route('/logout')
def logout():
    session.pop('authed', None)     # keep user_uid so history persists
    return redirect(url_for('login'))

@app.route('/admin/password')
def admin_password():
    if request.args.get('key') != ADMIN_KEY:
        return 'Unauthorized.', 403
    return render_template('admin_password.html', password=get_password())

@app.route('/admin/users')
def admin_users():
    if request.args.get('key') != ADMIN_KEY:
        return 'Unauthorized.', 403

    users = UserProfile.query.order_by(UserProfile.first_seen.desc()).all()
    user_data = []
    for u in users:
        queries  = Query.query.filter_by(user_uid=u.user_uid).all()
        wins     = sum(1 for q in queries if q.outcome == 'win')
        losses   = sum(1 for q in queries if q.outcome == 'loss')
        referrals = UserProfile.query.filter_by(referred_by=u.user_uid).count()
        user_data.append({
            'uid':       u.user_uid[:8] + '…',
            'full_uid':  u.user_uid,
            'joined':    u.first_seen.strftime('%b %d, %Y'),
            'last_seen': u.last_seen.strftime('%b %d, %Y'),
            'queries':   len(queries),
            'wins':      wins,
            'losses':    losses,
            'referrals': referrals,
            'referred_by': (u.referred_by[:8] + '…') if u.referred_by else '—',
        })

    total_users   = len(users)
    total_queries = Query.query.count()
    return render_template('admin_users.html',
        users=user_data,
        total_users=total_users,
        total_queries=total_queries,
        admin_key=ADMIN_KEY
    )

# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if not is_authed():
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/journal')
def journal():
    if not is_authed():
        return redirect(url_for('login'))
    uid = get_user_uid()
    entries = Query.query.filter_by(user_uid=uid).order_by(Query.created_at.desc()).all()
    return render_template('journal.html', entries=entries)

@app.route('/journal/save', methods=['POST'])
def journal_save():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    analysis_text = data.get('analysis', '')
    winner, confidence = parse_winner(analysis_text)

    a_odds_pct = data.get('a_odds_pct')
    b_odds_pct = data.get('b_odds_pct')

    # Detect upset alert: AI picks winner with 55%+ but Vegas only gives them ≤40%
    is_upset = False
    if a_odds_pct is not None and b_odds_pct is not None and winner:
        comp1 = data.get('comp1', '')
        ai_a_pct = int((data.get('a_pct') or 50))
        vegas_a_pct = float(a_odds_pct)
        winner_is_a = winner.lower().startswith(comp1.split()[0].lower()) if comp1 else False
        if winner_is_a and ai_a_pct >= 55 and vegas_a_pct <= 40:
            is_upset = True
        elif not winner_is_a and (100 - ai_a_pct) >= 55 and (100 - vegas_a_pct) <= 40:
            is_upset = True

    entry = Query(
        user_uid       = get_user_uid(),
        sport          = data.get('sport', '')[:100],
        competitor_a   = data.get('comp1', '')[:100],
        competitor_b   = data.get('comp2', '')[:100],
        winner         = winner,
        confidence     = confidence,
        analysis       = analysis_text[:8000],
        ai_a_pct       = int(data['a_pct']) if data.get('a_pct') is not None else None,
        ai_b_pct       = int(data['b_pct']) if data.get('b_pct') is not None else None,
        a_odds_pct     = float(a_odds_pct) if a_odds_pct is not None else None,
        b_odds_pct     = float(b_odds_pct) if b_odds_pct is not None else None,
        is_upset_alert = is_upset,
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'ok': True, 'upset_alert': is_upset})

@app.route('/journal/outcome/<int:entry_id>', methods=['POST'])
def journal_outcome(entry_id):
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    entry = Query.query.get_or_404(entry_id)
    if entry.user_uid != get_user_uid():
        return jsonify({'error': 'Forbidden'}), 403
    outcome = request.form.get('outcome', 'pending')
    if outcome not in ('win', 'loss', 'pending'):
        return jsonify({'error': 'Invalid'}), 400
    entry.outcome = outcome
    db.session.commit()
    return redirect(url_for('journal'))

@app.route('/journal/delete/<int:entry_id>', methods=['POST'])
def journal_delete(entry_id):
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    entry = Query.query.get_or_404(entry_id)
    if entry.user_uid != get_user_uid():
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(entry)
    db.session.commit()
    return redirect(url_for('journal'))

# ── Analyze ───────────────────────────────────────────────────────────────────

@app.route('/analyze', methods=['POST'])
def analyze():
    if not is_authed():
        return Response("Unauthorized.", status=401)

    sport   = request.form.get('sport', '').strip()
    comp1   = request.form.get('comp1', '').strip()
    comp2   = request.form.get('comp2', '').strip()
    context = request.form.get('context', '').strip()

    if not all([sport, comp1, comp2]):
        return Response("Missing fields.", status=400)
    if not ANTHROPIC_API_KEY:
        return Response("ERROR: ANTHROPIC_API_KEY is not set.", status=500)

    prompt = build_prompt(sport, comp1, comp2, context)

    def generate():
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            with client.messages.stream(
                model="claude-opus-4-8",
                max_tokens=1800,
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except anthropic.AuthenticationError:
            yield "ERROR: Invalid Anthropic API key."
        except anthropic.PermissionDeniedError as e:
            yield f"ERROR: API permission denied — {str(e)}"
        except anthropic.RateLimitError:
            yield "ERROR: Rate limit hit. Try again in a moment."
        except Exception as e:
            yield f"ERROR: {str(e)}\n\n{traceback.format_exc()}"

    return Response(stream_with_context(generate()), mimetype='text/plain')

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_winner(text):
    w = re.search(r'^WINNER:\s*(.+)$', text, re.MULTILINE)
    c = re.search(r'^CONFIDENCE:\s*(.+)$', text, re.MULTILINE)
    if w:
        return w.group(1).strip(), (c.group(1).strip() if c else 'Medium')
    return None, None

ANALYSIS_PROMPT = """You are an elite quantitative sports analyst. You apply rigorous mathematical models combined with deep scouting knowledge. Your predictions outperform simple favorites-picking because you use the right formula for the right sport.

SPORT: {sport}
COMPETITOR A: {comp1}
COMPETITOR B: {comp2}
{vegas_block}{search_block}{context_block}

MATHEMATICAL ANALYSIS PROTOCOL (run all steps silently — never appear in output):

CRITICAL KNOWLEDGE RULE:
Your training data contains extensive knowledge of professional and amateur sports worldwide — ATP/WTA/ITF rankings, college tennis ITA rankings, UTR ratings, Challenger/Futures circuits, NCAA programs, NFL/NBA/MLB/NHL player stats, boxing records, MMA fight histories, and much more.
ALWAYS draw on this training knowledge first. The supplemental live research above may add recent data, but an empty research result does NOT mean you have no knowledge — it means the web search didn't return results. If you know a player from training, use that knowledge. If you genuinely have no knowledge of either competitor from any source, set CONFIDENCE to Low — but never assume ignorance when you actually know something.
For college athletes: recall NCAA division, conference (SEC/ACC/Big Ten etc.), ITA ranking, and recent season performance. These are in your training data.
For ITF/Challenger players: recall ATP ranking, career high, win/loss records, and competition level. These are in your training data.

STEP 1 — SPORT-SPECIFIC MATHEMATICAL BASELINE:
Choose and apply the appropriate model:

TENNIS/RACKET: Elo probability formula: P(A wins) = 1 / (1 + 10^((B_Elo - A_Elo) / 400))
  UTR gap of 1.0 = ~Elo 150 = ~70% win probability. Surface adjustment: clay vs grass specialist = +/-5-8%.
  ATP/WTA ranking gap, Challenger vs ITF competition tier are key inputs. Apply directly.

SOCCER/FOOTBALL: Poisson model. Estimate xG for each team (attack strength x defense weakness x league avg).
  P(A wins) = sum of P(A=i goals) x P(B=j goals, j<i) for all i,j 0-6. xG differential is the strongest single predictor.

BASKETBALL: Pythagorean expectation W% = Pts^13.91 / (Pts^13.91 + Pts_allowed^13.91).
  Net rating differential: each +1.0 net rating = +2.5% win probability. True Shooting edge: +3% TS = +2% win prob.

NFL/AMERICAN FOOTBALL: Point spread model. Each 1 point on spread = 2.8% shift from 50%.
  3-point favorite = 58% win prob. 7-point = 69%. Turnover margin: +1 per game = +4% win prob.

BASEBALL: Log5 formula: P(A beats B) = (WP_A - WP_A x WP_B) / (WP_A + WP_B - 2 x WP_A x WP_B)
  Pitching matchup dominates (~60% of edge). ERA differential is primary input.

MMA/BOXING: No clean formula. Use strike accuracy differential, finishing rate, takedown defense %, control time %.
  Style math: southpaw vs orthodox historically gives southpaw ~52/48 edge in boxing.

GENERAL: Start 50/50. Apply each adjustment below as direct percentage shifts.

STEP 2 — TEMPORAL WEIGHTING (exponential decay on recent form):
  Weight multipliers: most recent = 1.0x, one before = 0.75x, two before = 0.56x, three before = 0.42x, four before = 0.32x.
  Weighted win rate = sum(result x weight) / sum(weights). This prevents stale results from distorting current form.

STEP 3 — QUANTIFIED CONTEXTUAL ADJUSTMENTS (add/subtract % from baseline):
  Home venue advantage: +3.5% team sports, +2% individual sports
  Altitude advantage (acclimatized team): +2%
  Travel fatigue (long-haul, under 48hrs rest): -3%
  Injury impact: -5% minor, -10% significant, -15% severe/to key player
  H2H edge: +1.5% per win beyond 50/50 in direct head-to-head record
  Style mismatch advantage: +3% to +7% depending on severity of counter-style edge
  Age curve (prime vs declining): +2% for competitor at their athletic peak

STEP 4 — BAYESIAN UPDATE WITH MARKET PRIOR (apply when Vegas odds are provided above):
  Vegas lines represent aggregated sharp-money wisdom. Treat as a Bayesian prior.
  Formula: P_final = (W x P_analysis) + ((1-W) x P_market)
  Weight W by your confidence: High = 0.65, Medium = 0.50, Low = 0.35
  If your analysis diverges from market by more than 15 percentage points, double-check your reasoning.
  If evidence holds — that divergence IS the edge. Hold your position.

STEP 5 — IT FACTOR MODIFIER (+/-5% max):
  After all mathematical steps, apply this final adjustment for what formulas cannot capture:
  - Will to win under maximum cost: +2% to +5%
  - In-competition adaptation (adjusts when plan A fails): +0% to +3%
  - Peak ceiling: who reaches higher when fully locked in? +0% to +3%
  - Competitive hunger (motivated challenger vs comfortable champion): +0% to +2%
  Total IT Factor adjustment capped at +5% or -5%.

STEP 6 — FINAL PROBABILITY:
  Combine baseline + contextual adjustments + Bayesian update + IT Factor.
  Hard cap: 95% max, 5% floor. No certainties in sports.
  CONFIDENCE level:
  - HIGH: mathematical baseline AND 2+ adjustments AND IT Factor all clearly favor one side
  - MEDIUM: baseline favors one, adjustments mixed or minor uncertainty
  - LOW: limited data, conflicting signals, or result within 5% of 50/50

STEP 0 — KNOWLEDGE RECALL (do this FIRST before any math):
Before running the mathematical steps, write what you know about each competitor.
Pull from your training data: rankings, records, programs, competition level, notable results, anything.
Format as:
KNOW_A: [everything you know about {comp1} as a {sport} competitor — be specific]
KNOW_B: [everything you know about {comp2} as a {sport} competitor — be specific]

Then run Steps 1-6 using that knowledge as your data foundation.

OUTPUT FORMAT — output these eight lines in this exact order:
KNOW_A: [your knowledge of {comp1} — 1-3 sentences, specific facts]
KNOW_B: [your knowledge of {comp2} — 1-3 sentences, specific facts]
A_PCT: [number]
B_PCT: [number]
WINNER: [full name]
CONFIDENCE: [High/Medium/Low]
EDGE: [number]
REASON: [3-5 sentences: level/ranking edge with specifics, H2H if known, key deciding factor, optional caution flag]

RULES:
- NEVER refuse or output anything outside these eight lines.
- With zero knowledge of either competitor, write "Unknown — limited training data" in KNOW fields and set CONFIDENCE to Low.
- Percentages sum to exactly 100.
- REASON must cite real data points wherever you know them."""
def web_search(query, depth="basic", max_results=5):
    """Search Tavily. depth='advanced' gives deeper crawl (costs 2 credits vs 1)."""
    if not TAVILY_API_KEY:
        return ''
    try:
        payload = json.dumps({
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": depth,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res = urllib.request.urlopen(req, timeout=10)
        data = json.loads(res.read())
        parts = []
        if data.get('answer'):
            parts.append(data['answer'])
        for r in data.get('results', []):
            if r.get('content'):
                parts.append(f"[{r.get('url','')}] {r['content'][:400]}")
        return '\n'.join(parts)
    except Exception:
        return ''

SPORT_SEARCH_TERMS = {
    'tennis': ['ITA college tennis', 'ATP ITF UTR ranking', 'Challenger Futures pro tennis'],
    'mma': ['UFC Bellator MMA record', 'fight history Sherdog Tapology'],
    'boxing': ['boxing record BoxRec', 'pro boxing career fights'],
    'basketball': ['NBA G League basketball stats', 'college basketball NCAA'],
    'football': ['NFL college football stats', 'pro football reference'],
    'soccer': ['football career goals assists', 'transfer market stats'],
    'baseball': ['MLB minor league baseball stats', 'Baseball Reference'],
    'golf': ['PGA Tour golf ranking OWGR', 'golf stats strokes gained'],
}

def fetch_utr(name):
    """Query UTR Sports API for a player's singles UTR rating. Returns dict or None."""
    try:
        url = f"https://api.utrsports.net/v2/search?query={urllib.parse.quote(name)}&top=3&type=players"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        data = json.loads(urllib.request.urlopen(req, timeout=6).read())
        hits = data.get('players', {}).get('hits', [])
        if not hits:
            return None
        source = hits[0].get('source', {})
        utr = source.get('singlesUtr') or source.get('threeMonthRating')
        if not utr:
            return None
        return {
            'name': source.get('displayName', name),
            'utr': round(float(utr), 2),
            'utr_status': source.get('ratingStatusSingles', ''),
        }
    except Exception:
        return None

def utr_win_probability(utr_a, utr_b):
    """Elo-based win probability from UTR ratings. 1.0 UTR ≈ 150 Elo points."""
    elo_diff = (utr_a - utr_b) * 150
    return round(1 / (1 + 10 ** (-elo_diff / 400)) * 100, 1)

def research_competitor(name, sport):
    """Run targeted searches for a competitor, using sport-specific terms."""
    sport_key = sport.lower().split()[0]
    extras = SPORT_SEARCH_TERMS.get(sport_key, [sport])

    queries = [
        f"{name} {extras[0] if extras else sport} career statistics",
        f"{name} {extras[1] if len(extras) > 1 else sport} ranking record 2025 2026",
        f"{name} {sport} profile results history",
    ]

    results = []
    for i, q in enumerate(queries):
        depth = "advanced" if i == 0 else "basic"
        result = web_search(q, depth=depth, max_results=5)
        if result:
            results.append(result)
    return '\n'.join(results) if results else ''

def build_prompt(sport, comp1, comp2, context):
    research = {}

    def fetch(key, fn, *args):
        try:
            research[key] = fn(*args)
        except Exception:
            research[key] = None

    is_tennis = any(w in sport.lower() for w in ['tennis', 'atp', 'wta', 'itf', 'challenger', 'utr'])

    threads = [
        threading.Thread(target=fetch, args=('comp1', research_competitor, comp1, sport)),
        threading.Thread(target=fetch, args=('comp2', research_competitor, comp2, sport)),
        threading.Thread(target=fetch, args=('h2h', web_search,
            f"{comp1} vs {comp2} {sport} head to head history", "basic", 4)),
        threading.Thread(target=fetch, args=('odds', fetch_odds, sport, comp1, comp2)),
    ]
    if is_tennis:
        threads += [
            threading.Thread(target=fetch, args=('utr1', fetch_utr, comp1)),
            threading.Thread(target=fetch, args=('utr2', fetch_utr, comp2)),
        ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=14)

    # UTR block for tennis
    utr_block = ''
    if is_tennis:
        u1 = research.get('utr1')
        u2 = research.get('utr2')
        if u1 or u2:
            utr_block = '\nLIVE UTR RATINGS (from UTR Sports database — use these as your Elo baseline):\n'
            if u1:
                utr_block += f'  {comp1}: UTR {u1["utr"]} ({u1["utr_status"]})\n'
            if u2:
                utr_block += f'  {comp2}: UTR {u2["utr"]} ({u2["utr_status"]})\n'
            if u1 and u2:
                pct = utr_win_probability(u1['utr'], u2['utr'])
                utr_block += f'  Elo-based win probability from UTR gap: {comp1} {pct}% / {comp2} {round(100-pct, 1)}%\n'
                utr_block += f'  (Formula: P = 1/(1+10^((UTR_B - UTR_A) x 150 / 400)))\n'

    # Vegas block for Bayesian prior
    vegas_block = ''
    odds = research.get('odds')
    if odds and odds.get('found'):
        vegas_block = (
            f'\nVEGAS MARKET ODDS (use as Bayesian prior in Step 4):\n'
            f'  {comp1}: {odds["a_pct"]}% implied probability\n'
            f'  {comp2}: {odds["b_pct"]}% implied probability\n'
        )

    search_block = utr_block  # UTR data takes priority for tennis
    if research.get('comp1') or research.get('comp2') or research.get('h2h'):
        search_block += '\n\nSUPPLEMENTAL RESEARCH (use to verify/update training knowledge):\n'
        if research.get('comp1'): search_block += f'\n[RESEARCH: {comp1}]\n{research["comp1"]}\n'
        if research.get('comp2'): search_block += f'\n[RESEARCH: {comp2}]\n{research["comp2"]}\n'
        if research.get('h2h'):  search_block += f'\n[HEAD-TO-HEAD]\n{research["h2h"]}\n'

    context_block = f'\nAdditional context: {context}' if context.strip() else ''

    return ANALYSIS_PROMPT.format(
        sport=sport, comp1=comp1, comp2=comp2,
        vegas_block=vegas_block,
        search_block=search_block,
        context_block=context_block
    )

# ── Prediction Markets ────────────────────────────────────────────────────────

KALSHI_API_KEY = os.environ.get('KALSHI_API_KEY', '')

def _json_get(url, headers=None):
    try:
        h = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        if headers: h.update(headers)
        req = urllib.request.Request(url, headers=h)
        return json.loads(urllib.request.urlopen(req, timeout=8).read())
    except Exception:
        return {}

def _parse_poly_prices(mkt):
    rp = mkt.get('outcomePrices', '[]')
    ro = mkt.get('outcomes', '[]')
    if isinstance(rp, str): rp = json.loads(rp)
    if isinstance(ro, str): ro = json.loads(ro)
    return [(o, round(float(p) * 100, 1)) for o, p in zip(ro, rp)]

_SPORT_KEYWORDS = {'win','beat','advance','champion','title','cup','bowl','match','game',
                   'fight','score','points','goals','playoff','final','series','season',
                   'league','tournament','medal','race','round','bout','vs','versus'}

def _name_matches(text, name):
    """Check if a competitor name appears in text — requires meaningful word match."""
    text = text.lower()
    # Use words longer than 3 chars to avoid false positives on short words like 'los'
    parts = [p for p in name.lower().split() if len(p) > 3]
    if not parts:
        parts = [p for p in name.lower().split() if len(p) > 2]
    return any(p in text for p in parts)

def _is_sport_market(question):
    """Check if a Polymarket question is sports-related."""
    q = question.lower()
    return any(kw in q for kw in _SPORT_KEYWORDS)

_poly_cache = {'data': [], 'ts': 0}

def _get_all_poly_markets():
    """Fetch all active Polymarket sports markets (cached 10 min)."""
    if time.time() - _poly_cache['ts'] < 600 and _poly_cache['data']:
        return _poly_cache['data']
    markets = []
    for offset in [0, 100, 200]:
        data = _json_get(f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}")
        if isinstance(data, list):
            markets.extend(data)
        else:
            break
    _poly_cache['data'] = markets
    _poly_cache['ts'] = time.time()
    return markets

def fetch_polymarket(comp1, comp2, sport):
    """Scan all active Polymarket markets and return ones matching this matchup."""
    all_markets = _get_all_poly_markets()
    results = []
    seen = set()

    for mkt in all_markets:
        q = mkt.get('question', '')
        if q in seen: continue
        q_lower = q.lower()
        mentions_a = _name_matches(q_lower, comp1)
        mentions_b = _name_matches(q_lower, comp2)
        if not (mentions_a or mentions_b): continue
        if not _is_sport_market(q): continue
        seen.add(q)

        prices = _parse_poly_prices(mkt)
        a_price, b_price = None, None

        # Check if outcomes are named (e.g. ["Spain","Germany"])
        a_price = next((p for o, p in prices if _name_matches(o.lower(), comp1)), None)
        b_price = next((p for o, p in prices if _name_matches(o.lower(), comp2)), None)

        # Yes/No market — assign based on which competitor the question is about
        if a_price is None and b_price is None and len(prices) == 2:
            yes_p = prices[0][1]
            if mentions_a and ('win' in q_lower or 'advance' in q_lower or 'champion' in q_lower):
                a_price = yes_p
                b_price = round(100 - yes_p, 1)
            elif mentions_b and ('win' in q_lower or 'advance' in q_lower or 'champion' in q_lower):
                b_price = yes_p
                a_price = round(100 - yes_p, 1)

        if a_price is None and b_price is None: continue

        results.append({
            'source': 'Polymarket',
            'question': q,
            'a_price': a_price,
            'b_price': b_price,
            'volume24h': float(mkt.get('volume24hr') or 0),
            'url': f"https://polymarket.com/event/{mkt.get('slug', '')}",
            'active': True,
            'live': 'in-game' in q_lower or 'live' in q_lower,
        })

    return sorted(results, key=lambda x: -x['volume24h'])[:4]

_kalshi_cache = {'data': [], 'ts': 0}

def _get_all_kalshi_events():
    """Fetch Kalshi events (cleaner structure than markets). Cached 10 min."""
    if time.time() - _kalshi_cache['ts'] < 600 and _kalshi_cache['data']:
        return _kalshi_cache['data']
    base = 'https://api.elections.kalshi.com/trade-api/v2'
    events = []
    for cat in ['sports', '']:
        url = f"{base}/events?limit=100&status=open" + (f"&category={cat}" if cat else "")
        data = _json_get(url)
        events.extend(data.get('events', []))
    _kalshi_cache['data'] = events
    _kalshi_cache['ts'] = time.time()
    return events

def fetch_kalshi(comp1, comp2, sport):
    """Scan Kalshi events for markets matching this matchup."""
    base = 'https://api.elections.kalshi.com/trade-api/v2'
    results = []
    seen = set()

    # Try events first (cleaner titles)
    events = _get_all_kalshi_events()
    for event in events:
        title = event.get('title', '')
        t_lower = title.lower()
        if not (_name_matches(t_lower, comp1) or _name_matches(t_lower, comp2)):
            continue
        # Fetch individual markets within this event
        et = event.get('event_ticker', '')
        mkts = _json_get(f"{base}/events/{et}").get('markets', [])
        for mkt in mkts:
            mt = mkt.get('title', '') + ' ' + mkt.get('yes_sub_title', '')
            if mt in seen: continue
            seen.add(mt)
            yes_price = float(mkt.get('last_price_dollars') or mkt.get('yes_bid_dollars') or mkt.get('yes_ask_dollars') or 0) * 100
            ticker = mkt.get('ticker', '')
            a_price, b_price = None, None
            if _name_matches(t_lower, comp1):
                a_price = round(yes_price, 1); b_price = round(100 - yes_price, 1)
            elif _name_matches(t_lower, comp2):
                b_price = round(yes_price, 1); a_price = round(100 - yes_price, 1)
            if a_price is None: continue
            series = et.split('-')[0].lower() if et else ''
            results.append({
                'source': 'Kalshi',
                'question': title,
                'a_price': a_price,
                'b_price': b_price,
                'volume24h': float(mkt.get('volume_24h_fp') or 0) / 100,
                'url': f"https://kalshi.com/markets/{series}/{ticker}",
                'active': True,
                'live': 'live' in t_lower or 'in-game' in t_lower,
            })

    return sorted(results, key=lambda x: -x['volume24h'])[:3]

@app.route('/api/markets')
def api_markets():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    comp1 = request.args.get('comp1', '')
    comp2 = request.args.get('comp2', '')
    sport = request.args.get('sport', '')
    ai_a  = float(request.args.get('ai_a', 50))
    ai_b  = float(request.args.get('ai_b', 50))

    poly = fetch_polymarket(comp1, comp2, sport)
    kalshi = fetch_kalshi(comp1, comp2, sport)
    all_markets = poly + kalshi

    # Calculate value gap for each market
    for m in all_markets:
        if m['a_price'] is not None:
            m['a_gap'] = round(ai_a - m['a_price'], 1)
        if m['b_price'] is not None:
            m['b_gap'] = round(ai_b - m['b_price'], 1)
        # Flag as dip opportunity: AI significantly higher than market
        m['dip_a'] = m.get('a_gap', 0) >= 10
        m['dip_b'] = m.get('b_gap', 0) >= 10

    return jsonify({
        'markets': all_markets,
        'has_live': any(m['live'] for m in all_markets),
        'comp1': comp1,
        'comp2': comp2,
    })

# ── Odds API ──────────────────────────────────────────────────────────────────

SPORT_KEY_MAP = {
    # Basketball
    'nba':'basketball_nba','basketball':'basketball_nba',
    'wnba':'basketball_wnba','ncaa basketball':'basketball_ncaab',
    'college basketball':'basketball_ncaab',
    # Football
    'nfl':'americanfootball_nfl','football':'americanfootball_nfl',
    'nfl preseason':'americanfootball_nfl_preseason',
    'college football':'americanfootball_ncaaf','ncaaf':'americanfootball_ncaaf',
    'cfl':'americanfootball_cfl',
    # Baseball
    'mlb':'baseball_mlb','baseball':'baseball_mlb',
    # Hockey
    'nhl':'icehockey_nhl','hockey':'icehockey_nhl',
    # MMA / Boxing
    'mma':'mma_mixed_martial_arts','ufc':'mma_mixed_martial_arts',
    'boxing':'boxing_boxing',
    # Soccer
    'soccer':'soccer_epl','football (soccer)':'soccer_epl',
    'epl':'soccer_epl','premier league':'soccer_epl','english premier league':'soccer_epl',
    'mls':'soccer_usa_mls','major league soccer':'soccer_usa_mls',
    'la liga':'soccer_spain_la_liga','bundesliga':'soccer_germany_bundesliga',
    'serie a':'soccer_italy_serie_a','ligue 1':'soccer_france_ligue_one',
    'champions league':'soccer_uefa_champs_league',
    'world cup':'soccer_fifa_world_cup','fifa world cup':'soccer_fifa_world_cup',
    'fifa':'soccer_fifa_world_cup','soccer world cup':'soccer_fifa_world_cup',
    'copa libertadores':'soccer_conmebol_copa_libertadores',
}

def american_to_pct(odds):
    odds = float(odds)
    if odds > 0:
        return round(100 / (odds + 100) * 100, 1)
    else:
        return round(abs(odds) / (abs(odds) + 100) * 100, 1)

def name_match(api_name, user_name):
    a = api_name.lower().strip()
    u = user_name.lower().strip()
    return (u in a or a in u or
            u.split()[-1] in a or a.split()[-1] in u)

def resolve_sport_key(sport):
    s = sport.lower().strip()
    if s in SPORT_KEY_MAP:
        return SPORT_KEY_MAP[s]
    # Live fuzzy match against available sports
    try:
        url = f"https://api.the-odds-api.com/v4/sports/?apiKey={ODDS_API_KEY}"
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        sports = json.loads(urllib.request.urlopen(req, timeout=5).read())
        words = [w for w in s.split() if len(w) > 3]
        for sp in sports:
            title = sp['title'].lower()
            key   = sp['key'].lower()
            if s in title or s in key:
                return sp['key']
            if words and any(w in title for w in words):
                return sp['key']
    except Exception:
        pass
    return s.replace(' ', '_')

def fetch_odds(sport, comp1, comp2):
    if not ODDS_API_KEY:
        return None
    sport_key = resolve_sport_key(sport)
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
               f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h&oddsFormat=american")
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=8)
        events = json.loads(res.read())
        if not isinstance(events, list):
            return None
    except Exception:
        return None

    for ev in events:
        teams = [ev.get('home_team',''), ev.get('away_team','')]
        if not (any(name_match(t, comp1) for t in teams) and
                any(name_match(t, comp2) for t in teams)):
            continue
        # Collect odds across bookmakers
        a_prices, b_prices = [], []
        for bk in ev.get('bookmakers', [])[:5]:
            for mkt in bk.get('markets', []):
                if mkt.get('key') != 'h2h':
                    continue
                for outcome in mkt.get('outcomes', []):
                    nm = outcome.get('name', '')
                    pr = outcome.get('price', 0)
                    if name_match(nm, comp1):
                        a_prices.append(pr)
                    elif name_match(nm, comp2):
                        b_prices.append(pr)
        if a_prices and b_prices:
            a_pct = american_to_pct(sum(a_prices)/len(a_prices))
            b_pct = american_to_pct(sum(b_prices)/len(b_prices))
            # Normalize to 100%
            total = a_pct + b_pct
            a_pct = round(a_pct / total * 100, 1)
            b_pct = round(b_pct / total * 100, 1)
            return {'a_pct': a_pct, 'b_pct': b_pct, 'found': True}
    return None

@app.route('/api/odds')
def api_odds():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    if not ODDS_API_KEY:
        return jsonify({'found': False, 'reason': 'no_key'})
    sport = request.args.get('sport', '')
    comp1 = request.args.get('comp1', '')
    comp2 = request.args.get('comp2', '')
    try:
        result = fetch_odds(sport, comp1, comp2)
        if result:
            return jsonify(result)
        return jsonify({'found': False, 'reason': 'no_match'})
    except Exception:
        return jsonify({'found': False, 'reason': 'error'})

# ── Leaderboard ───────────────────────────────────────────────────────────────

@app.route('/leaderboard')
def leaderboard():
    if not is_authed():
        return redirect(url_for('login'))

    MIN_SETTLED = 3
    users = UserProfile.query.all()
    board = []
    for u in users:
        queries = Query.query.filter_by(user_uid=u.user_uid).all()
        settled_qs = sorted(
            [q for q in queries if q.outcome in ('win','loss')],
            key=lambda q: q.created_at
        )
        wins    = sum(1 for q in settled_qs if q.outcome == 'win')
        losses  = len(settled_qs) - wins
        settled = wins + losses
        if settled < MIN_SETTLED:
            continue
        hit_rate = round((wins / settled) * 100)

        # Current win streak (count consecutive wins from most recent)
        streak = 0
        for q in reversed(settled_qs):
            if q.outcome == 'win':
                streak += 1
            else:
                break
        streak_badge = ''
        if streak >= 10: streak_badge = '👑'
        elif streak >= 5: streak_badge = '💀'
        elif streak >= 3: streak_badge = '🔥'

        board.append({
            'name':         get_display_name(u.user_uid),
            'hit_rate':     hit_rate,
            'wins':         wins,
            'losses':       losses,
            'total':        len(queries),
            'streak':       streak,
            'streak_badge': streak_badge,
            'is_me':        u.user_uid == get_user_uid(),
        })

    board.sort(key=lambda x: (-x['hit_rate'], -x['wins']))
    return render_template('leaderboard.html', board=board)

# ── Model vs. Vegas ───────────────────────────────────────────────────────────

@app.route('/vs-vegas')
def vs_vegas():
    if not is_authed():
        return redirect(url_for('login'))

    picks = (Query.query
             .filter(Query.outcome != 'pending')
             .order_by(Query.created_at.desc())
             .all())

    total      = len(picks)
    ai_correct = sum(1 for p in picks if p.outcome == 'win')
    ai_rate    = round(ai_correct / total * 100) if total else None

    # Vegas accuracy only counts picks where we have odds data
    odds_picks    = [p for p in picks if p.a_odds_pct is not None]
    vegas_correct = 0
    for p in odds_picks:
        vegas_picked_a = (p.a_odds_pct or 0) >= (p.b_odds_pct or 0)
        if vegas_picked_a == (p.outcome == 'win'):
            vegas_correct += 1
    vegas_rate = round(vegas_correct / len(odds_picks) * 100) if odds_picks else None

    diff = (ai_rate - vegas_rate) if (ai_rate and vegas_rate) else None
    verdict = 'win' if (diff and diff > 0) else ('loss' if (diff and diff < 0) else 'tie')

    return render_template('vs_vegas.html',
        picks=picks, total=total,
        ai_rate=ai_rate, vegas_rate=vegas_rate,
        ai_correct=ai_correct, vegas_correct=vegas_correct,
        odds_count=len(odds_picks),
        diff=abs(diff) if diff else None,
        verdict=verdict,
    )

# ── Trending + Accuracy API ───────────────────────────────────────────────────

@app.route('/api/trending')
def api_trending():
    week_ago = datetime.utcnow() - timedelta(days=7)
    rows = (
        db.session.query(
            func.lower(Query.sport).label('sport'),
            func.lower(Query.competitor_a).label('a'),
            func.lower(Query.competitor_b).label('b'),
            func.count().label('cnt')
        )
        .filter(Query.created_at >= week_ago)
        .group_by(func.lower(Query.sport), func.lower(Query.competitor_a), func.lower(Query.competitor_b))
        .order_by(func.count().desc())
        .limit(5)
        .all()
    )
    results = [{'sport': r.sport.title(), 'comp_a': r.a.title(), 'comp_b': r.b.title(), 'count': r.cnt} for r in rows]
    return jsonify(results)

@app.route('/api/accuracy')
def api_accuracy():
    total   = Query.query.filter(Query.outcome != 'pending').count()
    correct = Query.query.filter_by(outcome='win').count()
    rate    = round((correct / total) * 100) if total else None
    return jsonify({'total': total, 'correct': correct, 'rate': rate})

# ── Upcoming Events ───────────────────────────────────────────────────────────

ESPN_DISPLAY_NAMES = {
    'nba': 'NBA', 'nfl': 'NFL', 'mlb': 'MLB', 'nhl': 'NHL',
    'nfl_preseason': 'NFL Preseason', 'wnba': 'WNBA',
    'usa.1': 'MLS', 'nba_dleague': 'G League',
    'ufc': 'UFC', 'boxing': 'Boxing',
    'ncaaf': 'NCAAF', 'ncaab': 'NCAAB',
}

def fetch_espn_events(sport, league):
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=6)
        data = json.loads(res.read())
        events = []
        display = ESPN_DISPLAY_NAMES.get(league, league.upper().replace('_', ' ').replace('.', ' '))
        for ev in data.get('events', [])[:3]:
            comps = ev.get('competitions', [{}])[0].get('competitors', [])
            if len(comps) >= 2:
                names = [c.get('team', {}).get('displayName', c.get('athlete', {}).get('displayName', '')) for c in comps]
                date_str = ev.get('date', '')
                try:
                    dt = datetime.strptime(date_str[:10], '%Y-%m-%d').strftime('%b %d')
                except Exception:
                    dt = ''
                if names[0] and len(names) > 1 and names[1]:
                    events.append({'sport': display, 'comp_a': names[0], 'comp_b': names[1], 'date': dt})
        return events
    except Exception:
        return []

@app.route('/api/events')
def api_events():
    global _events_cache
    if time.time() - _events_cache['ts'] < _EVENTS_TTL and _events_cache['data']:
        return jsonify(_events_cache['data'])

    feeds = [
        ('basketball', 'nba'), ('football', 'nfl'), ('baseball', 'mlb'),
        ('hockey', 'nhl'), ('soccer', 'usa.1'), ('basketball', 'wnba'),
    ]
    all_events = []
    for sport, league in feeds:
        all_events.extend(fetch_espn_events(sport, league))

    _events_cache = {'data': all_events[:12], 'ts': time.time()}
    return jsonify(all_events[:12])

# ── Share image ───────────────────────────────────────────────────────────────

def _pil_font(size, bold=True):
    paths = [
        f"/System/Library/Fonts/Supplemental/Arial {'Bold' if bold else ''}.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p.strip(), size)
        except Exception:
            continue
    return ImageFont.load_default()

def generate_share_image(comp1, comp2, sport, a_pct, b_pct, winner, confidence, reason=''):
    W, H = 1200, 630
    BG       = (20, 18, 15)
    SURFACE  = (31, 28, 23)
    BORDER   = (58, 53, 42)
    BLUE     = (126, 184, 212)
    SAGE     = (122, 170, 130)
    TEXT     = (237, 232, 224)
    MUTED    = (140, 132, 121)

    img  = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Card background
    draw.rounded_rectangle([40, 40, W-40, H-40], radius=24, fill=SURFACE, outline=BORDER, width=2)

    # Sport tag
    sport_font = _pil_font(22)
    draw.rounded_rectangle([60, 60, 60 + len(sport)*14 + 24, 100], radius=20, fill=BORDER)
    draw.text((72, 66), sport.upper(), font=sport_font, fill=MUTED)

    # WhoWins logo top right
    logo_font = _pil_font(26)
    logo_text = "WhoWins"
    lb = draw.textbbox((0,0), logo_text, font=logo_font)
    draw.text((W - 80 - (lb[2]-lb[0]), 66), logo_text, font=logo_font, fill=BLUE)

    # Determine winner side
    a_wins = winner and (
        winner.lower().startswith(comp1.split()[0].lower()) or
        comp1.lower().startswith(winner.split()[0].lower())
    )

    # Names
    name_font = _pil_font(42)
    a_col = BLUE if a_wins else MUTED
    b_col = MUTED if a_wins else BLUE

    a_nb = draw.textbbox((0,0), comp1, font=name_font)
    b_nb = draw.textbbox((0,0), comp2, font=name_font)
    draw.text((W//4 - (a_nb[2]-a_nb[0])//2, 145), comp1, font=name_font, fill=a_col)
    draw.text((3*W//4 - (b_nb[2]-b_nb[0])//2, 145), comp2, font=name_font, fill=b_col)

    # Big percentages
    pct_font = _pil_font(140)
    a_txt = f"{a_pct}%"
    b_txt = f"{b_pct}%"
    a_pb = draw.textbbox((0,0), a_txt, font=pct_font)
    b_pb = draw.textbbox((0,0), b_txt, font=pct_font)
    draw.text((W//4 - (a_pb[2]-a_pb[0])//2, 195), a_txt, font=pct_font, fill=a_col)
    draw.text((3*W//4 - (b_pb[2]-b_pb[0])//2, 195), b_txt, font=pct_font, fill=b_col)

    # VS divider
    vs_font = _pil_font(28)
    vs_b = draw.textbbox((0,0), "VS", font=vs_font)
    draw.text((W//2 - (vs_b[2]-vs_b[0])//2, 295), "VS", font=vs_font, fill=BORDER)
    draw.line([(W//2, 200), (W//2, 380)], fill=BORDER, width=2)

    # Probability bar
    bar_x, bar_y, bar_w, bar_h = 80, 420, W-160, 16
    draw.rounded_rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h], radius=8, fill=BORDER)
    fill_w = int(bar_w * (a_pct / 100))
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x+fill_w, bar_y+bar_h], radius=8, fill=BLUE)
    remain = bar_w - fill_w
    if remain > 0:
        draw.rounded_rectangle([bar_x+fill_w, bar_y, bar_x+bar_w, bar_y+bar_h], radius=8, fill=SAGE)

    # Winner line
    conf_emoji = {'High': '🔥', 'Medium': '⚡', 'Low': '🎲'}.get(confidence, '⚡')
    win_text = f"{winner} wins  ·  {confidence} Confidence"
    win_font = _pil_font(30)
    wb = draw.textbbox((0,0), win_text, font=win_font)
    draw.text((W//2 - (wb[2]-wb[0])//2, 458), win_text, font=win_font, fill=TEXT)

    # Reason text (wrapped)
    if reason:
        reason_font = _pil_font(22, bold=False)
        max_w = W - 160
        words = reason.split()
        lines, line = [], ''
        for word in words:
            test = (line + ' ' + word).strip()
            tb = draw.textbbox((0,0), test, font=reason_font)
            if tb[2]-tb[0] > max_w and line:
                lines.append(line); line = word
            else:
                line = test
        if line: lines.append(line)
        y_r = 504
        for ln in lines[:2]:
            lb = draw.textbbox((0,0), ln, font=reason_font)
            draw.text((W//2 - (lb[2]-lb[0])//2, y_r), ln, font=reason_font, fill=MUTED)
            y_r += 30

    # Bottom URL
    url_font = _pil_font(22, bold=False)
    url_text = "whowins.onrender.com"
    ub = draw.textbbox((0,0), url_text, font=url_font)
    draw.text((W//2 - (ub[2]-ub[0])//2, 578), url_text, font=url_font, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf

@app.route('/share/image', methods=['POST'])
def share_image():
    if not is_authed():
        return 'Unauthorized', 401
    data = request.get_json() or {}
    buf = generate_share_image(
        comp1=data.get('comp1', ''),
        comp2=data.get('comp2', ''),
        sport=data.get('sport', ''),
        a_pct=int(data.get('a_pct', 50)),
        b_pct=int(data.get('b_pct', 50)),
        winner=data.get('winner', ''),
        confidence=data.get('confidence', 'Medium'),
        reason=data.get('reason', ''),
    )
    return send_file(buf, mimetype='image/png', download_name='whowins-prediction.png')

if __name__ == '__main__':
    app.run(debug=True)
