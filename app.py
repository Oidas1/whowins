import os
from flask import Flask, render_template, request, Response, stream_with_context
import anthropic

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

ANALYSIS_PROMPT = """You are a sharp, straight-talking sports analyst. You've studied this sport deeply and you don't hedge — you give a real opinion backed by evidence.

Analyze this matchup:

Sport: {sport}
Competitor A: {comp1}
Competitor B: {comp2}
{context_block}

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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    sport  = request.form.get('sport', '').strip()
    comp1  = request.form.get('comp1', '').strip()
    comp2  = request.form.get('comp2', '').strip()
    context = request.form.get('context', '').strip()

    if not all([sport, comp1, comp2]):
        return Response("Missing fields.", status=400)

    if not ANTHROPIC_API_KEY:
        return Response("ANTHROPIC_API_KEY not set.", status=500)

    prompt = build_prompt(sport, comp1, comp2, context)

    def generate():
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        with client.messages.stream(
            model="claude-opus-4-8",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                yield text

    return Response(stream_with_context(generate()), mimetype='text/plain')


if __name__ == '__main__':
    app.run(debug=True)
