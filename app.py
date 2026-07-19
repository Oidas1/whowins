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
from sqlalchemy import func, text
import random
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

class AppConfig(db.Model):
    """Key-value store for app-level config that must survive server restarts."""
    __tablename__ = 'ww_config'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)

class UserProfile(db.Model):
    __tablename__ = 'ww_users'
    id          = db.Column(db.Integer, primary_key=True)
    user_uid    = db.Column(db.String(64), unique=True, nullable=False, index=True)
    referred_by = db.Column(db.String(64), nullable=True)
    handle      = db.Column(db.String(50), unique=True, nullable=True, index=True)
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
    is_fade        = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

class Waitlist(db.Model):
    __tablename__ = 'ww_waitlist'
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(200), unique=True, nullable=False)
    source     = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Squad(db.Model):
    __tablename__ = 'ww_squads'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    invite_code = db.Column(db.String(20), unique=True, nullable=False)
    created_by  = db.Column(db.String(64), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

class SquadMember(db.Model):
    __tablename__ = 'ww_squad_members'
    id        = db.Column(db.Integer, primary_key=True)
    squad_id  = db.Column(db.Integer, db.ForeignKey('ww_squads.id'), nullable=False)
    user_uid  = db.Column(db.String(64), nullable=False, index=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('squad_id', 'user_uid'),)

with app.app_context():
    try:
        db.create_all()
        # Column migrations for existing tables (ADD COLUMN IF NOT EXISTS is Postgres-safe)
        try:
            with db.engine.connect() as conn:
                for stmt in [
                    "ALTER TABLE ww_users   ADD COLUMN IF NOT EXISTS handle  VARCHAR(50) UNIQUE",
                    "ALTER TABLE ww_queries ADD COLUMN IF NOT EXISTS is_fade BOOLEAN DEFAULT FALSE",
                ]:
                    conn.execute(text(stmt))
                conn.commit()
        except Exception:
            pass
        # Load a stable SECRET_KEY from DB so sessions survive every server restart
        # and every SITE_PASSWORD rotation (which triggers a Render redeploy).
        try:
            row = AppConfig.query.filter_by(key='secret_key').first()
            if row:
                app.secret_key = row.value
            else:
                # First boot: use env var if set, otherwise generate one, then pin it in DB
                sk = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
                try:
                    db.session.add(AppConfig(key='secret_key', value=sk))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    # Another worker beat us to it — read theirs
                    row = AppConfig.query.filter_by(key='secret_key').first()
                    if row:
                        sk = row.value
                app.secret_key = sk
        except Exception as e:
            print(f"Warning: could not pin SECRET_KEY in DB: {e}")
            # Falls back to the value already set at module level
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

# ── Rank / streak / stats helpers ─────────────────────────────────────────────

def _get_rank(wins, rate, total):
    if total < 5:
        return {'name': 'Rookie', 'emoji': '🎯', 'tier': 'rookie',
                'progress': total, 'next_at': 5, 'next_name': 'Sharp'}
    score = wins * max(0, (rate - 45)) / 10.0
    if score >= 30:
        return {'name': 'Legend', 'emoji': '👑', 'tier': 'legend', 'score': round(score, 1)}
    elif score >= 12:
        return {'name': 'Elite',  'emoji': '🔥', 'tier': 'elite',  'score': round(score, 1),
                'next_at': 30, 'next_name': 'Legend'}
    elif score >= 4:
        return {'name': 'Sharp',  'emoji': '⚡', 'tier': 'sharp',  'score': round(score, 1),
                'next_at': 12, 'next_name': 'Elite'}
    else:
        return {'name': 'Rookie', 'emoji': '🎯', 'tier': 'rookie', 'score': round(score, 1),
                'next_at': 4,  'next_name': 'Sharp'}

def _calc_streak(uid):
    """Positive = win streak length, negative = loss streak length, 0 = no data."""
    recent = Query.query.filter(
        Query.user_uid == uid,
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == False  # noqa: E712
    ).order_by(Query.created_at.desc()).limit(20).all()
    if not recent:
        return 0
    first, count = recent[0].outcome, 0
    for q in recent:
        if q.outcome == first: count += 1
        else: break
    return count if first == 'win' else -count

def _user_stats(uid):
    """Returns (wins, total_settled, hit_rate_pct) ignoring fade picks."""
    settled = Query.query.filter(
        Query.user_uid == uid,
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == False  # noqa: E712
    ).all()
    total = len(settled)
    wins  = sum(1 for q in settled if q.outcome == 'win')
    rate  = round(wins / total * 100) if total else 0
    return wins, total, rate

def _value_edge(q):
    """Percentage points by which Scout's chosen-side probability exceeded the market."""
    if not q.a_odds_pct or not q.winner or not q.competitor_a:
        return 0
    winner_is_a = q.winner.lower() in q.competitor_a.lower()
    scout = (q.ai_a_pct if winner_is_a else q.ai_b_pct) or 0
    mkt   = (q.a_odds_pct if winner_is_a else q.b_odds_pct) or 0
    return abs(scout - mkt)

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


def _make_invite_token(days=7):
    """Create an HMAC-signed invite token valid for `days` days."""
    expires  = str(int(time.time()) + days * 86400)
    sig      = hmac.new(app.secret_key.encode(), expires.encode(), hashlib.sha256).hexdigest()[:20]
    raw      = f"{expires}.{sig}"
    return urllib.parse.quote(raw, safe='')

def _verify_invite_token(token):
    try:
        raw              = urllib.parse.unquote(token)
        expires_str, sig = raw.split('.', 1)
        if time.time() > int(expires_str):
            return False
        expected = hmac.new(app.secret_key.encode(), expires_str.encode(), hashlib.sha256).hexdigest()[:20]
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

@app.route('/i/<token>')
def invite_login(token):
    """Friend-shareable invite link — no password required."""
    if _verify_invite_token(token):
        session.permanent = True
        session['authed'] = True
        get_user_uid()
        return redirect(url_for('index'))
    return redirect(url_for('login'))

@app.route('/admin/invite-link')
def admin_invite_link():
    """Generate a 7-day invite link. Requires ADMIN_KEY."""
    if not _safe_eq(request.args.get('key', ''), ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    days  = min(int(request.args.get('days', 7)), 30)
    token = _make_invite_token(days)
    link  = url_for('invite_login', token=token, _external=True)
    return jsonify({'link': link, 'valid_days': days,
                    'note': 'Share this URL with friends — no password needed.'})


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
    # Check if marking a win created a notable streak — pass it to journal for modal
    streak = 0
    if outcome == 'win':
        streak = _calc_streak(entry.user_uid)
    if streak >= 5:
        return redirect(url_for('journal', streak=streak))
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

RECENCY OVERRIDE RULE (critical):
Your training data has a knowledge cutoff. Any statistics, rankings, records, or form data from the past 12 months in the LIVE RESEARCH above must take ABSOLUTE PRIORITY over whatever you recall from training. If live research shows a ranking, win/loss record, or recent form that contradicts your training memory — use the live research figure and treat your training memory as potentially stale. Flag clearly if the live data is sparse or contradictory.

STEP 0 — KNOWLEDGE RECALL (do this FIRST):
Before any math, recall what you know about each subject from training data.
KNOW_A: [everything you know about {comp1} in the context of {sport} — rankings, records, polls, stats, history.
  NAME COLLISION RULE: If {comp1} is a common name that belongs to a famous person in a DIFFERENT sport, reason explicitly: "There is a famous [other sport] player named {comp1}, but in the context of {sport}, I am analyzing [name] as a {sport} competitor. Any UTR/ranking data from live research refers to the {sport} version of this person." Use all available data — UTR ratings, rankings, records — for the {sport} context. Do NOT discard live research data because of a name collision; instead interpret it correctly.]
KNOW_B: [same depth and same name-collision reasoning for {comp2}]

STEP 0.25 — BASE RATE ANCHOR:
Before any individual adjustments, establish the historical base rate for this TYPE of matchup. This is your statistical prior — do not stray more than 20-25% from it without multiple strong signals.
  Tennis: Top-10 vs Top-50 ≈ 65-70%; #1 vs #10 ≈ 62%; similar-ranked players ≈ 50-55%
  NBA/NFL: Home favorite ≈ 65-70%; road favorite ≈ 55-60%; home underdog ≈ 40%
  Soccer: Home team wins ≈ 46% of all games; draw ≈ 26%; away ≈ 28%
  MMA/Boxing: Champion vs. ranked challenger ≈ 60-65%; similar-ranked ≈ 50-55%
  Politics: Incumbent in general election ≈ 70-75% historically; challenger primary ≈ varies
  Crypto/Finance: Past price momentum predicts next-period direction ≈ 55% (weak signal)
  Entertainment awards: Critical consensus front-runner wins ≈ 60-70% of the time
  Generic unknown: Start at 50%.
  NOTE: If your research reveals a dramatically different base rate (e.g., an all-time dominant player), document it explicitly and justify the deviation.

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

CONDITIONS ANALYSIS (always evaluate — these factors shift probability independently of skill):
  VENUE / HOME ADVANTAGE:
    Is this at a neutral site, or does one side have home advantage? Home advantage in team sports = +3-6%.
    For individual sports: is this a tournament where one player has historically performed well?
  REST & SCHEDULE:
    How many days since each competitor's last competition? Ideal rest = 2-5 days for most sports.
    Back-to-back games, 4th game in 5 nights, grueling recent schedule → -3-7% for fatigued side.
  TRAVEL & TIMEZONE:
    International travel across >3 time zones → -2-4%. Long east-west travel same day = meaningful factor.
  SURFACE / FORMAT / ENVIRONMENT:
    Tennis: clay vs. hard vs. grass can shift probabilities 10-15% for style-dependent players.
    Outdoor sports: heavy rain or extreme heat neutralizes technical advantage → pushes toward 50/50.
    Playoff format vs. best-of vs. single-game: high-variance formats benefit underdogs.
  MOTIVATION / STAKES:
    Elimination game: underdog desperation may close gap. Meaningless game: favorite may coast.
    Revenge match, grudge factor, or personal stakes often elevates underdog intensity.
  Apply any relevant conditions adjustments BEFORE reaching Step 1.

STEP 1 — DOMAIN-SPECIFIC ANALYTICAL BASELINE:
Identify the domain from TOPIC and apply the appropriate model. Extract the specific metrics listed under each sport from the research data provided above — these numbers matter more than narrative descriptions.

SPORTS:
  TENNIS: Elo formula P = 1/(1+10^((B_Elo-A_Elo)/400)). UTR gap 1.0 = ~70% win prob. ALWAYS use UTR data when provided.
    EXTRACT FROM RESEARCH: surface-specific win% (clay/hard/grass), first-serve%, break points saved/converted, H2H on this surface, recent seeding, ace rate.
    SURFACE SHIFT: A player's probability can shift 10-20% depending on surface vs their preferred surface.

  SOCCER/FOOTBALL: Poisson model on recent xG (expected goals for and against).
    EXTRACT FROM RESEARCH: xG for and against last 5 games, form table (W/D/L string), goals scored/conceded, clean sheets %, H2H at this venue type.
    NOTE: Single soccer match = very high variance. A 60% favorite should rarely get above 60% for a single game.

  BASKETBALL (NBA/WNBA): Pythagorean W% = Pts^13.91/(Pts^13.91+PA^13.91).
    EXTRACT FROM RESEARCH: Net rating (offensive rating minus defensive rating — most predictive single stat), eFG%, pace, last-10-game record, home/road splits, key player injury status.
    Net rating differential: each +5 net rating = approximately +8% win probability in regular season.

  FOOTBALL (NFL/NCAA): 1 spread point = 2.8% win probability shift.
    EXTRACT FROM RESEARCH: EPA per play (offense and defense), turnover differential, red zone efficiency, yards per attempt, third-down conversion rate, rest advantage.
    Turnover differential: each +1 turnover per game edge = ~+4% win probability.

  BASEBALL (MLB): Log5 formula. Run differential = best team quality predictor.
    EXTRACT FROM RESEARCH: Team wOBA or OPS (offense), starting pitcher FIP or ERA (not ERA alone — FIP removes luck), bullpen ERA, BABIP (if >.360 = luck, if <.240 = unlucky — adjust accordingly), run differential.

  MMA/BOXING: Strike accuracy × finishing rate = base threat level.
    EXTRACT FROM RESEARCH: Striking accuracy%, takedown defense%, finishing rate, significant strikes per minute, reach advantage, recent opponent quality, weight-cut history.

  GOLF: Strokes gained (SG) is the gold standard.
    EXTRACT FROM RESEARCH: SG Total, SG Off-the-Tee, SG Approach, SG Putting, course history for this specific venue, current form in recent tournaments, course type match (links/parkland/elevation).

POLITICS / ELECTIONS:
  Polling average gap: each +5% polling lead = ~+8% win probability, with incumbency +3%.
  Fundraising edge: each 2x fundraising advantage = ~+3% probability.
  EXTRACT FROM RESEARCH: polling average margin, trend direction, approval ratings, demographic breakdowns, early vote performance.

CRYPTO / FINANCE:
  Market dominance: relative market cap, 30-day price momentum, developer activity (GitHub commits).
  EXTRACT FROM RESEARCH: price momentum (30/90-day), volume trend, institutional holding%, on-chain metrics.

ENTERTAINMENT / AWARDS:
  Historical base rate: prior wins, nomination frequency, precursor awards won.
  EXTRACT FROM RESEARCH: Metacritic/RT score, social media sentiment, box office/chart trajectory, industry campaign spend.

BUSINESS / COMPANIES:
  Revenue growth differential, market cap trajectory, competitive moat depth.

GEOPOLITICS:
  Power asymmetry (GDP, military, alliance structure), historical precedent, current leverage.

GENERAL:
  Start 50/50, apply evidence-weighted adjustments for each advantage found.

STEP 2 — TEMPORAL WEIGHTING:
  Most recent data = 1.0x. One period prior = 0.75x. Two prior = 0.56x. Three = 0.42x. Four = 0.32x.
  Weight recent polls/results/form more heavily than historical averages.

STEP 3 — QUANTIFIED ADJUSTMENTS (add/subtract %):
  SPORTS: Home +3.5%, injury -5 to -15%, H2H edge +1.5% per win above 50/50, style mismatch +3-7%
  POLITICS: Incumbency +3%, geographic stronghold +2-5%, debate performance +/-2%, scandal -5 to -10%
  FINANCE: Macro tailwind/headwind +/-5%, regulatory risk -5%, network effect moat +3%
  GENERAL: Apply domain-appropriate modifiers with similar magnitude ranges

  REGRESSION-TO-MEAN (always check):
    Hot streaks (7+ consecutive wins) reliably regress toward the long-run average. Apply -2 to -4% to the hot competitor.
    Cold streaks (6+ consecutive losses) also regress upward. Apply +2 to +4% to the cold competitor.
    Baseball BABIP: if >.360 = luck running hot (expect regression down); if <.240 = unlucky (expect regression up). Adjust ±4%.
    A team/player WELL above their season average in recent games → expect partial regression.

  STRENGTH OF SCHEDULE (SOS):
    Recent wins over bottom-5 opponents count ~50% of face value. Wins over top-5 opponents count 150%.
    Recent losses to top-5 opponents matter ~50% as much as a loss to an equal competitor.
    If research reveals one side has been padding their record against weak competition, apply -3 to -5%.
    If research shows one side has been battling top competition and staying competitive, credit +2 to +4%.

  FORMAT VARIANCE (affects CONFIDENCE level, not just probability):
    Single elimination / one game (especially soccer): HIGH VARIANCE. Cap CONFIDENCE at MEDIUM even if analytics favor one side 65%+.
    Best-of-3: 60% single-game edge → ~65% series. Moderate variance — MEDIUM confidence appropriate.
    Best-of-5: 60% game → ~68% series. 65% game → ~76% series.
    Best-of-7: 60% game → ~71% series. 65% game → ~80% series. Lower variance — HIGH confidence more supportable.
    Round-robin tournament: rewards consistency, not peaks — weight season record more heavily than recent hot streak.

STEP 4 — BAYESIAN UPDATE (when market odds provided above):
  P_final = (W x P_analysis) + ((1-W) x P_market)
  W = 0.65 (High confidence), 0.50 (Medium), 0.35 (Low)
  LIQUIDITY WEIGHTING: Prediction markets aggregate thousands of informed bettors. High-volume markets (>$100k 24h) are highly efficient — reduce W by 0.10 (give market more weight). Low-volume markets (<$5k) are thin/noisy — increase W by 0.10 (trust your analysis more).
  CONSENSUS RULE: When Vegas AND Polymarket AND Kalshi all converge within 5% of each other, treat this as very strong evidence. Only deviate from market consensus when you have a specific, verifiable edge the market may not have priced in.
  Market divergence >15% = re-examine your assumptions from Step 1. If your data is solid, hold. If research was thin, defer to market.

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

STEP 5.5 — DEVIL'S ADVOCATE (run this before finalizing):
  Argue the STRONGEST REALISTIC CASE for the competitor you're currently predicting to LOSE.
  What specific scenario leads to an upset?
  • Is there a style matchup disadvantage for the favorite that only shows up under pressure?
  • Could the underdog's best-case recent form beat the favorite's worst-case recent form?
  • Is there a key X-factor (surface, format, crowd, revenge motivation) that neutralizes the skill gap?
  • Has the predicted loser beaten or nearly beaten this exact type of opponent before?
  After arguing their case: does it move your probability by >5%? If YES → adjust. If NO → document it in SCOUT_TIP as the main risk.
  This step MUST change something: either your probability OR your SCOUT_TIP. It cannot be skipped.

STEP 6 — FINAL:
  Combine all steps. Cap at 95%, floor at 5%.
  HIGH confidence: baseline + 2+ adjustments + IT Factor all favor same side.
  MEDIUM: baseline favors one, adjustments mixed.
  LOW: limited data, conflicting signals, or genuinely near 50/50.

CONFIDENCE CALIBRATION (follow exactly — this is critical for accuracy):
  HIGH requires ALL THREE: (a) baseline model strongly favors one side, (b) at least 2 independent adjustments (H2H, form, style, market) confirm it, AND (c) probability gap is ≥60/40.
  MEDIUM: two signals agree, OR gap is 55/45–59/41 with some supporting evidence.
  LOW: only one signal, conflicting evidence, gap under 55/45, or limited data.
  CRITICAL RULES:
  — Do NOT output HIGH when ranking is the only favorable signal.
  — Do NOT output HIGH when research results returned empty or thin data.
  — A 50/50 call is not embarrassing; it is honest and accurate.
  — If you have real doubt, output MEDIUM. Reserve HIGH for genuine slam dunks with multiple proof points.

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

# Sport-specific query profiles — each tuple is (query_template, tavily_depth)
# {name} is substituted with the competitor's name
SPORT_RESEARCH_PROFILES = {
    'tennis': [
        ("{name} ATP WTA ITF ranking surface win percentage clay hard grass statistics 2025 2026", "advanced"),
        ("{name} tennis recent matches results form last 10 wins losses 2026", "basic"),
        ("{name} tennis serve statistics first serve percentage aces break points career", "basic"),
    ],
    'atp':  'tennis',
    'wta':  'tennis',
    'itf':  'tennis',
    'nba': [
        ("{name} NBA statistics 2025-26 season points rebounds assists net rating offensive defensive rating efficiency", "advanced"),
        ("{name} NBA team recent form last 10 games win loss streak 2026", "basic"),
        ("{name} NBA shooting percentage eFG true shooting home road splits conference standing", "basic"),
    ],
    'basketball': 'nba',
    'wnba': [
        ("{name} WNBA statistics 2025 2026 season points rebounds assists efficiency", "advanced"),
        ("{name} WNBA team recent form results 2026", "basic"),
        ("{name} WNBA standings record conference 2026", "basic"),
    ],
    'nfl': [
        ("{name} NFL statistics 2025 season EPA yards touchdowns passing rushing efficiency", "advanced"),
        ("{name} NFL recent games last 5 performance results 2025 2026", "basic"),
        ("{name} NFL team record standings division turnover differential red zone 2025", "basic"),
    ],
    'football': 'nfl',
    'mlb': [
        ("{name} MLB statistics 2025 2026 batting average OBP slugging wOBA ERA WHIP FIP BABIP", "advanced"),
        ("{name} MLB recent games last 10 results form 2026", "basic"),
        ("{name} MLB team standings wins losses run differential home away record 2026", "basic"),
    ],
    'baseball': 'mlb',
    'nhl': [
        ("{name} NHL statistics 2025-26 goals assists points Corsi Fenwick possession shooting percentage", "advanced"),
        ("{name} NHL team recent form last 10 games win loss streak 2026", "basic"),
        ("{name} NHL standings points power play percentage penalty kill goals for against 2026", "basic"),
    ],
    'hockey': 'nhl',
    'soccer': [
        ("{name} soccer football goals assists xG expected goals statistics 2025 2026 season", "advanced"),
        ("{name} football recent form last 5 matches results table position 2025 2026", "basic"),
        ("{name} soccer defensive record clean sheets goals conceded form table standing 2026", "basic"),
    ],
    'epl': 'soccer', 'mls': 'soccer', 'bundesliga': 'soccer', 'laliga': 'soccer',
    'champions': 'soccer', 'world': 'soccer', 'fifa': 'soccer',
    'mma': [
        ("{name} MMA UFC record wins losses KO TKO submission significant strikes statistics career", "advanced"),
        ("{name} MMA fighter recent fights 2025 2026 results performance", "basic"),
        ("{name} fighter striking accuracy takedown defense grappling wrestling style", "basic"),
    ],
    'ufc': 'mma',
    'boxing': [
        ("{name} boxing professional record wins losses knockouts KO ratio BoxRec ranking", "advanced"),
        ("{name} boxer recent fights 2025 2026 results performance KO TKO decision", "basic"),
        ("{name} boxing punch power speed reach style strengths weaknesses analysis", "basic"),
    ],
    'golf': [
        ("{name} PGA Tour LPGA strokes gained total approach putting off tee 2025 2026", "advanced"),
        ("{name} golf recent tournament results form finishes 2025 2026", "basic"),
        ("{name} golf course history specific venue performance statistics", "basic"),
    ],
    'cricket': [
        ("{name} cricket batting average runs wickets bowling economy statistics 2025 2026", "advanced"),
        ("{name} cricket recent matches form results 2025 2026", "basic"),
        ("{name} cricket pitch conditions format test ODI T20 record", "basic"),
    ],
    'formula': [
        ("{name} F1 Formula One championship points fastest laps qualifying statistics 2025 2026", "advanced"),
        ("{name} F1 recent race results form podiums 2025 2026", "basic"),
        ("{name} F1 team constructor reliability tire strategy circuit history", "basic"),
    ],
}

def _resolve_sport_profile(sport):
    """Resolve sport string to a research profile, following alias chains."""
    key = sport.lower().split()[0]
    profile = SPORT_RESEARCH_PROFILES.get(key)
    # Follow string aliases (e.g. 'basketball' -> 'nba')
    if isinstance(profile, str):
        profile = SPORT_RESEARCH_PROFILES.get(profile)
    return profile  # None if not found → use generic

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

ESPN_LEAGUE_MAP = {
    'nba': ('basketball', 'nba'),
    'basketball': ('basketball', 'nba'),
    'nfl': ('football', 'nfl'),
    'football': ('football', 'nfl'),
    'mlb': ('baseball', 'mlb'),
    'baseball': ('baseball', 'mlb'),
    'nhl': ('hockey', 'nhl'),
    'hockey': ('hockey', 'nhl'),
    'mls': ('soccer', 'usa.1'),
    'soccer': ('soccer', 'eng.1'),   # default EPL for generic 'soccer'
    'epl': ('soccer', 'eng.1'),
}

def fetch_espn_team_stats(sport_str, team_name):
    """Pull structured standings + recent form from ESPN's free API."""
    sport_key = sport_str.lower().split()[0]
    league_info = ESPN_LEAGUE_MAP.get(sport_key)
    if not league_info:
        return None
    sport_path, league_path = league_info
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/teams"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=6).read())
        teams = data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])
        # Find best match by name
        team_name_lower = team_name.lower()
        matched = None
        for t in teams:
            tn = t.get('team', {})
            names = [tn.get('displayName',''), tn.get('shortDisplayName',''), tn.get('name',''), tn.get('abbreviation','')]
            if any(team_name_lower in n.lower() or n.lower() in team_name_lower for n in names if n):
                matched = tn
                break
        if not matched:
            return None
        tid = matched.get('id')
        # Get team record from scoreboard
        sb_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/teams/{tid}"
        req2 = urllib.request.Request(sb_url, headers={'User-Agent': 'Mozilla/5.0'})
        tdata = json.loads(urllib.request.urlopen(req2, timeout=6).read())
        record = tdata.get('team', {}).get('record', {}).get('items', [{}])[0]
        stats = {s.get('name'): s.get('displayValue') for s in record.get('stats', [])}
        return {
            'team': matched.get('displayName', team_name),
            'wins': stats.get('wins', '?'),
            'losses': stats.get('losses', '?'),
            'pct': stats.get('winPercent', '?'),
            'streak': stats.get('streak', '?'),
            'home': stats.get('home', '?'),
            'away': stats.get('away', '?'),
        }
    except Exception:
        return None

def utr_win_probability(utr_a, utr_b):
    """Elo-based win probability from UTR ratings. 1.0 UTR ≈ 150 Elo points."""
    elo_diff = (utr_a - utr_b) * 150
    return round(1 / (1 + 10 ** (-elo_diff / 400)) * 100, 1)

def research_competitor(name, sport):
    """Run KPI-targeted searches for a competitor using sport-specific query profiles."""
    profile = _resolve_sport_profile(sport)

    if profile:
        query_pairs = [(tmpl.format(name=name), depth) for tmpl, depth in profile]
    else:
        # Generic fallback
        query_pairs = [
            (f"{name} {sport} career statistics record performance history", "advanced"),
            (f"{name} {sport} recent form results wins losses 2025 2026", "basic"),
            (f"{name} {sport} ranking current season standing efficiency metrics", "basic"),
        ]

    results = []
    for q, depth in query_pairs:
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

    is_tennis     = any(w in sport.lower() for w in ['tennis', 'atp', 'wta', 'itf', 'challenger', 'utr'])
    is_team_sport = any(w in sport.lower() for w in list(ESPN_LEAGUE_MAP.keys()))

    threads = [
        threading.Thread(target=fetch, args=('comp1', research_competitor, comp1, sport)),
        threading.Thread(target=fetch, args=('comp2', research_competitor, comp2, sport)),
        threading.Thread(target=fetch, args=('h2h', web_search,
            f"{comp1} vs {comp2} {sport} head to head career history results", "basic", 4)),
        threading.Thread(target=fetch, args=('odds', fetch_odds, sport, comp1, comp2)),
        threading.Thread(target=fetch, args=('news', web_search,
            f"{comp1} OR {comp2} {sport} injury news update latest", "basic", 3)),
        threading.Thread(target=fetch, args=('recent_h2h', web_search,
            f"{comp1} vs {comp2} {sport} 2025 2026 recent match results", "basic", 3)),
        threading.Thread(target=fetch, args=('conditions', web_search,
            f"{comp1} vs {comp2} {sport} venue location home away schedule rest days weather", "basic", 3)),
        threading.Thread(target=fetch, args=('lineup', web_search,
            f"{comp1} vs {comp2} {sport} starting lineup active roster available players confirmed tonight 2026", "basic", 3)),
    ]
    if is_tennis:
        threads += [
            threading.Thread(target=fetch, args=('utr1', fetch_utr, comp1)),
            threading.Thread(target=fetch, args=('utr2', fetch_utr, comp2)),
        ]
    if is_team_sport:
        threads += [
            threading.Thread(target=fetch, args=('espn1', fetch_espn_team_stats, sport, comp1)),
            threading.Thread(target=fetch, args=('espn2', fetch_espn_team_stats, sport, comp2)),
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
        n      = odds.get('book_count', 1)
        spread = odds.get('spread', 0)
        a_low  = odds.get('a_low', odds['a_pct'])
        a_high = odds.get('a_high', odds['a_pct'])
        tight  = odds.get('tight', True)
        if tight:
            consensus_label = f'TIGHT consensus ({spread}pp spread) — books strongly agree, weight this heavily'
        else:
            consensus_label = f'WIDE spread ({spread}pp) — books disagree; may signal sharp vs. public split or genuine uncertainty. Weight with caution.'
        vegas_block = (
            f'\nVEGAS MARKET ODDS — {n} bookmaker{"s" if n != 1 else ""} (vig-removed, use as Bayesian prior in Step 4):\n'
            f'  {comp1}: {odds["a_pct"]}% consensus (range: {a_low}%–{a_high}%)\n'
            f'  {comp2}: {odds["b_pct"]}% consensus\n'
            f'  {consensus_label}\n'
        )

    # ESPN structured stats block
    espn_block = ''
    e1, e2 = research.get('espn1'), research.get('espn2')
    if e1 or e2:
        espn_block = '\nESPN LIVE TEAM RECORDS (structured — use as primary record data):\n'
        for label, e, name in [(comp1, e1, comp1), (comp2, e2, comp2)]:
            if e:
                espn_block += (
                    f'  {e["team"]}: {e["wins"]}W-{e["losses"]}L ({e["pct"]} pct) | '
                    f'Home: {e["home"]} | Away: {e["away"]} | Streak: {e["streak"]}\n'
                )

    search_block = utr_block + espn_block  # structured data first
    has_research = any(research.get(k) for k in ('comp1','comp2','h2h','news','recent_h2h','conditions','lineup'))
    if has_research:
        search_block += '\n\nSUPPLEMENTAL RESEARCH (use to verify/update training knowledge):\n'
        if research.get('news'):       search_block += f'\n[BREAKING NEWS / INJURIES]\n{research["news"]}\n'
        if research.get('lineup'):     search_block += f'\n[STARTING LINEUP / ROSTER AVAILABILITY — check for absences]\n{research["lineup"]}\n'
        if research.get('conditions'): search_block += f'\n[CONDITIONS: VENUE / REST / SCHEDULE / WEATHER]\n{research["conditions"]}\n'
        if research.get('comp1'):      search_block += f'\n[RESEARCH: {comp1} — KPI-targeted]\n{research["comp1"]}\n'
        if research.get('comp2'):      search_block += f'\n[RESEARCH: {comp2} — KPI-targeted]\n{research["comp2"]}\n'
        if research.get('h2h'):        search_block += f'\n[HEAD-TO-HEAD (career)]\n{research["h2h"]}\n'
        if research.get('recent_h2h'): search_block += f'\n[HEAD-TO-HEAD (recent — 2025/2026)]\n{research["recent_h2h"]}\n'

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
        # Collect vig-removed probabilities per bookmaker (not just raw prices)
        a_book_pcts, b_book_pcts = [], []
        for bk in ev.get('bookmakers', [])[:8]:
            a_pr, b_pr = None, None
            for mkt in bk.get('markets', []):
                if mkt.get('key') != 'h2h':
                    continue
                for outcome in mkt.get('outcomes', []):
                    nm = outcome.get('name', '')
                    pr = outcome.get('price', 0)
                    if name_match(nm, comp1):
                        a_pr = pr
                    elif name_match(nm, comp2):
                        b_pr = pr
            if a_pr is not None and b_pr is not None:
                ap = american_to_pct(a_pr)
                bp = american_to_pct(b_pr)
                tot = ap + bp
                if tot > 0:
                    a_book_pcts.append(round(ap / tot * 100, 1))
        if a_book_pcts:
            a_pct = round(sum(a_book_pcts) / len(a_book_pcts), 1)
            b_pct = round(100 - a_pct, 1)
            n     = len(a_book_pcts)
            a_low  = min(a_book_pcts)
            a_high = max(a_book_pcts)
            spread = round(a_high - a_low, 1)
            return {
                'a_pct': a_pct, 'b_pct': b_pct,
                'a_low': a_low, 'a_high': a_high,
                'book_count': n, 'spread': spread,
                'tight': spread <= 4.0,
                'found': True,
            }
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


@app.route('/verify', methods=['POST'])
def verify_analysis():
    """Red-team challenger: argues for the predicted loser, returns an adjustment."""
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.get_json() or {}
    sport  = data.get('sport', '')
    comp1  = data.get('comp1', '')
    comp2  = data.get('comp2', '')
    a_pct  = int(data.get('a_pct', 50))
    b_pct  = int(data.get('b_pct', 50))
    winner = data.get('winner', comp1)
    conf   = data.get('confidence', 'Medium')

    # Determine who is the predicted loser
    first_word_a = comp1.split()[0].lower() if comp1 else ''
    winner_is_a  = winner.lower().startswith(first_word_a) or first_word_a in winner.lower()
    loser        = comp2 if winner_is_a else comp1
    gap          = abs(a_pct - b_pct)

    challenge = (
        f"You are a contrarian analyst stress-testing a sports/competition prediction.\n\n"
        f"MATCHUP: {comp1} vs {comp2} ({sport})\n"
        f"CURRENT PREDICTION: {comp1} {a_pct}% | {comp2} {b_pct}%\n"
        f"PREDICTED WINNER: {winner} ({conf} confidence, {gap}pp gap)\n\n"
        f"The analysis already favored {winner}. Your sole job: find what it may have gotten WRONG "
        f"or systematically UNDERWEIGHTED when evaluating {loser}.\n\n"
        f"Challenge angles:\n"
        f"1. Is there a stylistic edge, historical pattern, or situational factor that actually favors {loser}?\n"
        f"2. Did the analysis overweight one signal (ranking, recent form, home advantage) "
        f"while missing a countervailing factor?\n"
        f"3. Is the {gap}pp gap wider than the evidence actually supports — "
        f"is this really a coin-flip that looks decided?\n"
        f"4. Are there uncertainty factors (key injury risk, variance, format, motivation) "
        f"that compress the true probability gap?\n\n"
        f"Output exactly two lines:\n"
        f"ADJ: [integer 0–20: percentage points {loser} deserves to gain. 0 = analysis was sound]\n"
        f"NOTE: [one sentence: the specific overlooked factor, or 'Analysis appears well-calibrated.' if ADJ is 0]"
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=120,
            messages=[{'role': 'user', 'content': challenge}]
        )
        raw        = msg.content[0].text.strip()
        adj_m      = re.search(r'^ADJ:\s*(\d+)', raw, re.MULTILINE)
        note_m     = re.search(r'^NOTE:\s*(.+)$', raw, re.MULTILINE)
        adj        = min(int(adj_m.group(1)), 20) if adj_m else 0
        note       = note_m.group(1).strip() if note_m else ''
        # Only apply half the challenger's adjustment to avoid overcorrection
        real_adj   = round(adj * 0.4) if adj >= 5 else 0
        if winner_is_a:
            new_a = max(5, a_pct - real_adj)
            new_b = 100 - new_a
        else:
            new_b = max(5, b_pct - real_adj)
            new_a = 100 - new_b
        return jsonify({
            'adj': adj, 'real_adj': real_adj,
            'new_a_pct': new_a, 'new_b_pct': new_b,
            'note': note,
            'verified': adj < 5,
        })
    except Exception as e:
        return jsonify({'error': str(e), 'verified': True, 'adj': 0, 'real_adj': 0,
                        'new_a_pct': a_pct, 'new_b_pct': b_pct})


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

@app.route('/api/calibration')
def api_calibration():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    settled = Query.query.filter(Query.outcome != 'pending').all()
    buckets = {
        'High':   {'wins': 0, 'total': 0},
        'Medium': {'wins': 0, 'total': 0},
        'Low':    {'wins': 0, 'total': 0},
    }
    for q in settled:
        conf = (q.confidence or 'Medium').strip().capitalize()
        if conf not in buckets:
            conf = 'Medium'
        buckets[conf]['total'] += 1
        if q.outcome == 'win':
            buckets[conf]['wins'] += 1

    result = {}
    for conf, d in buckets.items():
        result[conf.lower()] = {
            'total': d['total'],
            'wins':  d['wins'],
            'rate':  round(d['wins'] / d['total'] * 100) if d['total'] >= 3 else None,
        }
    # Well-calibrated targets: High~72%, Medium~60%, Low~48%
    result['targets'] = {'high': 72, 'medium': 60, 'low': 48}
    return jsonify(result)

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

def generate_weekly_recap_card(uid):
    week_ago = datetime.utcnow() - timedelta(days=7)
    picks = Query.query.filter(
        Query.user_uid == uid,
        Query.created_at >= week_ago,
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == False  # noqa: E712
    ).order_by(Query.created_at.asc()).all()
    wins   = sum(1 for p in picks if p.outcome == 'win')
    losses = len(picks) - wins
    rate   = round(wins / len(picks) * 100) if picks else 0
    best   = next((p for p in reversed(picks) if p.outcome == 'win' and p.confidence == 'High'),
                   next((p for p in reversed(picks) if p.outcome == 'win'), None))
    streak = _calc_streak(uid)

    W, H    = 1200, 630
    BG      = (20, 18, 15);  SURFACE = (31, 28, 23);  BORDER = (58, 53, 42)
    BLUE    = (126, 184, 212); TEXT   = (237, 232, 224); MUTED  = (140, 132, 121)
    GREEN   = (107, 178, 130); RED    = (210, 100, 90);  GOLD   = (212, 180, 100)

    img  = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([40, 40, W-40, H-40], radius=24, fill=SURFACE, outline=BORDER, width=2)

    draw.text((80, 68), "WhoWins · Weekly Recap", font=_pil_font(26, bold=False), fill=MUTED)

    rec_font = _pil_font(130)
    rec_text = f"{wins}–{losses}"
    rb = draw.textbbox((0,0), rec_text, font=rec_font)
    draw.text((W//2 - (rb[2]-rb[0])//2, 110), rec_text, font=rec_font, fill=TEXT)

    rate_text  = f"{rate}% hit rate this week"
    rate_color = GREEN if rate >= 60 else (GOLD if rate >= 50 else RED)
    rt = _pil_font(40)
    rb2 = draw.textbbox((0,0), rate_text, font=rt)
    draw.text((W//2 - (rb2[2]-rb2[0])//2, 305), rate_text, font=rt, fill=rate_color)

    if best:
        draw.text((80, 390), "BEST CALL", font=_pil_font(20, bold=False), fill=MUTED)
        loser = (best.competitor_b if best.competitor_a and best.winner and best.winner.lower() in best.competitor_a.lower() else best.competitor_a) or ''
        best_text = f"✓ {best.winner}  over  {loser}".strip()
        if draw.textbbox((0,0), best_text, font=_pil_font(34))[2] > W-160:
            best_text = f"✓ {best.winner}"
        draw.text((80, 420), best_text, font=_pil_font(34), fill=GREEN)
        if best.sport:
            st = best.sport.upper()
            sb = draw.textbbox((0,0), st, font=_pil_font(20))
            draw.rounded_rectangle([W-180, 390, W-60, 470], radius=18, fill=BORDER)
            draw.text((W-120 - (sb[2]-sb[0])//2, 420), st, font=_pil_font(20), fill=MUTED)

    if streak >= 3:
        draw.text((80, 505), f"🔥 On a {streak}-pick win streak", font=_pil_font(26), fill=GOLD)

    url_text = "whowins.onrender.com"
    ub = draw.textbbox((0,0), url_text, font=_pil_font(22, bold=False))
    draw.text((W//2 - (ub[2]-ub[0])//2, 576), url_text, font=_pil_font(22, bold=False), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf


def generate_streak_card(streak, recent_picks):
    W, H    = 1200, 630
    BG      = (20, 18, 15);  SURFACE = (31, 28, 23)
    TEXT    = (237, 232, 224); MUTED  = (140, 132, 121)
    GREEN   = (107, 178, 130); GOLD   = (212, 180, 100)

    img  = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([40, 40, W-40, H-40], radius=24, fill=SURFACE, outline=GOLD, width=3)

    fires = '🔥' * min(streak, 8)
    draw.text((80, 68), fires, font=_pil_font(40), fill=GOLD)

    num_font = _pil_font(160)
    nb = draw.textbbox((0,0), str(streak), font=num_font)
    draw.text((W//2 - (nb[2]-nb[0])//2, 95), str(streak), font=num_font, fill=GOLD)

    lbl = f"pick win streak with Scout"
    lb  = draw.textbbox((0,0), lbl, font=_pil_font(42))
    draw.text((W//2 - (lb[2]-lb[0])//2, 315), lbl, font=_pil_font(42), fill=TEXT)

    pf = _pil_font(26, bold=False)
    y  = 394
    for p in recent_picks[:4]:
        txt = f"✓  {p.winner}  ·  {p.sport or ''}"
        draw.text((W//2 - 220, y), txt, font=pf, fill=GREEN)
        y += 42

    cta = "who wins next?  whowins.onrender.com"
    cb  = draw.textbbox((0,0), cta, font=_pil_font(24, bold=False))
    draw.text((W//2 - (cb[2]-cb[0])//2, 570), cta, font=_pil_font(24, bold=False), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf


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

# ── Plays of the Day / Week ───────────────────────────────────────────────────

_plays_cache = {'plays': [], 'ts': 0}
_PLAYS_TTL   = 3600 * 6   # regenerate every 6 hours

PLAYS_BATCH_PROMPT = """You are an elite prediction market analyst.

TODAY'S DATE: {today}

CRITICAL — LIVE CONTEXT OVERRIDES TRAINING DATA:
Your training data has a knowledge cutoff and may be months or years out of date. Each market below includes LIVE CONTEXT fetched minutes ago. You MUST use the live context to determine current event state.

Examples of how training data goes wrong:
- Training may think a tournament has many rounds left; live context shows it is in the final
- Training may think a candidate leads in polls; live context shows they dropped out
- Training may think a team's star player is healthy; live context shows they are injured

Always re-anchor to the live context. If the live context reveals the market question is already effectively settled (e.g., only 2 teams left in a 48-team tournament and the market asks if one of them wins), adjust your probability accordingly — do NOT use the old training-data frame.

Analyze each market below. For each, determine the TRUE current probability of the YES outcome using the live context provided, then compare to the market price to identify mispricing.

{markets_block}

For each numbered market, output EXACTLY one line:
N|PCT:number|CONF:High/Medium/Low|TIP:one sentence

Rules:
- PCT = your best estimate of the true YES probability as a whole number (1 to 99)
- CONF = High if live context is clear and decisive; Medium if partial; Low if live context is absent or ambiguous
- TIP = one sentence explaining what the live context shows vs. what the market price implies
- If live context is missing, output: N|PCT:market_price|CONF:Low|TIP:No live data available — market price used as-is
- Output ONLY the numbered pipe-delimited lines — no headers, no extra text, never refuse a line"""


def _fetch_plays_markets():
    """Pull top binary markets from Polymarket + Kalshi for plays analysis."""
    candidates = []

    # Polymarket — active binary markets in the interesting price range
    for mkt in (_get_all_poly_markets() or []):
        q = mkt.get('question', '')
        if not q:
            continue
        prices = _parse_poly_prices(mkt)
        if len(prices) != 2:
            continue
        yes_pct = prices[0][1]
        if not (18 <= yes_pct <= 82):   # skip near-certain outcomes
            continue
        vol = float(mkt.get('volume24hr') or 0)
        if vol < 300:
            continue
        candidates.append({
            'source':      'Polymarket',
            'question':    q,
            'yes_pct':     yes_pct,
            'no_pct':      round(100 - yes_pct, 1),
            'yes_outcome': prices[0][0],
            'volume24h':   vol,
            'url':         f"https://polymarket.com/event/{mkt.get('slug', '')}",
        })

    # Kalshi — top series active markets
    base = 'https://api.elections.kalshi.com/trade-api/v2'
    for series in ['KXWNBAGAME', 'KXWNFLGAME', 'KXMLBF3', 'KXATPMATCH',
                   'KXWCGAME', 'KXMLSGAME', 'KXNASCARRACE', 'KXF1RACEPODIUM']:
        data = _json_get(f"{base}/markets?series_ticker={series}&limit=20&status=open")
        for mkt in data.get('markets', []):
            title = mkt.get('title', '')
            if not title:
                continue
            yes_price = float(mkt.get('last_price_dollars') or
                              mkt.get('yes_bid_dollars') or 0) * 100
            if not (18 <= yes_price <= 82):
                continue
            vol = float(mkt.get('volume_24h_fp') or 0) / 100
            if vol < 50:
                continue
            ticker = mkt.get('ticker', '')
            candidates.append({
                'source':      'Kalshi',
                'question':    title,
                'yes_pct':     round(yes_price, 1),
                'no_pct':      round(100 - yes_price, 1),
                'yes_outcome': 'Yes',
                'volume24h':   vol,
                'url':         f"https://kalshi.com/markets/{series.lower()}/{ticker}",
            })

    # Deduplicate, sort by volume, take top 15
    seen, out = set(), []
    for c in sorted(candidates, key=lambda x: -x['volume24h']):
        if c['question'] not in seen:
            seen.add(c['question'])
            out.append(c)
        if len(out) >= 15:
            break
    return out


def _generate_plays():
    """Run a single batch AI call across candidate markets; return ranked plays."""
    markets = _fetch_plays_markets()
    if not markets or not ANTHROPIC_API_KEY:
        return []

    # Research each market in parallel to get live current-state context
    def _research_market(mkt):
        q = mkt['question']
        result = web_search(
            f"{q} current status result latest news today 2026", depth="basic", max_results=3
        )
        mkt['_research'] = (result or '').strip()[:600]

    threads = [threading.Thread(target=_research_market, args=(m,)) for m in markets]
    for t in threads: t.start()
    for t in threads: t.join(timeout=12)

    today = datetime.utcnow().strftime('%B %d, %Y')
    lines = []
    for i, m in enumerate(markets, 1):
        entry = (
            f"MARKET {i}:\n"
            f"  Question: {m['question']}\n"
            f"  Current market price: YES {m['yes_pct']}% / NO {m['no_pct']}%\n"
            f"  Source: {m['source']}"
        )
        if m.get('_research'):
            entry += f"\n  LIVE CONTEXT (as of {today}):\n  {m['_research']}"
        else:
            entry += f"\n  LIVE CONTEXT: [no live data retrieved — rely only on market price]"
        lines.append(entry)
    markets_block = '\n\n'.join(lines)
    prompt = PLAYS_BATCH_PROMPT.format(markets_block=markets_block, today=today)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
    except Exception:
        return []

    plays = []
    for line in raw.split('\n'):
        line = line.strip()
        if not line:
            continue
        m_match = re.match(
            r'^(\d+)\s*\|?\s*PCT:(\d+)\s*\|?\s*CONF:(High|Medium|Low)\s*\|?\s*TIP:(.+)$',
            line, re.IGNORECASE
        )
        if not m_match:
            continue
        idx     = int(m_match.group(1)) - 1
        ai_pct  = int(m_match.group(2))
        conf    = m_match.group(3).strip().capitalize()
        tip     = m_match.group(4).strip()

        if idx < 0 or idx >= len(markets):
            continue

        mkt        = markets[idx]
        mkt_pct    = mkt['yes_pct']
        edge       = round(ai_pct - mkt_pct, 1)
        abs_edge   = abs(edge)

        if abs_edge < 6:     # skip near-consensus markets
            continue
        if conf == 'Low':    # skip low-confidence plays
            continue

        # Determine the recommended pick side
        if edge > 0:
            pick      = 'YES'
            pick_pct  = ai_pct
        else:
            pick      = 'NO'
            pick_pct  = round(100 - ai_pct, 1)

        plays.append({
            **mkt,
            'ai_pct':    ai_pct,
            'edge':      edge,
            'abs_edge':  abs_edge,
            'pick':      pick,
            'pick_pct':  pick_pct,
            'confidence': conf,
            'tip':        tip,
        })

    plays.sort(key=lambda x: -x['abs_edge'])
    return plays


def get_cached_plays(force=False):
    if not force and time.time() - _plays_cache['ts'] < _PLAYS_TTL and _plays_cache['plays']:
        return _plays_cache['plays']
    try:
        plays = _generate_plays()
        if plays:
            _plays_cache['plays'] = plays
            _plays_cache['ts']    = time.time()
        return plays or _plays_cache.get('plays', [])
    except Exception:
        return _plays_cache.get('plays', [])


@app.route('/plays')
def plays():
    if not is_authed():
        return redirect(url_for('login'))
    return render_template('plays.html')


@app.route('/api/plays')
def api_plays():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    all_plays  = get_cached_plays()
    ts         = _plays_cache.get('ts', 0)
    age_min    = round((time.time() - ts) / 60) if ts else None
    return jsonify({
        'day':       all_plays[:3],
        'week':      all_plays[3:8],
        'age_min':   age_min,
        'total':     len(all_plays),
    })


@app.route('/api/plays/refresh')
def api_plays_refresh():
    if not _safe_eq(request.args.get('key', ''), ADMIN_KEY):
        return 'Unauthorized.', 403
    threading.Thread(target=get_cached_plays, kwargs={'force': True}, daemon=True).start()
    return jsonify({'ok': True, 'msg': 'Refresh triggered in background'})


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

# ── Public record page ────────────────────────────────────────────────────────

@app.route('/record')
def public_record():
    settled = Query.query.filter(
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == False  # noqa: E712
    ).all()
    total = len(settled)
    wins  = sum(1 for q in settled if q.outcome == 'win')
    rate  = round(wins / total * 100) if total else 0

    sport_map = {}
    for q in settled:
        s = (q.sport or 'other').split()[0].lower()
        sport_map.setdefault(s, {'wins': 0, 'total': 0})
        sport_map[s]['total'] += 1
        if q.outcome == 'win': sport_map[s]['wins'] += 1
    for s, st in sport_map.items():
        st['rate'] = round(st['wins'] / st['total'] * 100) if st['total'] else 0
    sport_list = sorted(sport_map.items(), key=lambda x: -x[1]['total'])[:8]

    best_picks = Query.query.filter_by(outcome='win', confidence='High')\
        .order_by(Query.created_at.desc()).limit(6).all()
    recent_activity = Query.query.filter(Query.outcome.in_(['win', 'loss']))\
        .order_by(Query.created_at.desc()).limit(12).all()

    odds_picks = Query.query.filter(
        Query.outcome.in_(['win', 'loss']),
        Query.a_odds_pct.isnot(None),
        Query.is_fade == False  # noqa: E712
    ).all()
    vegas_wins = sum(1 for q in odds_picks
        if (q.winner and q.competitor_a and q.winner.lower() in q.competitor_a.lower()
            and q.a_odds_pct and q.a_odds_pct >= 50 and q.outcome == 'win')
        or (q.winner and q.competitor_a and q.winner.lower() not in q.competitor_a.lower()
            and q.b_odds_pct and q.b_odds_pct >= 50 and q.outcome == 'win'))
    vegas_rate = round(vegas_wins / len(odds_picks) * 100) if odds_picks else None

    vp = [q for q in odds_picks if _value_edge(q) >= 10]
    vw = sum(1 for q in vp if q.outcome == 'win')
    value_rate = round(vw / len(vp) * 100) if vp else None

    return render_template('record.html',
        total=total, wins=wins, rate=rate,
        sport_list=sport_list, best_picks=best_picks,
        recent_activity=recent_activity,
        vegas_rate=vegas_rate, odds_count=len(odds_picks),
        value_rate=value_rate, value_count=len(vp),
    )


# ── Waitlist ──────────────────────────────────────────────────────────────────

@app.route('/wait', methods=['GET', 'POST'])
def waitlist_page():
    submitted = False
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if email and '@' in email and '.' in email.split('@')[-1]:
            if not Waitlist.query.filter_by(email=email).first():
                db.session.add(Waitlist(email=email, source=request.args.get('ref')))
                db.session.commit()
            submitted = True
    count = Waitlist.query.count()
    return render_template('wait.html', submitted=submitted, count=count)

@app.route('/admin/waitlist')
def admin_waitlist():
    if not _safe_eq(request.args.get('key', ''), ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    entries = Waitlist.query.order_by(Waitlist.created_at.desc()).all()
    return jsonify([{'email': e.email, 'source': e.source,
                     'created_at': e.created_at.isoformat()} for e in entries])


# ── Public profile ────────────────────────────────────────────────────────────

@app.route('/u/<handle>')
def public_profile(handle):
    profile = UserProfile.query.filter(
        func.lower(UserProfile.handle) == handle.lower()
    ).first_or_404()
    uid = profile.user_uid
    wins, total, rate = _user_stats(uid)
    rank   = _get_rank(wins, rate, total)
    streak = _calc_streak(uid)
    recent = Query.query.filter(
        Query.user_uid == uid,
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == False  # noqa: E712
    ).order_by(Query.created_at.desc()).limit(8).all()
    best = Query.query.filter_by(user_uid=uid, outcome='win', confidence='High')\
        .order_by(Query.created_at.desc()).limit(3).all()
    crew_count = UserProfile.query.filter_by(referred_by=uid).count()
    is_me = is_authed() and session.get('user_uid') == uid
    return render_template('profile.html',
        handle=handle, profile=profile,
        wins=wins, total=total, rate=rate,
        rank=rank, streak=streak, recent=recent, best=best,
        crew_count=crew_count, is_me=is_me,
    )

@app.route('/api/set-handle', methods=['POST'])
def set_handle():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    handle = (request.get_json() or {}).get('handle', '').strip().lower()
    if not handle or len(handle) < 3 or len(handle) > 30:
        return jsonify({'error': 'Handle must be 3–30 characters'}), 400
    if not re.match(r'^[a-z0-9_]+$', handle):
        return jsonify({'error': 'Only letters, numbers, and underscores'}), 400
    uid = get_user_uid()
    clash = UserProfile.query.filter(
        func.lower(UserProfile.handle) == handle,
        UserProfile.user_uid != uid
    ).first()
    if clash:
        return jsonify({'error': 'Handle already taken'}), 409
    p = UserProfile.query.filter_by(user_uid=uid).first()
    if p:
        p.handle = handle
        db.session.commit()
    profile_url = url_for('public_profile', handle=handle, _external=True)
    return jsonify({'success': True, 'handle': handle, 'url': profile_url})


# ── Image endpoints ───────────────────────────────────────────────────────────

@app.route('/api/weekly-recap-image')
def weekly_recap_image():
    if not is_authed():
        return 'Unauthorized', 401
    buf = generate_weekly_recap_card(get_user_uid())
    return send_file(buf, mimetype='image/png',
                     download_name='whowins-weekly-recap.png', as_attachment=True)

@app.route('/api/streak-image')
def streak_image_route():
    if not is_authed():
        return 'Unauthorized', 401
    uid    = get_user_uid()
    streak = _calc_streak(uid)
    if streak < 1:
        return jsonify({'error': 'No active win streak'}), 400
    recent = Query.query.filter(
        Query.user_uid == uid, Query.outcome == 'win',
        Query.is_fade == False  # noqa: E712
    ).order_by(Query.created_at.desc()).limit(streak).all()
    buf = generate_streak_card(streak, recent)
    return send_file(buf, mimetype='image/png',
                     download_name=f'whowins-streak-{streak}.png', as_attachment=True)


# ── My stats / invite / crew ──────────────────────────────────────────────────

@app.route('/api/my-stats')
def my_stats():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    uid   = get_user_uid()
    wins, total, rate = _user_stats(uid)
    streak = _calc_streak(uid)
    rank   = _get_rank(wins, rate, total)
    crew   = UserProfile.query.filter_by(referred_by=uid).count()
    p      = UserProfile.query.filter_by(user_uid=uid).first()
    invite = url_for('login', ref=uid, _external=True)
    profile_url = url_for('public_profile', handle=p.handle, _external=True) if p and p.handle else None
    vp = Query.query.filter(
        Query.user_uid == uid, Query.outcome.in_(['win','loss']),
        Query.a_odds_pct.isnot(None), Query.is_fade == False  # noqa: E712
    ).all()
    vp_edge = [q for q in vp if _value_edge(q) >= 10]
    vw = sum(1 for q in vp_edge if q.outcome == 'win')
    return jsonify({
        'wins': wins, 'total': total, 'rate': rate,
        'streak': streak, 'rank': rank, 'crew': crew,
        'invite_url': invite, 'profile_url': profile_url,
        'handle': p.handle if p else None,
        'value_rate': round(vw / len(vp_edge) * 100) if vp_edge else None,
        'value_count': len(vp_edge),
    })


# ── Fade Scout ────────────────────────────────────────────────────────────────

@app.route('/api/fade', methods=['POST'])
def fade_pick():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    d   = request.get_json() or {}
    uid = get_user_uid()
    entry = Query(
        user_uid     = uid,
        sport        = d.get('sport', ''),
        competitor_a = d.get('comp1', ''),
        competitor_b = d.get('comp2', ''),
        winner       = d.get('fade_winner', ''),
        confidence   = d.get('confidence', 'Medium'),
        analysis     = '',
        ai_a_pct     = d.get('b_pct'),
        ai_b_pct     = d.get('a_pct'),
        a_odds_pct   = d.get('a_odds_pct'),
        b_odds_pct   = d.get('b_odds_pct'),
        is_fade      = True,
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'success': True, 'id': entry.id})

@app.route('/api/fade-stats')
def fade_stats():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    uid  = get_user_uid()
    fades = Query.query.filter(
        Query.user_uid == uid,
        Query.outcome.in_(['win', 'loss']),
        Query.is_fade == True  # noqa: E712
    ).all()
    total = len(fades)
    wins  = sum(1 for f in fades if f.outcome == 'win')
    return jsonify({'total': total, 'wins': wins,
                    'rate': round(wins / total * 100) if total else None})


# ── Squad ─────────────────────────────────────────────────────────────────────

@app.route('/squad/create', methods=['POST'])
def squad_create():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    name = (request.get_json() or {}).get('name', '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': 'Name must be 1–50 characters'}), 400
    uid   = get_user_uid()
    code  = secrets.token_urlsafe(6)
    squad = Squad(name=name, invite_code=code, created_by=uid)
    db.session.add(squad)
    db.session.flush()
    db.session.add(SquadMember(squad_id=squad.id, user_uid=uid))
    db.session.commit()
    return jsonify({'success': True, 'squad_id': squad.id, 'invite_code': code,
                    'url': url_for('squad_page', squad_id=squad.id, _external=True)})

@app.route('/squad/join/<code>')
def squad_join(code):
    if not is_authed():
        session['after_login'] = request.url
        return redirect(url_for('login'))
    uid   = get_user_uid()
    squad = Squad.query.filter_by(invite_code=code).first_or_404()
    if not SquadMember.query.filter_by(squad_id=squad.id, user_uid=uid).first():
        db.session.add(SquadMember(squad_id=squad.id, user_uid=uid))
        db.session.commit()
    return redirect(url_for('squad_page', squad_id=squad.id))

@app.route('/squad/<int:squad_id>')
def squad_page(squad_id):
    if not is_authed():
        return redirect(url_for('login'))
    squad  = Squad.query.get_or_404(squad_id)
    uid    = get_user_uid()
    is_member  = SquadMember.query.filter_by(squad_id=squad_id, user_uid=uid).first() is not None
    members    = SquadMember.query.filter_by(squad_id=squad_id).all()
    member_stats = []
    for m in members:
        p = UserProfile.query.filter_by(user_uid=m.user_uid).first()
        w, t, r = _user_stats(m.user_uid)
        member_stats.append({
            'handle': (p.handle if p and p.handle else get_display_name(m.user_uid)),
            'wins': w, 'total': t, 'rate': r,
            'streak': _calc_streak(m.user_uid),
            'rank': _get_rank(w, r, t),
            'is_me': m.user_uid == uid,
        })
    member_stats.sort(key=lambda x: (-x['rate'], -x['wins']))
    join_url = url_for('squad_join', code=squad.invite_code, _external=True)
    return render_template('squad.html',
        squad=squad, is_member=is_member,
        member_stats=member_stats, join_url=join_url,
        is_creator=(squad.created_by == uid),
    )

@app.route('/api/my-squads')
def my_squads():
    if not is_authed():
        return jsonify({'error': 'Unauthorized'}), 401
    uid       = get_user_uid()
    membships = SquadMember.query.filter_by(user_uid=uid).all()
    result    = []
    for ms in membships:
        sq = Squad.query.get(ms.squad_id)
        if sq:
            result.append({'id': sq.id, 'name': sq.name,
                           'member_count': SquadMember.query.filter_by(squad_id=sq.id).count()})
    return jsonify(result)


if __name__ == '__main__':
    app.run(debug=True)
