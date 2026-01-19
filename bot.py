"""
bot.py
------
Core chatbot logic for the WhatsApp sales analytics bot.
This module processes user queries, interacts with the LLM,
executes SQL queries on the SQLite database, and formats responses.
"""

import sqlite3
import pandas as pd
import re
import yaml
import json
import copy
from datetime import datetime, timedelta
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

# ============================================================
# CONFIGURATION
# ============================================================

DB_FILE = "data/sales.db"
TABLE_NAME = "sales"
MODEL = "gpt-3.5-turbo"

MAX_ROWS = 5
MAX_TEXT_LEN = 28
DEFAULT_PERIOD = "90d"

conn = None
llm = None
COLUMNS = None

LOGS_FILE = "data/bot_logs.jsonl"

# ============================================================
# CONVERSATION MEMORY
# ============================================================

DEFAULT_CONTEXT = {
    "last_intent": None,
    "last_dealer": None,
    "last_period": DEFAULT_PERIOD,
    "last_dataframe": None,
    "pagination_index": 0,
    "last_snapshot": None,
    "disambiguation_options": [],
    "awaiting_selection": False
}

conversation_contexts = {}

def get_context(user_id: str) -> dict:
    if user_id not in conversation_contexts:
        conversation_contexts[user_id] = copy.deepcopy(DEFAULT_CONTEXT)
    return conversation_contexts[user_id]

def reset_context(user_id: str):
    conversation_contexts[user_id] = copy.deepcopy(DEFAULT_CONTEXT)

# ============================================================
# GREETING + MENU
# ============================================================

def is_greeting(msg: str) -> bool:
    return msg.lower().strip() in ["hi", "hello", "hey", "start", "help", "menu"]

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
        "You can also ask questions like:\n"
        "• Dealers with low sales activity\n"
        "• Sales trend last quarter\n"
    )

def is_end_conversation(msg: str) -> bool:
    return msg.lower().strip() in ["bye", "exit", "end", "reset"]

# ============================================================
# PERIOD PARSING
# ============================================================

def parse_period(msg: str) -> str:
    q = msg.lower()
    if "last month" in q:
        return "30d"
    if "last quarter" in q:
        return "90d"
    if "ytd" in q or "year to date" in q:
        return "ytd"
    return DEFAULT_PERIOD

def period_clause(period: str) -> str:
    if period == "ytd":
        return "Invoice Date >= date('now','start of year')"
    days = int(period.replace("d", ""))
    return f"Invoice Date >= date('now','-{days} day')"

# ============================================================
# EXTERNAL INTENT PATTERNS
# ============================================================

INTENT_PATTERNS = {}

def load_intents(path: str = "intents.yaml"):
    global INTENT_PATTERNS
    try:
        with open(path, "r") as f:
            INTENT_PATTERNS = yaml.safe_load(f)
        return True
    except Exception:
        load_default_intents()
        return False

def load_default_intents():
    global INTENT_PATTERNS
    INTENT_PATTERNS = {
        "SNAPSHOT": {"exact": ["1"], "keywords": ["snapshot", "dealer"], "phrases": [], "regex": []},
        "COMPARE": {"exact": ["2"], "keywords": ["compare", "vs"], "phrases": [], "regex": []},
        "TOP_DEALERS": {"exact": ["3"], "keywords": ["top"], "phrases": [], "regex": []},
        "STATE": {"exact": ["4"], "keywords": ["state"], "phrases": [], "regex": []},
        "PRODUCT": {"exact": ["5"], "keywords": ["product"], "phrases": [], "regex": []},
        "RISK_ANALYSIS": {"exact": [], "keywords": ["risk", "overdue"], "phrases": [], "regex": []},
        "DASHBOARD": {"exact": [], "keywords": ["dashboard"], "phrases": [], "regex": []},
        "ANALYTICS": {"exact": [], "keywords": ["trend", "average"], "phrases": [], "regex": []},
    }

def calculate_intent_score(msg: str, intent_patterns: dict = None) -> dict:
    intent_patterns = intent_patterns or INTENT_PATTERNS
    q = msg.lower().strip()
    scores = {}
    for intent, patterns in intent_patterns.items():
        score = 0
        if q in patterns.get("exact", []):
            score += 100
        for kw in patterns.get("keywords", []):
            if kw in q:
                score += 10
        scores[intent] = score
    return scores

def detect_intent(msg: str, context: dict) -> str:
    if context["awaiting_selection"] and msg.strip().isdigit():
        return "SKIP"

    scores = calculate_intent_score(msg)
    valid = {k: v for k, v in scores.items() if v > 0}
    if not valid:
        return "ANALYTICS"
    return max(valid, key=valid.get)

def handle_intent_with_context(intent: str, msg: str, context: dict) -> str:
    context["last_intent"] = intent
    context["last_period"] = parse_period(msg)

    if intent == "SNAPSHOT":
        dealers = extract_dealers(msg)
        dealer = dealers[0] if dealers else context["last_dealer"]
        if not dealer:
            return "❓ Which dealer would you like to check?"

        matches = fuzzy_dealers(dealer)
        if not matches:
            return f"❌ Dealer '{dealer}' not found."

        if len(matches) == 1:
            context["last_dealer"] = matches[0]
            snap = dealer_snapshot(matches[0], context["last_period"])
            score, status, reasons = health_score(snap)
            return snapshot_card(snap, score, status, reasons)

        context["disambiguation_options"] = matches
        context["awaiting_selection"] = True
        return "\n".join(f"{i+1}️⃣ {d}" for i, d in enumerate(matches))

    if intent == "COMPARE":
        dealers = extract_dealers(msg)
        if context["last_dealer"] and len(dealers) == 1:
            dealers.insert(0, context["last_dealer"])
        if len(dealers) < 2:
            return "❓ Please specify at least two dealers to compare."
        return compare_dealers(dealers, context["last_period"])

    if intent == "TOP_DEALERS":
        return top_dealers_overall(context["last_period"])

    if intent == "STATE":
        return sales_by_state(context["last_period"])

    if intent == "PRODUCT":
        products = fuzzy_products(msg)
        if not products:
            return "❓ Please specify a product."
        return top_dealers_by_product(products[0], context["last_period"])

    if intent == "RISK_ANALYSIS":
        return dealers_at_risk(context["last_period"])

    if intent == "DASHBOARD":
        return summary_dashboard(context["last_period"])

    prompt = ChatPromptTemplate.from_messages([
        ("system", f"Generate one safe SQL SELECT query for table {TABLE_NAME}"),
        ("human", msg)
    ])
    sql = llm.invoke(prompt.format_messages()).content.replace("`", '"')
    if not is_safe_sql(sql):
        return "Invalid analytics query."
    df = pd.read_sql(sql, conn)
    context["last_dataframe"] = df
    context["pagination_index"] = 0
    return format_table(df)

def search_answer(msg: str, context: dict, user_id: str) -> str:
    if is_end_conversation(msg):
        reset_context(user_id)
        return (
            "👋 *Goodbye!*\n\n"
            "I've cleared our conversation.\n"
            "Send *hi* or *menu* anytime to start again."
        )

    if is_greeting(msg):
        return menu_message()

    if msg == "show next" and context["last_dataframe"] is not None:
        context["pagination_index"] += MAX_ROWS
        return format_table(context["last_dataframe"], context["pagination_index"])

    if context["awaiting_selection"] and msg.isdigit():
        idx = int(msg) - 1
        choice = context["disambiguation_options"][idx]
        context["awaiting_selection"] = False
        context["last_dealer"] = choice
        snap = dealer_snapshot(choice, context["last_period"])
        score, status, reasons = health_score(snap)
        return snapshot_card(snap, score, status, reasons)

    intent = detect_intent(msg, context)
    response = handle_intent_with_context(intent, msg, context)
    log_conversation(msg, response, intent)
    return response

def handle_message(user_id: str, msg: str) -> tuple[str, None]:
    context = get_context(user_id)
    response = search_answer(msg, context, user_id)
    return response, None

def extract_dealers(msg: str) -> list:
    """Extract dealer names from message using regex"""
    parts = re.split(r"and|vs", msg, flags=re.IGNORECASE)
    dealers = []
    for p in parts:
        if "dealer" in p.lower():
            dealers.append(p.lower().split("dealer", 1)[1].strip().title())
    return dealers

def fuzzy_dealers(name: str) -> list:
    """Find dealers matching a search term"""
    df = pd.read_sql(
        "SELECT DISTINCT Dealer FROM sales WHERE Dealer LIKE ? LIMIT 5",
        conn,
        params=[f"%{name}%"]
    )
    return df["Dealer"].tolist()

def get_unique_products(limit: int = 10) -> list:
    """Fetch distinct products from the database"""
    df = pd.read_sql(
        "SELECT DISTINCT Product FROM sales LIMIT ?",
        conn,
        params=[limit]
    )
    return df["Product"].tolist()

def fuzzy_products(name: str) -> list:
    """Find products matching a search term"""
    df = pd.read_sql(
        "SELECT DISTINCT Product FROM sales WHERE Product LIKE ? LIMIT 5",
        conn,
        params=[f"%{name}%"]
    )
    return df["Product"].tolist()

def top_dealers_by_product(product: str, period: str, limit: int = 5) -> str:
    """Get top dealers for a specific product"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        Dealer,
        SUM("Basic Amount") as TotalSales,
        COUNT(*) as Orders
    FROM {TABLE_NAME}
    WHERE Product LIKE ? AND {period_filter}
    GROUP BY Dealer
    ORDER BY TotalSales DESC
    LIMIT ?
    """
    
    df = pd.read_sql(query, conn, params=[f"%{product}%", limit])
    
    if df.empty:
        return f"No dealers found for product '{product}'."
    
    return format_table(df)

def dealer_snapshot(dealer: str, period: str) -> dict:
    """Get sales snapshot for a specific dealer"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        '{dealer}' as Dealer,
        (SELECT DISTINCT State FROM {TABLE_NAME} WHERE Dealer = ? LIMIT 1) as State,
        SUM("Basic Amount") as TotalSales,
        COUNT(*) as Orders,
        MAX("Invoice Date") as LastTxn
    FROM {TABLE_NAME}
    WHERE Dealer = ? AND {period_filter}
    """
    
    df = pd.read_sql(query, conn, params=[dealer, dealer])
    if df.empty:
        return None
    
    result = df.iloc[0].to_dict()
    result["TotalSales"] = result["TotalSales"] or 0
    result["Orders"] = result["Orders"] or 0
    return result

def snapshot_previous_period(dealer: str, days: int) -> float:
    """Get total sales for previous period"""
    query = f"""
    SELECT SUM("Basic Amount") as TotalSales
    FROM {TABLE_NAME}
    WHERE Dealer = ? 
    AND Invoice Date >= date('now', '-{days*2} day')
    AND Invoice Date < date('now', '-{days} day')
    """
    
    df = pd.read_sql(query, conn, params=[dealer])
    return df.iloc[0]["TotalSales"] or 0 if not df.empty else 0

def health_score(snapshot: dict) -> tuple[int, str, list]:
    """Calculate health score for a dealer"""
    today = pd.Timestamp.today()
    days_since = (today - pd.to_datetime(snapshot["LastTxn"])).days

    rec = 100 if days_since <= 30 else 60 if days_since <= 60 else 20
    freq = 100 if snapshot["Orders"] >= 20 else 60 if snapshot["Orders"] >= 5 else 20
    mon = 100 if snapshot["TotalSales"] >= 1_000_000 else 60 if snapshot["TotalSales"] >= 500_000 else 20

    score = round(rec*0.3 + freq*0.25 + mon*0.3 )
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
    """Get dealer's rank within their state"""
    query = """
    SELECT Dealer
    FROM sales
    WHERE State = ?
    GROUP BY Dealer
    ORDER BY SUM("Basic Amount") DESC
    """
    df = pd.read_sql(query, conn, params=[state])
    ranked = df["Dealer"].tolist()
    return ranked.index(dealer) + 1 if dealer in ranked else -1

def snapshot_card(snapshot: dict, score: int, status: str, reasons: list) -> str:
    """Format detailed dealer snapshot response"""
    prev_sales = snapshot_previous_period(snapshot["Dealer"], 30)
    delta = snapshot["TotalSales"] - prev_sales
    delta_text = f"⬆️ ₹{int(delta):,}" if delta >= 0 else f"⬇️ ₹{int(abs(delta)):,}"

    rank = dealer_rank(snapshot["Dealer"], snapshot["State"])

    followups = []
    if score < 60:
        followups.append("📍 Plan a dealer visit")
    if delta < 0:
        followups.append("📉 Sales dropped vs last period")

    return f"""
📍 *Dealer Snapshot*

🏪 Dealer: *{snapshot['Dealer']}*
📍 Territory: {snapshot['State']}
🏆 Rank in State: #{rank}

💰 Sales: ₹{int(snapshot['TotalSales']):,} ({delta_text})
📦 Orders: {int(snapshot['Orders'])}
🕒 Last Order: {snapshot['LastTxn']}


{status} *Health Score*: {score}

🔍 *Why?*
• {"; ".join(reasons) if reasons else "Strong performance across KPIs"}

👉 *Suggested Actions*
• {"; ".join(followups) if followups else "Maintain engagement"}
""".strip()

def compare_dealers(dealers: list, period: str) -> str:
    """Compare two or more dealers"""
    rows = []

    for dealer in dealers:
        snap = dealer_snapshot(dealer, period)
        if snap:
            score, _, _ = health_score(snap)
            rows.append({
                "Dealer": dealer,
                "Sales": int(snap["TotalSales"]),
                "Orders": int(snap["Orders"]),
                "HealthScore": score
            })

    if len(rows) < 2:
        return "Please specify at least *two valid dealers* to compare."

    df = pd.DataFrame(rows)

    best_sales = df.loc[df["Sales"].idxmax()]["Dealer"]
    best_health = df.loc[df["HealthScore"].idxmax()]["Dealer"]

    return (
        format_table(df) +
        f"\n🏆 *Highest Sales*: {best_sales}\n"
        f"💚 *Best Health Score*: {best_health}"
    )

def format_table(df: pd.DataFrame, start: int = 0) -> str:
    """Format DataFrame as WhatsApp-compatible table"""
    chunk = df.iloc[start:start+MAX_ROWS]
    header = " | ".join(chunk.columns)
    lines = [header, "-" * len(header)]

    for _, r in chunk.iterrows():
        lines.append(" | ".join(str(v)[:MAX_TEXT_LEN] for v in r))

    text = "```\n" + "\n".join(lines) + "\n```"
    if start + MAX_ROWS < len(df):
        text += "\n➡️ Type *show next* to see more"
    return text

def top_dealers_overall(period: str, limit: int = 5) -> str:
    """Get top dealers by sales overall"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        Dealer,
        SUM("Basic Amount") as TotalSales,
        COUNT(*) as Orders
    FROM {TABLE_NAME}
    WHERE {period_filter}
    GROUP BY Dealer
    ORDER BY TotalSales DESC
    LIMIT ?
    """
    
    df = pd.read_sql(query, conn, params=[limit])
    if df.empty:
        return "No dealer data available."
    return format_table(df)

def sales_by_state(period: str) -> str:
    """Get sales aggregated by state"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        State,
        COUNT(DISTINCT Dealer) as DealerCount,
        SUM("Basic Amount") as TotalSales,
        COUNT(*) as Orders
    FROM {TABLE_NAME}
    WHERE {period_filter}
    GROUP BY State
    ORDER BY TotalSales DESC
    """
    
    df = pd.read_sql(query, conn)
    if df.empty:
        return "No state data available."
    return format_table(df)

def dealers_at_risk(period: str) -> str:
    """Find dealers with poor health scores"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        Dealer,
        SUM("Basic Amount") as TotalSales,
        COUNT(*) as Orders
    FROM {TABLE_NAME}
    WHERE {period_filter}
    GROUP BY Dealer
    HAVING Orders < 5 OR TotalSales < 100000
    ORDER BY TotalSales ASC
    """
    
    df = pd.read_sql(query, conn)
    if df.empty:
        return "🟢 All dealers look healthy!"
    return "🔴 **Dealers At Risk:**\n" + format_table(df)

def summary_dashboard(period: str) -> str:
    """Show overall sales health snapshot"""
    period_filter = period_clause(period)
    query = f"""
    SELECT 
        COUNT(DISTINCT Dealer) as TotalDealers,
        COUNT(DISTINCT State) as TotalStates,
        SUM("Basic Amount") as TotalSales,
        AVG("Basic Amount") as AvgOrderValue
    FROM {TABLE_NAME}
    WHERE {period_filter}
    """
    
    df = pd.read_sql(query, conn)
    stats = df.iloc[0].to_dict()
    
    return f"""
📊 *Sales Dashboard* ({period})

🏪 Total Dealers: {stats['TotalDealers']}
📍 States Covered: {stats['TotalStates']}
💰 Total Sales: ₹{int(stats['TotalSales']):,}
📦 Avg Order Value: ₹{int(stats['AvgOrderValue']):,}
""".strip()

def log_conversation(msg: str, response: str, intent: str):
    """Log user queries and bot responses"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user_message": msg,
        "intent": intent,
        "response": response[:100],
    }
    with open(LOGS_FILE, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

def setup_bot():
    global conn, llm, COLUMNS
    load_intents()
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    df = pd.read_sql(f"SELECT * FROM {TABLE_NAME} LIMIT 1", conn)
    COLUMNS = df.columns.tolist()
    llm = ChatOpenAI(model=MODEL, temperature=0)

