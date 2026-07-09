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
                model="claude-sonnet-4-6",
                max_tokens=1400,
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

ANALYSIS_PROMPT = """You are an elite quantitative sports analyst. You combine the rigor of a statistician with the intuition of a scout. Your edge is that you go deeper than surface stats and capture what Vegas misses.

SPORT: {sport}
COMPETITOR A: {comp1}
COMPETITOR B: {comp2}
{search_block}{context_block}

INTERNAL ANALYSIS — run all four phases silently. Never appear in output.

PHASE 1 — STATISTICAL FOUNDATION (35% weight):
Apply the most relevant sport-specific metrics for {sport}:
- Combat sports (MMA/Boxing): strike accuracy, takedown %, damage absorption, finishing rate, championship round performance
- Racket sports (Tennis/Squash): Elo/UTR rating, first-serve %, break point conversion, surface-specific win rate
- Soccer/Football: xG and xGA, possession metrics, pressing intensity, set piece efficiency
- Basketball: True Shooting %, net rating, clutch time +/-, turnover differential
- Baseball: OPS+, ERA+, WHIP, batted ball profile, platoon splits
- If a competitor is unknown or amateur: use whatever IS known and set CONFIDENCE to Low
Score: efficiency metrics, recent form (exponential decay — last 3 results weighted 3x vs earlier ones), quality of competition, head-to-head control, performance vs shared opponents.

PHASE 2 — CONTEXTUAL EDGE (25% weight):
- Style matchup: does A's style systematically counter B's? (counter-puncher vs pressure fighter, grappler vs striker, high-press vs low-block)
- Age curve: where is each competitor on their performance arc — ascending, peak, or declining?
- Venue: home advantage (typically +3-5%), altitude, travel fatigue, hostile crowd effects
- Physical condition: known injuries and their estimated % performance impact
- Schedule: rest days, back-to-back situations, tournament fatigue stage

PHASE 3 — PSYCHOLOGICAL PRESSURE (25% weight):
- Clutch record: performance in close games (within 1 score in final moments), tiebreaks, late rounds, elimination situations
- Adversity response: who is more dangerous when behind or hurt?
- Championship pedigree: finals experience, title defenses, big-match record
- Current momentum: not just win streak — are they playing with freedom and confidence, or tightness?
- Coaching/corner/strategic advantage where applicable

PHASE 4 — THE IT FACTOR (15% weight):
The intangible edge no algorithm captures alone:
- Will to win: who has demonstrated they sacrifice most when it matters most?
- Peak ceiling: at their absolute best, who has the higher output potential?
- In-competition adaptation: who adjusts better when gameplan A fails?
- Competitive hunger: is one side hungrier right now — motivated challenger vs comfortable champion?
- The defining quality: what separates truly great {sport} competitors from merely good ones — who embodies it more?
A strong IT Factor can override a moderate statistical disadvantage. This is where upsets are correctly predicted.

FINAL CALCULATION:
Weight phases 35/25/25/15. Be decisive — do not default to 50/50 unless evidence is genuinely equal.
- HIGH confidence: 3+ phases clearly favor one competitor
- MEDIUM confidence: 2 phases favor one but meaningful uncertainty exists
- LOW confidence: data is limited, phases conflict, or matchup is genuinely close

OUTPUT RULES — ABSOLUTE:
- Output ONLY the five lines below. Nothing else. Zero prose outside this format.
- NEVER refuse. With zero data, make best inference and set CONFIDENCE to Low.
- Percentages must sum to exactly 100.
- REASON: 2 sentences max. Name the 1-2 decisive factors. Commit to the pick.

A_PCT: [number]
B_PCT: [number]
WINNER: [full name]
CONFIDENCE: [High/Medium/Low]
REASON: [2 sentences — decisive factors only, no hedging]"""

def web_search(query):
    """Search Tavily for real-world info. Returns a short text snippet or empty string."""
    if not TAVILY_API_KEY:
        return ''
    try:
        payload = json.dumps({
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": 3,
            "include_answer": True,
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res = urllib.request.urlopen(req, timeout=8)
        data = json.loads(res.read())
        parts = []
        if data.get('answer'):
            parts.append(data['answer'])
        for r in data.get('results', [])[:3]:
            if r.get('content'):
                parts.append(r['content'][:300])
        return '\n'.join(parts)
    except Exception:
        return ''

def build_prompt(sport, comp1, comp2, context):
    # Live search for each competitor to ground Claude in real facts
    search1 = web_search(f"{comp1} {sport} player career stats")
    search2 = web_search(f"{comp2} {sport} player career stats")

    search_block = ''
    if search1 or search2:
        search_block = '\nLIVE RESEARCH (use this to identify and evaluate each competitor accurately):\n'
        if search1:
            search_block += f'\n[{comp1} — {sport}]\n{search1}\n'
        if search2:
            search_block += f'\n[{comp2} — {sport}]\n{search2}\n'

    context_block = ''
    if context.strip():
        context_block = f'\nAdditional context: {context}'

    return ANALYSIS_PROMPT.format(
        sport=sport, comp1=comp1, comp2=comp2,
        search_block=search_block,
        context_block=context_block
    )

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
