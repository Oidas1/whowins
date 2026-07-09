import os
import re
import uuid
import traceback
import secrets
import threading
from datetime import datetime
from flask import Flask, render_template, request, Response, stream_with_context, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
import anthropic
import urllib.request
import json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

db_url = os.environ.get('DATABASE_URL', 'sqlite:///whowins.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', 'adminkey123')
RENDER_API_KEY    = os.environ.get('RENDER_API_KEY', '')
RENDER_SERVICE_ID = os.environ.get('RENDER_SERVICE_ID', '')

_password_store = {'value': os.environ.get('SITE_PASSWORD', 'whowins2026')}

# ── Models ────────────────────────────────────────────────────────────────────

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
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Warning: could not create tables on startup: {e}")

# ── Password helpers ──────────────────────────────────────────────────────────

def get_password():
    return _password_store['value']

def rotate_password():
    new_pw = secrets.token_urlsafe(8)
    _password_store['value'] = new_pw
    threading.Thread(target=_sync_to_render, args=(new_pw,), daemon=True).start()
    return new_pw

def _sync_to_render(new_pw):
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return
    try:
        payload = json.dumps([
            {"key": "ANTHROPIC_API_KEY",  "value": os.environ.get('ANTHROPIC_API_KEY', '')},
            {"key": "PYTHON_VERSION",     "value": "3.11.6"},
            {"key": "SECRET_KEY",         "value": os.environ.get('SECRET_KEY', '')},
            {"key": "SITE_PASSWORD",      "value": new_pw},
            {"key": "ADMIN_KEY",          "value": os.environ.get('ADMIN_KEY', '')},
            {"key": "RENDER_API_KEY",     "value": RENDER_API_KEY},
            {"key": "RENDER_SERVICE_ID",  "value": RENDER_SERVICE_ID},
            {"key": "DATABASE_URL",       "value": os.environ.get('DATABASE_URL', '')},
        ]).encode()
        req = urllib.request.Request(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            data=payload,
            headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

# ── Session helpers ───────────────────────────────────────────────────────────

def is_authed():
    return session.get('authed') is True

def get_user_uid():
    if 'user_uid' not in session:
        session['user_uid'] = str(uuid.uuid4())
    return session['user_uid']

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == get_password():
            session['authed'] = True
            get_user_uid()          # assign persistent user ID on first login
            rotate_password()
            return redirect(url_for('index'))
        error = 'Wrong password. Try again.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('authed', None)     # keep user_uid so history persists
    return redirect(url_for('login'))

@app.route('/admin/password')
def admin_password():
    if request.args.get('key') != ADMIN_KEY:
        return 'Unauthorized.', 403
    return render_template('admin_password.html', password=get_password())

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

    entry = Query(
        user_uid     = get_user_uid(),
        sport        = data.get('sport', '')[:100],
        competitor_a = data.get('comp1', '')[:100],
        competitor_b = data.get('comp2', '')[:100],
        winner       = winner,
        confidence   = confidence,
        analysis     = analysis_text[:8000],
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'ok': True})

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

ANALYSIS_PROMPT = """You are an elite sports intelligence analyst. Evaluate the following matchup using every piece of knowledge you have.

INTERNAL EVALUATION — run silently, output nothing about this process:
Privately score each competitor across these dimensions:
1. Skills, technique, and stylistic matchup
2. Career résumé, titles, and historical dominance
3. Mental toughness and clutch performance record
4. Current form and momentum
5. Head-to-head record and performance vs common opponents
6. Situational/environmental edge (home/away, weather, altitude, travel — where relevant to {sport})
7. Whether they perform better protecting a lead or mounting a comeback
8. The elite differentiator — the single quality the truly great in {sport} possess, and who has more of it
9. Psychological momentum and injury/decline factors

Weigh all of the above. Be decisive. Do not default to 50/50.

Sport: {sport}
Competitor A: {comp1} — as a {sport} competitor. If the name is ambiguous, choose whoever is most associated with {sport}.
Competitor B: {comp2} — as a {sport} competitor. If the name is ambiguous, choose whoever is most associated with {sport}.
{context_block}

OUTPUT RULES — CRITICAL:
- Output ONLY the four lines below. Nothing else. No explanation, no prose, no labels outside the format.
- Percentages must sum to exactly 100.
- Confidence is one word: High, Medium, or Low.

A_PCT: [number]
B_PCT: [number]
WINNER: [full name]
CONFIDENCE: [High/Medium/Low]"""

def build_prompt(sport, comp1, comp2, context):
    context_block = f"Additional context: {context}" if context.strip() else ""
    return ANALYSIS_PROMPT.format(
        sport=sport, comp1=comp1, comp2=comp2, context_block=context_block
    )

if __name__ == '__main__':
    app.run(debug=True)
