#!/usr/bin/env python3
"""Gladys Tuti ebook store.

PayPal (automatic), Kenyan bank transfer and Opay (manual approval).
Run:  python3 app.py     (set ADMIN_PASSWORD first, see README.md)
"""
import base64
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

BASE = Path(__file__).resolve().parent


def load_env():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
EBOOKS = DATA / "ebooks"      # private, never served directly
COVERS = DATA / "covers"
for d in (EBOOKS, COVERS):
    d.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "store.db"

app = Flask(__name__)
_key = DATA / "secret.key"
if not _key.exists():
    _key.write_text(secrets.token_hex(32))
    _key.chmod(0o600)
app.secret_key = _key.read_text().strip()
app.config.update(
    MAX_CONTENT_LENGTH=150 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
)

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  subtitle TEXT DEFAULT '',
  description TEXT DEFAULT '',
  price_usd REAL NOT NULL DEFAULT 0,
  price_kes REAL NOT NULL DEFAULT 0,
  price_ngn REAL NOT NULL DEFAULT 0,
  cover_file TEXT DEFAULT '',
  ebook_file TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL UNIQUE,
  product_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  method TEXT NOT NULL,              -- paypal | bank | opay
  currency TEXT NOT NULL,
  amount REAL NOT NULL,
  reference TEXT DEFAULT '',
  paypal_order_id TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | rejected
  downloads INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL,
  paid_at INTEGER,
  link_start INTEGER
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SEED = [
    ("The $10K Ebook", "Your first $10,000 in under a month",
     "A short guide to making your first $10,000 in under a month by selling "
     "an acne serum and face cream combo you make at home, using hidden "
     "(dark post) ads.", 5, 500, 5000),
    ("The Language of the Universe", "Angel numbers and how to use them",
     "On the universe, angel numbers, and using them to line yourself up "
     "with the life and the money you want.", 10, 1000, 10000),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_e):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def q(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    cur = get_db().execute(sql, args)
    get_db().commit()
    return cur.lastrowid


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(orders)")]
        if "link_start" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN link_start INTEGER")
            c.execute("UPDATE orders SET link_start=paid_at WHERE link_start IS NULL")
        if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            for t, s, d, u, k, n in SEED:
                c.execute(
                    "INSERT INTO products (title,subtitle,description,price_usd,"
                    "price_kes,price_ngn,created) VALUES (?,?,?,?,?,?,?)",
                    (t, s, d, u, k, n, int(time.time())))


def setting(key, default=""):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings (key,value) VALUES (?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------- helpers

CURRENCY_FIELD = {"USD": "price_usd", "KES": "price_kes", "NGN": "price_ngn"}
METHOD_CURRENCY = {"paypal": "USD", "bank": "KES", "opay": "NGN"}
METHOD_LABEL = {"paypal": "PayPal / card", "bank": "Kenyan bank transfer",
                "opay": "Opay"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.template_filter("money")
def money(v, cur):
    v = float(v)
    if cur == "USD":
        s = f"{v:,.2f}"
        return "$" + (s[:-3] if s.endswith(".00") else s)
    if cur == "KES":
        return f"KSh {v:,.0f}"
    if cur == "NGN":
        return f"₦{v:,.0f}"
    return f"{v:,.2f} {cur}"


@app.template_filter("when")
def when(ts):
    return time.strftime("%d %b %Y, %H:%M", time.gmtime(ts)) + " UTC" if ts else ""


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "ebook"


def paypal_ready():
    return bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_SECRET"))


def enabled_methods():
    m = []
    if paypal_ready():
        m.append("paypal")
    if setting("bank_details").strip():
        m.append("bank")
    if setting("opay_details").strip():
        m.append("opay")
    return m


def site_url():
    return os.environ.get("SITE_URL", "").rstrip("/") or request.host_url.rstrip("/")


def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        port = int(os.environ.get("SMTP_PORT", "587"))
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15)
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            s.starttls(context=ssl.create_default_context())
        with s:
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True
    except Exception as e:  # email is best effort, the order page still works
        app.logger.warning("email failed: %s", e)
        return False


def mail_download_link(order):
    link = f"{site_url()}/order/{order['token']}"
    return send_mail(
        order["email"], "Your ebook is ready",
        f"Hi {order['name']},\n\nYour payment is confirmed. Download your ebook here:\n"
        f"{link}\n\nOpen it when you're ready to download. The link works once and stops a few "
        f"minutes after you first open it, so save the file straight away.\n\n"
        f"Gladys Tuti\n")


def mark_paid(order_id):
    now = int(time.time())
    # link_start stays empty: the clock starts when the buyer first opens their paid page
    ex("UPDATE orders SET status='paid', paid_at=?, link_start=NULL WHERE id=?", (now, order_id))


def link_limits():
    def num(key, default):
        try:
            return max(1, int(float(setting(key, str(default)))))
        except ValueError:
            return default
    return num("max_downloads", 1), num("link_minutes", 5)


UNOPENED_MAX_DAYS = 7   # a paid link nobody opened for this long is retired


def start_clock(o):
    """Start the expiry clock the first time a paid order page or download is opened."""
    if o["status"] == "paid" and o["link_start"] is None:
        ex("UPDATE orders SET link_start=? WHERE id=? AND link_start IS NULL",
           (int(time.time()), o["id"]))


def link_state(o):
    """Returns (usable, downloads_left, seconds_left) for a paid order."""
    max_dl, minutes = link_limits()
    now = time.time()
    left_dl = max(0, max_dl - o["downloads"])
    if o["link_start"] is None:
        stale = now > (o["paid_at"] or o["created"]) + UNOPENED_MAX_DAYS * 86400
        left_t = 0 if stale else minutes * 60
    else:
        left_t = max(0, int(o["link_start"] + minutes * 60 - now))
    return (left_dl > 0 and left_t > 0), left_dl, left_t


# ---------------------------------------------------------------- security

@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    if request.method == "POST":
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF", "")
        if not hmac.compare_digest(tok, session["csrf"]):
            abort(400)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/admin") or request.path.startswith("/order"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.context_processor
def inject():
    return {"csrf_token": session.get("csrf", ""), "is_admin": bool(session.get("admin")),
            "METHOD_LABEL": METHOD_LABEL}


_fails = {}


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- public

@app.get("/")
def index():
    products = q("SELECT * FROM products WHERE active=1 ORDER BY id")
    return render_template("index.html", products=products)


@app.get("/cover/<int:pid>")
def cover(pid):
    p = q("SELECT cover_file FROM products WHERE id=?", (pid,), one=True)
    if not p or not p["cover_file"]:
        abort(404)
    path = COVERS / p["cover_file"]
    if not path.exists():
        abort(404)
    return send_file(path, max_age=3600)


def buyable(pid):
    p = q("SELECT * FROM products WHERE id=? AND active=1", (pid,), one=True)
    if not p or not p["ebook_file"]:
        return None
    return p


@app.get("/buy/<int:pid>")
def buy(pid):
    p = buyable(pid)
    if not p:
        abort(404)
    return render_template(
        "buy.html", p=p, methods=enabled_methods(),
        bank=setting("bank_details"), opay=setting("opay_details"),
        paypal_client_id=os.environ.get("PAYPAL_CLIENT_ID", "") if paypal_ready() else "")


def read_buyer(form):
    name = (form.get("name") or "").strip()[:120]
    email = (form.get("email") or "").strip().lower()[:200]
    if not name or not EMAIL_RE.match(email):
        return None, None, "Please enter your name and a valid email."
    if form.get("terms") not in ("on", "1", "true", True):
        return None, None, "Please tick the box to confirm the refund policy."
    return name, email, None


@app.post("/buy/<int:pid>/manual")
def buy_manual(pid):
    p = buyable(pid)
    if not p:
        abort(404)
    method = request.form.get("method")
    if method not in ("bank", "opay") or method not in enabled_methods():
        abort(400)
    name, email, err = read_buyer(request.form)
    reference = (request.form.get("reference") or "").strip()[:200]
    if not err and not reference:
        err = "Please enter your payment reference or the name you paid from."
    if not err:
        n = q("SELECT COUNT(*) c FROM orders WHERE email=? AND status='pending'", (email,), one=True)["c"]
        if n >= 5:
            err = "You already have several payments waiting to be checked."
    if err:
        flash(err)
        return redirect(url_for("buy", pid=pid))
    cur = METHOD_CURRENCY[method]
    token = secrets.token_urlsafe(16)
    ex("INSERT INTO orders (token,product_id,name,email,method,currency,amount,reference,created)"
       " VALUES (?,?,?,?,?,?,?,?,?)",
       (token, pid, name, email, method, cur, p[CURRENCY_FIELD[cur]], reference, int(time.time())))
    admin_email = os.environ.get("ADMIN_EMAIL")
    if admin_email:
        send_mail(admin_email, "New ebook payment to check",
                  f"{name} ({email}) says they paid {money(p[CURRENCY_FIELD[cur]], cur)} "
                  f"for {p['title']} via {METHOD_LABEL[method]}.\nReference: {reference}\n"
                  f"Approve it at {site_url()}/admin")
    return redirect(url_for("order", token=token))


@app.get("/order/<token>")
def order(token):
    o = q("SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id WHERE o.token=?",
          (token,), one=True)
    if not o:
        abort(404)
    if o["status"] == "paid" and o["link_start"] is None and link_state(o)[0]:
        start_clock(o)
        o = q("SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id "
              "WHERE o.token=?", (token,), one=True)
    usable, left_dl, left_t = link_state(o) if o["status"] == "paid" else (False, 0, 0)
    return render_template("order.html", o=o, usable=usable, left_dl=left_dl,
                           left_min=max(1, -(-left_t // 60)) if left_t else 0)


@app.get("/download/<token>")
def download(token):
    o = q("SELECT o.*, p.title, p.ebook_file FROM orders o "
          "JOIN products p ON p.id=o.product_id WHERE o.token=?", (token,), one=True)
    if not o or o["status"] != "paid" or not o["ebook_file"]:
        abort(404)
    if not link_state(o)[0]:
        return render_template("message.html", title="This download link has expired",
                               text="Each purchase can be downloaded a limited number of times. "
                                    "If you bought this ebook and need your link again, "
                                    "message us with the email you used."), 410
    path = EBOOKS / o["ebook_file"]
    if not path.exists():
        abort(404)
    start_clock(o)
    # count the download atomically so two quick requests can't both pass the limit
    max_dl = link_limits()[0]
    cur = get_db().execute("UPDATE orders SET downloads=downloads+1 WHERE id=? AND downloads<?",
                           (o["id"], max_dl))
    get_db().commit()
    if cur.rowcount != 1:
        return render_template("message.html", title="This download link has expired",
                               text="This purchase has already been downloaded. "
                                    "If you bought this ebook and need your link again, "
                                    "message us with the email you used."), 410
    return send_file(path, as_attachment=True, download_name=slug(o["title"]) + path.suffix)


# ---------------------------------------------------------------- PayPal

def pp_base():
    return ("https://api-m.paypal.com" if os.environ.get("PAYPAL_ENV", "live") == "live"
            else "https://api-m.sandbox.paypal.com")


def pp_request(path, payload=None, token=None):
    url = pp_base() + path
    if token is None:
        auth = base64.b64encode(
            f"{os.environ['PAYPAL_CLIENT_ID']}:{os.environ['PAYPAL_SECRET']}".encode()).decode()
        req = urllib.request.Request(url, data=b"grant_type=client_credentials", method="POST",
                                     headers={"Authorization": f"Basic {auth}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload or {}).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


@app.post("/paypal/create")
def paypal_create():
    if not paypal_ready():
        abort(404)
    data = request.get_json(silent=True) or {}
    p = buyable(int(data.get("pid") or 0))
    if not p:
        return jsonify(error="This ebook isn't available."), 404
    name, email, err = read_buyer(data)
    if err:
        return jsonify(error=err), 400
    amount = f"{p['price_usd']:.2f}"
    try:
        tok = pp_request("/v1/oauth2/token")["access_token"]
        res = pp_request("/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{"description": p["title"][:120],
                                "amount": {"currency_code": "USD", "value": amount}}],
        }, tok)
    except (urllib.error.URLError, KeyError, ValueError) as e:
        app.logger.error("paypal create failed: %s", e)
        return jsonify(error="PayPal isn't responding. Please try again."), 502
    ex("INSERT INTO orders (token,product_id,name,email,method,currency,amount,paypal_order_id,created)"
       " VALUES (?,?,?,?,?,?,?,?,?)",
       (secrets.token_urlsafe(16), p["id"], name, email, "paypal", "USD", p["price_usd"],
        res["id"], int(time.time())))
    return jsonify(id=res["id"])


@app.post("/paypal/capture")
def paypal_capture():
    if not paypal_ready():
        abort(404)
    data = request.get_json(silent=True) or {}
    o = q("SELECT * FROM orders WHERE paypal_order_id=? AND method='paypal'",
          (str(data.get("order_id", "")),), one=True)
    if not o:
        return jsonify(error="Order not found."), 404
    if o["status"] == "paid":
        return jsonify(redirect=url_for("order", token=o["token"]))
    try:
        tok = pp_request("/v1/oauth2/token")["access_token"]
        res = pp_request(f"/v2/checkout/orders/{o['paypal_order_id']}/capture", {}, tok)
        cap = res["purchase_units"][0]["payments"]["captures"][0]
        ok = (res.get("status") == "COMPLETED" and cap["status"] == "COMPLETED"
              and cap["amount"]["currency_code"] == "USD"
              and abs(float(cap["amount"]["value"]) - float(o["amount"])) < 0.005)
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as e:
        app.logger.error("paypal capture failed: %s", e)
        return jsonify(error="We couldn't confirm the payment. If PayPal took the money, "
                             "message us and we'll sort it out."), 502
    if not ok:
        return jsonify(error="The payment wasn't completed."), 402
    mark_paid(o["id"])
    mail_download_link(q("SELECT * FROM orders WHERE id=?", (o["id"],), one=True))
    return jsonify(redirect=url_for("order", token=o["token"]))


# ---------------------------------------------------------------- admin

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        count, until = _fails.get(ip, (0, 0))
        if until > time.time():
            flash("Too many tries. Wait a few minutes.")
        else:
            expected = os.environ.get("ADMIN_PASSWORD", "")
            given = request.form.get("password", "")
            if expected and hmac.compare_digest(given.encode(), expected.encode()):
                _fails.pop(ip, None)
                session.clear()
                session["admin"] = True
                session["csrf"] = secrets.token_urlsafe(24)
                return redirect(url_for("admin"))
            count += 1
            _fails[ip] = (count, time.time() + 300 if count >= 5 else 0)
            flash("Wrong password." if expected else "ADMIN_PASSWORD isn't set on the server.")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin():
    pending = q("SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.status='pending' AND o.method!='paypal' ORDER BY o.id")
    recent = q("SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id "
               "WHERE o.status!='pending' ORDER BY o.id DESC LIMIT 50")
    products = q("SELECT * FROM products ORDER BY id")
    return render_template("admin.html", pending=pending, recent=recent, products=products,
                           site=site_url())


@app.post("/admin/order/<int:oid>/<action>")
@admin_required
def admin_order(oid, action):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o or action not in ("approve", "reject") or o["status"] != "pending":
        abort(400)
    if action == "approve":
        mark_paid(oid)
        sent = mail_download_link(q("SELECT * FROM orders WHERE id=?", (oid,), one=True))
        flash("Approved. " + ("Download link emailed." if sent else
                              "Email isn't set up, so send the buyer their link below."))
    else:
        ex("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        flash("Rejected.")
    return redirect(url_for("admin"))


@app.post("/admin/order/<int:oid>/renew")
@admin_required
def admin_renew(oid):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o or o["status"] != "paid":
        abort(400)
    ex("UPDATE orders SET downloads=0, link_start=NULL, paid_at=? WHERE id=?", (int(time.time()), oid))
    flash("Link renewed. The buyer can download again once they open their page.")
    return redirect(url_for("admin"))


def save_upload(field, folder, allowed, old=""):
    f = request.files.get(field)
    if not f or not f.filename:
        return old
    ext = Path(f.filename).suffix.lower()
    if ext not in allowed:
        raise ValueError(f"{field}: use {', '.join(sorted(allowed))}")
    name = f"{secrets.token_hex(8)}{ext}"
    f.save(folder / name)
    if old and (folder / old).exists():
        (folder / old).unlink()
    return name


@app.route("/admin/product/new", methods=["GET", "POST"])
@app.route("/admin/product/<int:pid>", methods=["GET", "POST"])
@admin_required
def admin_product(pid=None):
    p = q("SELECT * FROM products WHERE id=?", (pid,), one=True) if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            prices = [max(0.0, float(f.get(k) or 0)) for k in ("price_usd", "price_kes", "price_ngn")]
            title = (f.get("title") or "").strip()
            if not title:
                raise ValueError("Title is required.")
            cover_file = save_upload("cover", COVERS, {".png", ".jpg", ".jpeg", ".webp"},
                                     p["cover_file"] if p else "")
            ebook_file = save_upload("ebook", EBOOKS, {".pdf", ".epub"},
                                     p["ebook_file"] if p else "")
        except ValueError as e:
            flash(str(e))
            return render_template("admin_product.html", p=p or f), 400
        vals = (title, (f.get("subtitle") or "").strip(), (f.get("description") or "").strip(),
                *prices, cover_file, ebook_file, 1 if f.get("active") else 0)
        if p:
            ex("UPDATE products SET title=?,subtitle=?,description=?,price_usd=?,price_kes=?,"
               "price_ngn=?,cover_file=?,ebook_file=?,active=? WHERE id=?", (*vals, pid))
        else:
            ex("INSERT INTO products (title,subtitle,description,price_usd,price_kes,price_ngn,"
               "cover_file,ebook_file,active,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (*vals, int(time.time())))
        flash("Saved.")
        return redirect(url_for("admin"))
    return render_template("admin_product.html", p=p)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        set_setting("bank_details", (request.form.get("bank_details") or "").strip())
        set_setting("opay_details", (request.form.get("opay_details") or "").strip())
        for key in ("max_downloads", "link_minutes"):
            v = (request.form.get(key) or "").strip()
            if v.isdigit() and int(v) >= 1:
                set_setting(key, v)
        flash("Saved.")
        return redirect(url_for("admin_settings"))
    max_dl, minutes = link_limits()
    return render_template("admin_settings.html", max_dl=max_dl, minutes=minutes,
                           bank=setting("bank_details"),
                           opay=setting("opay_details"), paypal=paypal_ready(),
                           smtp=bool(os.environ.get("SMTP_HOST")))


@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Not found",
                           text="That page doesn't exist."), 404


@app.errorhandler(400)
def bad_request(_e):
    return render_template("message.html", title="Something went wrong",
                           text="Please go back, refresh the page and try again."), 400


@app.errorhandler(413)
def too_big(_e):
    return render_template("message.html", title="File too large",
                           text="Uploads are limited to 150 MB."), 413


init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))
