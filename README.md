# B² Apparel — Reviews API

A small, self-owned product reviews service for the B² Apparel headless site.
Flask + PostgreSQL, photo uploads to Cloudflare R2, verified-purchase checks
against Shopify orders, and approve-before-publish moderation.

## What it does
- `GET /api/reviews?product_id=…` — returns approved reviews + count + average
- `POST /api/reviews` — accepts a review (multipart form, optional photos), checks
  the email against Shopify orders, stores it as **pending**
- `GET /admin` — moderation page; approve/reject pending reviews (token-protected)

Nothing is shown publicly until you approve it.

## 1. Deploy to Railway
1. Create a new Railway project and add this folder as the service (GitHub repo or
   `railway up`).
2. Add the **PostgreSQL** plugin — Railway sets `DATABASE_URL` for you.
3. Set the environment variables from `.env.example` (Variables tab).
4. Railway uses the `Procfile` to start it with gunicorn. Tables are created on boot.

## 2. Shopify Admin token (for verified purchase)
Verification reads your orders, which needs an **Admin API** token with the
`read_orders` scope. This is a server-side secret — it lives only in Railway's
env vars, never in the website.
- Create/extend a custom app for the store, enable **Admin API** access with
  `read_orders`, install it, and copy the **Admin API access token** (`shpat_…`).
- Put it in `SHOPIFY_ADMIN_TOKEN`. This is a different credential from the public
  Storefront token the website uses.

> Set `REQUIRE_VERIFIED=false` if you'd rather let anyone post and only show a
> "Verified" badge on matched buyers, instead of blocking unverified submissions.

## 3. Photos (Cloudflare R2)
1. Create an R2 bucket (e.g. `b2-reviews`) and enable public access (or attach a
   custom domain).
2. Create an R2 API token (access key + secret).
3. Fill in `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, and
   `R2_PUBLIC_BASE` (the bucket's public URL).

## 4. Lock down origins
Set `ALLOWED_ORIGINS` to your real site origin before launch so only your site can
post reviews.

## 5. Connect the website
Once deployed, you'll have an API URL like `https://b2-reviews.up.railway.app`.
Hand that URL over and the site's review section gets wired to:
- `GET {API}/api/reviews` to display approved reviews + the average
- `POST {API}/api/reviews` to submit new ones (with photos)

## Local run
```
pip install -r requirements.txt
cp .env.example .env   # fill in values; without Postgres it falls back to SQLite
flask --app app run --port 8000   # or: python app.py
```
