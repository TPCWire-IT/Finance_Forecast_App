import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()


def send_email(subject, body, recipients):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", "25"))
    smtp_sender = os.getenv("SMTP_SENDER")

    if not smtp_server or not smtp_sender:
        print("SMTP not configured. Email skipped.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(smtp_server, smtp_port) as smtp:
        smtp.send_message(msg)


def get_dso_recipients():
    return [
        email.strip()
        for email in os.getenv("DSO_EMAILS", "").split(",")
        if email.strip()
    ]


def get_finance_recipients():
    return [
        email.strip()
        for email in os.getenv("FINANCE_EMAILS", "").split(",")
        if email.strip()
    ]


def notify_dso_forecast_submitted(forecast_month, submitted_by):
    send_email(
        subject=f"Forecast submitted for {forecast_month:%b %Y}",
        body=f"Finance submitted forecast for {forecast_month:%b %Y}.\n\nSubmitted by: {submitted_by}",
        recipients=get_dso_recipients(),
    )


def notify_processing_success(forecast_month):
    subject = f"Forecast processing completed for {forecast_month:%b %Y}"

    dso_body = f"Forecast processing completed successfully for {forecast_month:%b %Y}."
    finance_body = (
        f"Forecast was processed successfully for {forecast_month:%b %Y}.\n\n"
        "Data will be visible in Power BI after the midnight refresh."
    )

    send_email(subject, dso_body, get_dso_recipients())
    send_email(subject, finance_body, get_finance_recipients())


def notify_processing_failure(forecast_month, error_message):
    dso_subject = f"Forecast processing failed for {forecast_month:%b %Y}"
    fin_subject = f"Forecast processing issue for {forecast_month:%b %Y}"

    dso_body = (
        f"Forecast processing failed for {forecast_month:%b %Y}.\n\n"
        f"Error:\n{error_message}"
    )

    finance_body = (
        f"Forecast was submitted for {forecast_month:%b %Y}, "
        "but processing failed. DSO has been notified."
    )

    send_email(dso_subject, dso_body, get_dso_recipients())
    send_email(fin_subject, finance_body, get_finance_recipients())