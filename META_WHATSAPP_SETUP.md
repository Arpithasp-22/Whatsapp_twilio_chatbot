# Meta WhatsApp Business API Setup Guide

This guide will help you migrate from Twilio WhatsApp to Meta's WhatsApp Business API.

## Prerequisites

1. **Meta Business Account**
   - Go to [business.facebook.com](https://business.facebook.com)
   - Create or use existing business account

2. **Meta Developer Account**
   - Go to [developers.facebook.com](https://developers.facebook.com)
   - Create developer account if you don't have one

## Step 1: Create Meta App

1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps)
2. Click **"Create App"**
3. Select **"Business"** as app type
4. Fill in app details:
   - App Name: `Sales Analytics WhatsApp Bot`
   - Contact Email: Your email
   - Business Account: Select your business account
5. Click **"Create App"**

## Step 2: Add WhatsApp Product

1. In your app dashboard, find **"WhatsApp"** in the products list
2. Click **"Set Up"**
3. Select your **Business Portfolio**
4. You'll be taken to WhatsApp setup page

## Step 3: Get Your Credentials

### 3.1 Phone Number ID
1. In WhatsApp setup, go to **"API Setup"** tab
2. Copy the **"Phone number ID"** (looks like: `123456789012345`)
3. This is your `META_PHONE_NUMBER_ID`

### 3.2 Access Token
1. In the same "API Setup" tab, you'll see a **"Temporary access token"**
2. For production, generate a **permanent token**:
   - Go to **"System Users"** in Business Settings
   - Create a system user
   - Generate token with `whatsapp_business_messaging` permission
3. This is your `META_ACCESS_TOKEN`

### 3.3 Verify Token
1. Create your own random string (e.g., `my_secure_verify_token_12345`)
2. This is your `META_VERIFY_TOKEN` (you'll use this in webhook setup)

## Step 4: Configure Environment Variables

Add these to your `.env` file:

```bash
# Meta WhatsApp Business API
META_VERIFY_TOKEN=my_secure_verify_token_12345
META_ACCESS_TOKEN=EAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
META_PHONE_NUMBER_ID=123456789012345
```

## Step 5: Expose Your Local Server

You need a public HTTPS URL for Meta to send webhooks. Use one of these:

### Option A: ngrok (Recommended)
```bash
# Install ngrok
brew install ngrok  # macOS
# or download from ngrok.com

# Run ngrok
ngrok http 5000
```

### Option B: localtunnel
```bash
npx localtunnel --port 5000
```

### Option C: Cloudflare Tunnel
```bash
cloudflared tunnel --url http://localhost:5000
```

Copy the HTTPS URL you get (e.g., `https://abc123.ngrok.io`)

## Step 6: Configure Webhook in Meta

1. Go to your app's **WhatsApp > Configuration** page
2. Find **"Webhook"** section
3. Click **"Edit"**
4. Enter:
   - **Callback URL**: `https://your-ngrok-url.ngrok.io/webhook`
   - **Verify Token**: Your `META_VERIFY_TOKEN` (same as in .env)
5. Click **"Verify and Save"**

If successful, you'll see a green checkmark ✓

## Step 7: Subscribe to Webhook Fields

1. In the same Webhook section, click **"Manage"**
2. Subscribe to these fields:
   - ✅ `messages` (required)
   - ✅ `message_status` (optional, for delivery status)
3. Click **"Save"**

## Step 8: Start Your Server

```bash
# Make sure you're in the project directory
cd /Users/admin/Library/CloudStorage/OneDrive-PARADIGMITTECHNOLOGYSERVICESPVTLTD/Codebase/samrat_twilio_chatbot

# Run the Meta WhatsApp server
python app_meta.py
```

You should see:
```
============================================================
Meta WhatsApp Bot Server Starting...
============================================================
Webhook URL: http://localhost:5000/webhook
Health Check: http://localhost:5000/health
============================================================
```

## Step 9: Test the Integration

### Test 1: Send a Test Message from Meta
1. In WhatsApp API Setup page, find **"Send and receive messages"**
2. Click **"Send test message"**
3. Enter your phone number
4. You should receive a test message on WhatsApp

### Test 2: Reply to the Bot
1. Reply to the test message with: `Hi`
2. Check your Flask logs - you should see:
   ```
   [WEBHOOK] Received data: {...}
   [MESSAGE] From: 917032490934, Type: text
   [MESSAGE] Text: Hi
   [BOT RESPONSE] 👋 *Sales Analytics Bot*...
   [SEND] Success: {...}
   ```
3. You should receive the bot's response on WhatsApp

### Test 3: Try a Query
Send: `Total sales in 2024`

You should get a response with sales data.

## Step 10: Add Your Number to Allowed List

Initially, you can only message numbers added to your app:

1. Go to **WhatsApp > API Setup**
2. Find **"To"** section
3. Click **"Manage phone number list"**
4. Add phone numbers that can interact with your bot
5. They'll receive a verification code on WhatsApp

## Production Deployment

For production, you need:

1. **Verified Business Account**
   - Complete business verification in Meta Business Manager
   
2. **Official WhatsApp Business Number**
   - Purchase or migrate your business number
   
3. **Production Server**
   - Deploy to a cloud service (AWS, GCP, Azure, Heroku, etc.)
   - Use proper HTTPS with SSL certificate
   
4. **Permanent Access Token**
   - Generate from System User (not temporary token)

## Comparison: Twilio vs Meta

| Feature | Twilio | Meta |
|---------|--------|------|
| Setup Complexity | Easy | Moderate |
| Cost | Pay per message | Free (with limits) |
| Message Limits | None (paid) | 1000/day (free tier) |
| Business Verification | Not required | Required for production |
| API Complexity | Simple | More complex |
| Features | Basic | Rich (buttons, media, etc.) |

## Troubleshooting

### Webhook Verification Fails
- Check `META_VERIFY_TOKEN` matches in .env and Meta console
- Ensure your server is running and accessible via public URL
- Check Flask logs for verification attempts

### Messages Not Received
- Verify webhook subscription includes `messages` field
- Check if sender's number is in allowed list
- Review Flask logs for incoming webhook data

### Messages Not Sent
- Verify `META_ACCESS_TOKEN` is valid
- Check `META_PHONE_NUMBER_ID` is correct
- Review error messages in Flask logs
- Ensure access token has `whatsapp_business_messaging` permission

### Rate Limits
- Free tier: 1000 messages/day
- Need more? Apply for higher tier or use Twilio

## Files Overview

- `app_meta.py` - New Flask app for Meta WhatsApp
- `app.py` - Original Twilio app (keep for backup)
- `bot.py` - Bot logic (shared by both)
- `.env` - Environment variables (add Meta credentials)

## Next Steps

1. ✅ Complete Meta app setup
2. ✅ Configure webhook
3. ✅ Test with your number
4. ✅ Add team members' numbers
5. ⬜ Apply for business verification (for production)
6. ⬜ Deploy to production server
7. ⬜ Monitor usage and costs

## Support

- Meta WhatsApp Docs: https://developers.facebook.com/docs/whatsapp
- WhatsApp Business API: https://business.whatsapp.com/
- Meta Developer Community: https://developers.facebook.com/community/

---

**Note**: Keep your Twilio setup (`app.py`) as backup until Meta integration is fully tested and working.
