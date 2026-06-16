from flask import Blueprint, jsonify, session, request as flask_request
from flask_login import login_required
from app import db
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
                account_id = account['account_id']
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

        print("416359 ==> Starting Opening Balance calculation")
        for plaid_item in plaid_items:
            print(f"416359 ==> Processing Plaid item: {plaid_item.item_id}")
            accounts = get_account_balance(plaid_item.decrypted_access_token)
            for account in accounts:
                account_id = account['account_id']
                account_name = account['name']
                bank_name = fetch_institution_name(plaid_item.access_token)
                current_balance = account['balances']['current']
                print(f"416359 ==> Account {account_name} ({account_id}) current balance: {current_balance}")

                # Sum all transactions in DB for this account
                total_transactions = db.session.query(
                    db.func.sum(Transaction.amount)
                ).filter_by(user_id=user_id, account_id=account_id).scalar() or 0.0
                print(f"416359 ==> Total transactions sum for {account_name}: {total_transactions}")

                starting_balance = current_balance - total_transactions
                print(f"416359 ==> Calculated starting balance for {account_name}: {starting_balance}")

                # Check if an opening balance transaction already exists
                opening_exists = Transaction.query.filter_by(
                    user_id=user_id,
                    account_id=account_id,
                    name="Opening Balance"
                ).first()

                if not opening_exists:
                    print(f"416359 ==> No Opening Balance transaction found for {account_name}, creating one.")
                    # Get the oldest transaction date for this account
                    oldest_txn = Transaction.query.filter_by(
                        user_id=user_id,
                        account_id=account_id
                    ).order_by(Transaction.date.asc()).first()

                    if oldest_txn:
                        opening_date = oldest_txn.date - timedelta(days=1)
                        print(f"416359 ==> Oldest txn date for {account_name}: {oldest_txn.date}, opening date set to {opening_date}")
                    else:
                        opening_date = datetime.utcnow().date()
                        print(f"416359 ==> No transactions found for {account_name}, using today as opening date: {opening_date}")

                    opening_txn = Transaction(
                        user_id=user_id,
                        transaction_id=f"opening-{account_id}",
                        date=opening_date,
                        timestamp=datetime.utcnow(),
                        name="Opening Balance",
                        division="balance",
                        amount=starting_balance,
                        account_id=account_id,
                        bank_account=account_name,
                        bank_name=bank_name,
                        item_id=plaid_item.item_id,
                        pending=False
                    )
                    db.session.add(opening_txn)
                    print(f"416359 ==> Added Opening Balance for {account_name}: {starting_balance}")
                else:
                    print(f"416359 ==> Opening Balance transaction already exists for {account_name}, skipping.")

        db.session.commit()
        print("416359 ==> Finished Opening Balance calculation")


        
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





