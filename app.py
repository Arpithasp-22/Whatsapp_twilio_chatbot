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

@app.route("/", methods=["GET"])
def health_check():
    """
    Health check endpoint to verify server is running.
    """
    return "WhatsApp Bot Server is running! ✅", 200

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_reply():
    """
    Main Twilio webhook endpoint.
    Receives incoming WhatsApp messages and sends responses.
    """
    print("\n" + "="*60)
    print(f"[WEBHOOK] Received {request.method} request to /whatsapp")
    print(f"[WEBHOOK] Headers: {dict(request.headers)}")
    print(f"[WEBHOOK] Form data: {dict(request.form)}")
    print(f"[WEBHOOK] Values: {dict(request.values)}")
    print("="*60)

    if request.method == "GET":
        return "WhatsApp webhook endpoint is active! Use POST to send messages.", 200

    incoming_text = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "").strip()

    print(f"[MESSAGE] Incoming: '{incoming_text}'")
    print(f"[MESSAGE] From: {from_number}")
    
    if not incoming_text:
        print("[WARNING] Empty message received!")
        return "No message body", 400
    
    # Forward message to bot logic
    print(f"[BOT] Processing message...")
    response_text, _ = handle_message(
        incoming_text,
        user_id=from_number or "anonymous"
    )
    print(f"[BOT] Response: {response_text[:100]}...")
    
    # Build Twilio response
    resp = MessagingResponse()
    resp.message(response_text)
    
    print(f"[TWILIO] Sending response XML")
    print("="*60 + "\n")

    return str(resp)

# ============================================================
# LOCAL DEV ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 WhatsApp Bot Server Starting...")
    print("="*60)
    print("Server: http://localhost:5001")
    print("Webhook: http://localhost:5001/whatsapp")
    print("Health: http://localhost:5001/")
    print("="*60 + "\n")
    app.run(port=5001,host='0.0.0.0', debug=True)
