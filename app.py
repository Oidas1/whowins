import os
import traceback
from flask import Flask, render_template, request, Response, stream_with_context, session, redirect, url_for
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SITE_PASSWORD = os.environ.get('SITE_PASSWORD', 'whowins123')

def is_authed():
    return session.get('authed') is True

ANALYSIS_PROMPT = """You are a sharp, straight-talking sports analyst. You've studied this sport deeply and you don't hedge — you give a real opinion backed by evidence.

Analyze this matchup:

Sport: {sport}
Competitor A: {comp1} — specifically as a {sport} competitor, not any other athlete who may share a similar name
Competitor B: {comp2} — specifically as a {sport} competitor, not any other athlete who may share a similar name
{context_block}

IMPORTANT: Base your entire analysis on these two people strictly within the context of {sport}. If a name could refer to multiple people, always choose the one most relevant to {sport}.

Give a full breakdown in this exact structure:

## ⚔️ {comp1} vs {comp2}

### 🎯 Skills & Style
Compare their technical abilities, playing/fighting style, strengths and weaknesses head-to-head.

### 🏆 Accolades & Record
Career achievements, titles, championships, stats, records — who has the more impressive résumé?

### 🌱 Background & Upbringing
How did where they came from shape who they are as a competitor? What drove them to this level?

### 🧠 Mindset & Attitude
How do they handle pressure, adversity, big moments? Work ethic, hunger, competitive fire.

### 📊 Current Form & Momentum
Recent performances, trajectory — who's peaking right now?

### 🔮 The Pick
Who wins, why, and how this plays out. Be specific. End with exactly this line:
**WINNER: [name] | Confidence: [High / Medium / Low]**"""


def build_prompt(sport, comp1, comp2, context):
    context_block = f"Additional context: {context}" if context.strip() else ""
    return ANALYSIS_PROMPT.format(
        sport=sport, comp1=comp1, comp2=comp2, context_block=context_block
    )


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == SITE_PASSWORD:
            session['authed'] = True
            return redirect(url_for('index'))
        error = 'Wrong password. Try again.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not is_authed():
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    sport   = request.form.get('sport', '').strip()
    comp1   = request.form.get('comp1', '').strip()
    comp2   = request.form.get('comp2', '').strip()
    context = request.form.get('context', '').strip()

    if not is_authed():
        return Response("Unauthorized.", status=401)

    if not all([sport, comp1, comp2]):
        return Response("Missing fields.", status=400)

    if not ANTHROPIC_API_KEY:
        return Response("ERROR: ANTHROPIC_API_KEY is not set on this server.", status=500)

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
            yield "ERROR: Invalid Anthropic API key. Check your ANTHROPIC_API_KEY environment variable."
        except anthropic.PermissionDeniedError as e:
            yield f"ERROR: API permission denied — {str(e)}"
        except anthropic.RateLimitError:
            yield "ERROR: Rate limit hit. Try again in a moment."
        except Exception as e:
            yield f"ERROR: {str(e)}\n\n{traceback.format_exc()}"

    return Response(stream_with_context(generate()), mimetype='text/plain')


if __name__ == '__main__':
    app.run(debug=True)
