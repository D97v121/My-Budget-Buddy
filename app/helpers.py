import csv
import datetime
import pytz
import requests
import urllib
import uuid
import os
import time
import re
import datetime as dt
from flask import current_app
import json
import logging
from openai import OpenAI
from flask_sqlalchemy import SQLAlchemy
from decimal import Decimal
from cryptography.fernet import Fernet
from plaid.exceptions import ApiException
from flask_login import current_user

import base64

from flask import redirect, render_template, request, session
from functools import wraps
from app import db
from app.models import Save, Give, Spend, Invest, Money, Expense, Tags, TagColor, Transaction

model_map = {
    "save": Save,
    "spend": Spend,
    "give": Give,
    "invest": Invest,
    "expense": Expense
}

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def apology(message, code=400):
    """Render message as an apology to user."""

    def escape(s):
        """
        Escape special characters.

        https://github.com/jacebrowning/memegen#special-characters
        """
        for old, new in [
            ("-", "--"),
            (" ", "-"),
            ("_", "__"),
            ("?", "~q"),
            ("%", "~p"),
            ("#", "~h"),
            ("/", "~s"),
            ('"', "''"),
        ]:
            s = s.replace(old, new)
        return s

    return render_template("apology.html", top=code, bottom=escape(message)), code


def login_required(f):
    """
    Decorate routes to require login.

    https://flask.palletsprojects.com/en/latest/patterns/viewdecorators/
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.id is None:
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated_function


def lookup(symbol):
    """Look up quote for symbol."""

    # Prepare API request
    symbol = symbol.upper()
    end = datetime.datetime.now(pytz.timezone("US/Eastern"))
    start = end - datetime.timedelta(days=7)

    # Yahoo Finance API
    url = (
        f"https://query1.finance.yahoo.com/v7/finance/download/{urllib.parse.quote_plus(symbol)}"
        f"?period1={int(start.timestamp())}"
        f"&period2={int(end.timestamp())}"
        f"&interval=1d&events=history&includeAdjustedClose=true"
    )

    # Query API
    try:
        response = requests.get(
            url,
            cookies={"session": str(uuid.uuid4())},
            headers={"Accept": "*/*", "User-Agent": request.headers.get("User-Agent")},
        )
        response.raise_for_status()

        # CSV header: Date,Open,High,Low,Close,Adj Close,Volume
        quotes = list(csv.DictReader(response.content.decode("utf-8").splitlines()))
        price = round(float(quotes[-1]["Adj Close"]), 2)
        return {"price": price, "symbol": symbol}
    except (KeyError, IndexError, requests.RequestException, ValueError):
        return None

def classify_transaction_amount(txn):
    """
    Returns a reversed-sign amount based on Plaid's direction logic and account type.
    This assumes your system treats incoming money as negative (e.g., credit) and spending as positive.
    """
    raw_amount = txn.get("amount", 0)
    direction = (txn.get("direction") or "").upper()
    account_type = (txn.get("account_type") or "").lower()  # e.g., 'credit', 'depository'

    # Safety check
    if direction not in {"INFLOW", "OUTFLOW"}:
        print(f"[warning] Unknown direction for transaction: {txn.get('name')}")
        return -raw_amount  # fallback: reversed sign just in case

    if account_type == "credit":
        # For credit cards:
        if direction == "OUTFLOW":
            return -abs(raw_amount)  # card charge → negative
        elif direction == "INFLOW":
            return abs(raw_amount)   # payment/refund → positive
    else:
        # For checking, savings, and debit:
        if direction == "OUTFLOW":
            return abs(raw_amount)   # money out → positive
        elif direction == "INFLOW":
            return -abs(raw_amount)  # money in → negative

    return -raw_amount  # fallback




def usd(value):
    if not value:
        value = 0
    if value >= 0:
        return "${:,.2f}".format(abs(value))
    else:
        return "-${:,.2f}".format(abs(value))
    
def exit_usd(usd_string):
    # Keep the negative sign if it exists at the beginning
    cleaned_string = re.sub(r'[^\d.-]', '', usd_string)
    return float(cleaned_string)
    
def dollar(value):
    if value is None:
        value = 0
    if value >= 1000 or value <= -1000:
        return f"${value:,.0f}"
    else:
        return usd(value)

def timestamp_editor(value):
    
    return value.strftime("%m/%d/%Y")

def initialize_money_record(db):
    user_id = current_user.id
    money_record = db.session.query(Money).filter_by(user_id=user_id).first()
    if money_record is None:
        logging.debug("No existing money record found. Creating a new one.")
        money_record = Money(user_id=user_id, save=0, spend=0, give=0, expense=0)
        db.session.add(money_record)
        db.session.commit()
    return money_record

def calculate_totals_by_division(db, Transaction, user_id, division):
    # Calculate the total amount for transactions with the specified division
    total = (
        db.session.query(Transaction).query.with_entities(db.func.sum(Transaction.amount))
        .filter_by(user_id=user_id, division=division)
        .scalar()
    )
    return total or 0

def cycle_through_money_table(db, money_record):
    if money_record is None:
        logging.error("money_record is None in cycle_through_money_table")
        return

    user_id = current_user.id
    if user_id is None:
        logging.error("User ID is None. Cannot proceed.")
        return

    divisions = ["save", "spend", "give", "invest", "expense"]

    for division in divisions:
        logging.debug(f"Processing transactions for division: {division}")

        # Calculate total amount for the division
        total = calculate_totals_by_division(db, Transaction, user_id, division)

        # Update the money record
        setattr(money_record, division, total)
        logging.debug(f"Updated {division} in money_record to {total}")

    db.session.commit()
    logging.debug("Money record updated successfully")


def calculateDivision(db, Transaction, label, money, bank_name, tag_objects, division="none", date=dt.datetime.now(), description="none"):
    with current_app.app_context():
        logging.debug(f"Calculating division: {label} with amount: {money} with date: {date}")
        logging.debug(f"division: { division } ")
        user_id = current_user.id
        if user_id is None:
            logging.error("User ID is None. Cannot proceed with calculation.")
            return
        
        logging.debug(f"User ID: {user_id}")
        try:  
            logging.debug(f"No existing division record found, creating a new one")
            if tag_objects:
                unique_tags = list({tag.id: tag for tag in tag_objects}.values())  # Filter duplicates
            else:
                unique_tags = []
            if date:
                date=date
            else:
                date=dt.datetime.now()
            if isinstance(date, str):
                date = datetime.strptime(date, '%m/%d/%Y')
            new_record = Transaction(user_id=user_id, amount=money, bank_name=bank_name, timestamp=dt.datetime.now(), date=date, division=division, note=description)
            if unique_tags:
                tag_ids = [tag.id for tag in tag_objects]
                unique_tags = Tags.query.filter(Tags.id.in_(tag_ids)).all()
            else:
                unique_tags = []

            # Assign unique tags to the transaction
            db.session.add(new_record)
            db.session.flush()  # get the new_record.id before commit

            # ← ADD THESE TWO LINES
            for tag in unique_tags:
                if tag not in new_record.tags:
                    new_record.tags.append(tag)

            db.session.commit()
            
            money_record = initialize_money_record(db)
            
            logging.debug(f"Updating existing money record: { money_record }")

            granularity = '%Y-%m-%d %H:%M:%S'
            _, _, last_record = process_data(granularity, Transaction)
        
            setattr(money_record, division, last_record)
            db.session.commit()

            logging.debug("Transaction committed successfully")

        except Exception as e:
            db.session.rollback()
            logging.error(f"Transaction failed and rolled back: {e}")
            raise

#calculate where money goes and what percentage goes there
def calculateAllMoney(db, Transaction, tag_objects, money, division, date, bank_name, user, description='none'):
    logging.debug("Calculating all money allocations")
    money = Decimal(money)

    if not user:
        raise ValueError("User not found.")
    savePercentage = (user.savePercentage or Decimal('0')) * Decimal('0.01')
    givePercentage = (user.givePercentage or Decimal('0')) * Decimal('0.01')
    spendPercentage = (user.spendPercentage or Decimal('0')) * Decimal('0.01')
    investPercentage = (user.investPercentage or Decimal('0')) * Decimal('0.01')
    expensePercentage = (user.expensePercentage or Decimal('0')) * Decimal('0.01')

    save_amount = money * savePercentage
    give_amount = money * givePercentage
    spend_amount = money * spendPercentage
    invest_amount = money * investPercentage
    expense_amount = money * expensePercentage
    

    logging.debug(f"Save amount: {save_amount}")
    logging.debug(f"Give amount: {give_amount}")
    logging.debug(f"Spend amount: {spend_amount}")
    logging.debug(f"Invest amount: {invest_amount}")
    logging.debug(f"Expense amount: {expense_amount}")

    calculateDivision(db, Transaction, 'save', save_amount, bank_name=bank_name, division="save", tag_objects=tag_objects, date=date, description=description)
    calculateDivision(db, Transaction, 'give', give_amount, bank_name=bank_name, division="give", tag_objects=tag_objects, date=date, description=description)
    calculateDivision(db, Transaction, 'spend', spend_amount, bank_name=bank_name, division="spend", tag_objects=tag_objects, date=date, description=description)
    calculateDivision(db, Transaction, 'invest', invest_amount, bank_name=bank_name, division="invest", tag_objects=tag_objects, date=date, description=description)
    calculateDivision(db, Transaction, 'expense', expense_amount, bank_name=bank_name, division="expense", tag_objects=tag_objects, date=date, description=description)

def process_data(granularity, Transaction):
        user_id = current_user.id
        amounts_query = Transaction.query.with_entities(Transaction.amount, Transaction.timestamp).order_by(Transaction.timestamp).filter_by(user_id=user_id).all()
        data = []
        unique_dates = set()
        cumulative = 0
        previous_date = None

        for amount, timestamp in amounts_query:
            date_str = timestamp.strftime(granularity)
            unique_dates.add(date_str)

            if previous_date is not None and date_str != previous_date:
                data.append({
                    'date': previous_date,
                    'cumulative_amount': cumulative
                })

            cumulative += amount
            previous_date = date_str

        if previous_date is not None:
            data.append({
                'date': previous_date,
                'cumulative_amount': cumulative
            })

        last_record = data[-1]['cumulative_amount'] if data else None
        
        

        return data, unique_dates, last_record 

    
def graph_records(Transaction):
    # Step 1: Check unique dates by day
    granularity = '%Y-%m-%d'
    data, unique_dates,_ = process_data(granularity, Transaction)

    # Step 2: If fewer than 5 unique days, check by date and time
    if len(unique_dates) < 4:
        granularity = '%Y-%m-%d %H:%M:%S'
        data, unique_dates,_ = process_data(granularity, Transaction)

    # Step 3: If more than 60 unique days, check by months
    elif len(unique_dates) > 60:
        granularity = '%Y-%m'
        data, unique_dates,_ = process_data(granularity, Transaction)


    cumulative_float = [float(entry['cumulative_amount']) for entry in data]
    unique_dates = sorted(list(unique_dates))

    return cumulative_float, unique_dates


def delete_record(db, record_id, Transaction):
    # Replace Money with the appropriate model if necessary
    try:
        user_id = current_user.id
        logging.debug(f"delete record: {record_id}")
        record = db.session.query(Transaction).filter_by(id=record_id, user_id=user_id).first()
        db.session.delete(record)
        logging.debug(f"delete successful")
        db.session.commit()
        
    except Exception as e:
        db.session.rollback()
        logging.debug(f"delete not successful {e}")
        return str(e), 500

def populate_tags(db, user_id):
    logging.debug(f"populating table")
    new_tag_save = Tags(name='Save', color_id='#506680', status=True, user_id=user_id)
    new_tag_spend = Tags(name='Spend', color_id='#A1AAB3', status=True, user_id=user_id)
    new_tag_give = Tags(name='Give', color_id='#2E3D4B', status=True, user_id=user_id)
    new_tag_invest = Tags(name='Invest', color_id='#CBD7E3', status=True, user_id=user_id)
    new_tag_expense = Tags(name='Expense', color_id='#395B75', status=True, user_id=user_id)

    tags_to_add = [new_tag_save, new_tag_spend, new_tag_give, new_tag_invest, new_tag_expense]
    db.session.add_all(tags_to_add)
    db.session.commit()
    logging.debug(f"Tags populated for user ID: {user_id}")



def edit_transaction_name(original_name: str) -> str:
    """
    Uses OpenAI to clean up Plaid transaction names.
    Example: "Purchase TST*ZINQUE MALIBU" → "Zinque Malibu"
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # or gpt-3.5-turbo if cost/speed is a factor
            messages=[
                {"role": "system", "content": "You are an assistant that cleans up messy bank transaction names to make them short, clean, and human-readable. Always remove noise like 'Purchase', 'TST*', 'FD*', etc. Capitalize the result properly. Respond with only the cleaned-up name."},
                {"role": "user", "content": f"Original: {original_name}"}
            ],
            max_tokens=30,
            temperature=0.2
        )
        cleaned_name = response.choices[0].message.content.strip()
        return cleaned_name
    except Exception as e:
        print(f"OpenAI name cleanup failed: {e}")
        return original_name  # fallback



def format_error(error):
    """
    Takes in an Exception or error object and returns a clean string message.
    Can handle both string and object errors.
    """
    if isinstance(error, str):
        return error
    elif isinstance(error, Exception):
        return f"{type(error).__name__}: {str(error)}"
    elif isinstance(error, dict):
        # For API errors or custom error formats
        return error.get("message") or str(error)
    else:
        return str(error)
    
def pretty_print_response(response):
  print(json.dumps(response, indent=2, sort_keys=True, default=str))

def poll_with_retries(fn, max_attempts=20, delay=1):
    """
    Repeatedly calls a function until it returns a successful response or hits max_attempts.
    `fn` should be a lambda that makes the Plaid API call.
    """
    for i in range(max_attempts):
        try:
            response = fn()
            return response
        except ApiException as e:
            if e.status == 400 and 'PRODUCT_NOT_READY' in str(e):
                time.sleep(delay)
            else:
                raise
    raise TimeoutError("Max retries reached. Asset report not ready.")

