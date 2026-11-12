"""
bot.py
------
Core chatbot logic for the WhatsApp sales analytics bot.

Flow: incoming text -> intent detection (rules from intents.yaml) -> either a
canned, parameterised SQL report or an LLM-generated (and validated) SELECT
against a read-only SQLite connection -> WhatsApp-friendly text.
"""

import copy
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION (override via environment variables)
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DB_FILE = os.getenv("DB_FILE", str(BASE_DIR / "data" / "sales.db"))
LOGS_FILE = os.getenv("LOGS_FILE", str(BASE_DIR / "data" / "bot_logs.jsonl"))
INTENTS_FILE = os.getenv("INTENTS_FILE", str(BASE_DIR / "intents.yaml"))
TABLE_NAME = "sales"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MAX_ROWS = 5                  # rows per WhatsApp message
MAX_TEXT_LEN = 28             # truncate long cell values
MAX_SQL_ROWS = 200            # hard cap on LLM-generated queries
DEFAULT_PERIOD = "90d"
MAX_PERIOD_DAYS = 3650

# Health-score thresholds
RISK_MIN_ORDERS = 5
RISK_MIN_SALES = 100_000

# ============================================================
# CONVERSATION MEMORY
# ============================================================

DEFAULT_CONTEXT = {
    "last_intent": None,
    "last_dealer": None,
    "last_period": DEFAULT_PERIOD,
    "last_dataframe": None,
    "pagination_index": 0,
    "disambiguation_options": [],
    "awaiting_selection": False,
    "pending_intent": None,   # intent waiting for a missing dealer/product name
}

_conversation_contexts: dict = {}
_lock = threading.RLock()


def get_context(user_id: str) -> dict:
    if user_id not in _conversation_contexts:
        _conversation_contexts[user_id] = copy.deepcopy(DEFAULT_CONTEXT)
    return _conversation_contexts[user_id]


def reset_context(user_id: str):
    _conversation_contexts[user_id] = copy.deepcopy(DEFAULT_CONTEXT)


# ============================================================
# DATABASE
# ============================================================

def get_conn() -> sqlite3.Connection:
    """Open a short-lived, read-only connection (safe to use from any thread)."""
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_query(sql: str, params: Optional[list] = None) -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql(sql, conn, params=params or [])
    finally:
        conn.close()


def _like(term: str) -> str:
    """Escape LIKE wildcards in user input."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ============================================================
# GREETING + MENU
# ============================================================

def is_greeting(msg: str) -> bool:
    return msg.lower().strip() in {"hi", "hello", "hey", "start", "help", "menu"}


def is_end_conversation(msg: str) -> bool:
    return msg.lower().strip() in {"bye", "exit", "end", "reset"}


def menu_message() -> str:
    return (
        "👋 *Welcome!*\n\n"
        "I can help you quickly analyze dealer performance and sales data.\n\n"
        "What would you like to do?\n\n"
        "1️⃣ Dealer Snapshot\n"
        "2️⃣ Compare Dealers\n"
        "3️⃣ Top Dealers by Sales\n"
        "4️⃣ Sales by State\n"
        "5️⃣ Sales by Product\n\n"
        "You can also ask things like:\n"
        "• Dealers at risk\n"
        "• Sales dashboard last month\n"
        "• Top 5 dealers for TMT\n"
    )


# ============================================================
# PERIOD PARSING
# ============================================================

def parse_period(msg: str) -> Optional[str]:
    """Return a period like '30d' / 'ytd' if the message names one, else None."""
    q = msg.lower()
    m = re.search(r"(?:last|past)\s+(\d{1,4})\s+days?", q) or re.search(r"\b(\d{1,4})\s*days?\b", q)
    if m:
        return f"{min(int(m.group(1)), MAX_PERIOD_DAYS)}d"
    if "last week" in q:
        return "7d"
    if "last month" in q:
        return "30d"
    if "last quarter" in q:
        return "90d"
    if "last year" in q:
        return "365d"
    if "ytd" in q or "year to date" in q:
        return "ytd"
    return None


def period_days(period: str) -> int:
    if period == "ytd":
        return (datetime.now() - datetime(datetime.now().year, 1, 1)).days + 1
    return int(period.rstrip("d"))


def period_label(period: str) -> str:
    return "year to date" if period == "ytd" else f"last {period_days(period)} days"


def period_clause(period: str) -> str:
    """SQL condition for the current period. Days are int-cast, so no injection."""
    if period == "ytd":
        return "\"Invoice Date\" >= date('now','start of year')"
    return f"\"Invoice Date\" >= date('now','-{int(period_days(period))} day')"


def previous_period_clause(period: str) -> str:
    """SQL condition for the period of equal length immediately before this one."""
    d = period_days(period)
    return (
        f"\"Invoice Date\" >= date('now','-{2 * d} day') "
        f"AND \"Invoice Date\" < date('now','-{d} day')"
    )


# ============================================================
# INTENT DETECTION (rules live in intents.yaml)
# ============================================================

INTENT_PATTERNS: dict = {}

EXACT_SCORE, REGEX_SCORE, PHRASE_SCORE, KEYWORD_SCORE = 100, 30, 20, 10


def load_default_intents():
    global INTENT_PATTERNS
    INTENT_PATTERNS = {
        "SNAPSHOT": {"exact": ["1"], "keywords": ["snapshot"], "phrases": [], "regex": []},
        "COMPARE": {"exact": ["2"], "keywords": ["compare", "vs"], "phrases": [], "regex": []},
        "TOP_DEALERS": {"exact": ["3"], "keywords": ["top dealers"], "phrases": [], "regex": []},
        "STATE": {"exact": ["4"], "keywords": ["state"], "phrases": [], "regex": []},
        "PRODUCT": {"exact": ["5"], "keywords": ["product"], "phrases": [], "regex": []},
        "RISK_ANALYSIS": {"exact": [], "keywords": ["risk", "overdue"], "phrases": [], "regex": []},
        "DASHBOARD": {"exact": [], "keywords": ["dashboard"], "phrases": [], "regex": []},
        "ANALYTICS": {"exact": [], "keywords": ["trend", "average"], "phrases": [], "regex": []},
    }


def load_intents(path: Optional[str] = None) -> bool:
    global INTENT_PATTERNS
    try:
        with open(path or INTENTS_FILE, "r", encoding="utf-8") as f:
            INTENT_PATTERNS = yaml.safe_load(f) or {}
        return True
    except Exception:
        logger.exception("Could not load intents file; using defaults")
        load_default_intents()
        return False


def _contains_word(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def calculate_intent_score(msg: str, intent_patterns: Optional[dict] = None) -> dict:
    intent_patterns = intent_patterns or INTENT_PATTERNS
    q = msg.lower().strip()
    scores = {}
    for intent, p in intent_patterns.items():
        score = 0
        if q in p.get("exact", []):
            score += EXACT_SCORE
        score += REGEX_SCORE * sum(1 for rx in p.get("regex", []) if re.search(rx, q))
        score += PHRASE_SCORE * sum(1 for ph in p.get("phrases", []) if _contains_word(q, ph))
        score += KEYWORD_SCORE * sum(1 for kw in p.get("keywords", []) if _contains_word(q, kw))
        scores[intent] = score
    return scores


def detect_intent(msg: str) -> Optional[str]:
    """Best-scoring intent, or None when nothing matches."""
    scores = {k: v for k, v in calculate_intent_score(msg).items() if v > 0}
    return max(scores, key=scores.get) if scores else None


# ============================================================
# ENTITY EXTRACTION
# ============================================================

_NOISE = re.compile(
    r"\b(snapshot|dealers?|check|tell me about|info|show|me|of|for|the|performance|"
    r"compare|comparison|versus|vs|with|and|top|best|\d+|"
    r"last\s+\w+|past\s+\d+\s+days?|ytd|year to date|sales|product|products|"
    r"dealers? for|state|by)\b\.?",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"\s+(?:vs\.?|versus|and|with)\s+", re.IGNORECASE)


def clean_name(text: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", text)).strip(" ,.-")


def extract_dealers(msg: str) -> list:
    """Dealer search terms from a message ('compare A vs B', 'snapshot of A', 'A')."""
    body = re.sub(r"^\s*(compare|comparison)\b", "", msg, flags=re.IGNORECASE)
    parts = _SPLIT.split(body)
    names = [clean_name(p) for p in parts]
    return [n for n in names if n]


def extract_product(msg: str) -> str:
    m = re.search(r"\b(?:for|of)\s+(.+)$", msg, flags=re.IGNORECASE)
    term = m.group(1) if m else msg
    return clean_name(term)


def fuzzy_dealers(name: str, limit: int = 5) -> list:
    """Dealers matching a search term; an exact (case-insensitive) hit wins."""
    df = run_query(
        "SELECT DISTINCT Dealer FROM sales WHERE Dealer LIKE ? ESCAPE '\\' LIMIT ?",
        [_like(name), limit],
    )
    matches = df["Dealer"].tolist()
    exact = [d for d in matches if d.lower() == name.lower()]
    return exact or matches


def fuzzy_products(name: str, limit: int = 5) -> list:
    df = run_query(
        "SELECT DISTINCT Product FROM sales WHERE Product LIKE ? ESCAPE '\\' LIMIT ?",
        [_like(name), limit],
    )
    return df["Product"].tolist()


# ============================================================
# REPORTS
# ============================================================

def dealer_snapshot(dealer: str, period: str) -> dict:
    """Sales for one dealer in the period, plus last order date (all time)."""
    cur = run_query(
        f"""
        SELECT COALESCE(SUM("Basic Amount"), 0) AS TotalSales, COUNT(*) AS Orders
        FROM {TABLE_NAME}
        WHERE Dealer = ? AND {period_clause(period)}
        """,
        [dealer],
    ).iloc[0]
    info = run_query(
        f"""SELECT MIN(State) AS State, MAX("Invoice Date") AS LastTxn
            FROM {TABLE_NAME} WHERE Dealer = ?""",
        [dealer],
    ).iloc[0]
    return {
        "Dealer": dealer,
        "State": info["State"],
        "TotalSales": float(cur["TotalSales"]),
        "Orders": int(cur["Orders"]),
        "LastTxn": info["LastTxn"],
        "Period": period,
    }


def previous_period_sales(dealer: str, period: str) -> float:
    df = run_query(
        f"""SELECT COALESCE(SUM("Basic Amount"), 0) AS TotalSales
            FROM {TABLE_NAME} WHERE Dealer = ? AND {previous_period_clause(period)}""",
        [dealer],
    )
    return float(df.iloc[0]["TotalSales"])


def health_score(snapshot: dict) -> tuple:
    """Weighted recency / frequency / monetary score (weights sum to 1.0)."""
    last = pd.to_datetime(snapshot["LastTxn"], errors="coerce")
    days_since = 9999 if pd.isna(last) else (pd.Timestamp.today() - last).days

    rec = 100 if days_since <= 30 else 60 if days_since <= 60 else 20
    freq = 100 if snapshot["Orders"] >= 20 else 60 if snapshot["Orders"] >= 5 else 20
    mon = 100 if snapshot["TotalSales"] >= 1_000_000 else 60 if snapshot["TotalSales"] >= 500_000 else 20

    score = round(rec * 0.35 + freq * 0.30 + mon * 0.35)
    status = "🟢 Healthy" if score >= 80 else "🟡 Watch" if score >= 60 else "🔴 At Risk"

    reasons = []
    if rec < 60:
        reasons.append("Low recent activity")
    if freq < 60:
        reasons.append("Low order frequency")
    if mon < 60:
        reasons.append("Low sales contribution")
    return score, status, reasons


def dealer_rank(dealer: str, state: str) -> int:
    df = run_query(
        f"""SELECT Dealer FROM {TABLE_NAME} WHERE State = ?
            GROUP BY Dealer ORDER BY SUM("Basic Amount") DESC""",
        [state],
    )
    ranked = df["Dealer"].tolist()
    return ranked.index(dealer) + 1 if dealer in ranked else -1


def snapshot_card(snapshot: dict, score: int, status: str, reasons: list) -> str:
    period = snapshot["Period"]
    prev = previous_period_sales(snapshot["Dealer"], period)
    delta = snapshot["TotalSales"] - prev
    delta_text = f"⬆️ ₹{int(delta):,}" if delta >= 0 else f"⬇️ ₹{int(abs(delta)):,}"
    rank = dealer_rank(snapshot["Dealer"], snapshot["State"])
    rank_text = f"#{rank}" if rank > 0 else "n/a"

    followups = []
    if score < 60:
        followups.append("📍 Plan a dealer visit")
    if delta < 0:
        followups.append("📉 Sales dropped vs previous period")

    return f"""
📍 *Dealer Snapshot* ({period_label(period)})

🏪 Dealer: *{snapshot['Dealer']}*
📍 Territory: {snapshot['State']}
🏆 Rank in State: {rank_text}

💰 Sales: ₹{int(snapshot['TotalSales']):,} ({delta_text} vs previous)
📦 Orders: {snapshot['Orders']}
🕒 Last Order: {snapshot['LastTxn'] or 'n/a'}

{status} *Health Score*: {score}

🔍 *Why?*
• {"; ".join(reasons) if reasons else "Strong performance across KPIs"}

👉 *Suggested Actions*
• {"; ".join(followups) if followups else "Maintain engagement"}
""".strip()


def compare_dealers(dealers: list, period: str) -> str:
    rows = []
    for dealer in dealers:
        snap = dealer_snapshot(dealer, period)
        score, _, _ = health_score(snap)
        rows.append({
            "Dealer": dealer,
            "Sales": int(snap["TotalSales"]),
            "Orders": snap["Orders"],
            "Health": score,
        })
    df = pd.DataFrame(rows)
    best_sales = df.loc[df["Sales"].idxmax(), "Dealer"]
    best_health = df.loc[df["Health"].idxmax(), "Dealer"]
    return (
        format_table(df)
        + f"\n🏆 *Highest Sales*: {best_sales}"
        + f"\n💚 *Best Health Score*: {best_health}"
    )


def top_dealers_overall(period: str, limit: int = 10) -> pd.DataFrame:
    return run_query(
        f"""SELECT Dealer, SUM("Basic Amount") AS TotalSales, COUNT(*) AS Orders
            FROM {TABLE_NAME} WHERE {period_clause(period)}
            GROUP BY Dealer ORDER BY TotalSales DESC LIMIT ?""",
        [limit],
    )


def top_dealers_by_product(product: str, period: str, limit: int = 10) -> pd.DataFrame:
    return run_query(
        f"""SELECT Dealer, SUM("Basic Amount") AS TotalSales, COUNT(*) AS Orders
            FROM {TABLE_NAME}
            WHERE Product LIKE ? ESCAPE '\\' AND {period_clause(period)}
            GROUP BY Dealer ORDER BY TotalSales DESC LIMIT ?""",
        [_like(product), limit],
    )


def sales_by_state(period: str) -> pd.DataFrame:
    return run_query(
        f"""SELECT State, COUNT(DISTINCT Dealer) AS Dealers,
                   SUM("Basic Amount") AS TotalSales, COUNT(*) AS Orders
            FROM {TABLE_NAME} WHERE {period_clause(period)}
            GROUP BY State ORDER BY TotalSales DESC"""
    )


def dealers_at_risk(period: str) -> pd.DataFrame:
    """Includes dealers with *no* sales in the period (the most at-risk ones)."""
    return run_query(
        f"""SELECT Dealer,
                   SUM(CASE WHEN {period_clause(period)} THEN "Basic Amount" ELSE 0 END) AS TotalSales,
                   SUM(CASE WHEN {period_clause(period)} THEN 1 ELSE 0 END) AS Orders
            FROM {TABLE_NAME}
            GROUP BY Dealer
            HAVING Orders < ? OR TotalSales < ?
            ORDER BY TotalSales ASC""",
        [RISK_MIN_ORDERS, RISK_MIN_SALES],
    )


def summary_dashboard(period: str) -> str:
    s = run_query(
        f"""SELECT COUNT(DISTINCT Dealer) AS Dealers, COUNT(DISTINCT State) AS States,
                   COALESCE(SUM("Basic Amount"), 0) AS Total,
                   COALESCE(AVG("Basic Amount"), 0) AS Avg
            FROM {TABLE_NAME} WHERE {period_clause(period)}"""
    ).iloc[0]
    return f"""
📊 *Sales Dashboard* ({period_label(period)})

🏪 Active Dealers: {int(s['Dealers'])}
📍 States Covered: {int(s['States'])}
💰 Total Sales: ₹{int(s['Total']):,}
📦 Avg Order Value: ₹{int(s['Avg']):,}
""".strip()


# ============================================================
# FORMATTING
# ============================================================

def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.0f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v)[:MAX_TEXT_LEN]


def format_table(df: pd.DataFrame, start: int = 0) -> str:
    """Format a DataFrame as a monospaced WhatsApp table, MAX_ROWS at a time."""
    chunk = df.iloc[start:start + MAX_ROWS]
    header = " | ".join(map(str, chunk.columns))
    lines = [header, "-" * len(header)]
    for row in chunk.itertuples(index=False):
        lines.append(" | ".join(_fmt(v) for v in row))
    text = "```\n" + "\n".join(lines) + "\n```"
    if start + MAX_ROWS < len(df):
        text += "\n➡️ Type *show next* to see more"
    return text


def reply_table(df: pd.DataFrame, context: dict, title: str = "") -> str:
    """Store a result for 'show next' paging and return its first page."""
    context["last_dataframe"] = df
    context["pagination_index"] = 0
    return (f"{title}\n" if title else "") + format_table(df)


# ============================================================
# LLM FALLBACK (free-form analytics)
# ============================================================

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|"
    r"reindex|load_extension|truncate)\b",
    re.IGNORECASE,
)


def is_safe_sql(sql: str) -> bool:
    """Allow exactly one read-only SELECT statement and nothing else."""
    s = sql.strip().rstrip(";").strip()
    if not s or ";" in s or "--" in s or "/*" in s:
        return False
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        return False
    return not _FORBIDDEN_SQL.search(s)


def _schema_description() -> str:
    df = run_query(f"SELECT * FROM {TABLE_NAME} LIMIT 1")
    return ", ".join(f'"{c}"' for c in df.columns)


def generate_sql(msg: str) -> str:
    """Ask the LLM for a SELECT. Imported lazily so tests/other intents need no API key."""
    from langchain_openai import ChatOpenAI

    system = (
        f"You write one SQLite SELECT query over table {TABLE_NAME}.\n"
        f"Columns (always double-quote them): {_schema_description()}.\n"
        "Amounts are in \"Basic Amount\" (INR). \"Invoice Date\" is ISO text (YYYY-MM-DD); "
        "use date('now', ...) for relative dates.\n"
        f"Return ONLY the SQL, no explanation, no markdown. Always include LIMIT {MAX_SQL_ROWS} or less."
    )
    llm = ChatOpenAI(model=MODEL, temperature=0)
    out = llm.invoke([("system", system), ("human", msg)]).content
    return re.sub(r"^```(?:sql)?|```$", "", out.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()


def answer_with_llm(msg: str, context: dict) -> str:
    sql = generate_sql(msg)
    if not is_safe_sql(sql):
        logger.warning("Rejected unsafe SQL: %s", sql)
        return "❌ I couldn't turn that into a safe query. Try rephrasing, or send *menu*."
    sql = f"SELECT * FROM ({sql.rstrip(';')}) LIMIT {MAX_SQL_ROWS}"
    try:
        df = run_query(sql)
    except Exception:
        logger.exception("LLM SQL failed: %s", sql)
        return "❌ I couldn't run that query. Try rephrasing, or send *menu*."
    if df.empty:
        return "No data found for that question."
    return reply_table(df, context)


# ============================================================
# INTENT HANDLING
# ============================================================

def _select_dealer(name: str, context: dict) -> tuple:
    """Resolve a search term. Returns (dealer, None) or (None, message_for_user)."""
    matches = fuzzy_dealers(name)
    if not matches:
        return None, f"❌ Dealer '{name}' not found."
    if len(matches) == 1:
        return matches[0], None
    context["disambiguation_options"] = matches
    context["awaiting_selection"] = True
    options = "\n".join(f"{i + 1}️⃣ {d}" for i, d in enumerate(matches))
    return None, f"Did you mean:\n{options}\n\nReply with a number."


def _snapshot_for(dealer: str, context: dict) -> str:
    context["last_dealer"] = dealer
    snap = dealer_snapshot(dealer, context["last_period"])
    score, status, reasons = health_score(snap)
    return snapshot_card(snap, score, status, reasons)


def handle_intent(intent: str, msg: str, context: dict) -> str:
    context["last_intent"] = intent
    context["pending_intent"] = None
    context["last_period"] = parse_period(msg) or context["last_period"]
    period = context["last_period"]

    if intent == "SNAPSHOT":
        names = extract_dealers(msg)
        name = names[0] if names else context["last_dealer"]
        if not name:
            context["pending_intent"] = "SNAPSHOT"
            return "❓ Which dealer would you like to check?"
        dealer, problem = _select_dealer(name, context)
        return problem or _snapshot_for(dealer, context)

    if intent == "COMPARE":
        names = extract_dealers(msg)
        if context["last_dealer"] and len(names) == 1:
            names.insert(0, context["last_dealer"])
        if len(names) < 2:
            context["pending_intent"] = "COMPARE"
            return "❓ Which dealers? e.g. *Dealer A vs Dealer B*"
        resolved = []
        for n in names:
            matches = fuzzy_dealers(n)
            if not matches:
                return f"❌ Dealer '{n}' not found."
            if len(matches) > 1:
                return f"'{n}' matches several dealers: {', '.join(matches)}.\nPlease be more specific."
            resolved.append(matches[0])
        return compare_dealers(resolved, period)

    if intent == "TOP_DEALERS":
        df = top_dealers_overall(period)
        if df.empty:
            return "No dealer data available for that period."
        return reply_table(df, context, f"🏆 *Top Dealers* ({period_label(period)})")

    if intent == "STATE":
        df = sales_by_state(period)
        if df.empty:
            return "No state data available for that period."
        return reply_table(df, context, f"📍 *Sales by State* ({period_label(period)})")

    if intent == "PRODUCT":
        term = extract_product(msg)
        products = fuzzy_products(term) if term else []
        if not products:
            context["pending_intent"] = "PRODUCT"
            if term:
                return f"❌ No product matching '{term}'. Which product?"
            return "❓ Which product? e.g. *top dealers for TMT*"
        df = top_dealers_by_product(term, period)
        if df.empty:
            return f"No sales for '{term}' in that period."
        return reply_table(df, context, f"📦 *Top Dealers for {term}* ({period_label(period)})")

    if intent == "RISK_ANALYSIS":
        df = dealers_at_risk(period)
        if df.empty:
            return "🟢 All dealers look healthy!"
        return reply_table(df, context, f"🔴 *Dealers At Risk* ({period_label(period)})")

    if intent == "DASHBOARD":
        return summary_dashboard(period)

    return answer_with_llm(msg, context)


def search_answer(msg: str, context: dict, user_id: str) -> tuple:
    """Returns (response, intent_label) for logging."""
    if is_end_conversation(msg):
        reset_context(user_id)
        return (
            "👋 *Goodbye!*\n\nI've cleared our conversation.\n"
            "Send *hi* or *menu* anytime to start again.",
            "END",
        )

    if is_greeting(msg):
        return menu_message(), "GREETING"

    if msg.lower() == "show next":
        df = context["last_dataframe"]
        if df is None or context["pagination_index"] + MAX_ROWS >= len(df):
            return "There's nothing more to show.", "PAGINATION"
        context["pagination_index"] += MAX_ROWS
        return format_table(df, context["pagination_index"]), "PAGINATION"

    if context["awaiting_selection"]:
        options = context["disambiguation_options"]
        if msg.isdigit():
            idx = int(msg) - 1
            if 0 <= idx < len(options):
                context["awaiting_selection"] = False
                return _snapshot_for(options[idx], context), "SNAPSHOT"
            return f"Please reply with a number between 1 and {len(options)}.", "SELECTION"
        context["awaiting_selection"] = False  # they moved on

    intent = detect_intent(msg)
    if intent is None:
        intent = context["pending_intent"] or "ANALYTICS"
    return handle_intent(intent, msg, context), intent


def handle_message(user_id: str, msg: str) -> str:
    msg = (msg or "").strip()
    if not msg:
        return menu_message()
    with _lock:
        context = get_context(user_id)
        try:
            response, intent = search_answer(msg, context, user_id)
        except Exception:
            logger.exception("Error handling message")
            response, intent = "⚠️ Something went wrong. Please try again, or send *menu*.", "ERROR"
    log_conversation(msg, response, intent)
    return response


# ============================================================
# LOGGING + SETUP
# ============================================================

def log_conversation(msg: str, response: str, intent: str):
    """Append a JSON line per exchange. Never lets logging break a reply."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_message": msg,
        "intent": intent,
        "response": response[:100],
    }
    try:
        os.makedirs(os.path.dirname(LOGS_FILE), exist_ok=True)
        with open(LOGS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Could not write conversation log")


def setup_bot():
    """Load intent rules and fail fast if the database is missing."""
    load_intents()
    if not os.path.exists(DB_FILE):
        raise FileNotFoundError(
            f"{DB_FILE} not found. Put your CSV at data/sales.csv and run: python csv_to_sql.py"
        )
