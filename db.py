# db.py
import sqlite3
from flask import g 

# -------------------- DATABASE --------------------
DATABASE = "chat_history.db"

def get_db():
    """Return a request-scoped SQLite connection (used inside Flask routes)."""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

def close_db(_exc=None):
    """Close the request-scoped connection at app teardown."""
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def column_exists(c, table, colname):
    c.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == colname for r in c.fetchall())

def ensure_default_user(db):
    """Seed a friendly default account if no users yet."""
    c = db.cursor()
    c.execute("SELECT COUNT(*) AS n FROM users")
    if c.fetchone()["n"] == 0:
        from werkzeug.security import generate_password_hash
        c.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            ("kiitian", "kiitian@supergpt.local", generate_password_hash("supergpt123"))
        )
        db.commit()

def init_db(app=None):
    """
    Create tables if missing and migrate schemas if needed.
    Call this once at startup (inside app context).
    """
    # If caller passed app, ensure an app context is active.
    ctx = None
    if app is not None:
        ctx = app.app_context()
        ctx.push()
    try:
        db = get_db()
        c = db.cursor()

        # users table (initial create if not exists)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # --- migration: add email column if it's missing ---
        if not column_exists(c, "users", "email"):
            # Can’t add UNIQUE constraint via ALTER easily; add as nullable text.
            c.execute("ALTER TABLE users ADD COLUMN email TEXT")
        db.commit()

        # chat history
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT,
                bot_reply TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # scraped pages
        c.execute("""
            CREATE TABLE IF NOT EXISTS scraped_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                content TEXT,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        ensure_default_user(db)
    finally:
        if ctx is not None:
            ctx.pop()

def open_raw_connection():
    """
    Return a standalone SQLite connection for background threads (not tied to g).
    Use this in the scraper thread.
    """
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
