# app/routes/tracking_routes.py

from flask import Blueprint, render_template, session
from flask_login import login_required
from sqlalchemy import func
from collections import defaultdict
from app.models import Transaction
from app import db
from app.helpers import usd  # if you have a helper like this
from flask_login import current_user

tracking_bp = Blueprint("tracking_bp", __name__)

@tracking_bp.route('/Tracking', methods=["GET", "POST"])
@login_required
def tracking():
    user_id = current_user.id

    # Aggregate totals
    totals = db.session.query(
        Transaction.division,
        func.sum(Transaction.amount)
    ).filter_by(user_id=user_id).group_by(Transaction.division).all()

    total_dict = defaultdict(float, {division.lower(): amount for division, amount in totals})

    unique_dates_count = db.session.query(
        func.count(func.distinct(func.to_char(Transaction.date, 'YYYY-MM-DD')))
    ).filter(Transaction.user_id == user_id).scalar()

    date_format = "YYYY-MM-DD" if unique_dates_count <= 30 else "YYYY-MM"

    def get_graph_data_for_division(division_name):
        period = func.to_char(Transaction.date, date_format).label('period')

        results = db.session.query(
            period,
            func.sum(Transaction.amount)
        ).filter_by(user_id=user_id, division=division_name)\
         .group_by(period).order_by(period).all()

        running_total = 0
        dates = []
        values = []

        for period_val, amount in results:
            running_total += amount
            dates.append(period_val)
            values.append(running_total)

        return values, dates

    save_float, save_dates = get_graph_data_for_division("save")
    spend_float, spend_dates = get_graph_data_for_division("spend")
    give_float, give_dates = get_graph_data_for_division("give")
    invest_float, invest_dates = get_graph_data_for_division("invest")
    expense_float, expense_dates = get_graph_data_for_division("expense")

    return render_template('Tracking.html',
                           save_data=save_float, save_dates=save_dates,
                           spend_data=spend_float, spend_dates=spend_dates,
                           give_data=give_float, give_dates=give_dates,
                           invest_data=invest_float, invest_dates=invest_dates,
                           expense_data=expense_float, expense_dates=expense_dates,
                           save=usd(total_dict["save"]),
                           spend=usd(total_dict["spend"]),
                           give=usd(total_dict["give"]),
                           invest=usd(total_dict["invest"]),
                           expense=usd(total_dict["expense"]))