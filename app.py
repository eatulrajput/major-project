import os
import re
import time
import sqlite3
import html
import threading
import requests
import fitz  # PyMuPDF
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from urllib import robotparser
from flask import (
    Flask, request, jsonify, render_template, g,
    redirect, url_for, session
)
from werkzeug.security import generate_password_hash, check_password_hash

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# >>> NEW: import DB utilities <<<
from db import (
    DATABASE, get_db, init_db, close_db, open_raw_connection
)

# -------------------- FLASK CONFIG --------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# -------------------- MODEL & API CONFIG --------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")     
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "meta-llama/llama-4-scout-17b-16e-instruct"

# -------------------- UPLOADED PDF BUFFER --------------------
uploaded_pdf_text = ""

# -------------------- SCRAPER CONFIG --------------------
START_ROOT = "kiit.ac.in"
MAX_PAGES_DEFAULT = int(os.environ.get("KIIT_SCRAPE_MAX", 150))
DELAY_DEFAULT = float(os.environ.get("KIIT_SCRAPE_DELAY", 0.8))
USER_AGENT = "SuperGPT-Scraper/1.3 (local-dev; polite crawler)"

# -------------------- APP TEARDOWN --------------------
@app.teardown_appcontext
def teardown_db(_exc):
    close_db(_exc)

# -------------------- LOGIN REQUIRED DECORATOR --------------------
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

# -------------------- UTILS --------------------
def clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def visible_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "svg", "meta", "nav"]):
        tag.decompose()
    return clean_text(soup.get_text(separator=" "))

def normalize_url(base: str, link: str) -> str:
    joined = urljoin(base, link)
    p = urlparse(joined)
    return f"{p.scheme}://{p.netloc}{p.path}"

def upsert_page(db_conn, url: str, title: str, content: str):
    c = db_conn.cursor()
    c.execute("""
        INSERT INTO scraped_pages (url, title, content, fetched_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            content=excluded.content,
            fetched_at=CURRENT_TIMESTAMP
    """, (url, title, content))
    db_conn.commit()

# -------------------- ROUTES: SEPARATED UI --------------------
@app.route("/")
def root():
    return redirect(url_for("chat_page"))

@app.route("/chat", methods=["GET"])
@login_required
def chat_page():
    return render_template("chat.html")

@app.route("/scraper", methods=["GET"])
@login_required
def scraper_page():
    return render_template("scraper.html")

# ---------- AUTH ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_or_email = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "")
        db = get_db()
        c = db.cursor()
        # allow login using username or email
        c.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username_or_email, username_or_email))
        row = c.fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session["user_id"] = row["id"]
            session["username"] = row["username"]
            return redirect(url_for("chat_page"))
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "")
        confirm = (request.form.get("confirm") or "")

        if not username or not email or not password:
            return render_template("register.html", error="All fields are required.")
        if password != confirm:
            return render_template("register.html", error="Passwords do not match.")

        db = get_db()
        c = db.cursor()

        # check duplicates (username or email)
        c.execute("SELECT 1 FROM users WHERE username = ? OR email = ?", (username, email))
        if c.fetchone():
            return render_template("register.html", error="Username or email already exists.")

        c.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, generate_password_hash(password))
        )
        db.commit()

        # auto login
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        user = c.fetchone()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("chat_page"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------- PDF UPLOAD --------------------
@app.route("/upload", methods=["POST"])
@login_required
def upload_pdf():
    global uploaded_pdf_text
    if "file" not in request.files:
        return jsonify({"message": "No file uploaded"}), 400
    file = request.files["file"]
    if file.filename == "" or not file.filename.lower().endswith(".pdf"):
        return jsonify({"message": "Invalid file type"}), 400
    try:
        doc = fitz.open(stream=file.read(), filetype="pdf")
        text = "\n".join([page.get_text("text") for page in doc])
        uploaded_pdf_text = text if text.strip() else "No text found in PDF."
        return jsonify({"message": "PDF uploaded successfully"})
    except Exception as e:
        return jsonify({"message": f"Error processing PDF: {str(e)}"}), 500

# -------------------- SCRAPER --------------------
_scrape_state = {
    "running": False, "pages_saved": 0,
    "started_at": None, "finished_at": None,
    "last_url": None, "error": None
}

def can_fetch_url(url: str, rp: robotparser.RobotFileParser) -> bool:
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True

def background_scrape(start_url: str, max_pages: int, delay: float):
    global _scrape_state
    _scrape_state.update({
        "running": True, "pages_saved": 0,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None, "error": None
    })
    try:
        # >>> changed: use a raw connection from db.py for thread <<<
        db_conn = open_raw_connection()

        rp = robotparser.RobotFileParser()
        try:
            rp.set_url(urljoin(start_url, "/robots.txt"))
            rp.read()
        except Exception:
            pass

        headers = {"User-Agent": USER_AGENT}
        visited = set()
        queue = [start_url]
        root = START_ROOT

        while queue and len(visited) < max_pages and _scrape_state["running"]:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            _scrape_state["last_url"] = url

            if not urlparse(url).netloc.endswith(root):
                continue
            if not can_fetch_url(url, rp):
                time.sleep(delay); continue

            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code != 200:
                    time.sleep(delay); continue

                ctype = (resp.headers.get("content-type") or "").lower()
                content, title, soup = "", url, None

                if "pdf" in ctype or url.lower().endswith(".pdf"):
                    try:
                        doc = fitz.open(stream=resp.content, filetype="pdf")
                        content = "\n".join([p.get_text("text") for p in doc])
                        title = url.split("/")[-1] or url
                    except Exception:
                        time.sleep(delay); continue
                elif "text" in ctype or "html" in ctype:
                    html_text = resp.text
                    soup = BeautifulSoup(html_text, "html.parser")
                    title = soup.title.string.strip() if soup.title and soup.title.string else url
                    content = visible_text(html_text)
                else:
                    time.sleep(delay); continue

                if content.strip():
                    upsert_page(db_conn, url, title, content)
                    _scrape_state["pages_saved"] += 1

                if soup is not None:
                    for a in soup.find_all("a", href=True):
                        href = a.get("href")
                        if href.startswith(("mailto:", "tel:", "javascript:")):
                            continue
                        normalized = normalize_url(url, href)
                        if urlparse(normalized).netloc.endswith(root) and normalized not in visited:
                            queue.append(normalized)

                time.sleep(delay)
            except Exception:
                time.sleep(delay)
                continue
    except Exception as e:
        _scrape_state["error"] = str(e)
    finally:
        _scrape_state["running"] = False
        _scrape_state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

@app.route("/scrape/start", methods=["POST"])
@login_required
def scrape_start():
    if _scrape_state["running"]:
        return jsonify({"message": "Scraper already running", "status": _scrape_state})
    params = request.get_json(silent=True) or {}
    start_url = params.get("start_url", f"https://{START_ROOT}")
    max_pages = int(params.get("max_pages", MAX_PAGES_DEFAULT))
    delay = float(params.get("delay", DELAY_DEFAULT))
    t = threading.Thread(target=background_scrape, args=(start_url, max_pages, delay), daemon=True)
    t.start()
    return jsonify({"message": "Scraper started", "status": _scrape_state})

@app.route("/scrape/status")
@login_required
def scrape_status():
    return jsonify(_scrape_state)

@app.route("/scrape/stop", methods=["POST"])
@login_required
def scrape_stop():
    _scrape_state["running"] = False
    return jsonify({"message": "Stop signal sent", "status": _scrape_state})

# -------------------- TF-IDF --------------------
_tfidf_vectorizer = None
_tfidf_matrix = None
_tfidf_rows = []
_last_index_count = 0

def build_tfidf_index():
    global _tfidf_vectorizer, _tfidf_matrix, _tfidf_rows, _last_index_count
    db = get_db()
    c = db.cursor()
    c.execute("SELECT url, title, content FROM scraped_pages")
    rows = c.fetchall()
    docs = []
    _tfidf_rows = []
    for r in rows:
        text = clean_text((r["title"] or "") + " " + (r["content"] or ""))
        docs.append(text)
        _tfidf_rows.append({"url": r["url"], "title": r["title"], "content": r["content"]})
    if not docs:
        _tfidf_vectorizer = None
        _tfidf_matrix = None
        _last_index_count = 0
        return {"indexed_pages": 0}
    _tfidf_vectorizer = TfidfVectorizer(stop_words="english", max_features=50000)
    _tfidf_matrix = _tfidf_vectorizer.fit_transform(docs)
    _last_index_count = len(docs)
    return {"indexed_pages": _last_index_count}

def ensure_index_up_to_date():
    db = get_db()
    c = db.cursor()
    c.execute("SELECT COUNT(*) AS n FROM scraped_pages")
    n = c.fetchone()["n"]
    if n != _last_index_count or _tfidf_vectorizer is None or _tfidf_matrix is None:
        return build_tfidf_index()
    return {"indexed_pages": _last_index_count}

@app.route("/reindex", methods=["POST"])
@login_required
def reindex_endpoint():
    info = build_tfidf_index()
    return jsonify({"message": "TF-IDF index rebuilt", **info})

def retrieve_tfidf(query: str, top_n=5):
    if not query.strip():
        return []
    ensure_index_up_to_date()
    if _tfidf_vectorizer is None or _tfidf_matrix is None or not _tfidf_rows:
        return []
    q_vec = _tfidf_vectorizer.transform([query])
    sims = cosine_similarity(q_vec, _tfidf_matrix).ravel()
    idxs = sims.argsort()[::-1][:top_n]
    results = []
    for i in idxs:
        row = _tfidf_rows[i]
        snippet = clean_text((row["content"] or "")[:800])
        results.append({
            "url": row["url"], "title": row["title"],
            "snippet": snippet, "score": float(sims[i])
        })
    return results

# -------------------- CHAT API --------------------
@app.route("/api/chat", methods=["POST"])
@login_required
def chat_api():
    global uploaded_pdf_text
    user_message = (request.json.get("message") or "").strip()
    if not user_message:
        return jsonify({"reply": "Please enter a message."}), 400

    relevant = retrieve_tfidf(user_message, 5)
    relevant_text = "\n".join([f"{r['title']} - {r['url']}\n{r['snippet']}" for r in relevant])

    system_prompt = (
        "You are KiitGPT, a chatbot for KIIT students. "
        "Use the following context from the KIIT website when helpful, and include the URL in answers:\n\n"
        f"{relevant_text}\n\n"
        f"Uploaded PDF content (if any):\n\n{uploaded_pdf_text}"
    )
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": 0.5
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    try:
        r = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        bot_reply = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        if relevant:
            # graceful fallback with retrieved snippets
            lines = ["I couldn't reach the AI backend. Here's relevant info I found:\n"]
            for doc in relevant:
                lines.append(f"- {doc['title']} ({doc['url']}): {doc['snippet'][:300]}...")
            bot_reply = "\n\n".join(lines)
        else:
            bot_reply = f"Temporary error: {e}"

    db = get_db()
    db.execute("INSERT INTO chat_history (user_message, bot_reply) VALUES (?, ?)", (user_message, bot_reply))
    db.commit()
    return jsonify({"reply": bot_reply})

# -------------------- HISTORY --------------------
@app.route("/history")
@login_required
def get_chat_history():
    db = get_db()
    rows = db.execute("SELECT user_message, bot_reply FROM chat_history ORDER BY id DESC").fetchall()
    return jsonify({"history": [{"user": r["user_message"], "bot": r["bot_reply"]} for r in rows]})

# -------------------- INIT --------------------
# Initialize DB once at startup
init_db(app)

if __name__ == "__main__":
    app.run(debug=True)
