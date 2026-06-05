import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_dance.contrib.google import make_google_blueprint, google
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.middleware.proxy_fix import ProxyFix
import pdfplumber
import docx
import json
import io
import re
import pytesseract
from PIL import Image
from pdf2image import convert_from_bytes
from groq import Groq
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# Tesseract path — Windows only
if os.name == 'nt':
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "recruitiq2026supersecretkey"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config['SESSION_COOKIE_NAME'] = 'recruitiq_session'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True

# ===== SUPABASE =====
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)

# ===== GROQ =====
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ===== GOOGLE OAUTH =====
is_hf = os.environ.get("SPACE_ID") is not None

redirect_uri = (
    "https://sriramkumarm95-recruitiq.hf.space/login/google/authorized"
    if is_hf else
    "http://localhost:5000/login/google/authorized"
)

google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    scope=["openid",
           "https://www.googleapis.com/auth/userinfo.email",
           "https://www.googleapis.com/auth/userinfo.profile"],
)
app.register_blueprint(google_bp, url_prefix="/login")

# ===== LOGIN MANAGER =====
login_manager = LoginManager(app)

@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for('login_page'))

class User(UserMixin):
    def __init__(self, id, name, email):
        self.id = id
        self.name = name
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    try:
        result = supabase.table("users").select("*").eq("id", user_id).execute()
        if result.data:
            u = result.data[0]
            return User(u['id'], u['name'], u['email'])
    except:
        pass
    return None

# ===== FILE PARSER WITH OCR =====
def extract_text(file):
    filename = file.filename.lower()
    content = file.read()

    if filename.endswith('.pdf'):
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = ' '.join(page.extract_text() or '' for page in pdf.pages)
            if len(text.strip()) > 100:
                return text.strip(), False
        except:
            pass
        try:
            images = convert_from_bytes(content, dpi=200)
            text = ''
            for img in images:
                text += pytesseract.image_to_string(img) + ' '
            return text.strip(), True
        except Exception as e:
            print(f"OCR error: {e}")
            return "", False

    elif filename.endswith('.docx'):
        doc = docx.Document(io.BytesIO(content))
        return ' '.join(p.text for p in doc.paragraphs), False

    elif filename.endswith('.txt'):
        return content.decode('utf-8', errors='ignore'), False

    return "", False

# ===== EXPERIENCE CHECKER =====
def check_experience_requirement(jd_text, resume_text):
    jd_lower = jd_text.lower()
    required_years = 0
    patterns = [
        r'(\d+)\+?\s*years?\s*(?:of\s*)?(?:experience|exp)',
        r'minimum\s*(\d+)\s*years?',
        r'at\s*least\s*(\d+)\s*years?',
    ]
    for pattern in patterns:
        match = re.search(pattern, jd_lower)
        if match:
            required_years = int(match.group(1))
            break

    candidate_years = 0
    exp_patterns = [
        r'(\d+)\+?\s*years?\s*of\s*experience',
        r'(\d+)\+?\s*years?\s*experience',
        r'(\d{4})\s*[-–]\s*(\d{4}|present|current)',
    ]
    years = []
    for pattern in exp_patterns:
        matches = re.findall(pattern, resume_text.lower())
        for match in matches:
            if isinstance(match, tuple):
                try:
                    start = int(match[0])
                    end = 2026 if match[1] in ['present','current'] else int(match[1])
                    if 1990 <= start <= 2026:
                        years.append(end - start)
                except:
                    pass
            else:
                try:
                    years.append(int(match))
                except:
                    pass
    candidate_years = max(years) if years else 0

    return {
        "required_years": required_years,
        "candidate_years": candidate_years,
        "meets_requirement": candidate_years >= required_years if required_years > 0 else True,
        "gap_years": max(0, required_years - candidate_years)
    }

# ===== SCORER =====
def score_resume_text(resume_text, jd_text):
    prompt = f"""You are an expert recruiter with 10 years experience.
Score this resume against the job description on a scale of 0-100.
Return ONLY valid JSON — no extra text.

STRICT RULES:
- Only mention skills EXPLICITLY written in the resume
- key_matches must only contain skills found word-for-word in resume
- key_gaps must only contain skills from JD missing in resume

JOB DESCRIPTION:
{jd_text[:1000]}

RESUME:
{resume_text[:1000]}

Return exactly this JSON:
{{
  "score": <integer 0-100>,
  "confidence": <integer 0-100>,
  "reasoning": "<2 sentences>",
  "key_matches": ["skill1", "skill2", "skill3"],
  "key_gaps": ["gap1", "gap2", "gap3"]
}}"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0,
        seed=42
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    confidence = result.get('confidence', 100)
    if confidence < 40:
        result['warning'] = 'Low confidence — resume may be unreadable or too short'
        result['score'] = min(result['score'], 30)

    result['score'] = max(0, min(100, int(result.get('score', 0))))

    verified_matches = []
    resume_lower = resume_text.lower()
    for skill in result.get('key_matches', []):
        if skill.lower() in resume_lower:
            verified_matches.append(skill)
    result['key_matches'] = verified_matches

    return result

# ===== IMPROVEMENT RECOMMENDATIONS =====
def generate_improvements(resume_text, jd_text, key_gaps, score):
    prompt = f"""You are a career coach helping a rejected job candidate improve.
The candidate scored {score}/100. Missing skills: {', '.join(key_gaps[:5])}

JOB DESCRIPTION:
{jd_text[:600]}

RESUME:
{resume_text[:600]}

Return ONLY valid JSON:
{{
  "summary": "<1 sentence overall advice>",
  "improvements": [
    {{
      "skill": "<skill name>",
      "why_needed": "<1 sentence>",
      "how_to_learn": "<specific course or resource>",
      "time_estimate": "<e.g. 2 weeks>",
      "priority": "<High/Medium/Low>"
    }}
  ],
  "reapply_timeline": "<e.g. 2-3 months>",
  "reapply_score_estimate": <integer>
}}"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0,
        seed=42
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ===== ROUTES =====
@app.route('/')
def home():
    if current_user.is_authenticated:
        return render_template('index.html', user=current_user)
    
    # Handle Google OAuth callback
    if google.authorized:
        try:
            resp = google.get("/oauth2/v2/userinfo")
            if resp.ok:
                info = resp.json()
                google_id = info['id']
                name = info.get('name', '')
                email = info.get('email', '')
                result = supabase.table("users").select("*").eq("google_id", google_id).execute()
                if result.data:
                    user_data = result.data[0]
                else:
                    new_user = supabase.table("users").insert({
                        "google_id": google_id,
                        "name": name,
                        "email": email
                    }).execute()
                    user_data = new_user.data[0]
                user = User(user_data['id'], user_data['name'], user_data['email'])
                login_user(user)
                return render_template('index.html', user=user)
        except Exception as e:
            print("Auth error:", e)
    
    return render_template('landing.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    return redirect(url_for('google.login'))


@app.route('/logout')
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('home'))

@app.route('/score', methods=['POST'])
def score():
    try:
        jd = request.form.get('jd', '')
        resume_file = request.files.get('resume')

        if not jd or not resume_file:
            return jsonify({'error': 'Missing JD or resume file'})

        resume_text, is_scanned = extract_text(resume_file)
        if not resume_text.strip():
            return jsonify({'error': 'Could not extract text from file'})

        result = score_resume_text(resume_text, jd)
        exp_check = check_experience_requirement(jd, resume_text)
        result['experience_check'] = exp_check
        result['is_scanned'] = is_scanned

        threshold = int(request.form.get('threshold', 50))
        if result['score'] < threshold and result.get('key_gaps'):
            try:
                improvements = generate_improvements(
                    resume_text, jd,
                    result.get('key_gaps', []),
                    result['score']
                )
                result['improvements'] = improvements
            except Exception as e:
                print(f"Improvement error: {e}")
                result['improvements'] = None

        if current_user.is_authenticated:
            try:
                supabase.table("screenings").insert({
                    "user_id": current_user.id,
                    "filename": resume_file.filename,
                    "jd_text": jd[:500],
                    "score": result['score'],
                    "reasoning": result['reasoning'],
                    "key_matches": result.get('key_matches', []),
                    "key_gaps": result.get('key_gaps', [])
                }).execute()
            except Exception as e:
                print(f"DB error: {e}")

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/history')
def history():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Not logged in'})
    try:
        result = supabase.table("screenings")\
            .select("*")\
            .eq("user_id", current_user.id)\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='127.0.0.1', port=port)