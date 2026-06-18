from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from flask_login import login_required
import logging, json
from datetime import datetime
from flask_wtf import csrf, CSRFProtect
from app.models import Transaction, Tags, User
from app.helpers import exit_usd, calculateAllMoney
from app.helpers import populate_tags
from app.ai_helpers import predict_transaction_division, handle_user_division_edit, predict_transaction_tags
from flask_login import current_user
from app import csrf 
from sqlalchemy import func


from app.helpers import exit_usd, delete_record  # adjust import paths as needed
from app import db

transaction_bp = Blueprint("transaction", __name__)

@transaction_bp.route('/update_record', methods=['POST'])
@login_required
def update_record():
    tag = request.form.get('tag')
    division = request.form.get('division')
    record_id = request.form.get('record_id')
    amount = exit_usd(request.form.get('amount'))
    bank_account = request.form.get('bank_account')
    deleteBoolean = request.form.get('deleteBoolean')
    logging.debug(f"tag: {tag}")

    if deleteBoolean == "true":
        logging.debug(f"deleteBoolean is true")
        delete_record(db, record_id, Transaction)
        return redirect(url_for('history_bp.history'))

    if not record_id:
        logging.debug("no record_id")
        return redirect(url_for('history_bp.history'))

    record = Transaction.query.get_or_404(record_id)
    try:
        record.tag = tag
        record.amount = float(amount)
        record.bank_account = request.form.get('bank_account')  # ← add this
        record.bank_name = request.form.get('bank_name') or record.bank_name  # keep existing if blank
        record.division = division
        db.session.commit()
    except Exception as e:
        logging.debug(f"Error while updating record: {e}")
        db.session.rollback()

    return redirect(url_for('history_bp.history'))  


@transaction_bp.route('/fetch_transactions', methods=['GET'])
@login_required
def fetch_transactions():
    user_id = current_user.id # Fetch the logged-in user's ID
    print(f"Fetching transactions for user_id: {user_id}")
    
    tags_list = Tags.query.filter_by(user_id=user_id).all()
    print(f"Retrieved {len(tags_list)} tags for user_id: {user_id}")
    
    if not user_id:
        print("User not logged in, redirecting to login page.")
        return redirect(url_for('auth.login'))  # Redirect if not logged in

    # Query the database for transactions belonging to the user
    transactions = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.timestamp.desc()).all()
    print(f"Retrieved {len(transactions)} transactions for user_id: {user_id}")
    
    transaction = Transaction.query.filter_by(id=11).first()
    if transaction:
        print(f"Retrieved transaction ID: {transaction.id}")
    else:
        print("Transaction with ID 11 not found.")
    
    populate_tags(db, user_id)
    
    if transaction and transaction.tags:
        print(f"Transaction ID: {transaction.id} has the following tags:")
        for tag in transaction.tags:
            print(f"Tag ID: {tag.id}, Name: {tag.name}, Status: {tag.status}, Color ID: {tag.color_id}")
    else:
        print(f"Transaction ID: {transaction.id if transaction else 'Unknown'} has no tags.")
    
    # Pass transactions to the template
    return render_template('fetch_transactions.html', transactions=transactions, tags_list=tags_list)

@transaction_bp.route('/update_transaction/<int:transaction_id>', methods=['POST'])
@login_required
def update_transaction(transaction_id):
    if request.method == "POST":
        logging.debug(f"Form data received: {request.form}")
        user_id = current_user.id

        transaction = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first_or_404()

        tags_json = request.form.get("tags")
        try:
            tag_names = json.loads(tags_json)
        except (TypeError, json.JSONDecodeError):
            tag_names = []

        
        # Existing tags
        existing_tags = {tag.name for tag in transaction.tags}

        # Combine new and existing tags
        all_tags = set(tag_names).union(existing_tags)

        division = request.form.get('division')
        if not division:  # Only categorize if empty
            predicted_division = predict_transaction_division(transaction)
            division = predicted_division
        record_id = transaction_id
        amount = exit_usd(request.form.get('amount'))
        bank_account = request.form.get('bank_account')
        deleteBoolean = request.form.get('deleteBoolean')
        date = request.form.get('date')
        time = request.form.get('time')
        name = request.form.get('name')
        note= request.form.get('note')
        date_str = request.form.get('date')  # Example: '01/02/2025'
        if date_str:
            date = datetime.strptime(date_str, '%m/%d/%Y')
        else:
            date = datetime.now()

        if deleteBoolean == "true":
            logging.debug(f"Delete request for transaction ID: {transaction_id}")
            # Find and delete the record
            record = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
            if record:
                from sqlalchemy import text
                db.session.execute(
                    text("DELETE FROM ai_categorization_log WHERE transaction_id = :tid"),
                    {"tid": transaction_id}
                )
                db.session.flush()
                db.session.delete(record)
                db.session.commit()
                logging.debug(f"Transaction ID {transaction_id} deleted successfully.")
            else:
                logging.error(f"Transaction ID {transaction_id} not found.")
            return redirect(url_for('history_bp.history'))


        if not record_id:
            logging.debug(f"no record_id")
            return redirect(url_for('history_bp.history'))

        if Transaction is None:
            logging.debug(f"model was none")
            return redirect(url_for('history_bp.history'))
        if division == "general":
            tag_objects = Tags.query.filter(Tags.name.in_(all_tags)).all()
            user = User.query.get(user_id)
            calculateAllMoney(db, Transaction, tag_objects=tag_objects, money=amount, date=date, bank_name=record.bank_name, division=division, user=user)
            
            # Fetch the current transaction and delete it after calculation
            record = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
            if record:
                from sqlalchemy import text
                db.session.execute(
                    text("DELETE FROM ai_categorization_log WHERE transaction_id = :tid"),
                    {"tid": transaction_id}
                )
                db.session.flush()
                db.session.delete(record)
                db.session.commit()
                logging.debug(f"Transaction ID {transaction_id} deleted successfully.")
            else:
                logging.error(f"Transaction ID {transaction_id} not found.")
            return redirect(url_for('history_bp.history'))
        
        time_str = request.form.get('time')
        try:
            # If you're just collecting a time like "14:30", combine it with today's date
            time = datetime.combine(date.date(), datetime.strptime(time_str, '%H:%M').time())
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid time format received: {time_str}, error: {e}")
            time = datetime.now()

        
        record = Transaction.query.filter_by(id=record_id, user_id=user_id).first_or_404()
        print(f"Parsed time: {time}, name: {name}, note: {note}")
        try:
            record.amount = float(amount)
            record.bank_account = request.form.get('bank_account')  # ← add this
            record.bank_name = request.form.get('bank_name') or record.bank_name
            record.division = division
            record.timestamp = time
            record.date = date
            record.name = name
            record.note = note
            record.tags.clear()  # Clear existing tags
            for tag_name in tag_names:
                tag = Tags.query.filter_by(name=tag_name, user_id=user_id).first()
                if tag:
                    transaction.tags.append(tag)
            
            handle_user_division_edit(record, division)

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            logging.error(f"Failed to update transaction {transaction_id}: {e}")

        return redirect(url_for('history_bp.history'))
    return redirect(url_for('history_bp.history')) 


@transaction_bp.route("/api/autofill_divisions", methods=["POST"])
@login_required
@csrf.exempt
def autofill_transaction_divisions():
    user_id = current_user.id

    try:
        transactions = Transaction.query.filter(
            Transaction.user_id == user_id,
            (Transaction.division.is_(None)) | (func.lower(Transaction.division) == "none")
        ).all()

        updated_count = 0
        for t in transactions:
            try:
                predicted_division = predict_transaction_division(t)
                print(f"💡 Transaction {t.id}: predicted division = {predicted_division}")
                t.division = predicted_division
                db.session.add(t)
                updated_count += 1
            except Exception as e:
                print(f"❌ Error predicting division for transaction {t.id}: {e}")

        db.session.commit()
        print(f"✅ Autofill complete. {updated_count} transactions updated.")
        return jsonify({"status": "success", "message": f"{updated_count} transactions updated."})

    except Exception as e:
        print(f"❌ Autofill error: {e}")
        return jsonify({"status": "error", "message": "Autofill failed"}), 500
    

@transaction_bp.route("/api/autofill_tags", methods=["POST"])
@login_required
@csrf.exempt
def autofill_transaction_tags():
    user_id = current_user.id

    try:
        # Step 1: Get transactions with no tags
        transactions = (
            Transaction.query
            .filter(
                Transaction.user_id == user_id,
                ~Transaction.tags.any()  # <-- no tags associated
            )
            .all()
        )

        updated_count = 0
        for t in transactions:
            try:
                # Step 2: Predict one or more tags
                predicted_tags = predict_transaction_tags(t)  # <-- you'll create this function (AI-assisted)
                print(f"💡 Transaction {t.id}: predicted tags = {predicted_tags}")

                for tag_name in predicted_tags:
                    # Step 3: Find or create the tag for this user
                    tag = Tags.query.filter_by(user_id=user_id, name=tag_name).first()
                    if not tag:
                        tag = Tags(user_id=user_id, name=tag_name)
                        db.session.add(tag)
                        db.session.flush()  # flush to get ID without full commit

                    # Step 4: Associate the tag with the transaction
                    if tag not in t.tags:
                        t.tags.append(tag)

                db.session.add(t)
                updated_count += 1

            except Exception as e:
                print(f"❌ Error predicting tags for transaction {t.id}: {e}")

        db.session.commit()
        print(f"✅ Autofill complete. {updated_count} transactions updated.")
        return jsonify({"status": "success", "message": f"{updated_count} transactions updated."})

    except Exception as e:
        print(f"❌ Autofill error: {e}")
        return jsonify({"status": "error", "message": "Autofill failed"}), 500




@transaction_bp.route("/api/division_summary", methods=["GET"])
@login_required
def division_summary():
    user_id = current_user.id
    from sqlalchemy import func

    # Totals per division
    totals = db.session.query(
        Transaction.division,
        func.sum(Transaction.amount)
    ).filter(
        Transaction.user_id == user_id
    ).group_by(Transaction.division).all()

    division_totals = {div: float(total) for div, total in totals}

    # Get starting balance transaction
    starting_balance_txn = Transaction.query.filter_by(
        user_id=user_id,
        name="Opening Balance"
    ).order_by(Transaction.date.asc()).first()

    return jsonify({
        "division_totals": division_totals,
        "starting_balance": {
            "amount": float(starting_balance_txn.amount) if starting_balance_txn else 0.0,
            "date": starting_balance_txn.date.strftime('%Y-%m-%d') if starting_balance_txn else None
        }
    })

@transaction_bp.route("/api/update_starting_balance", methods=["POST"])
@login_required
def update_starting_balance():
    user_id = current_user.id
    data = request.json
    new_amount = float(data.get("amount", 0))

    starting_balance_txn = Transaction.query.filter_by(
        user_id=user_id,
        name="Opening Balance"
    ).first()

    if starting_balance_txn:
        starting_balance_txn.amount = new_amount
        db.session.commit()
        return jsonify({"status": "success", "message": "Starting balance updated."})
    else:
        return jsonify({"status": "error", "message": "Starting balance transaction not found."}), 404
