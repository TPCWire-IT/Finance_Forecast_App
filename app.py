import os
from datetime import date
from functools import wraps
from threading import Thread

import pyodbc
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

from forecast_processor import process_forecast
from notifications import (
    notify_dso_forecast_submitted,
    notify_processing_success,
    notify_processing_failure,
)

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False

csrf = CSRFProtect(app)

USERS = {
    "finance_user": {
        "password_hash": generate_password_hash("Forecast@123"),
        "role": "finance",
        "display_name": "Finance User",
    },
    "dso_admin": {
        "password_hash": generate_password_hash("Forecast@123"),
        "role": "dso",
        "display_name": "DSO Admin",
    },
}


def get_sql_connection():
    connection_string = os.getenv("SQL_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError("SQL_CONNECTION_STRING is missing in .env file")
    return pyodbc.connect(connection_string)


def get_current_month():
    return date.today().replace(day=1)


def get_sales_teams():
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT SalesTeam
        FROM loki.Forecast_Sales_Team
        WHERE IsActive = 1
        ORDER BY SalesTeam
        """
    )

    teams = [row.SalesTeam for row in cursor.fetchall()]
    conn.close()
    return teams


def get_all_sales_teams():
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT SalesTeamId, SalesTeam, IsActive
        FROM loki.Forecast_Sales_Team
        ORDER BY IsActive DESC, SalesTeam
        """
    )

    teams = cursor.fetchall()
    conn.close()
    return teams


def add_sales_team(sales_team):
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO loki.Forecast_Sales_Team (SalesTeam, IsActive)
        VALUES (?, 1)
        """,
        sales_team,
    )

    conn.commit()
    conn.close()


def set_sales_team_status(sales_team_id, is_active):
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE loki.Forecast_Sales_Team
        SET IsActive = ?
        WHERE SalesTeamId = ?
        """,
        is_active,
        sales_team_id,
    )

    conn.commit()
    conn.close()


def get_available_months():
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT DISTINCT ForecastMonth
        FROM loki.Forecast_Input
        ORDER BY ForecastMonth DESC
        """
    )

    months = [row.ForecastMonth for row in cursor.fetchall()]
    conn.close()
    return months


def get_forecast_rows(forecast_month):
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT SalesTeam, OrderForecast, RevenueForecast, Status
        FROM loki.Forecast_Input
        WHERE ForecastMonth = ?
        """,
        forecast_month,
    )

    rows = cursor.fetchall()
    conn.close()

    saved_values = {}
    status = None

    for row in rows:
        saved_values[row.SalesTeam] = {
            "order_forecast": row.OrderForecast,
            "revenue_forecast": row.RevenueForecast,
        }
        status = row.Status

    return saved_values, status


def save_forecast_to_sql(forecast_month, rows, status):
    conn = get_sql_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM loki.Forecast_Input
            WHERE ForecastMonth = ?
              AND Status IN ('Draft', 'Submitted')
            """,
            forecast_month,
        )

        for row in rows:
            cursor.execute(
                """
                INSERT INTO loki.Forecast_Input
                (
                    ForecastMonth,
                    SalesTeam,
                    OrderForecast,
                    RevenueForecast,
                    Status
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                forecast_month,
                row["sales_team"],
                row["order_forecast"],
                row["revenue_forecast"],
                status,
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def reopen_forecast_month(forecast_month):
    conn = get_sql_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE loki.Forecast_Input
        SET Status = 'Draft'
        WHERE ForecastMonth = ?
          AND Status = 'Submitted'
        """,
        forecast_month,
    )

    conn.commit()
    conn.close()


def run_processing_background(forecast_month, submitted_by):
    try:
        print("Forecast processing started:", forecast_month)

        notify_dso_forecast_submitted(forecast_month, submitted_by)

        process_forecast(forecast_month)

        notify_processing_success(forecast_month)

        print("Forecast processing completed successfully:", forecast_month)

    except Exception as error:
        print("Forecast processing failed:", str(error))
        notify_processing_failure(forecast_month, str(error))


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if session.get("role") not in allowed_roles:
                flash("You do not have access to this page.", "danger")
                return redirect(url_for("login"))
            return view_func(*args, **kwargs)

        return wrapper

    return decorator


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = USERS.get(username)

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "danger")
            return render_template("login.html")

        session.clear()
        session["username"] = username
        session["role"] = user["role"]
        session["display_name"] = user["display_name"]

        return redirect(url_for("forecast_input"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/forecast", methods=["GET", "POST"])
@login_required
@role_required("finance", "dso")
def forecast_input():
    forecast_month = get_current_month()
    current_month_label = forecast_month.strftime("%b %Y")

    sales_teams = get_sales_teams()
    saved_values, forecast_status = get_forecast_rows(forecast_month)
    already_submitted = forecast_status == "Submitted"

    if request.method == "POST":
        action = request.form.get("action")

        if action == "clear":
            return render_template(
                "forecast_input.html",
                sales_teams=sales_teams,
                current_month_label=current_month_label,
                user_display=session.get("display_name"),
                role=session.get("role"),
                already_submitted=already_submitted,
                saved_values={},
                forecast_status=forecast_status,
            )

        if already_submitted:
            flash("Final forecast already submitted for this month. Contact DSO to reopen.", "danger")
            return redirect(url_for("forecast_input"))

        rows = []
        errors = []

        for team in sales_teams:
            order_raw = request.form.get(f"order_{team}", "").strip()
            revenue_raw = request.form.get(f"revenue_{team}", "").strip()

            try:
                order_value = float(order_raw)
                revenue_value = float(revenue_raw)
            except ValueError:
                errors.append(f"Invalid number entered for {team}.")
                continue

            if order_value < 0 or revenue_value < 0:
                errors.append(f"Negative values are not allowed for {team}.")

            rows.append(
                {
                    "sales_team": team,
                    "order_forecast": order_value,
                    "revenue_forecast": revenue_value,
                }
            )

        if errors:
            for error in errors:
                flash(error, "danger")

            return render_template(
                "forecast_input.html",
                sales_teams=sales_teams,
                current_month_label=current_month_label,
                user_display=session.get("display_name"),
                role=session.get("role"),
                already_submitted=already_submitted,
                saved_values=saved_values,
                forecast_status=forecast_status,
            )

        if action == "save_draft":
            save_forecast_to_sql(
                forecast_month=forecast_month,
                rows=rows,
                status="Draft",
            )

            flash("Forecast saved as draft.", "success")
            return redirect(url_for("forecast_input"))

        if action == "submit_final":
            save_forecast_to_sql(
                forecast_month=forecast_month,
                rows=rows,
                status="Submitted",
            )

            Thread(
                target=run_processing_background,
                args=(forecast_month, session.get("username")),
                daemon=True,
            ).start()

            return redirect(url_for("success"))

    return render_template(
        "forecast_input.html",
        sales_teams=sales_teams,
        current_month_label=current_month_label,
        user_display=session.get("display_name"),
        role=session.get("role"),
        already_submitted=already_submitted,
        saved_values=saved_values,
        forecast_status=forecast_status,
    )


@app.route("/sales-teams", methods=["GET", "POST"])
@login_required
@role_required("dso")
def sales_teams_admin():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            sales_team = request.form.get("sales_team", "").strip().upper()

            if not sales_team:
                flash("Sales Team name is required.", "danger")
            else:
                try:
                    add_sales_team(sales_team)
                    flash(f"{sales_team} added successfully.", "success")
                except Exception as error:
                    flash(f"Could not add team. It may already exist. Error: {str(error)}", "danger")

        elif action == "deactivate":
            sales_team_id = request.form.get("sales_team_id")
            set_sales_team_status(sales_team_id, 0)
            flash("Sales Team removed from active list.", "success")

        elif action == "activate":
            sales_team_id = request.form.get("sales_team_id")
            set_sales_team_status(sales_team_id, 1)
            flash("Sales Team activated.", "success")

        return redirect(url_for("sales_teams_admin"))

    return render_template(
        "sales_teams.html",
        teams=get_all_sales_teams(),
        user_display=session.get("display_name"),
        role=session.get("role"),
    )


@app.route("/admin", methods=["GET", "POST"])
@login_required
@role_required("dso")
def admin():
    available_months = get_available_months()

    selected_month_str = request.values.get("forecast_month")

    if selected_month_str:
        selected_month = date.fromisoformat(selected_month_str)
    else:
        selected_month = get_current_month()

    if selected_month not in available_months:
        available_months.insert(0, selected_month)

    current_month_label = selected_month.strftime("%b %Y")
    saved_values, forecast_status = get_forecast_rows(selected_month)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "reopen":
            if forecast_status != "Submitted":
                flash("Only submitted forecasts can be reopened.", "warning")
            else:
                reopen_forecast_month(selected_month)
                flash(f"{current_month_label} forecast reopened for Finance.", "success")

        return redirect(url_for("admin", forecast_month=selected_month.isoformat()))

    return render_template(
        "admin.html",
        current_month_label=current_month_label,
        forecast_status=forecast_status,
        available_months=available_months,
        selected_month=selected_month,
        user_display=session.get("display_name"),
        role=session.get("role"),
    )


@app.route("/success")
@login_required
def success():
    return render_template("success.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)