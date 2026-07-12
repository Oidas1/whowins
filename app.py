import os
import re
import uuid
import hashlib
import hmac
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

# ── Security ──────────────────────────────────────────────────────────────────
# Simple in-memory rate limiter (per IP)
_rate_store: dict = {}

def _rate_limit(key: str, max_calls: int, window: int) -> bool:
    """Return True if allowed, False if rate limited."""
    now = time.time()
    calls = [t for t in _rate_store.get(key, []) if now - t < window]
    if len(calls) >= max_calls:
        return False
    calls.append(now)
    _rate_store[key] = calls
    return True

def _client_ip() -> str:
    return request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()

def _safe_eq(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(a.encode(), b.encode())

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    # Allow our own resources + prediction market APIs
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://api.anthropic.com https://api.tavily.com "
        "https://api.the-odds-api.com https://gamma-api.polymarket.com "
        "https://api.elections.kalshi.com https://api.utrsports.net; "
        "frame-ancestors 'none';"
    )
    return response
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')
app.config['PERMANENT_SESSION_LIFETIME']  = timedelta(days=60)
app.config['SESSION_COOKIE_HTTPONLY']     = True
app.config['SESSION_COOKIE_SAMESITE']     = 'Lax'
app.config['SESSION_COOKIE_SECURE']       = os.environ.get('RENDER') is not None  # HTTPS only on Render
app.config['MAX_CONTENT_LENGTH']          = 64 * 1024  # 64 KB max request body

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

class Command(db.Model):
    __tablename__ = 'ww_commands'
    id         = db.Column(db.Integer, primary_key=True)
    text       = db.Column(db.Text, nullable=False)
    status     = db.Column(db.String(20), default='pending')  # pending / done
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Parlay(db.Model):
    __tablename__ = 'ww_parlays'
    id          = db.Column(db.Integer, primary_key=True)
    user_uid    = db.Column(db.String(64), nullable=False, index=True)
    picks       = db.Column(db.JSON)        # list of pick dicts
    combined_pct= db.Column(db.Float)       # combined win probability %
    fair_odds   = db.Column(db.Float)       # 1 / combined_pct * 100
    outcome     = db.Column(db.String(10), default='pending')  # pending / win / loss
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

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
        ip = _client_ip()
        if not _rate_limit(f'login:{ip}', max_calls=10, window=300):
            error = 'Too many attempts. Wait a few minutes.'
        elif request.form.get('password') == get_password():
            session.permanent = True
            session['authed'] = True
            get_user_uid()
            rotate_password()
            return redirect(url_for('index'))
        else:
            error = 'Wrong password. Try again.'
    return render_template('login.html', error=error, ref=ref or session.get('ref', ''))

@app.route('/logout')
def logout():
    session.pop('authed', None)     # keep user_uid so history persists
    return redirect(url_for('login'))

@app.route('/admin/password')
def admin_password():
    if not _safe_eq(request.args.get('key',''), ADMIN_KEY):
        return 'Unauthorized.', 403
    return render_template('admin_password.html', password=get_password())

@app.route('/admin/users')
def admin_users():
    if not _safe_eq(request.args.get('key',''), ADMIN_KEY):
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
    uid     = get_user_uid()
    entries = Query.query.filter_by(user_uid=uid).order_by(Query.created_at.desc()).all()
    parlays = Parlay.query.filter_by(user_uid=uid).order_by(Parlay.created_at.desc()).all()
    return render_template('journal.html', entries=entries, parlays=parlays)

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

# ── Parlay ────────────────────────────────────────────────────────────────────

@app.route('/parlay/save', methods=['POST'])
def parlay_save():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    picks = data.get('picks', [])
    if not picks:
        return jsonify({'error': 'No picks'}), 400

    # Combined probability = product of all win percentages
    combined = 1.0
    for p in picks:
        combined *= (p.get('pct', 50) / 100)
    combined_pct = round(combined * 100, 2)
    fair_odds    = round(1 / combined, 2) if combined > 0 else 0

    parlay = Parlay(
        user_uid     = get_user_uid(),
        picks        = picks,
        combined_pct = combined_pct,
        fair_odds    = fair_odds,
    )
    db.session.add(parlay)
    db.session.commit()
    return jsonify({'ok': True, 'id': parlay.id, 'combined_pct': combined_pct, 'fair_odds': fair_odds})

@app.route('/parlay/outcome/<int:parlay_id>', methods=['POST'])
def parlay_outcome(parlay_id):
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    parlay = Parlay.query.get_or_404(parlay_id)
    if parlay.user_uid != get_user_uid():
        return jsonify({'error': 'Forbidden'}), 403
    outcome = request.form.get('outcome', 'pending')
    if outcome not in ('win', 'loss', 'pending'):
        return jsonify({'error': 'Invalid'}), 400
    parlay.outcome = outcome
    db.session.commit()
    return redirect(url_for('journal'))

@app.route('/parlay/delete/<int:parlay_id>', methods=['POST'])
def parlay_delete(parlay_id):
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    parlay = Parlay.query.get_or_404(parlay_id)
    if parlay.user_uid != get_user_uid():
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(parlay)
    db.session.commit()
    return redirect(url_for('journal'))

# ── Analyze ───────────────────────────────────────────────────────────────────

@app.route('/analyze', methods=['POST'])
def analyze():
    if not is_authed():
        return Response("Unauthorized.", status=401)

    # Rate limit: 30 analyses per user per 10 minutes
    uid = get_user_uid()
    if not _rate_limit(f'analyze:{uid}', max_calls=30, window=600):
        return Response("Rate limit reached. Please wait a few minutes.", status=429)

    sport   = request.form.get('sport', '').strip()[:100]
    comp1   = request.form.get('comp1', '').strip()[:100]
    comp2   = request.form.get('comp2', '').strip()[:100]
    context = request.form.get('context', '').strip()[:500]

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
                max_tokens=2400,
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

ANALYSIS_PROMPT = """You are an elite quantitative analyst. You apply rigorous mathematical models and deep research knowledge to predict outcomes across any competitive domain — sports, politics, markets, entertainment, business, and beyond.

TOPIC: {sport}
SUBJECT A: {comp1}
SUBJECT B: {comp2}
{vegas_block}{search_block}{context_block}

MATHEMATICAL ANALYSIS PROTOCOL (run all steps silently — never appear in output):

CRITICAL KNOWLEDGE RULE:
Your training data contains deep knowledge across all competitive domains. For sports: ATP/WTA/ITF rankings, UTR ratings, NFL/NBA/MLB/NHL stats, boxing records, MMA fight histories. For politics: polling averages, approval ratings, fundraising totals, electoral history. For crypto/finance: market caps, on-chain metrics, price history. For entertainment: chart positions, award history, streaming numbers. ALWAYS draw on this training knowledge first. Empty research results do NOT mean you have no knowledge.

STEP 0 — KNOWLEDGE RECALL (do this FIRST):
Before any math, recall what you know about each subject from training data.
KNOW_A: [everything you know about {comp1} in the context of {sport} — rankings, records, polls, stats, history.
  NAME COLLISION RULE: If {comp1} is a common name that belongs to a famous person in a DIFFERENT sport, reason explicitly: "There is a famous [other sport] player named {comp1}, but in the context of {sport}, I am analyzing [name] as a {sport} competitor. Any UTR/ranking data from live research refers to the {sport} version of this person." Use all available data — UTR ratings, rankings, records — for the {sport} context. Do NOT discard live research data because of a name collision; instead interpret it correctly.]
KNOW_B: [same depth and same name-collision reasoning for {comp2}]

STEP 0.5 — STYLE MATCHUP ANALYSIS (sports only):
Before running math, analyze HOW each competitor plays and whether one style systematically beats the other:
- Offensive/defensive identity: does one impose their game, the other react?
- Pace preference: does one thrive in fast/physical play, the other in slow/technical?
- Key weapon vs key vulnerability: does A's best strength attack B's biggest weakness, or vice versa?
- Historical style precedent: does this TYPE of matchup (e.g., counter-puncher vs brawler, zone defense vs isolation scorer, big-server vs baseliner) have a documented historical winner?
- Score this 0-10 for each side and factor into Step 1 baseline.

RANKING TRAP GUARD (critical rule — apply before every analysis):
Rankings are backward-looking aggregates. Do NOT let #1 ranking alone push probability above 75% without corroborating evidence from MULTIPLE factors. Always ask:
• Is the top-ranked competitor in current peak form, or are there injury/fatigue/slump signals?
• Does the H2H record support the ranking gap, or does the lower-ranked competitor have wins against them?
• Does the style matchup create a ceiling on the top seed's advantage (e.g., a big-server vs a great returner)?
• Is the lower-ranked competitor on a recent hot streak, trending up, or peaking at the right time?
• Is the higher-ranked competitor defending a ranking vs actually being in form right now?
A #1 player or top seed deserves meaningful weight — but a lower-ranked competitor with favorable conditions, strong H2H, a style advantage, or strong recent form can realistically sit at 35-50% probability. Never let a ranking alone settle the analysis.

STEP 1 — DOMAIN-SPECIFIC ANALYTICAL BASELINE:
Identify the domain from TOPIC and apply the appropriate model:

SPORTS (NBA/NFL/MLB/Soccer/Tennis/Boxing/MMA/Golf):
  Tennis: Elo formula P = 1/(1+10^((B_Elo-A_Elo)/400)). UTR gap 1.0 = ~70% win prob. ALWAYS use UTR data when provided — the live UTR lookup queries the actual tennis database, so any rating returned IS for that person as a tennis competitor. If a famous non-tennis player shares the same name, that does not invalidate the UTR data; the UTR database profile belongs to a distinct tennis-playing person with that name.
  Soccer: Poisson model on xG. Basketball: Pythagorean W% = Pts^13.91/(Pts^13.91+PA^13.91).
  NFL: 1 spread point = 2.8% shift. Baseball: Log5 formula. MMA/Boxing: strike accuracy, finishing rate.

POLITICS / ELECTIONS:
  Polling average gap: each +5% polling lead = ~+8% win probability, with incumbency +3%.
  Fundraising edge: each 2x fundraising advantage = ~+3% probability.
  Historical base rates: incumbent advantage, electoral college/geography, approval rating baseline.
  Apply: P(A wins) = base_rate + polling_adjustment + fundamentals_adjustment + momentum.

CRYPTO / FINANCE:
  Market dominance: relative market cap, 30-day price momentum, developer activity (GitHub commits).
  Adoption metrics: daily active wallets, transaction volume, institutional holding %.
  Macro environment: interest rate sensitivity, risk-on/risk-off positioning.

ENTERTAINMENT / AWARDS:
  Historical base rate: prior wins in this category, nomination frequency.
  Critical momentum: Metacritic/Rotten Tomatoes score, social media sentiment, precursor awards won.
  Industry positioning: label/studio support, campaign spend, public popularity.

BUSINESS / COMPANIES:
  Revenue growth differential, market cap trajectory, product pipeline strength.
  Competitive moat depth, customer retention, margin comparison.

GEOPOLITICS:
  Power asymmetry (GDP, military, alliance structure), historical precedent, current leverage.
  Diplomatic positioning, public opinion in relevant regions.

GENERAL (anything else):
  Start 50/50, apply evidence-weighted adjustments for each advantage found.

STEP 2 — TEMPORAL WEIGHTING:
  Most recent data = 1.0x. One period prior = 0.75x. Two prior = 0.56x. Three = 0.42x. Four = 0.32x.
  Weight recent polls/results/form more heavily than historical averages.

STEP 3 — QUANTIFIED ADJUSTMENTS (add/subtract %):
  SPORTS: Home +3.5%, injury -5 to -15%, H2H edge +1.5% per win above 50/50, style mismatch +3-7%
  POLITICS: Incumbency +3%, geographic stronghold +2-5%, debate performance +/-2%, scandal -5 to -10%
  FINANCE: Macro tailwind/headwind +/-5%, regulatory risk -5%, network effect moat +3%
  GENERAL: Apply domain-appropriate modifiers with similar magnitude ranges

STEP 4 — BAYESIAN UPDATE (when market odds provided above):
  P_final = (W x P_analysis) + ((1-W) x P_market)
  W = 0.65 (High confidence), 0.50 (Medium), 0.35 (Low)
  Market divergence >15% = re-examine assumptions, but hold if evidence is strong.

STEP 5 — IT FACTOR + COMEBACK DNA (+/-8% combined):
  Will to win / competitive drive under maximum pressure.
  Peak ceiling vs current trajectory.
  In-competition adaptability when plan A fails.
  Hunger: motivated challenger vs comfortable frontrunner.

  COMEBACK DNA (evaluate explicitly):
  Does either competitor have documented history of winning from a losing position?
  • Tennis: winning from a set down, saving match points, recovering from 0-5 in a set
  • Team sports: erasing multi-goal/run/point halftime deficits
  • Politics/markets: closing large polling gaps in final weeks
  • General: historical pattern of reversing unfavorable odds under pressure
  If one competitor has clearly stronger comeback ability AND the matchup is within 20%, apply +2-5% for the better comeback competitor. This is especially relevant when the underdog has a documented "never say die" pattern. Note it explicitly in COMEBACK_ALERT.

STEP 6 — FINAL:
  Combine all steps. Cap at 95%, floor at 5%.
  HIGH confidence: baseline + 2+ adjustments + IT Factor all favor same side.
  MEDIUM: baseline favors one, adjustments mixed.
  LOW: limited data, conflicting signals, or genuinely near 50/50.

OUTPUT FORMAT — output these ten lines exactly:
KNOW_A: [1-3 sentences of specific facts about {comp1} in context of {sport}]
KNOW_B: [1-3 sentences of specific facts about {comp2} in context of {sport}]
A_PCT: [number]
B_PCT: [number]
WINNER: [full name]
CONFIDENCE: [High/Medium/Low]
EDGE: [number]
REASON: [3-5 sentences: domain-specific edge with data, H2H/polling/metrics if known, key deciding factor, caution flag if any]
COMEBACK_ALERT: [Write "—" if neither competitor has notable comeback history. Otherwise write ONE sentence: "NAME has comeback DNA — [one specific documented example, e.g., 'saved 3 match points vs Djokovic at Roland Garros 2024']". Only flag this if genuinely documented, not generic.]
SCOUT_TIP: [1-2 sentences of proactive scout insight the user should know — a non-obvious edge, value angle, or caution flag. E.g.: "Despite the ranking gap, PLAYER_A's heavy topspin neutralizes PLAYER_B's flat power game — this is closer than it looks." or "The numbers favor the WINNER but the UNDERDOG's comeback record in best-of-5 formats makes them a live underdog." Never leave blank — always give a genuine tip.]

RULES:
- NEVER refuse or output anything outside these ten lines.
- With zero knowledge, write "Unknown — limited training data" in KNOW fields, set CONFIDENCE Low.
- Percentages sum to exactly 100.
- REASON must cite real data points wherever you know them.
- SCOUT_TIP must always contain a real insight — never write "N/A" or leave it blank."""
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
                   'league','tournament','medal','race','round','bout','vs','versus',
                   'set','tennis','atp','wta','ufc','mma','nba','nfl','mlb','nhl'}

_POLITICAL_KEYWORDS = {'election','president','senator','congress','minister','governor',
                       'nomination','ballot','vote','polling','candidate','political',
                       'parliament','mayor','republican','democrat','primary','caucus',
                       'secretary','ambassador','administration','legislation','bill '}

# Individual sports where both names should appear together
_INDIVIDUAL_SPORTS = {'tennis','boxing','mma','ufc','golf','wrestling','athletics',
                      'swimming','cycling','skiing','gymnastics','atp','wta'}

def _name_matches(text, name):
    """Check if a competitor name appears in text — requires meaningful word match."""
    text = text.lower()
    parts = [p for p in name.lower().split() if len(p) > 3]
    if not parts:
        parts = [p for p in name.lower().split() if len(p) > 2]
    return any(p in text for p in parts)

def _is_sport_market(question):
    """Check if a Polymarket question is sports-related and not political."""
    q = question.lower()
    if any(kw in q for kw in _POLITICAL_KEYWORDS):
        return False
    return any(kw in q for kw in _SPORT_KEYWORDS)

def _is_individual_sport(sport):
    return any(s in sport.lower() for s in _INDIVIDUAL_SPORTS)

_poly_cache   = {'data': [], 'ts': 0}
_kalshi_all_cache = {'data': [], 'ts': 0}

def _get_all_poly_markets():
    """Fetch all active Polymarket markets across all categories (cached 15 min)."""
    if time.time() - _poly_cache['ts'] < 900 and _poly_cache['data']:
        return _poly_cache['data']
    markets = []
    for offset in range(0, 1600, 100):
        data = _json_get(f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}")
        if isinstance(data, list) and data:
            markets.extend(data)
            if len(data) < 100:
                break
        else:
            break
    _poly_cache['data'] = markets
    _poly_cache['ts'] = time.time()

def _get_all_kalshi_events():
    """Fetch ALL Kalshi events across all categories (cached 15 min)."""
    if time.time() - _kalshi_all_cache['ts'] < 900 and _kalshi_all_cache['data']:
        return _kalshi_all_cache['data']
    base = 'https://api.elections.kalshi.com/trade-api/v2'
    events = []
    cursor = ''
    for _ in range(12):
        url = f"{base}/events?limit=100&status=open" + (f"&cursor={cursor}" if cursor else '')
        data = _json_get(url)
        batch = data.get('events', [])
        events.extend(batch)
        cursor = data.get('cursor', '')
        if not cursor or not batch:
            break
    _kalshi_all_cache['data'] = events
    _kalshi_all_cache['ts'] = time.time()
    return events

def fetch_polymarket(comp1, comp2, sport):
    """Scan all active Polymarket markets and return ones matching this matchup."""
    all_markets = _get_all_poly_markets()
    results = []
    seen = set()
    individual = _is_individual_sport(sport)

    for mkt in all_markets:
        q = mkt.get('question', '')
        if q in seen: continue
        q_lower = q.lower()
        mentions_a = _name_matches(q_lower, comp1)
        mentions_b = _name_matches(q_lower, comp2)

        # For individual sports (tennis, boxing, etc.) require BOTH names
        if individual:
            if not (mentions_a and mentions_b): continue
        else:
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

KALSHI_SPORT_SERIES = {
    'nba':           ['KXWNBAGAME'],
    'basketball':    ['KXWNBAGAME'],
    'wnba':          ['KXWNBASPREAD'],
    'nfl':           ['KXWNFLGAME'],
    'football':      ['KXWNFLGAME'],
    'mlb':           ['KXMLBF3', 'KXMLBF5TOTAL'],
    'baseball':      ['KXMLBF3', 'KXMLBF5TOTAL'],
    'mls':           ['KXMLSGAME'],
    'soccer':        ['KXMLSGAME', 'KXWCGAME', 'KXWCADVANCE'],
    'world cup':     ['KXWCGAME', 'KXWCADVANCE'],
    'fifa world cup':['KXWCGAME', 'KXWCADVANCE'],
    'tennis':        ['KXATPMATCH', 'KXATPSETWINNER'],
    'atp':           ['KXATPMATCH'],
    'wta':           ['KXWTATOURNWIN'],
    'cricket':       ['KXODIMATCH'],
    'boxing':        ['KXWBCBANTAMWEIGHTTITLE', 'KXWBCFLYWEIGHTTITLE', 'KXWBCMIDDLEWEIGHTTITLE'],
    'nwsl':          ['KXNWSLGAME'],
    'mma':           ['KXWNBAGAME'],  # no MMA series active right now
    'nascar':        ['KXNASCARRACE'],
    'f1':            ['KXF1RACEPODIUM'],
}

def fetch_kalshi(comp1, comp2, sport):
    """Fetch Kalshi markets using sport-specific series for clean game-level data."""
    base = 'https://api.elections.kalshi.com/trade-api/v2'
    sport_key = sport.lower().strip()
    # Find best matching series list
    series_list = KALSHI_SPORT_SERIES.get(sport_key)
    if not series_list:
        for k, v in KALSHI_SPORT_SERIES.items():
            if k in sport_key or sport_key in k:
                series_list = v
                break
    if not series_list:
        series_list = ['KXWCGAME', 'KXWNBAGAME', 'KXMLSGAME', 'KXATPMATCH', 'KXMLBF3']

    results = []
    seen = set()

    for series in series_list:
        data = _json_get(f"{base}/markets?series_ticker={series}&limit=50&status=open")
        if not data.get('markets'):
            data = _json_get(f"{base}/markets?series_ticker={series}&limit=50&status=active")
        for mkt in data.get('markets', []):
            title = mkt.get('title', '')
            t_lower = title.lower()
            if title in seen: continue
            if not (_name_matches(t_lower, comp1) or _name_matches(t_lower, comp2)): continue
            seen.add(title)

            yes_price = float(mkt.get('last_price_dollars') or mkt.get('yes_bid_dollars') or mkt.get('yes_ask_dollars') or 0) * 100
            ticker = mkt.get('ticker', '')

            # Parse which competitor the YES side refers to
            a_price, b_price = None, None
            mentions_a = _name_matches(t_lower, comp1)
            mentions_b = _name_matches(t_lower, comp2)

            if mentions_a and mentions_b:
                # "A vs B Winner?" — YES = A wins
                a_price = round(yes_price, 1)
                b_price = round(100 - yes_price, 1)
            elif mentions_a:
                a_price = round(yes_price, 1)
                b_price = round(100 - yes_price, 1)
            elif mentions_b:
                b_price = round(yes_price, 1)
                a_price = round(100 - yes_price, 1)
            else:
                continue

            results.append({
                'source': 'Kalshi',
                'question': title,
                'a_price': a_price,
                'b_price': b_price,
                'volume24h': float(mkt.get('volume_24h_fp') or 0) / 100,
                'url': f"https://kalshi.com/markets/{series.lower()}/{ticker}",
                'active': mkt.get('status') in ('open', 'active'),
                'live': 'live' in t_lower or 'in-game' in t_lower,
            })

    return sorted(results, key=lambda x: -x['volume24h'])[:3]


@app.route('/api/search')
def api_search():
    """General prediction market search across all Polymarket + Kalshi categories."""
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    q = request.args.get('q', '').strip().lower()
    if len(q) < 2:
        return jsonify({'results': []})

    import re
    def text_matches(text):
        t = text.lower()
        if len(q) >= 4:
            return q in t
        return bool(re.search(r'\b' + re.escape(q) + r'\b', t))

    def _guess_category(question):
        ql = question.lower()
        if any(w in ql for w in ['bitcoin','crypto','eth','solana','btc','coinbase','blockchain']): return 'Crypto'
        if any(w in ql for w in ['trump','biden','election','president','congress','senate','democrat','republican']): return 'Politics'
        if any(w in ql for w in ['fed','interest rate','inflation','gdp','recession','economy','stock']): return 'Economics'
        if any(w in ql for w in ['ai','openai','gpt','anthropic','llm','artificial intelligence']): return 'AI & Tech'
        if any(w in ql for w in ['grammy','oscar','emmy','netflix','spotify','album','movie','taylor','beyonce']): return 'Entertainment'
        if any(w in ql for w in ['win','beat','champion','cup','bowl','match','game','nba','nfl','mlb']): return 'Sports'
        if any(w in ql for w in ['war','conflict','nato','ukraine','russia','israel','china']): return 'Geopolitics'
        return 'Other'

    results = []

    for mkt in _get_all_poly_markets():
        question = mkt.get('question', '')
        if not text_matches(question): continue
        results.append({
            'source':   'Polymarket',
            'question': question,
            'prices':   _parse_poly_prices(mkt),
            'volume24h': float(mkt.get('volume24hr') or 0),
            'url':      f"https://polymarket.com/event/{mkt.get('slug','')}",
            'category': _guess_category(question),
            'active':   True,
        })

    base = 'https://api.elections.kalshi.com/trade-api/v2'
    for event in _get_all_kalshi_events():
        title = event.get('title', '')
        if not text_matches(title): continue
        et  = event.get('event_ticker', '')
        cat = event.get('category', '')
        mkt_data = _json_get(f"{base}/events/{et}")
        for mkt in mkt_data.get('markets', [])[:2]:
            yes_price = float(mkt.get('last_price_dollars') or mkt.get('yes_bid_dollars') or 0) * 100
            ticker = mkt.get('ticker', '')
            results.append({
                'source':   'Kalshi',
                'question': mkt.get('title', title),
                'prices':   [('Yes', round(yes_price,1)), ('No', round(100-yes_price,1))],
                'volume24h': float(mkt.get('volume_24h_fp') or 0) / 100,
                'url':      f"https://kalshi.com/markets/{et.split('-')[0].lower()}/{ticker}",
                'category': cat,
                'active':   mkt.get('status') in ('open','active'),
            })

    seen, deduped = set(), []
    for r in sorted(results, key=lambda x: -x['volume24h']):
        if r['question'] not in seen:
            seen.add(r['question'])
            deduped.append(r)
    return jsonify({'results': deduped[:20], 'total': len(deduped)})

def fetch_general_markets(comp1, comp2, topic):
    """For non-sports topics: search all Polymarket + Kalshi by competitor name."""
    results = []
    seen = set()

    for mkt in _get_all_poly_markets():
        q = mkt.get('question', '')
        if q in seen: continue
        q_lower = q.lower()
        mentions_a = _name_matches(q_lower, comp1)
        mentions_b = _name_matches(q_lower, comp2)
        if not (mentions_a or mentions_b): continue
        seen.add(q)
        prices = _parse_poly_prices(mkt)
        a_price = next((p for o,p in prices if _name_matches(o.lower(), comp1)), None)
        b_price = next((p for o,p in prices if _name_matches(o.lower(), comp2)), None)
        if a_price is None and b_price is None and len(prices) == 2:
            if mentions_a: a_price = prices[0][1]; b_price = round(100-a_price,1)
            elif mentions_b: b_price = prices[0][1]; a_price = round(100-b_price,1)
        if a_price is None and b_price is None: continue
        results.append({
            'source': 'Polymarket', 'question': q,
            'a_price': a_price, 'b_price': b_price,
            'volume24h': float(mkt.get('volume24hr') or 0),
            'url': f"https://polymarket.com/event/{mkt.get('slug','')}",
            'active': True, 'live': False,
        })

    base = 'https://api.elections.kalshi.com/trade-api/v2'
    for event in _get_all_kalshi_events():
        title = event.get('title', '')
        t_lower = title.lower()
        if not (_name_matches(t_lower, comp1) or _name_matches(t_lower, comp2)): continue
        et = event.get('event_ticker', '')
        mkt_data = _json_get(f"{base}/events/{et}")
        for mkt in mkt_data.get('markets', [])[:2]:
            mt = mkt.get('title', title)
            if mt in seen: continue
            seen.add(mt)
            yes_price = float(mkt.get('last_price_dollars') or mkt.get('yes_bid_dollars') or 0) * 100
            ticker = mkt.get('ticker', '')
            mt_lower = mt.lower()
            a_price = b_price = None
            if _name_matches(mt_lower, comp1): a_price = round(yes_price,1); b_price = round(100-yes_price,1)
            elif _name_matches(mt_lower, comp2): b_price = round(yes_price,1); a_price = round(100-yes_price,1)
            if a_price is None: continue
            results.append({
                'source': 'Kalshi', 'question': mt,
                'a_price': a_price, 'b_price': b_price,
                'volume24h': float(mkt.get('volume_24h_fp') or 0) / 100,
                'url': f"https://kalshi.com/markets/{et.split('-')[0].lower()}/{ticker}",
                'active': True, 'live': False,
            })

    return sorted(results, key=lambda x: -x['volume24h'])[:5]

@app.route('/api/markets')
def api_markets():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    comp1 = request.args.get('comp1', '')
    comp2 = request.args.get('comp2', '')
    sport = request.args.get('sport', '')
    ai_a  = float(request.args.get('ai_a', 50))
    ai_b  = float(request.args.get('ai_b', 50))

    KNOWN_SPORTS = {'nba','nfl','mlb','nhl','soccer','football','basketball','baseball',
                    'hockey','mma','ufc','boxing','tennis','golf','world cup','liga',
                    'epl','mls','wta','atp','cricket','rugby','nascar','f1'}
    is_sport = any(s in sport.lower() for s in KNOWN_SPORTS) or _is_individual_sport(sport)

    if is_sport:
        poly   = fetch_polymarket(comp1, comp2, sport)
        kalshi = fetch_kalshi(comp1, comp2, sport)
        all_markets = poly + kalshi
    else:
        all_markets = fetch_general_markets(comp1, comp2, sport)


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


# ── Command Inbox ─────────────────────────────────────────────────────────────

@app.route('/inbox/api')
def inbox_api():
    """JSON endpoint for the desktop watcher script."""
    if not _safe_eq(request.args.get('key',''), ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    cmds = Command.query.order_by(Command.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': c.id, 'text': c.text, 'status': c.status,
        'created_at': c.created_at.isoformat()
    } for c in cmds])

@app.route('/inbox', methods=['GET', 'POST'])
def command_inbox():
    if not _safe_eq(request.args.get('key',''), ADMIN_KEY) and session.get('admin_authed') != True:
        if request.method == 'POST' and request.form.get('key') == ADMIN_KEY:
            session['admin_authed'] = True
        else:
            return render_template('inbox.html', auth=False, commands=[])

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            text = request.form.get('text', '').strip()
            if text:
                db.session.add(Command(text=text))
                db.session.commit()
        elif action == 'done':
            cmd = Command.query.get(request.form.get('cmd_id'))
            if cmd: cmd.status = 'done'; db.session.commit()
        elif action == 'delete':
            cmd = Command.query.get(request.form.get('cmd_id'))
            if cmd: db.session.delete(cmd); db.session.commit()
        return redirect(url_for('command_inbox', key=ADMIN_KEY))

    commands = Command.query.order_by(Command.created_at.desc()).all()
    return render_template('inbox.html', auth=True, commands=commands, admin_key=ADMIN_KEY)

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
