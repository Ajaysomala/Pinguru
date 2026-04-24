# PinGuru Backend 🚀

Instagram DM Automation SaaS — FastAPI + MongoDB + Instagram Graph API

---

## 📁 Project Structure

```
pinguru/
├── app/
│   ├── main.py           # FastAPI app + lifespan
│   ├── config.py         # Pydantic settings (reads .env)
│   ├── database.py       # MongoDB Motor async client
│   ├── models/
│   │   └── models.py     # Pydantic schemas + enums
│   ├── routes/
│   │   ├── webhook.py    # Meta webhook (verify + events)
│   │   ├── auth.py       # Register, Login, Instagram OAuth
│   │   ├── automation.py # CRUD for automation rules
│   │   ├── dashboard.py  # Stats + DM logs
│   │   └── plans.py      # Plans + Razorpay checkout alias
│   └── services/
│       └── instagram.py  # Instagram Graph API calls
├── .do/app.yaml          # DigitalOcean App Platform spec
├── .env.example          # Copy to .env and fill values
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## ⚡ Local Setup

```bash
# 1. Clone repo
git clone https://github.com/YOUR_USERNAME/pinguru
cd pinguru

# 2. Create virtual env
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install deps
pip install -r requirements.txt

# 4. Copy env file and fill values
cp .env.example .env
# Edit .env with your MongoDB URI, Meta credentials, JWT secret

# 5. Run server
uvicorn app.main:app --reload --port 8000
```

API docs auto-available at: http://localhost:8000/docs

---

## 🌐 Deploy to DigitalOcean

1. Push this repo to GitHub (public or private)
2. Go to DigitalOcean → App Platform → Create App
3. Connect your GitHub repo
4. OR use the app.yaml spec: `doctl apps create --spec .do/app.yaml`
5. Add all environment variables in DO dashboard
6. Deploy → get your live URL

---

## 🔗 Meta Webhook Setup

After deploying to DO, you get a URL like:
`https://pinguru-backend-xxxxx.ondigitalocean.app`

Go to Meta Developer → Your App → Instagram → Webhooks:

- Callback URL: `https://your-do-url/webhook/instagram`
- Verify Token: whatever you set in `META_WEBHOOK_VERIFY_TOKEN`
- Subscribe to: `messages`, `comments`

---

## 💰 Plans

| Plan    | Price | Rules               | DMs       | Contacts           |
| ------- | ----- | ------------------- | --------- | ------------------ |
| Free    | ₹0    | 5 automation flows  | Unlimited | 500 contacts/month |
| Starter | ₹199  | 15 automation flows | Unlimited | Unlimited          |
| Pro     | ₹499  | Unlimited flows     | Unlimited | Unlimited          |

Starter and Pro support monthly, quarterly, and yearly billing cycles where enabled.

---

## 🔐 Security Controls

- Cookie-first auth with `HttpOnly`, `Secure` (production), and `SameSite=Lax`
- CSRF protection for state-changing cookie-auth requests (`X-CSRF-Token` required)
- Origin allowlist checks for browser cookie-auth mutations
- Rate limiting on auth and sensitive billing/status routes
- Webhook signature verification for Meta and Razorpay events
- Security headers: CSP, HSTS (production), frame deny, nosniff, referrer-policy

### CSRF Header Requirement

For browser calls that mutate state with auth cookies, include:

`X-CSRF-Token: <value from pg_csrf cookie>`

---

## 🛡️ Security Checks

Run these regularly:

```bash
# Python dependency review
pip list --outdated

# Frontend dependency audit (from frontend repo)
npm run audit
```

---

## 📡 Key API Endpoints

| Method | Route                         | Description            |
| ------ | ----------------------------- | ---------------------- |
| GET    | /health                       | Health check           |
| POST   | /auth/register                | Create account         |
| POST   | /auth/login                   | Login                  |
| GET    | /auth/instagram/callback      | Instagram OAuth        |
| POST   | /automation/rules             | Create DM rule         |
| GET    | /automation/rules             | List rules             |
| PATCH  | /automation/rules/{id}/toggle | Enable/disable         |
| GET    | /dashboard/stats              | DM stats               |
| GET    | /dashboard/dm-logs            | DM history             |
| GET    | /plans                        | Available plans        |
| POST   | /plans/checkout/{plan}        | Razorpay checkout      |
| GET    | /plans/status                 | Current billing status |
| POST   | /plans/razorpay-webhook       | Razorpay webhook       |
| GET    | /webhook/instagram            | Meta verification      |
| POST   | /webhook/instagram            | Meta events            |
