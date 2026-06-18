# My-Budget-Buddy (MBB)

## What & Why
My-Budget-Buddy is a full-stack personal finance web app built for teens and college students. It connects to real bank accounts via the Plaid API, automatically categorizes transactions using AI, and helps users visualize and manage their spending across budget categories (Save, Spend, Give, Invest). It also features a fully integrated AI assistant that lets users query their financial data conversationally.

**Live demo:** https://my-budget-buddy.com  
**Login:** `username: demo` · `password: demo123`

> The demo uses Plaid's sandbox environment — all bank data is simulated, no real accounts are linked. A fresh sandbox bank account with pre-loaded transactions is automatically seeded on boot.

<img width="1470" height="786" alt="Screenshot 2026-06-18 at 10 11 16 AM" src="https://github.com/user-attachments/assets/f8956737-82e7-4e47-920a-1971abf4f1c3" />

## Tech
Flask · SQLAlchemy · PostgreSQL (Supabase) · DigitalOcean App Platform · Plaid API · OpenAI API

## Highlights
- **Plaid integration:** full OAuth bank linking flow, transaction sync with cursor-based pagination, encrypted access token storage, and auto-seeded sandbox demo account
- **AI-powered categorization:** transactions are automatically categorized into budget divisions and tagged on sync using OpenAI
- **AI chat assistant:** conversational interface lets users query their own financial data in natural language
- **Productionized Flask:** WSGI entrypoint (`wsgi.py`) + Procfile + Gunicorn, health check route, idempotent DB bootstrap
- **Security:** secrets via env vars, CSRF protection, HTTPOnly/SameSite cookies, Fernet encryption for Plaid tokens
- **Clean architecture:** Blueprints for routes/services, Alembic migrations, PostgreSQL in production

## Quick Start (demo mode — no Plaid setup needed)
```bash
git clone github.com/D97v121/My-Budget-Buddy.git
cd My-Budget-Buddy && python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
flask --app wsgi run --debug
```
A demo user (`demo` / `demo123`) and a sandbox Plaid account with transactions are seeded automatically on first run.

## Roadmap
- Add pytest coverage for routes and services
- Expand charts and insights (trends, cash flow, month-over-month comparisons)
- Push notifications for budget thresholds
- Mobile-responsive design improvements

## Project Structure

├── .gitignore  
├── .python-version  
├── .vscode/  
├── app/  
│   ├── __init__.py  
│   ├── ai_helpers.py  
│   ├── encryption_utils.py  
│   ├── filters.py  
│   ├── forms.py  
│   ├── health.py  
│   ├── helpers.py  
│   ├── models/  
│   ├── plaid_helpers.py  
│   ├── routes/  
│   ├── static/  
│   └── templates/   
├── instance/  
│   └── money.db  
├── main.py  
├── migrations/  
│   ├── alembic.ini  
│   ├── env.py  
│   ├── README  
│   ├── script.py.mako  
│   └── versions/  
├── models.py  
├── my_budget_buddy.db  
├── Procfile   
├── requirements.txt     
└── wsgi.py  
