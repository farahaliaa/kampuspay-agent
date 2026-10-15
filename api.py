from agent import supabase
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agents import Runner
from agent import kai, log_action, supabase
from typing import Dict, List
from fastapi import HTTPException

app = FastAPI(title="KampusPay Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store: {session_id: [list of messages]}
# Each message: {"role": "user"|"assistant", "content": "..."}
SESSIONS: Dict[str, List[dict]] = {}


class ChatRequest(BaseModel):
    message: str
    user_id: int = 1
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    session_id: str


@app.get("/")
def health():
    return {"status": "ok", "agent": "Kai"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    # Get or create session history
    history = SESSIONS.get(req.session_id, [])

    # Log incoming user message
    log_action(str(req.user_id), "user_message",
               {"text": req.message, "session": req.session_id},
               "incoming chat")

    # Build input: prior history + new user message
    messages = history + [{"role": "user", "content": req.message}]

    # Run Kai with full context
    result = await Runner.run(kai, messages)

    # Append both turns to session history
    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": result.final_output})

    # Keep the last 20 messages only (avoid unbounded memory growth)
    SESSIONS[req.session_id] = history[-20:]

    # Log Kai's reply
    log_action("kai", "kai_reply",
               {"text": result.final_output, "session": req.session_id},
               "final answer")

    return ChatResponse(reply=result.final_output, session_id=req.session_id)


@app.post("/reset/{session_id}")
def reset_session(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"ok": True, "session_id": session_id}

@app.get("/dashboard/{user_id}")
def get_dashboard(user_id: int):
    """Return all data needed for the dashboard page."""
    # 1. Get user
    user = supabase.table("users").select("*").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")
    user_row = user.data[0]

    # 2. Get account
    account = supabase.table("accounts").select("*").eq("user_id", user_id).execute()
    account_row = account.data[0] if account.data else {"balance": 0, "currency": "USD"}

    # 3. Get budget plan
    plan = supabase.table("budget_plans").select("*").eq("user_id", user_id).execute()
    plan_row = plan.data[0] if plan.data else {
        "monthly_cap": 0, "tuition_due": 0, "stay_months": 0
    }

    # 4. Get transactions (last 30 days)
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    txs = supabase.table("transactions").select("*") \
        .eq("from_acct", account_row.get("id", 0)) \
        .gte("created_at", since) \
        .order("created_at", desc=True) \
        .execute()

    spent_30d = sum(float(t["amount"]) for t in txs.data)
    tx_count = len(txs.data)

    # 5. Recent activity (last 5)
    recent = supabase.table("transactions").select("*") \
        .eq("from_acct", account_row.get("id", 0)) \
        .order("created_at", desc=True) \
        .limit(5) \
        .execute()

    # 6. Compute projected monthly spend
    burn_rate = spent_30d / 30
    projected = burn_rate * 30
    cap = float(plan_row.get("monthly_cap", 0))
    over_by = max(0, projected - cap)

    return {
        "user": user_row,
        "account": account_row,
        "plan": plan_row,
        "spent_30d": round(spent_30d, 2),
        "tx_count": tx_count,
        "projected_monthly": round(projected, 2),
        "over_by": round(over_by, 2),
        "alert": projected > cap and cap > 0,
        "recent": recent.data,
    }
class FXRequest(BaseModel):
    amount_home: float
    pair: str
    shock_pct: float

@app.post("/fx-impact")
def fx_impact_endpoint(req: FXRequest):
    from engine_fx import compute_fx_impact  # we'll create this next
    return compute_fx_impact(req.amount_home, req.pair, req.shock_pct)