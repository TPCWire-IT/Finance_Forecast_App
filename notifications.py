import os
import requests
from dotenv import load_dotenv

load_dotenv()


def teams_enabled():
    return os.getenv("TEAMS_NOTIFICATIONS_ENABLED", "true").lower() == "true"


def send_teams_message(title, message):
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL")

    if not teams_enabled() or not webhook_url:
        print("Teams notification skipped.")
        return

    payload = {
        "title": title,
        "message": message
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        response.raise_for_status()
        print("Teams notification sent.")
    except Exception as error:
        print("Teams notification failed:", str(error))


def notify_dso_forecast_submitted(forecast_month, submitted_by):
    send_teams_message(
        title=f"Forecast Submitted - {forecast_month:%b %Y}",
        message=(
            f"Finance submitted the forecast for {forecast_month:%b %Y}.\n\n"
            f"Submitted by: {submitted_by}\n\n"
            "Processing has started."
        ),
    )


def notify_processing_success(forecast_month):
    send_teams_message(
        title=f"Forecast Processing Completed - {forecast_month:%b %Y}",
        message=(
            f"Forecast processing completed successfully for {forecast_month:%b %Y}.\n\n"
            "Forecast data will be visible in Power BI after the next scheduled refresh."
        ),
    )


def notify_processing_failure(forecast_month, error_message):
    send_teams_message(
        title=f"Forecast Processing Failed - {forecast_month:%b %Y}",
        message=(
            f"Forecast processing failed for {forecast_month:%b %Y}.\n\n"
            f"Error:\n{error_message}"
        ),
    )