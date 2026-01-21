"""
Flask application for Meta WhatsApp Business API integration.
Handles webhook verification and incoming messages from Meta's WhatsApp.
"""

import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from bot import setup_bot, handle_message

# ============================================================
# APP SETUP
# ============================================================

load_dotenv()

app = Flask(__name__)

# Meta WhatsApp API Configuration
VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")  # Your custom verify token
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")  # From Meta Developer Console
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")  # Your WhatsApp phone number ID
WHATSAPP_API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# Initialize bot
setup_bot()

# ============================================================
# WEBHOOK VERIFICATION (GET)
# ============================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Meta webhook verification endpoint.
    Meta will send a GET request with hub.mode, hub.verify_token, and hub.challenge.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    print(f"[WEBHOOK VERIFY] Mode: {mode}, Token: {token}")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[WEBHOOK VERIFY] Verification successful!")
        return challenge, 200
    else:
        print("[WEBHOOK VERIFY] Verification failed!")
        return "Forbidden", 403

# ============================================================
# MESSAGE WEBHOOK (POST)
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Main webhook endpoint for receiving messages from Meta WhatsApp.
    """
    try:
        data = request.get_json()
        print(f"\n[WEBHOOK] Received data: {data}")

        # Check if this is a message event
        if data.get("object") == "whatsapp_business_account":
            entries = data.get("entry", [])
            
            for entry in entries:
                changes = entry.get("changes", [])
                
                for change in changes:
                    value = change.get("value", {})
                    
                    # Check if there are messages
                    messages = value.get("messages", [])
                    if not messages:
                        continue
                    
                    # Process each message
                    for message in messages:
                        # Extract message details
                        from_number = message.get("from")
                        message_id = message.get("id")
                        message_type = message.get("type")
                        
                        print(f"[MESSAGE] From: {from_number}, Type: {message_type}, ID: {message_id}")
                        
                        # Handle text messages
                        if message_type == "text":
                            text_body = message.get("text", {}).get("body", "")
                            print(f"[MESSAGE] Text: {text_body}")
                            
                            # Process message through bot
                            response_text, _ = handle_message(text_body, user_id=from_number)
                            print(f"[BOT RESPONSE] {response_text}")
                            
                            # Send response back via Meta API
                            send_whatsapp_message(from_number, response_text)
                        
                        else:
                            print(f"[MESSAGE] Unsupported message type: {message_type}")
                            send_whatsapp_message(
                                from_number,
                                "Sorry, I can only process text messages at the moment."
                            )
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"[ERROR] Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

# ============================================================
# SEND MESSAGE VIA META API
# ============================================================

def send_whatsapp_message(to_number: str, message: str):
    """
    Send a message via Meta WhatsApp Business API.
    
    Args:
        to_number: Recipient phone number (without 'whatsapp:' prefix)
        message: Message text to send
    """
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message
        }
    }
    
    try:
        print(f"[SEND] Sending message to {to_number}")
        response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        
        result = response.json()
        print(f"[SEND] Success: {result}")
        return result
    
    except requests.exceptions.RequestException as e:
        print(f"[SEND ERROR] Failed to send message: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[SEND ERROR] Response: {e.response.text}")
        raise

# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "healthy", "service": "whatsapp-bot"}), 200

# ============================================================
# LOCAL DEV ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    # Validate environment variables
    if not VERIFY_TOKEN:
        print("WARNING: META_VERIFY_TOKEN not set in .env")
    if not ACCESS_TOKEN:
        print("WARNING: META_ACCESS_TOKEN not set in .env")
    if not PHONE_NUMBER_ID:
        print("WARNING: META_PHONE_NUMBER_ID not set in .env")
    
    print("\n" + "="*60)
    print("Meta WhatsApp Bot Server Starting...")
    print("="*60)
    print(f"Webhook URL: http://localhost:5000/webhook")
    print(f"Health Check: http://localhost:5000/health")
    print("="*60 + "\n")
    
    app.run(port=5000, debug=True)
