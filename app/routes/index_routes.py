from flask import Blueprint, render_template, request, redirect, session,  url_for
import json
from flask_login import login_required
from app.forms import TransactionForm
from app.models import User, Transaction, Tags, Expense, Money, PlaidItem
from app.helpers import calculateDivision, calculateAllMoney
from app import db
from app.plaid_helpers import client, PLAID_ENV
from plaid.model.accounts_get_request import AccountsGetRequest
from sqlalchemy import func, extract
from decimal import Decimal
from collections import defaultdict
import logging
from flask_login import current_user
import datetime as dt
from datetime import datetime

index_bp = Blueprint('index', __name__)

model_map = {
    "save",
    "spend",
    "give",
    "invest",
    "expense",
    "transactions"
}

@index_bp.route('/', methods=["GET", "POST"])
@login_required
def index():
    user_id = current_user.id
    user = User.query.get(user_id)
    form = TransactionForm()

    logging.debug("Index route accessed")
    moneyTable = Money.query.filter_by(user_id=user_id).first()
    division = request.form.get("division")
    form_id = request.form.get('form_id')
    description = request.form.get('description')

    if request.method == "POST":
        amount = Decimal(request.form.get("recordedTransaction"))

        if form_id == "transactionForm":
            selected_tags = request.form.get('tags')
        elif form_id == "transferForm":
            selected_tags = request.form.get('tags_transfer[]')
        else:
            selected_tags = None
        print("Raw selected_tags from form:", request.form.get('tags[]'))
        if selected_tags:
            selected_tags = json.loads(selected_tags)  # Convert JSON string back to list
            print("Parsed selected_tags (after JSON):", selected_tags)
            tag_objects = Tags.query.filter(
                Tags.name.in_(selected_tags),
                Tags.user_id == user_id  # 🛡️ Optional safeguard for multi-user support
            ).all()
            
        else:
            print("No tags selected.")
            tag_objects = []

        if form_id == 'transactionForm':
            label = request.form.get("division").lower() 
            bank_name = "manual input"
            try:
                if label in model_map:
                    print(f"[calculateDivision] Tags being passed: {[tag.name for tag in tag_objects]}")
                    calculateDivision(db, Transaction, label, amount, bank_name, tag_objects, division, description=description)
                else:
                    calculateAllMoney(db, Transaction,division=division, money=amount, user=user, tag_objects=tag_objects, date=dt.datetime.now(), bank_name=bank_name, description=description)
                db.session.commit()
                logging.debug("Transaction committed successfully")
                return redirect("/History")
            except Exception as e:
                db.session.rollback()
                logging.error(f"Transaction failed and rolled back: {e}")
                return f"Transaction failed and rolled back: {e}", 500
        elif form_id == "transferForm":
            to_label = request.form.get("toDivision").lower()
            from_label = request.form.get("fromDivision").lower()
            bank_name = from_label
            try:
                if to_label in model_map and from_label in model_map:
                    print(f"[calculateDivision] Tags being passed: {[tag.name for tag in tag_objects]}")
                    calculateDivision(db, Transaction, to_label, amount, bank_name, tag_objects, division=to_label, description=description)
                    calculateDivision(db, Transaction, from_label, -amount, bank_name="none", tag_objects=tag_objects, division=from_label, description=description)
                else:
                    calculateAllMoney(db, Transaction, money=amount, division=division, user=user, tag_objects=tag_objects, date=dt.datetime.now(), bank_name=bank_name, description=description)
                    calculateDivision(db, Transaction, from_label, -amount, bank_name, tag_objects, description=description, division=division)
                db.session.commit()
                logging.debug("Transaction committed successfully")
                return redirect("/History")
            except Exception as e:
                db.session.rollback()
                logging.error(f"Transaction failed and rolled back: {e}")
                return f"Transaction failed and rolled back: {e}", 500
        else:
            print("error")

    now = datetime.now()
    current_year = now.year
    current_month = now.month

    left_in_spend = db.session.query(func.sum(Transaction.amount))\
        .filter_by(user_id=user_id, division='spend')\
        .filter(Transaction.amount > 0)\
        .filter(extract('year', Transaction.date) == current_year)\
        .filter(extract('month', Transaction.date) == current_month)\
        .scalar() or 0 

    totalSave = db.session.query(func.sum(Transaction.amount))\
        .filter_by(user_id=user_id, division='save')\
        .filter(extract('year', Transaction.date) == current_year)\
        .filter(extract('month', Transaction.date) == current_month)\
        .scalar() or 0

    totalGive = db.session.query(func.sum(Transaction.amount))\
        .filter_by(user_id=user_id, division='give')\
        .filter(extract('year', Transaction.date) == current_year)\
        .filter(extract('month', Transaction.date) == current_month)\
        .scalar() or 0

    totalInvest = db.session.query(func.sum(Transaction.amount))\
        .filter_by(user_id=user_id, division='invest')\
        .filter(extract('year', Transaction.date) == current_year)\
        .filter(extract('month', Transaction.date) == current_month)\
        .scalar() or 0

    total_spend = db.session.query(func.sum(func.abs(Transaction.amount)))\
        .filter_by(user_id=user_id, division='spend')\
        .filter(extract('year', Transaction.date) == current_year)\
        .filter(extract('month', Transaction.date) == current_month)\
        .scalar() or 0
    spend_tag_color = Tags.query.filter_by(user_id=user_id, name='Spend').first()
    spending_color = spend_tag_color.color_id if spend_tag_color else '#BBA2C8'

    
    
    logging.debug(f"Spend tag color from DB: {spending_color}")


    expense_records = Expense.query.filter_by(user_id=user_id).all()
    tag_counter = defaultdict(float)
    for record in expense_records:
        tag_counter[record.tag] += float(record.amount)

    tags_list = Tags.query.filter_by(user_id=user_id, status=True).all()
    tag_color_map = {tag.name: tag.color_id for tag in tags_list if tag.name}

    tag_details = {}
    for tag, count in tag_counter.items():
        if tag in tag_color_map:
            tag_details[tag] = {
                'count': count,
                'color': tag_color_map.get(tag, '#FFFFFF')  # Default color if not in tag_color_map
            }

    colors = [details['color'] for details in tag_details.values()]
    counts = [details['count'] for details in tag_details.values()]
    tag_names = [tag if tag else 'none' for tag in tag_details.keys()]

    plaid_items = PlaidItem.query.filter_by(user_id=user_id).all()

    # Add account balances
    account_balances = []
    for item in plaid_items:
        access_token = item.decrypted_access_token
        def _token_env(token: str) -> str:
            try:
                return token.split("-")[1]
            except Exception:
                return "unknown"

        # ... inside index(), right BEFORE calling accounts_get
        print("[PlaidDebug] APP_ENV:", PLAID_ENV)

        # however you load the token today:
        # access_token = <from DB or session>

        src = "session" if session.get("access_token") else "db"
        print("[PlaidDebug] TOKEN_SRC:", src, "TOKEN_PREFIX:", access_token[:26], "TOKEN_ENV:", _token_env(access_token))

        expected = PLAID_ENV.lower()
        if _token_env(access_token) != expected:
            # purge everywhere we might have stashed it
            session.pop("access_token", None)

            item = (PlaidItem.query
                    .filter_by(user_id=current_user.id)
                    .filter(PlaidItem.access_token == access_token)
                    .first())

            if item:
                db.session.delete(item)   # <-- delete row instead of setting NULL
                db.session.commit()

            return redirect(url_for("plaid.create_link_token"))  # relink in prod

        try:
            accounts = client.accounts_get(AccountsGetRequest(access_token=access_token)).to_dict()['accounts']
            print("PLAID_ENV:", PLAID_ENV)
            print("ACCESS TOKEN:", access_token[:40])
            print("TOKEN ENV:", access_token.split("-")[1])
            ALLOWED_SUBTYPES = {'checking', 'savings', 'credit card'}
            for acct in accounts:
                if str(acct['subtype']) in ALLOWED_SUBTYPES:
                    account_balances.append({
                        'name': acct['name'],
                        'available': acct['balances']['available'],
                        'current': acct['balances']['current'],
                        'currency': acct['balances']['iso_currency_code']
                    })
        except Exception as e:
            logging.error(f"Plaid accounts_get failed for item {item.id}: {e}")
            # Remove the broken token so user can re-link
            db.session.delete(item)
            db.session.commit()
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    money_gained_this_month = db.session.query(func.sum(Transaction.amount))\
        .filter(
            Transaction.user_id == user_id,
            Transaction.amount > 0,
            extract('year', Transaction.date) == current_year,
            extract('month', Transaction.date) == current_month
        ).scalar() or 0
    money_lost_this_month = db.session.query(func.sum(Transaction.amount))\
        .filter(
            Transaction.user_id == user_id,
            Transaction.amount < 0,
            extract('year', Transaction.date) == current_year,
            extract('month', Transaction.date) == current_month
        ).scalar() or 0  
    
    has_linked_account = len(account_balances) > 0
    has_spend_transaction = Transaction.query.filter_by(user_id=user_id, division='spend').first() is not None

    show_graphs = has_linked_account or has_spend_transaction
    show_link_section = not show_graphs
        
    return render_template("index.html", show_graphs=show_graphs, show_link_section=show_link_section, form=form, account_balances=account_balances, moneyTable=moneyTable, left_in_spend=left_in_spend, tags_list=tags_list, colors=colors, counts=counts, tag_names=tag_names, spending_color=spending_color, total_spend=total_spend, totalSave=totalSave, totalGive=totalGive, totalInvest=totalInvest, money_gained_this_month=money_gained_this_month,
    money_lost_this_month=money_lost_this_month)

