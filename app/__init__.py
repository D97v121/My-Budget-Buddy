from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session as ServerSession
from flask_wtf.csrf import CSRFProtect
from flask_login import LoginManager
from flask_migrate import Migrate
import logging
from sqlalchemy import event
from sqlalchemy.engine import Engine
from datetime import timedelta
from pathlib import Path
from sqlalchemy.exc import IntegrityError
import os
from sqlalchemy import inspect
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash
from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions
from plaid.model.sandbox_public_token_create_request_options_transactions import SandboxPublicTokenCreateRequestOptionsTransactions

load_dotenv()  # will pick up the same .env in dev
# Initialize extensions
db = SQLAlchemy()
csrf = CSRFProtect()
login_manager = LoginManager()
server_session = ServerSession()
migrate = Migrate()


def create_app():
    app = Flask(__name__)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    # App config
    DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")
    os.makedirs(DATA_DIR, exist_ok=True)

    db_url = os.getenv('DATABASE_URL', 'sqlite:///money.db')
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if not db_url.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 280,
        }
    app.config['SECRET_KEY'] = 'super-secret-key'
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config["SESSION_PERMANENT"] = False
    app.config["SESSION_USE_SIGNER"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
    
    print("DB URI AT STARTUP:", app.config.get("SQLALCHEMY_DATABASE_URI"))

    # Initialize extensions
    from app.models import User
    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    server_session.init_app(app)
    migrate.init_app(app, db)

    login_manager.login_view = 'auth.login'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # ✅ Register Jinja filters from helpers
    from app.helpers import usd, timestamp_editor
    app.jinja_env.filters["usd"] = usd
    app.jinja_env.filters["timestamp_editor"] = timestamp_editor

    # ✅ Register blueprints/routes
    from app.routes import register_routes
    register_routes(app)

    from app.health import bp as health_bp
    app.register_blueprint(health_bp)

    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        if 'sqlite' in app.config.get('SQLALCHEMY_DATABASE_URI', ''):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.close()

    def _bootstrap_db(app):
        with app.app_context():
            insp = inspect(db.engine)
            if "user" not in insp.get_table_names():
                db.create_all()

            username = os.getenv("BOOTSTRAP_USERNAME", "demo")
            password = os.getenv("BOOTSTRAP_PASSWORD", "demo123")

            existing = User.query.filter_by(username=username).first()
            if existing:
                return

            u = User(username=username, name="Demo User")
            # prefer model helper if present, else set hash directly
            if hasattr(u, "set_password") and callable(getattr(u, "set_password")):
                u.set_password(password)
            else:
                u.hash = generate_password_hash(password)

            db.session.add(u)
            try:
                db.session.commit()
                print(f"[bootstrap] Created demo user: {username}/{password}")
            except IntegrityError:
                db.session.rollback()
                print("[bootflask --app wsgi runstrap] User already exists; skipped")

    # in create_app() **after** db.init_app(app):
    _bootstrap_db(app)
    _ensure_demo_user(app)
    _ensure_demo_plaid_token(app)

    # Health check: simple and cheap
    @app.get("/healthz")
    def healthz():
        return "ok", 200


    return app

def _ensure_demo_user(app):
    """Create a demo user once, if missing. Safe to call every boot."""
    from app import db
    from app.models.user import User  # adjust import if your path differs

    demo_username = os.getenv("DEMO_USERNAME", "demo")
    demo_password = os.getenv("DEMO_PASSWORD", "demo123")

    with app.app_context():
        # create tables if they don't exist (harmless if they do)
        db.create_all()

        if User.query.filter_by(username=demo_username).first():
            return  # already there

        u = User(username=demo_username, name="Demo User")
        # your model uses 'hash' for the password hash:
        u.hash = generate_password_hash(demo_password)

        db.session.add(u)
        try:
            db.session.commit()
            print(f"[seed] Demo user created: {demo_username}/{demo_password}")
        except IntegrityError:
            db.session.rollback()
            print("[seed] Demo user already exists; skipped")


def _ensure_demo_plaid_token(app):
    """Auto-seed a fresh Plaid sandbox token for the demo user on every boot."""
    from app.models.user import User
    from app.models.plaid import PlaidItem
    from app.models import Transaction
    from app.plaid_helpers import client, fetch_institution_name
    from app.routes.api_transactions import get_account_balance
    from app.encryption_utils import encrypt_data
    from app.helpers import classify_transaction_amount, edit_transaction_name
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
    from plaid.model.products import Products
    from datetime import datetime, timedelta, date

    demo_username = os.getenv("DEMO_USERNAME", "demo")
    ALLOWED_SUBTYPES = {'checking', 'savings', 'credit card'}

    with app.app_context():
        user = User.query.filter_by(username=demo_username).first()
        if not user:
            print("[plaid-seed] Demo user not found, skipping.")
            return

        existing = PlaidItem.query.filter_by(user_id=user.id).first()
        if existing:
            txn_count = Transaction.query.filter_by(user_id=user.id).count()
            if txn_count > 0:
                print(f"[plaid-seed] Demo user already has {txn_count} transactions, skipping.")
                return
            plaid_item = existing
            print("[plaid-seed] PlaidItem exists but no transactions — will sync.")
        else:
            try:
                public_token_response = client.sandbox_public_token_create(
                    SandboxPublicTokenCreateRequest(
                        institution_id='ins_109508',
                        initial_products=[Products('transactions')]
                    )
                )
                public_token = public_token_response.public_token

                exchange_response = client.item_public_token_exchange(
                    ItemPublicTokenExchangeRequest(public_token=public_token)
                ).to_dict()

                access_token = exchange_response['access_token']
                item_id = exchange_response['item_id']

                plaid_item = PlaidItem(
                    user_id=user.id,
                    item_id=item_id,
                    access_token=encrypt_data(access_token),
                    institution_id='ins_109508',
                    institution_name='First Platypus Bank'
                )
                db.session.add(plaid_item)
                db.session.commit()
                print("[plaid-seed] Sandbox token seeded for demo user.")

            except Exception as e:
                print(f"[plaid-seed] Failed to seed Plaid token: {e}")
                return

        try:
            access_token = plaid_item.decrypted_access_token
            new_count = 0
            offset = 0
            start_date = date.today() - timedelta(days=730)
            end_date = date.today()

            # Plaid sandbox needs a moment to prepare transactions
            import time
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    print(f"[plaid-seed] Waiting for Plaid to prepare transactions (attempt {attempt + 1})...")
                    time.sleep(10)

                    while True:
                        txn_request = TransactionsGetRequest(
                            access_token=access_token,
                            start_date=start_date,
                            end_date=end_date,
                            options=TransactionsGetRequestOptions(
                                count=500,
                                offset=offset
                            )
                        )
                        txn_response = client.transactions_get(txn_request).to_dict()
                        transactions = txn_response.get('transactions', [])

                        if not transactions:
                            break

                        # Build account map so we can look up names by account_id
                        account_details = get_account_balance(access_token)
                        account_map = {
                            acct['account_id']: acct['name']
                            for acct in account_details
                        }


                        for txn in transactions:
                            txn_id = txn['transaction_id']
                            if Transaction.query.filter_by(transaction_id=txn_id).first():
                                continue

                            date_str = str(txn.get('date', ''))
                            try:
                                parsed_timestamp = datetime.fromisoformat(f"{date_str}T12:00:00")
                            except Exception:
                                parsed_timestamp = datetime.utcnow()

                            new_txn = Transaction(
                                user_id=user.id,
                                transaction_id=txn_id,
                                date=txn['date'],
                                timestamp=parsed_timestamp,
                                name=edit_transaction_name(txn['name']),
                                division='none',
                                amount=classify_transaction_amount(txn),
                                account_id=txn.get('account_id'),
                                bank_account=account_map.get(txn.get('account_id'), 'First Platypus Bank'),
                                bank_name='First Platypus Bank',
                                item_id=plaid_item.item_id,
                                pending=txn.get('pending', False)
                            )
                            db.session.add(new_txn)
                            new_count += 1

                        offset += len(transactions)
                        if offset >= txn_response.get('total_transactions', 0):
                            break

                    # If we got here without an exception, break the retry loop
                    break

                except Exception as e:
                    if 'PRODUCT_NOT_READY' in str(e):
                        print(f"[plaid-seed] Product not ready, retrying...")
                        offset = 0  # reset offset for next attempt
                        continue
                    else:
                        raise  # re-raise non-retryable errors

            db.session.commit()
            print(f"[plaid-seed] Synced {new_count} transactions for demo user.")

            # Create opening balances
            bank_name = fetch_institution_name(access_token)
            accounts = get_account_balance(access_token)
            for account in accounts:
                if str(account.get('subtype', '')).lower() not in ALLOWED_SUBTYPES:
                    continue
                account_id = account['account_id']
                account_name = account['name']
                current_balance = account['balances']['current']

                if Transaction.query.filter_by(
                    user_id=user.id,
                    account_id=account_id,
                    name="Opening Balance"
                ).first():
                    continue

                oldest_txn = Transaction.query.filter_by(
                    user_id=user.id,
                    account_id=account_id
                ).order_by(Transaction.date.asc()).first()

                opening_date = (oldest_txn.date - timedelta(days=1)) if oldest_txn else date.today()

                db.session.add(Transaction(
                    user_id=user.id,
                    transaction_id=f"opening-{account_id}",
                    date=opening_date,
                    timestamp=datetime.utcnow(),
                    name="Opening Balance",
                    division="balance",
                    amount=current_balance,
                    account_id=account_id,
                    bank_account=account_name,
                    bank_name=bank_name,
                    item_id=plaid_item.item_id,
                    pending=False
                ))
            db.session.commit()
            print("[plaid-seed] Opening balances created.")

            # Run AI categorization
            from app.routes.api_transactions import _run_ai_categorization
            _run_ai_categorization(user.id)
            print("[plaid-seed] AI categorization complete.")

        except Exception as e:
            db.session.rollback()
            print(f"[plaid-seed] Failed to sync transactions: {e}")