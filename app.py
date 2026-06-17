"""
B2 Apparel - internal product reviews API
Flask + PostgreSQL, photos to S3-compatible storage (Cloudflare R2),
verified-purchase checks against Shopify orders, approve-before-publish moderation.

Endpoints
  GET  /                              health check
  GET  /api/reviews                   approved reviews (optional ?product_id=)
  POST /api/reviews                   submit a review (multipart form, optional photos)
  GET  /admin                         moderation UI (paste admin token)
  GET  /api/admin/pending             pending reviews (token)
  POST /api/admin/reviews/<id>/approve
  POST /api/admin/reviews/<id>/reject

All configuration comes from environment variables - see .env.example.
"""
import os
import uuid
import html
from datetime import datetime

from flask import Flask, request, jsonify, abort, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
import boto3
from botocore.client import Config

# ----------------------------------------------------------------------------
# Configuration (all from env)
# ----------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///reviews.db")
# Railway hands out postgres:// ; SQLAlchemy wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Shopify (verified purchase) - Admin API token with read_orders, server-side only
SHOPIFY_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
# If true, only people with a matching order can post. If false, everyone can
# post but only matched buyers get the "Verified" badge.
REQUIRE_VERIFIED = os.environ.get("REQUIRE_VERIFIED", "true").lower() == "true"

# Photos -> S3-compatible storage (Cloudflare R2)
MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "3"))
MAX_PHOTO_MB = float(os.environ.get("MAX_PHOTO_MB", "8"))
ALLOWED_IMG = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "")  # public base URL for the bucket

# Email notifications -> SendGrid
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "")
REVIEW_NOTIFY_EMAIL = os.environ.get("REVIEW_NOTIFY_EMAIL", "")
REVIEW_ADMIN_URL = os.environ.get(
    "REVIEW_ADMIN_URL",
    "https://web-production-e3d6f.up.railway.app/admin",
)

# ----------------------------------------------------------------------------
# App setup
# ----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# cap request body so a giant upload can't exhaust memory
app.config["MAX_CONTENT_LENGTH"] = int((MAX_PHOTO_MB * MAX_PHOTOS + 2) * 1024 * 1024)

db = SQLAlchemy(app)
CORS(app, origins=ALLOWED_ORIGINS or "*")
limiter = Limiter(get_remote_address, app=app, default_limits=[])


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(64), index=True)
    product_title = db.Column(db.String(255))
    name = db.Column(db.String(120))
    email = db.Column(db.String(255))
    rating = db.Column(db.Integer)
    body = db.Column(db.Text)
    photos = db.Column(db.Text)  # comma-separated URLs
    verified = db.Column(db.Boolean, default=False)
    approved = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def public(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "product_title": self.product_title,
            "name": self.name,
            "rating": self.rating,
            "body": self.body,
            "photos": [p for p in (self.photos or "").split(",") if p],
            "verified": self.verified,
            "created_at": self.created_at.isoformat() + "Z",
        }


with app.app_context():
    db.create_all()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def s3():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_photo(file_storage):
    """Validate and store one uploaded image; return its public URL."""
    ctype = (file_storage.mimetype or "").lower()
    if ctype not in ALLOWED_IMG:
        abort(415, "Only JPG, PNG, or WEBP images are allowed.")
    data = file_storage.read()
    if len(data) > MAX_PHOTO_MB * 1024 * 1024:
        abort(413, "One of the photos is too large.")
    key = f"reviews/{uuid.uuid4().hex}.{ALLOWED_IMG[ctype]}"
    s3().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType=ctype)
    return f"{R2_PUBLIC_BASE.rstrip('/')}/{key}"


def verify_purchase(email, product_id):
    """True if this email has any order containing this product."""
    if not (SHOPIFY_DOMAIN and SHOPIFY_ADMIN_TOKEN and email and product_id):
        return False
    url = f"https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {"status": "any", "email": email, "limit": 250, "fields": "line_items"}
    headers = {"X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return False
        for order in r.json().get("orders", []):
            for li in order.get("line_items", []):
                if str(li.get("product_id")) == str(product_id):
                    return True
    except requests.RequestException:
        return False
    return False


def send_pending_review_email(review):
    """Notify the business owner that a review is waiting for moderation.

    Notification failures are logged but do not block review submission.
    """
    if not (SENDGRID_API_KEY and SENDGRID_FROM_EMAIL and REVIEW_NOTIFY_EMAIL):
        app.logger.info("SendGrid notification skipped: missing email environment variables.")
        return

    stars = "★" * int(review.rating) + "☆" * (5 - int(review.rating))
    product = review.product_title or review.product_id or "Not specified"
    verified_text = "Yes" if review.verified else "No"
    review_body = review.body or ""

    plain_text = f"""New B Squared review pending approval

Name: {review.name}
Email: {review.email}
Product: {product}
Rating: {stars}
Verified purchase: {verified_text}

Review:
{review_body}

Moderate it here:
{REVIEW_ADMIN_URL}
"""

    safe_name = html.escape(review.name or "")
    safe_email = html.escape(review.email or "")
    safe_product = html.escape(product)
    safe_body = html.escape(review_body).replace("\n", "<br>")
    safe_url = html.escape(REVIEW_ADMIN_URL)

    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;line-height:1.55;color:#111827">
      <h2 style="margin:0 0 12px">New B Squared review pending approval</h2>
      <p><strong>Name:</strong> {safe_name}</p>
      <p><strong>Email:</strong> {safe_email}</p>
      <p><strong>Product:</strong> {safe_product}</p>
      <p><strong>Rating:</strong> {stars}</p>
      <p><strong>Verified purchase:</strong> {verified_text}</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:18px 0">
      <p><strong>Review:</strong></p>
      <p>{safe_body}</p>
      <p style="margin-top:22px">
        <a href="{safe_url}" style="background:#36d6ff;color:#07080c;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:bold">
          Open moderation panel
        </a>
      </p>
      <p style="font-size:12px;color:#6b7280">This notification was sent by the B Squared Reviews API.</p>
    </div>
    """

    payload = {
        "personalizations": [
            {
                "to": [{"email": REVIEW_NOTIFY_EMAIL}],
                "subject": "New B Squared review pending approval",
            }
        ],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": "B Squared Reviews"},
        "content": [
            {"type": "text/plain", "value": plain_text},
            {"type": "text/html", "value": html_body},
        ],
    }

    try:
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if response.status_code not in (200, 202):
            app.logger.warning(
                "SendGrid notification failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
    except requests.RequestException as exc:
        app.logger.warning("SendGrid notification failed: %s", exc)



def require_admin():
    token = request.headers.get("X-Admin-Token") or request.args.get("token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(401, "Unauthorized")


# ----------------------------------------------------------------------------
# Public routes
# ----------------------------------------------------------------------------
@app.get("/")
def health():
    return jsonify({"ok": True, "service": "b2-reviews"})


@app.get("/api/reviews")
def list_reviews():
    q = Review.query.filter_by(approved=True)
    pid = request.args.get("product_id")
    if pid:
        q = q.filter_by(product_id=pid)
    rows = q.order_by(Review.created_at.desc()).all()
    count = len(rows)
    avg = round(sum(r.rating for r in rows) / count, 2) if count else 0
    return jsonify({"count": count, "average": avg, "reviews": [r.public() for r in rows]})


@app.post("/api/reviews")
@limiter.limit("5 per hour")
def create_review():
    f = request.form
    name = (f.get("name") or "").strip()[:120]
    email = (f.get("email") or "").strip()[:255]
    product_id = (f.get("product_id") or "").strip()[:64]
    product_title = (f.get("product_title") or "").strip()[:255]
    body = (f.get("body") or "").strip()[:4000]
    try:
        rating = int(f.get("rating", "0"))
    except ValueError:
        rating = 0

    if not name or not email or not body or rating < 1 or rating > 5:
        abort(400, "Please provide a name, email, rating (1-5), and review text.")

    verified = verify_purchase(email, product_id)
    if REQUIRE_VERIFIED and not verified:
        return jsonify({
            "ok": False,
            "error": "We couldn't match that email to an order for this product. "
                     "Please use the email from your order."
        }), 403

    urls = []
    for fs in request.files.getlist("photos")[:MAX_PHOTOS]:
        if fs and fs.filename:
            urls.append(upload_photo(fs))

    rev = Review(
        product_id=product_id, product_title=product_title, name=name, email=email,
        rating=rating, body=body, photos=",".join(urls), verified=verified, approved=False,
    )
    db.session.add(rev)
    db.session.commit()
    send_pending_review_email(rev)
    return jsonify({"ok": True, "message": "Thanks! Your review is pending approval."}), 201


# ----------------------------------------------------------------------------
# Admin routes
# ----------------------------------------------------------------------------
@app.get("/api/admin/pending")
def admin_pending():
    require_admin()
    rows = Review.query.filter_by(approved=False).order_by(Review.created_at.asc()).all()
    return jsonify([{**r.public(), "email": r.email} for r in rows])


@app.post("/api/admin/reviews/<int:rid>/approve")
def admin_approve(rid):
    require_admin()
    r = db.session.get(Review, rid) or abort(404)
    r.approved = True
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/reviews/<int:rid>/reject")
def admin_reject(rid):
    require_admin()
    r = db.session.get(Review, rid) or abort(404)
    db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/admin")
def admin_page():
    return Response(ADMIN_HTML, mimetype="text/html")


ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>B2 Reviews - Moderation</title>
<style>
 body{font-family:system-ui,sans-serif;background:#07080c;color:#f4f6ff;margin:0;padding:24px}
 h1{font-size:20px} input{padding:9px 12px;border-radius:8px;border:1px solid #333;background:#11151f;color:#fff}
 .card{border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:16px;margin:12px 0;background:#0e1119}
 .top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center}
 .stars{color:#36d6ff} .muted{color:#98a0b6;font-size:13px}
 .badge{font-size:11px;border:1px solid #36d6ff;color:#36d6ff;border-radius:999px;padding:2px 8px}
 .imgs img{height:84px;border-radius:8px;margin:8px 8px 0 0;object-fit:cover}
 button{cursor:pointer;border:0;border-radius:8px;padding:8px 14px;font-weight:600}
 .ok{background:#36d6ff;color:#07080c} .no{background:#e45fd0;color:#07080c} .row{margin-top:12px;display:flex;gap:10px}
 #empty{color:#98a0b6}
</style></head><body>
<h1>B&sup2; Reviews - Moderation</h1>
<p class="muted">Paste your admin token, then approve or reject pending reviews. Photos appear here before going live.</p>
<input id="tok" type="password" placeholder="Admin token" style="width:280px">
<button class="ok" onclick="load()">Load pending</button>
<div id="list"></div>
<script>
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(path,method){const t=document.getElementById('tok').value;
  return fetch(path,{method:method||'GET',headers:{'X-Admin-Token':t}});}
async function load(){
  const r=await api('/api/admin/pending'); const el=document.getElementById('list');
  if(!r.ok){el.innerHTML='<p id="empty">Unauthorized - check your token.</p>';return;}
  const rows=await r.json();
  if(!rows.length){el.innerHTML='<p id="empty">No pending reviews.</p>';return;}
  el.innerHTML=rows.map(v=>`<div class="card" id="r${v.id}">
    <div class="top"><strong>${esc(v.name)}</strong>
      <span class="stars">${'\u2605'.repeat(v.rating)}${'\u2606'.repeat(5-v.rating)}</span></div>
    <div class="muted">${esc(v.email)} &middot; ${esc(v.product_title||v.product_id)} ${v.verified?'<span class="badge">Verified</span>':''}</div>
    <p>${esc(v.body)}</p>
    <div class="imgs">${(v.photos||[]).map(u=>`<img src="${u}">`).join('')}</div>
    <div class="row"><button class="ok" onclick="act(${v.id},'approve')">Approve</button>
      <button class="no" onclick="act(${v.id},'reject')">Reject</button></div></div>`).join('');
}
async function act(id,what){const r=await api('/api/admin/reviews/'+id+'/'+what,'POST');
  if(r.ok){document.getElementById('r'+id).remove();}else{alert('Action failed');}}
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
