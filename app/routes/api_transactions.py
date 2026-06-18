from flask import Blueprint, jsonify, session, request as flask_request
from flask_login import login_required
from app import db
from sqlalchemy import func
from app.plaid_helpers import client
from app.models import Transaction, PlaidItem, Tags
from app.helpers import (
    classify_transaction_amount,
    edit_transaction_name,
    format_error
)
from app.plaid_helpers import fetch_institution_name, get_accounts
from datetime import timedelta
from datetime import datetime
import logging
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.exceptions import ApiException
from flask_wtf.csrf import generate_csrf
from flask_login import current_user
import time
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

transactions_api = Blueprint('transactions_api', __name__)

def get_account_balance(access_token):
    request = AccountsBalanceGetRequest(access_token=access_token)
    response = client.accounts_balance_get(request).to_dict()
    return response['accounts']

@transactions_api.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    all_added = []
    all_modified = []
    all_removed = []
    new_transactions_count = 0
    duplicate_transactions_count = 0
    ALLOWED_SUBTYPES = {'checking', 'savings', 'credit card'}
    print("==> Entered /api/transactions route")

    try:
        user_id = current_user.id
        print(f"==> Retrieved user_id: {user_id}")
        if not user_id:
            print("==> No user_id in session")
            return jsonify({"error": "User not logged in"}), 401

        item_id_filter = flask_request.args.get('item_id')
        accounts_map = {}
        plaid_items = PlaidItem.query.filter_by(user_id=user_id).all()
        if item_id_filter:
            plaid_items = [item for item in plaid_items if item.item_id == item_id_filter]
            print(f"==> Filtering to Plaid item: {item_id_filter}")
        print(f"==> Retrieved {len(plaid_items)} Plaid items for user {user_id}")
        if not plaid_items:
            print("==> No Plaid items found")
            return jsonify({"error": "No access tokens found for user"}), 400

        for plaid_item in plaid_items:
            access_token = plaid_item.decrypted_access_token
            cursor = plaid_item.cursor or ''
            print(f"==> Starting sync for access_token {access_token[:6]}..., cursor: {cursor}")

            bank_name = fetch_institution_name(access_token)
            account_details = get_accounts(access_token)
            print(f"==> Got account details for access_token {access_token[:6]}")

            if account_details is None:
                print(f"==> No accounts found for access token {access_token[:6]}")
                continue

            new_accounts = {
                account['account_id']: {
                    "account_name": account['name'],
                    "bank_name": bank_name,
                    "subtype": account.get('subtype', '').lower()
                }
                for account in account_details['accounts']
            }
            accounts_map.update(new_accounts)

            max_retries = 10
            retry_delay = 3  # seconds

            has_more = True
            attempt = 0

            while has_more:
                print(f"==> Fetching transactions with cursor: {cursor}")
                try:
                    request = TransactionsSyncRequest(
                        access_token = plaid_item.decrypted_access_token,
                        cursor=cursor,
                    )
                    response = client.transactions_sync(request).to_dict()
                    print(f"==> Raw sync response: {response}")

                    # Wait for Plaid to finish preparing transactions
                    if response.get('transactions_update_status') == 'NOT_READY':
                        if attempt >= max_retries:
                            print("==> Max retries reached, exiting.")
                            return jsonify({"status": "pending", "message": "Transactions not ready yet."}), 202
                        attempt += 1
                        print(f"==> Transactions not ready. Retrying in {retry_delay}s (attempt {attempt}/{max_retries})")
                        time.sleep(retry_delay)
                        continue  # Try again
                    cursor = response.get('next_cursor', '')
                    plaid_item.cursor = cursor
                    db.session.add(plaid_item)

                    has_more = response.get('has_more', False)
                    all_added.extend(response.get('added', []))
                    all_modified.extend(response.get('modified', []))
                    all_removed.extend(response.get('removed', []))

                except ApiException as e:
                    print(f"==> Plaid API error: {e}")
                    raise
            db.session.commit()
            print(f"==> Committed updated cursor for {access_token[:6]}")

        for transaction in all_added:
            print(f"==> Processing transaction: {transaction['transaction_id']}")
            account_id = transaction.get('account_id')
            account_info = accounts_map.get(account_id, {})
            account_name = account_info.get("account_name", "Unknown Account")
            bank_name = account_info.get("bank_name", "Unknown Bank")
            account_subtype = account_info.get("subtype", "checking")
            raw_datetime = transaction.get("datetime")
            if raw_datetime:
                timestamp_str = str(raw_datetime)
            else:
                date_str = transaction.get("date", "")
                timestamp_str = f"{date_str}T12:00:00"

            try:
                parsed_timestamp = datetime.fromisoformat(timestamp_str)
            except Exception as e:
                print(f"==> Timestamp parse failed: {timestamp_str}, error: {e}")
                parsed_timestamp = datetime.utcnow()

            txn_id = transaction['transaction_id']
            existing_transaction = Transaction.query.filter_by(transaction_id=txn_id).first()

            if not existing_transaction:
                divisions = transaction.get('division', [])
                if isinstance(divisions, str):
                    divisions = [division]
                if divisions is None:
                    divisions = []
                divisions = [div.strip() for div in divisions if div.strip()]

                tag_objects = []
                for division in divisions:
                    tag = Tags.query.filter_by(name=division, user_id=user_id).first()
                    if not tag:
                        tag = Tags(name=division, user_id=user_id)
                        db.session.add(tag)
                        db.session.commit()
                    tag_objects.append(tag)

                division = ', '.join(divisions)
                amount = classify_transaction_amount(transaction)

                predicted_division = "none"

                new_transaction = Transaction(
                    user_id=user_id,
                    transaction_id=txn_id,
                    date=transaction['date'],
                    timestamp=parsed_timestamp,
                    name=edit_transaction_name(transaction["name"]),
                    division=predicted_division,
                    amount=amount,
                    account_id=account_id,
                    bank_account=account_name,
                    bank_name=bank_name,
                    item_id=plaid_item.item_id,
                    pending=transaction.get('pending', False)
                )
                db.session.add(new_transaction)
                db.session.commit()

                with db.session.no_autoflush:
                    for tag in tag_objects:
                        if tag not in new_transaction.tags:
                            new_transaction.tags.append(tag)

                new_transactions_count += 1
            else:
                print(f"==> Duplicate transaction found: {txn_id}")
                duplicate_transactions_count += 1

        db.session.commit()
        print(f"==> Committed {new_transactions_count} new transactions")

        starting_balances = {}
        for plaid_item in plaid_items:
            accounts = get_account_balance(plaid_item.decrypted_access_token)
            for account in accounts:
                if str(account.get('subtype', '')).lower() not in ALLOWED_SUBTYPES:
                    continue
                account_id = account['account_id']
                account_name = account['name']
                current_balance = account['balances']['current']
                # Sum transactions for this account in your DB
                total_transactions = db.session.query(
                    db.func.sum(Transaction.amount)
                ).filter_by(user_id=user_id, account_id=account_id).scalar() or 0.0

                starting_balance = current_balance - total_transactions
                starting_balances[account_id] = {
                    "account_name": account['name'],
                    "current_balance": current_balance,
                    "starting_balance": starting_balance
                }

        all_transactions = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.timestamp.desc()).all()
        print(f"==> Total transactions fetched: {len(all_transactions)}")
        for transaction in all_transactions:
            print(f"Transaction ID: {transaction.transaction_id}, Amount: {transaction.amount}")

        recent_transactions_list = [
            {
                "transaction_id": txn.transaction_id,
                "date": txn.date.strftime('%Y-%m-%d'),
                "name": txn.name,
                "division": txn.division,
                "amount": txn.amount
            }
            for txn in all_transactions[:10]
        ]
        # Run AI categorization if any new transactions were added
        if new_transactions_count > 0:
            _run_ai_categorization(user_id)

        return jsonify({
            "status": "success",
            "new_transactions": new_transactions_count,
            "duplicate_transactions": duplicate_transactions_count,
            "recent_transactions": recent_transactions_list,
            "starting_balances": starting_balances,
            "show_categorization_modal": new_transactions_count > 0 
        })

    except ApiException as e:
        print(f"==> Caught Plaid ApiException: {e}")
        error_response = format_error(e)
        return jsonify(error_response), 500
    except Exception as e:
        print(f"==> Caught general exception: {e}")
        return jsonify({"error": str(e)}), 500


@transactions_api.route('/api/refresh_transactions', methods=['POST'])
@login_required
def refresh_transactions():
    try:
        # Call the get_transactions function
        csrf_token=generate_csrf()
        get_transactions()
        return jsonify({"status": "success", "message": "Transactions refreshed successfully"}), 200
    except Exception as e:
        logging.error(f"Error refreshing transactions: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Failed to refresh transactions"}), 500

def _run_ai_categorization(user_id):
    """Run AI division and tag prediction for all uncategorized transactions."""
    from app.models import Tags
    from app.ai_helpers import predict_transaction_division, predict_transaction_tags

    try:
        # Autofill divisions
        uncategorized = Transaction.query.filter(
            Transaction.user_id == user_id,
            (Transaction.division.is_(None)) | (func.lower(Transaction.division) == "none")
        ).all()

        for t in uncategorized:
            try:
                t.division = predict_transaction_division(t)
                db.session.add(t)
            except Exception as e:
                print(f"[ai-categorize] Division prediction failed for {t.id}: {e}")

        db.session.commit()
        print(f"[ai-categorize] Divisions updated for {len(uncategorized)} transactions.")

        # Autofill tags
        untagged = Transaction.query.filter(
            Transaction.user_id == user_id,
            ~Transaction.tags.any()
        ).all()

        for t in untagged:
            try:
                predicted_tags = predict_transaction_tags(t)
                for tag_name in predicted_tags:
                    tag = Tags.query.filter_by(user_id=user_id, name=tag_name).first()
                    if not tag:
                        tag = Tags(user_id=user_id, name=tag_name)
                        db.session.add(tag)
                        db.session.flush()
                    if tag not in t.tags:
                        t.tags.append(tag)
                db.session.add(t)
            except Exception as e:
                print(f"[ai-categorize] Tag prediction failed for {t.id}: {e}")

        db.session.commit()
        print(f"[ai-categorize] Tags updated for {len(untagged)} transactions.")

    except Exception as e:
        db.session.rollback()
        print(f"[ai-categorize] Categorization failed: {e}")