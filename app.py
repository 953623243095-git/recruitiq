from flask import Flask, request, jsonify, render_template
import pdfplumber
import docx
import json
import io
import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
load_dotenv()
print("API KEY LOADED:", os.environ.get("GROQ_API_KEY")[:10] if os.environ.get("GROQ_API_KEY") else "NOT FOUND")

app = Flask(__name__)

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ===== FILE PARSER =====
def extract_text(file):
    filename = file.filename.lower()
    content = file.read()

    if filename.endswith('.pdf'):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return ' '.join(page.extract_text() or '' for page in pdf.pages)

    elif filename.endswith('.docx'):
        doc = docx.Document(io.BytesIO(content))
        return ' '.join(p.text for p in doc.paragraphs)

    elif filename.endswith('.txt'):
        return content.decode('utf-8', errors='ignore')

    return ""

# ===== SCORER =====
def score_resume_text(resume_text, jd_text):
    prompt = f"""You are an expert recruiter with 10 years experience.
Score this resume against the job description on a scale of 0-100.
Return ONLY valid JSON — no extra text.

JOB DESCRIPTION:
{jd_text[:1000]}

RESUME:
{resume_text[:1000]}

Return exactly this JSON:
{{
  "score": <integer 0-100>,
  "reasoning": "<2 sentences explaining the score>",
  "key_matches": ["skill1", "skill2", "skill3"],
  "key_gaps": ["gap1", "gap2", "gap3"]
}}"""

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ===== ROUTES =====
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/score', methods=['POST'])
def score():
    try:
        jd = request.form.get('jd', '')
        resume_file = request.files.get('resume')

        if not jd or not resume_file:
            return jsonify({'error': 'Missing JD or resume file'})

        resume_text = extract_text(resume_file)
        if not resume_text.strip():
            return jsonify({'error': 'Could not extract text from file'})

        result = score_resume_text(resume_text, jd)
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, port=5000)