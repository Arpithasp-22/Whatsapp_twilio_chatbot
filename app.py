"""
Flask application that acts as the webhook endpoint for the Twilio WhatsApp chatbot.
It receives incoming WhatsApp messages, forwards them to the bot logic, and returns
the generated responses back to the user via Twilio.
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask, Response, abort, request
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

from bot import handle_message, setup_bot  # noqa: E402  (after load_dotenv so env is set)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

WHATSAPP_MAX_CHARS = 1500  # Twilio rejects WhatsApp bodies over 1600 chars

app = Flask(__name__)
setup_bot()


def _valid_twilio_request() -> bool:
    """Verify the X-Twilio-Signature so only Twilio can call this webhook."""
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        app.logger.warning("TWILIO_AUTH_TOKEN not set: skipping signature check (dev only!)")
        return True
    # Behind ngrok/a proxy, request.url may differ from the URL Twilio called.
    url = os.getenv("PUBLIC_WEBHOOK_URL") or request.url
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(token).validate(url, request.form, signature)


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Twilio webhook: text in, text out."""
    if not _valid_twilio_request():
        abort(403)

    incoming_text = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "").strip()  # used as the user id

    response_text = handle_message(from_number, incoming_text)
    if len(response_text) > WHATSAPP_MAX_CHARS:
        response_text = response_text[: WHATSAPP_MAX_CHARS - 1] + "…"

    resp = MessagingResponse()
    resp.message(response_text)
    return Response(str(resp), mimetype="application/xml")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
