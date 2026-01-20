"""
Flask application that acts as the webhook endpoint for the Twilio WhatsApp chatbot.
It receives incoming WhatsApp messages, forwards them to the bot logic,
and returns responses back to the user via Twilio.
"""

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from bot import setup_bot, handle_message

# ============================================================
# APP SETUP
# ============================================================

load_dotenv()

app = Flask(__name__)

# Initialize bot (load intents, DB schema, LLM)
setup_bot()

# ============================================================
# TWILIO WEBHOOK
# ============================================================

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """
    Main Twilio webhook endpoint.
    Receives incoming WhatsApp messages and sends responses.
    """

    incoming_text = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "").strip()

    print("Incoming message:", incoming_text)
    print("From number:", from_number)
    # Forward message to bot logic
    response_text, _ = handle_message(
        incoming_text,
        user_id=from_number or "anonymous"
    )
    print("Response text:", response_text)
    # Build Twilio response
    resp = MessagingResponse()
    resp.message(response_text)

    return str(resp)

# ============================================================
# LOCAL DEV ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    app.run(port=5000, debug=True)
