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
    from app import db
    from app.models.user import User
    from app.models.plaid import PlaidItem
    from app.plaid_helpers import client
    from app.encryption_utils import encrypt_data
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.products import Products

    demo_username = os.getenv("DEMO_USERNAME", "demo")

    with app.app_context():
        user = User.query.filter_by(username=demo_username).first()
        if not user:
            print("[plaid-seed] Demo user not found, skipping.")
            return

        # If they already have a working token, skip
        existing = PlaidItem.query.filter_by(user_id=user.id).first()
        if existing:
            print("[plaid-seed] Demo user already has a PlaidItem, skipping.")
            return

        try:
            # Create a sandbox public token for First Platypus Bank
            public_token_response = client.sandbox_public_token_create(
                SandboxPublicTokenCreateRequest(
                    institution_id='ins_109508',  # First Platypus Bank - standard sandbox bank
                    initial_products=[Products('transactions')]
                )
            )
            public_token = public_token_response.public_token

            # Exchange it for a real access token
            exchange_response = client.item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token)
            ).to_dict()

            access_token = exchange_response['access_token']
            item_id = exchange_response['item_id']

            plaid_item = PlaidItem(
                user_id=user.id,
                item_id=item_id,
                access_token=encrypt_data(access_token),  # match your encryption
                institution_id='ins_109508',
                institution_name='First Platypus Bank'
            )
            db.session.add(plaid_item)
            db.session.commit()
            print(f"[plaid-seed] Sandbox token seeded for demo user.")

        except Exception as e:
            print(f"[plaid-seed] Failed to seed Plaid token: {e}")