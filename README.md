# Gladys Tuti ebook store

Pink and white store. Buyers pay by:

- **PayPal / card** (anywhere): automatic. The download unlocks the moment PayPal confirms.
- **Kenyan bank transfer** (KES) and **Opay** (NGN): manual. The buyer sends the money and submits a reference. You check your bank/Opay app and press **Approve** in the admin page. Then the download unlocks.

Prices are set per ebook in USD, KES and NGN.

## Run it on your computer (test)

1. `cp .env.example .env` and set `ADMIN_PASSWORD` (pick something long).
2. `pip install flask`
3. `python3 app.py`
4. Open http://127.0.0.1:5000 and log in at http://127.0.0.1:5000/admin/login

## First-time setup in the admin page

1. **Payment details**: type your Kenyan bank details and your Opay details. Whatever you leave empty is hidden at checkout.
2. **Ebooks**: open each one, check the title and description (I wrote placeholders), upload the PDF or EPUB and the cover. An ebook can't be bought until its file is uploaded.

## Turning on PayPal

1. Create a PayPal **Business** account, then go to developer.paypal.com > Apps & Credentials.
2. Test first: create a **Sandbox** app, put its Client ID and Secret in `.env` with `PAYPAL_ENV=sandbox`, and buy an ebook with a sandbox buyer account.
3. Go live: create a **Live** app, replace the two keys, set `PAYPAL_ENV=live`, restart.

## Putting it online

It needs a host that can run Python and keep files on disk (a small VPS, or a Python host such as PythonAnywhere or Render with a persistent disk). Don't use a host that wipes files on restart: your uploaded ebooks, orders and settings live in the `data/` folder.

- Run it with a real server instead of `python3 app.py`, for example `gunicorn -w 2 app:app`.
- Put it behind https, then set `COOKIE_SECURE=1` and `SITE_URL=https://your-domain` in `.env`.
- **Back up the `data/` folder regularly.** It holds everything.
- Never share `.env` or `data/secret.key`.

## Emails (optional)

Fill in the `SMTP_*` lines in `.env` and the download link is emailed when a payment is confirmed. `ADMIN_EMAIL` also emails you when someone says they've paid by bank or Opay. Without SMTP it still works: buyers get the link on their order page, and in Admin > Recent orders you can open any paid order's page and send the link to the buyer yourself (for example on WhatsApp).

## How safe is it

- Ebook files are stored outside the public folder and only download through a private, unguessable link after payment is confirmed.
- PayPal payments are confirmed on the server with PayPal, and the amount captured must match the price, so a buyer can't pay less.
- Admin has a password, login attempts are limited, and every form is protected against forged requests.
- Bank/Opay: only approve what you can actually see in your account. A screenshot alone is not proof.

## Changing things later

- Add a new ebook: Admin > Add an ebook.
- Replace a file: Edit it and upload a new one. Past buyers' links serve the new file.
- Hide an ebook: untick "Show on the store".

## Railway

1. Put this folder in a GitHub repo (do NOT commit `.env` or `data/`), then in Railway: New Project > Deploy from GitHub repo. Railway reads `requirements.txt` and `Procfile` by itself.
2. **Add a Volume** (service > Settings > Volumes) and mount it at `/data`. Without this, Railway deletes your ebooks and orders every time it redeploys.
3. Service > Variables, add:
   - `DATA_DIR` = `/data`
   - `ADMIN_PASSWORD` = a long password
   - `COOKIE_SECURE` = `1`
   - `SITE_URL` = your Railway or custom domain, e.g. `https://yourstore.up.railway.app`
   - later: `PAYPAL_ENV`, `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, and the `SMTP_*` lines if you want emails
4. Service > Settings > Networking > Generate Domain (or add your own domain).
5. Open `your-domain/admin/login` and set up as in the steps above.
