import sqlite3
import pandas as pd
import re
from copy import deepcopy
from datetime import datetime
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

# ============================================================
# CONFIG
# ============================================================

DB_FILE = "data/sales.db"
TABLE_NAME = "sales"

DATE_COL = "Date"
AMOUNT_COL = " Basic Amount "

MODEL = "gemini-2.0-flash-exp"

MAX_ROWS = 5
MAX_LIMIT = 50
DEFAULT_PERIOD = "30d"

llm = None
COLUMNS = None

# ============================================================
# DB
# ============================================================

def get_db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def read_sql(query, params=None):
    print(f"\n[DEBUG] read_sql called")
    print(f"[DEBUG] SQL Query: {query}")
    print(f"[DEBUG] Params: {params}")
    
    with get_db() as conn:
        df = pd.read_sql(query, conn, params=params)
    
    print(f"[DEBUG] Query returned {len(df)} rows")
    print(f"[DEBUG] Columns: {df.columns.tolist()}")
    if not df.empty:
        print(f"[DEBUG] First row: {df.iloc[0].to_dict()}")

    if AMOUNT_COL in df.columns:
        print(f"[DEBUG] Processing {AMOUNT_COL} column")
        df[AMOUNT_COL] = (
            df[AMOUNT_COL]
            .astype(str)
            .str.replace(r'[^0-9.]', '', regex=True)
            .replace('', '0')
            .astype(float)
        )
        print(f"[DEBUG] {AMOUNT_COL} after processing: {df[AMOUNT_COL].tolist()[:5]}")

    return df

# ============================================================
# NUMERIC SANITIZATION (🔥 FINAL FIX)
# ============================================================

def amount_expr():
    return (
        "CAST("
        "NULLIF("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        f'"{AMOUNT_COL}"'
        ", '₹', ''"
        "), 'Rs.', ''"
        "), ',', ''"
        "), ' ', ''"
        "), CHAR(160), ''"
        ")"
        ", ''"
        ") AS REAL)"
    )

# ============================================================
# CONTEXT
# ============================================================

DEFAULT_CONTEXT = {
    "last_dataframe": None,
    "pagination_index": 0,
    "page_size": MAX_ROWS,
    "last_period": DEFAULT_PERIOD,
    "last_dealer": None,
}

contexts = {}

def get_context(user_id):
    if user_id not in contexts:
        contexts[user_id] = deepcopy(DEFAULT_CONTEXT)
    return contexts[user_id]

def reset_context(user_id):
    contexts[user_id] = deepcopy(DEFAULT_CONTEXT)

# ============================================================
# GREETING / RESET
# ============================================================

def is_greeting(msg):
    return msg.lower().strip() in {"hi", "hello", "help", "menu", "start"}

def is_reset(msg):
    return msg.lower().strip() in {"reset", "restart", "start over", "clear", "bye"}

def menu_message():
    return (
        "👋 *Sales Analytics Bot*\n\n"
        "You can ask:\n"
        "• Total sales in 2023\n"
        "• Average sales by city in 2023\n"
        "• Top 5 cities by sales\n"
        "• Sales by product\n"
        "• Dealer health for ABC Traders\n"
        "• Compare ABC Traders vs XYZ Steel\n\n"
        "Type *reset* to start over."
    )

# ============================================================
# PERIOD
# ============================================================

def parse_period(msg):
    m = msg.lower()
    if "2023" in m:
        return "2023"
    if "2022" in m:
        return "2022"
    if "last quarter" in m:
        return "90d"
    if "last month" in m:
        return "30d"
    return DEFAULT_PERIOD

def period_clause(period):
    if period.isdigit():
        # Date format is DD-MMM-YY, extract last 2 digits and compare with year
        year_suffix = period[-2:]  # e.g., '2023' -> '23'
        return f"substr(\"{DATE_COL}\", -2) = '{year_suffix}'"
    days = int(period.replace("d", ""))
    # For date range queries, we need to parse the date properly
    # Since dates are in DD-MMM-YY format, we'll use a simpler approach
    return f"1=1"  # Temporary: accept all dates for relative queries

# ============================================================
# DIMENSION & SHAPE DETECTION
# ============================================================

DIMENSIONS = {
    "city": "City",
    "product": "Product",
    "dealer": "Dealer",
    "state": "State",
    "month": "strftime('%Y-%m', \"Date\")",
}

GROUP_TRIGGERS = [
    " by ",
    "wise",
    "top",
    "monthly",
    "per ",
]

def expects_grouped_output(msg):
    msg = msg.lower()
    return any(t in msg for t in GROUP_TRIGGERS)

# 🔧 FIX 1: plural-safe dimension detection (cities, dealers, products)
def detect_dimension(msg):
    msg = msg.lower()
    for key, col in DIMENSIONS.items():
        if key in msg or f"{key}s" in msg:
            return col
    return None

def detect_limit(msg):
    m = re.search(r"\btop\s+(\d+)", msg.lower())
    return min(int(m.group(1)), MAX_LIMIT) if m else None

def detect_aggregate(msg):
    msg = msg.lower()
    if "average" in msg or "avg" in msg:
        return "AVG"
    return "SUM"

# ============================================================
# GROUPED SQL
# ============================================================

def enforce_grouped_sql(msg, period):
    dim = detect_dimension(msg)
    if not dim:
        raise ValueError("Grouping requested but no dimension found")

    agg = detect_aggregate(msg)
    limit = detect_limit(msg)
    amt = amount_expr()

    if agg == "AVG":
        value_expr = f"AVG(NULLIF({amt}, 0))"
    else:
        value_expr = f"SUM({amt})"

    sql = f"""
    SELECT
        {dim},
        {value_expr} AS Value
    FROM {TABLE_NAME}
    WHERE {period_clause(period)}
    GROUP BY {dim}
    ORDER BY Value DESC
    """

    if limit:
        sql += f"\nLIMIT {limit}"

    return sql.strip()

# ============================================================
# FUZZY DEALERS
# ============================================================

def fuzzy_dealers(name):
    df = read_sql(
        f"SELECT DISTINCT Dealer FROM {TABLE_NAME} WHERE Dealer LIKE ? LIMIT 5",
        params=[f"%{name}%"]
    )
    return df["Dealer"].tolist()

# ============================================================
# DEALER SNAPSHOT + HEALTH
# ============================================================

def dealer_snapshot(dealer, period):
    amt = amount_expr()
    query = f"""
    SELECT
        Dealer,
        SUM({amt}) AS TotalSales,
        COUNT(*) AS Orders,
        AVG(NULLIF({amt}, 0)) AS AvgOrderValue,
        MAX("{DATE_COL}") AS LastTxn
    FROM {TABLE_NAME}
    WHERE Dealer = ? AND {period_clause(period)}
    """
    df = read_sql(query, params=[dealer])
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def dealer_health(snapshot):
    today = pd.Timestamp.today()
    days_since = (today - pd.to_datetime(snapshot["LastTxn"])).days

    recency = 100 if days_since <= 30 else 60 if days_since <= 60 else 20
    frequency = 100 if snapshot["Orders"] >= 10 else 60 if snapshot["Orders"] >= 5 else 20
    volume = 100 if snapshot["TotalSales"] >= 500000 else 60 if snapshot["TotalSales"] >= 200000 else 20

    score = round(recency * 0.3 + frequency * 0.3 + volume * 0.4)

    status = (
        "🟢 Healthy" if score >= 80 else
        "🟡 Watch" if score >= 60 else
        "🔴 At Risk"
    )

    return score, status

def dealer_health_card(snapshot):
    score, status = dealer_health(snapshot)
    return f"""
📍 *Dealer Health Snapshot*

🏪 Dealer: {snapshot['Dealer']}
💰 Total Sales: ₹{int(snapshot['TotalSales']):,}
📦 Orders: {int(snapshot['Orders'])}
🕒 Last Order: {snapshot['LastTxn']}

{status}
⭐ Health Score: {score}/100
""".strip()

# ============================================================
# DEALER COMPARISON
# ============================================================

def compare_dealers(dealers, period):
    rows = []
    for d in dealers:
        snap = dealer_snapshot(d, period)
        if snap:
            score, _ = dealer_health(snap)
            rows.append({
                "Dealer": d,
                "Sales": int(snap["TotalSales"]),
                "Orders": int(snap["Orders"]),
                "HealthScore": score
            })
    return pd.DataFrame(rows) if len(rows) >= 2 else None

# ============================================================
# LLM SQL (NON-GROUPED ONLY)
# ============================================================

def run_llm_sql(msg, period):
    print(f"\n[DEBUG] run_llm_sql called")
    print(f"[DEBUG] Message: {msg}")
    print(f"[DEBUG] Period: {period}")
    
    # Build comprehensive system prompt with all database context
    system_prompt = f"""You are a SQL query generator for a sales database.

Database: {TABLE_NAME}
Columns: {COLUMNS}

IMPORTANT NOTES:
1. Date column format: Dates are stored as text in 'DD-MMM-YY' format (e.g., '04-Apr-22', '15-Jan-23')
2. To filter by year, use: substr(\"Date\", -2) = 'YY' (e.g., '22' for 2022, '23' for 2023, '24' for 2024, '25' for 2025)
3. DO NOT use date() or datetime functions - dates are TEXT, not DATE type
4. Amount column: Use \" Basic Amount \" (with spaces before and after)
5. To clean amount values, use: CAST(NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(\" Basic Amount \", '₹', ''), 'Rs.', ''), ',', ''), ' ', ''), CHAR(160), ''), '') AS REAL)
6. For aggregations, use the cleaned amount expression above
7. For product/item searches, use LIKE with wildcards: \"Item Group\" LIKE '%keyword%'
8. Common item groups: 'Tiscon - Retail', 'HR Coils - Projects', 'Wiron - Binding Wire', 'Projects - CAB', etc.

User's time period: {period}
{f"Filter: substr(\"Date\", -2) = '{period[-2:]}' (year {period})" if period.isdigit() else "Filter: Use all available data - DO NOT add date filters for relative periods"}

Generate ONLY a valid SQL SELECT query. No explanations, no markdown, just the SQL.
Use double quotes for column names that have spaces.
If user asks about 'steel', match against item groups containing 'Tiscon', 'HR Coil', 'Wiron', etc."""
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", msg)
    ])

    print(f"[DEBUG] Calling LLM for SQL generation...")
    sql = llm.invoke(prompt.format_messages()).content.strip()
    print(f"[DEBUG] LLM generated SQL (raw): {sql}")
    
    # Clean up the SQL - remove markdown code blocks if present
    if "```" in sql:
        # Extract content between code fences
        parts = sql.split("```")
        if len(parts) >= 2:
            sql = parts[1]
            # Remove language identifier if present
            if sql.startswith("sql\n") or sql.startswith("sql "):
                sql = sql[3:].strip()
            elif sql.startswith("SQL\n") or sql.startswith("SQL "):
                sql = sql[3:].strip()
            sql = sql.strip()
    
    # Replace backticks with double quotes for column names
    sql = sql.replace("`", "\"")
    
    # Remove trailing semicolon if present
    sql = sql.rstrip(";").strip()
    
    print(f"[DEBUG] SQL after cleanup: {sql}")

    if not sql.lower().startswith("select"):
        print(f"[DEBUG] ERROR: SQL doesn't start with SELECT")
        raise ValueError("Invalid SQL")

    return read_sql(sql)

# ============================================================
# FORMAT
# ============================================================

def format_table(df, start, size):
    if df is None or df.empty:
        return "⚠️ No data found."
    
    chunk = df.iloc[start:start + size]
    header = " | ".join(chunk.columns)
    lines = [header, "-" * len(header)]
    for _, r in chunk.iterrows():
        lines.append(" | ".join(str(v)[:28] for v in r))
    out = "```\n" + "\n".join(lines) + "\n```"
    if start + size < len(df):
        out += "\n➡️ show next"
    return out

# ============================================================
# MAIN ENTRY
# ============================================================

def search_answer(msg, user_id):
    print(f"\n{'='*60}")
    print(f"[DEBUG] search_answer called")
    print(f"[DEBUG] User message: {msg}")
    print(f"[DEBUG] User ID: {user_id}")
    ctx = get_context(user_id)
    print(f"[DEBUG] Context: {ctx}")

    if is_reset(msg):
        print(f"[DEBUG] Reset command detected")
        reset_context(user_id)
        return menu_message()

    if is_greeting(msg):
        print(f"[DEBUG] Greeting detected")
        return menu_message()

    if ctx["last_dataframe"] is not None and "next" in msg.lower():
        print(f"[DEBUG] Pagination request detected")
        ctx["pagination_index"] += ctx["page_size"]
        return format_table(ctx["last_dataframe"], ctx["pagination_index"], ctx["page_size"])

    ctx["last_period"] = parse_period(msg)
    print(f"[DEBUG] Parsed period: {ctx['last_period']}")

    try:
        # Use LLM for all data queries
        print(f"[DEBUG] Using LLM for SQL generation")
        df = run_llm_sql(msg, ctx["last_period"])

        print(f"[DEBUG] Final dataframe shape: {df.shape}")
        print(f"[DEBUG] Final dataframe:\n{df}")
        
        if df.empty:
            return "⚠️ No data found for your query. Try a different time period or criteria."
        
        # Use LLM to generate natural language response from the data
        print(f"[DEBUG] Generating natural language response...")
        response_prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a sales analytics assistant for an Indian company. Given query results, provide a clear, concise answer. IMPORTANT: All amounts are in Indian Rupees (₹), NOT dollars. Format numbers with commas (Indian numbering system). Keep it brief and professional."),
            ("human", f"User asked: {msg}\n\nQuery results:\n{df.to_string()}\n\nProvide a natural language answer. Use ₹ symbol for currency, not $.")
        ])
        
        natural_response = llm.invoke(response_prompt.format_messages()).content.strip()
        print(f"[DEBUG] Natural language response: {natural_response}")
        
        # Also show the data table
        ctx["last_dataframe"] = df
        ctx["pagination_index"] = 0
        table_output = format_table(df, 0, ctx["page_size"])
        
        result = f"{natural_response}\n\n{table_output}"
        print(f"[DEBUG] Final result:\n{result}")
        print(f"{'='*60}\n")
        return result

    except Exception as e:
        print(f"[DEBUG] ERROR occurred: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[DEBUG] Traceback:\n{traceback.format_exc()}")
        print(f"{'='*60}\n")
        
        # Try to provide helpful error message
        error_msg = str(e).lower()
        if "no such column" in error_msg or "syntax" in error_msg:
            return f"❓ I had trouble understanding that query.\n\nError: {str(e)}\n\nTry rephrasing your question."
        
        return (
            "❓ I couldn't process that query.\n\n"
            "Try:\n"
            "• Total sales in 2022\n"
            "• Top 5 cities by sales\n"
            "• Sales by product in 2022\n"
            "• Show me dealers in Hyderabad"
        )

# ============================================================
# SETUP
# ============================================================

def setup_bot():
    global llm, COLUMNS
    print(f"\n[DEBUG] Setting up bot...")
    print(f"[DEBUG] Model: {MODEL}")
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    print(f"[DEBUG] LLM initialized")
    COLUMNS = read_sql(f"SELECT * FROM {TABLE_NAME} LIMIT 1").columns.tolist()
    print(f"[DEBUG] Columns loaded: {COLUMNS}")
    print(f"[DEBUG] Bot setup complete\n")

# ============================================================
# FLASK / TWILIO WRAPPER
# ============================================================

def handle_message(msg: str, user_id: str):
    print(f"\n[DEBUG] handle_message called")
    print(f"[DEBUG] Message: {msg}")
    print(f"[DEBUG] User ID: {user_id}")
    response_text = search_answer(msg, user_id)
    print(f"[DEBUG] Response text: {response_text}")
    return response_text, None
