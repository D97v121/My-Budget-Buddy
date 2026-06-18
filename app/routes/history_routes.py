# app/routes/history_routes.py

from flask import Blueprint, render_template, session
from flask_login import login_required
from flask_wtf.csrf import generate_csrf
from app.models import Transaction, Tags
from app.forms import TransactionForm
from flask_login import current_user

history_bp = Blueprint("history_bp", __name__)

@history_bp.route('/History', methods=["GET", "POST"])
@login_required
def history():
    user_id = current_user.id
    csrf_token = generate_csrf()
    form = TransactionForm()

    tags_list = Tags.query.filter_by(user_id=user_id).all()
    divisions_list = ['none', 'general', 'save', 'spend', 'give', 'invest', 'expense']
    
    transactions = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.date.desc()).all()
    
    return render_template("history.html", transactions=transactions,
                           tags_list=tags_list, divisions_list=divisions_list,
                           csrf_token=csrf_token, form=form)


@history_bp.route('/invest_history', methods=["GET", "POST"])
@login_required
def invest_history():
    records = Transaction.query.filter_by(user_id=current_user.id, division="invest").all()
    return render_template("invest_history.html", invest_records_data=_serialize(records))


@history_bp.route('/save_history', methods=["GET", "POST"])
@login_required
def save_history():
    records = Transaction.query.filter_by(user_id=current_user.id, division="save").all()
    return render_template("save_history.html", save_records_data=_serialize(records))


@history_bp.route('/spend_history', methods=["GET", "POST"])
@login_required
def spend_history():
    records = Transaction.query.filter_by(user_id=current_user.id, division="spend").all()
    return render_template("spend_history.html", spend_records_data=_serialize(records))


@history_bp.route('/give_history', methods=["GET", "POST"])
@login_required
def give_history():
    records = Transaction.query.filter_by(user_id=current_user.id, division="give").all()
    return render_template("give_history.html", give_records_data=_serialize(records))


@history_bp.route('/expense_history', methods=["GET", "POST"])
@login_required
def expense_history():
    records = Transaction.query.filter_by(user_id=current_user.id, division="expense").all()
    return render_template("expense_history.html", expense_records_data=_serialize(records))

@history_bp.route('/transactions_history', methods=["GET", "POST"])
@login_required
def transactions_history():
    csrf_token = generate_csrf()
    records = Transaction.query.filter_by(user_id=current_user.id, division="expense").all()
    return render_template("transactions_history.html",
                           expense_records_data=_serialize(records),
                           csrf_token=csrf_token)


def _serialize(records):
    return [{
         "tags": [tag.name for tag in record.tags] if record.tags else [],
        "amount": record.amount,
        "bank_name": record.bank_name,
        "division": record.division,
        "timestamp": record.timestamp
    } for record in records]
