# WhatsApp Sales Analytics Bot

A WhatsApp chatbot that answers sales questions in plain English. Common questions
(dealer snapshot, compare dealers, top dealers, sales by state/product, dealers at
risk, dashboard) run **fixed, parameterised SQL**. Anything else goes to an LLM that
writes a `SELECT`, which is validated and run on a **read-only** SQLite connection.
Numbers always come from your database, never from the model.

**Stack:** Flask · Twilio WhatsApp · SQLite · pandas · OpenAI (via LangChain)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your keys

# load your data (needs columns: Dealer, State, Product, Basic Amount, Invoice Date)
python csv_to_sql.py --csv data/sales.csv --db data/sales.db   # add --dayfirst for DD/MM/YYYY dates

python app.py                   # http://localhost:5000
```

Expose it to Twilio (e.g. `ngrok http 5000`) and set the WhatsApp sandbox
"When a message comes in" webhook to `https://<your-host>/whatsapp` (POST).
If you're behind ngrok or a proxy, set `PUBLIC_WEBHOOK_URL` to that exact URL so
Twilio's request signature can be verified.

For production use a real server: `gunicorn app:app`.

## What you can ask

| Say | Gets you |
|---|---|
| `hi` / `menu` | Menu |
| `snapshot of Balaji`, `dealer Anand last month` | Sales, rank, health score, suggested actions |
| `compare Balaji vs Anand` | Side-by-side table |
| `top dealers`, `top 5 dealers for TMT` | Ranking (overall or per product) |
| `sales by state` | State breakdown |
| `dealers at risk` | Low-activity dealers, including ones with no recent sales |
| `dashboard last quarter` | Headline numbers |
| `show next` | Next page of the last table |
| anything else | LLM-generated query, e.g. *which state sold the most coils?* |

Periods: `last week`, `last month`, `last quarter`, `last year`, `last 45 days`, `ytd`
(default 90 days). The period carries over to follow-up questions.

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | LLM fallback | required for free-form questions |
| `OPENAI_MODEL` | Model name | `gpt-4o-mini` |
| `TWILIO_AUTH_TOKEN` | Verifies webhook signatures (403 otherwise) | unset = no check, dev only |
| `PUBLIC_WEBHOOK_URL` | URL Twilio calls, if different from what Flask sees | – |
| `DB_FILE`, `LOGS_FILE` | Paths | `data/sales.db`, `data/bot_logs.jsonl` |
| `FLASK_DEBUG` | `1` to enable debug mode | off |

Intent rules (exact / keyword / phrase / regex) live in `intents.yaml`, so you can tune
routing without touching code.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use a small synthetic database and mock the LLM, so no API key is needed.

## Project layout

```
app.py          Flask webhook (signature check, TwiML reply, /health)
bot.py          Intent routing, reports, SQL safety, formatting
csv_to_sql.py   CSV -> SQLite loader (validates columns, normalises dates, indexes)
intents.yaml    Intent matching rules
tests/          pytest suite
```

## Notes and limits

- Conversation state is kept in memory per phone number, so it resets on restart and
  isn't shared across multiple workers. Use Redis or a DB if you scale out.
- `data/bot_logs.jsonl` records user messages; it's git-ignored. Keep it that way.
