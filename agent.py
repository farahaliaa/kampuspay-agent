import os
from dotenv import load_dotenv
from agents import Agent, Runner, function_tool
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)


def log_action(actor: str, action: str, payload: dict, reasoning: str = ""):
    """Write an entry to the audit_log table in Supabase."""
    try:
        supabase.table("audit_log").insert({
            "actor": actor,
            "action": action,
            "payload": payload,
            "reasoning": reasoning
        }).execute()
    except Exception as e:
        print(f"[audit_log write failed] {e}")


@function_tool
def get_balance(owner_name: str) -> dict:
    """Check a user's balance"""
    user = supabase.table("users").select("id").eq("name", owner_name).execute()
    if not user.data:
        log_action("kai", "tool:get_balance", {"owner": owner_name}, "user not found")
        return {"error": "User not found"}

    account = supabase.table("accounts").select("*").eq("user_id", user.data[0]["id"]).execute()
    result = account.data[0] if account.data else {"error": "Account not found"}

    log_action(
        "kai",
        "tool:get_balance",
        {"owner": owner_name, "result": result},
        f"fetched balance for {owner_name}"
    )
    return result


@function_tool
def fx_impact(amount_home: float, pair: str, shock_pct: float) -> dict:
    """Calculate FX shock impact on cost"""
    rate_row = supabase.table("fx_rates").select("rate").eq("pair", pair).order("ts", desc=True).limit(1).execute()
    base_rate = float(rate_row.data[0]["rate"]) if rate_row.data else 1.0
    shocked_rate = base_rate * (1 + shock_pct / 100)
    extra = amount_home * (shocked_rate - base_rate)

    result = {
        "pair": pair,
        "base_rate": round(base_rate, 6),
        "shocked_rate": round(shocked_rate, 6),
        "extra_cost": round(extra, 2),
        "recommended_buffer_pct": round(max(5, abs(shock_pct) * 1.5), 2)
    }

    log_action(
        "kai",
        "tool:fx_impact",
        {"pair": pair, "shock_pct": shock_pct, "extra_cost": result["extra_cost"]},
        f"analysed {shock_pct}% FX shock on {pair}"
    )
    return result


@function_tool
def build_plan(income: float, savings: float, tuition: float,
               stay_months: int, dest_ccy: str, living_estimate: float) -> dict:
    """Generate a personalised financial plan"""
    surplus = income - living_estimate
    months_needed = (tuition - savings) / max(surplus, 1)

    result = {
        "currency": dest_ccy,
        "monthly_cap": round(living_estimate * 0.9, 2),
        "months_to_cover_tuition": round(max(months_needed, 0), 1),
        "recommended_emergency_fund": round(living_estimate * 3, 2),
        "risks": ["FX volatility", "income delay", "visa costs"]
    }

    log_action(
        "kai",
        "tool:build_plan",
        {"currency": dest_ccy, "tuition": tuition, "stay_months": stay_months},
        f"built plan for {stay_months}-month stay"
    )
    return result


@function_tool
def execute_transfer(from_owner: str, to_owner: str, amount: float,
                     purpose: str = "transfer",
                     confirmed: bool = False) -> dict:
    """Execute a sandbox transfer. Subject to risk gating.

    Args:
        from_owner: Sender's name (must exist in users table)
        to_owner: Recipient's name
        amount: Amount to transfer
        purpose: Reason for transfer (e.g., 'tuition', 'allowance')
        confirmed: Set to True only after user explicitly confirms a high-risk transfer
    """
    # --- 1. Look up accounts ---
    from_user = supabase.table("users").select("id").eq("name", from_owner).execute()
    to_user = supabase.table("users").select("id").eq("name", to_owner).execute()

    if not from_user.data or not to_user.data:
        log_action("kai", "tool:execute_transfer",
                   {"from": from_owner, "to": to_owner, "amount": amount},
                   "account not found")
        return {"status": "failed", "error": "Account not found"}

    from_acct = supabase.table("accounts").select("id").eq("user_id", from_user.data[0]["id"]).execute()
    to_acct = supabase.table("accounts").select("id").eq("user_id", to_user.data[0]["id"]).execute()

    if not from_acct.data or not to_acct.data:
        log_action("kai", "tool:execute_transfer",
                   {"from": from_owner, "to": to_owner, "amount": amount},
                   "wallet not found")
        return {"status": "failed", "error": "Wallet not found"}

    # --- 2. Risk scoring ---
    risk_score = 0
    reasons = []

    if amount > 5000:
        risk_score += 40
        reasons.append("exceeds_per_tx_limit(5000)")

    if amount > 1000:
        history = supabase.table("transactions") \
            .select("id") \
            .eq("from_acct", from_acct.data[0]["id"]) \
            .eq("to_acct", to_acct.data[0]["id"]) \
            .execute()
        if not history.data:
            risk_score += 20
            reasons.append("new_beneficiary")
            if amount > 1000:
                risk_score += 15
                reasons.append("new_beneficiary_high_value")

    risk_reasoning = " | ".join(reasons) if reasons else "low_risk"

    # --- 3. Log the risk gate decision ---
    log_action(
        "kai",
        "risk_gate",
        {"from": from_owner, "to": to_owner, "amount": amount,
         "risk_score": risk_score, "requires_confirmation": risk_score >= 30},
        risk_reasoning
    )

    # --- 4. Gate: block if very high risk ---
    if risk_score >= 70:
        log_action("kai", "transfer_blocked",
                   {"from": from_owner, "to": to_owner, "amount": amount,
                    "risk_score": risk_score},
                   "risk_score >= 70 — blocked")
        return {
            "status": "blocked",
            "risk_score": risk_score,
            "reasons": reasons,
            "message": "Transfer blocked. Please contact support."
        }

    # --- 5. Gate: require confirmation if medium risk ---
    if risk_score >= 30 and not confirmed:
        log_action("kai", "awaiting_confirmation",
                   {"from": from_owner, "to": to_owner, "amount": amount,
                    "risk_score": risk_score},
                   "step-up authentication required")
        return {
            "status": "awaiting_confirmation",
            "risk_score": risk_score,
            "reasons": reasons,
            "message": "Please confirm this transfer before I proceed."
        }

    # --- 6. Execute ---
    try:
        result = supabase.rpc("execute_transfer", {
            "from_id": from_acct.data[0]["id"],
            "to_id": to_acct.data[0]["id"],
            "amt": amount,
            "purpose_text": purpose,
            "risk": risk_score
        }).execute()

        log_action(
            "kai",
            "transfer_executed",
            {"from": from_owner, "to": to_owner, "amount": amount,
             "purpose": purpose, "risk_score": risk_score},
            f"executed with risk_score={risk_score}"
        )

        return {
            "status": "executed",
            "amount": amount,
            "from": from_owner,
            "to": to_owner,
            "risk_score": risk_score
        }
    except Exception as e:
        log_action("kai", "transfer_failed",
                   {"from": from_owner, "to": to_owner, "amount": amount},
                   f"error: {str(e)}")
        return {"status": "failed", "error": str(e)}


kai = Agent(
    name="Kai",
    model="gpt-4o-mini",
    instructions="""You are Kai, a cross-border financial agent for international students.

Use available tools to complete tasks.

Rules:
1. When asked to transfer money, ALWAYS use execute_transfer.
2. If execute_transfer returns status "awaiting_confirmation", show the risk_score
   and reasons to the user, then ask them to confirm.
3. If the user confirms, call execute_transfer again with confirmed=True.
4. If it returns status "blocked", explain that the action was blocked by the risk gate.
5. Always state your reasoning before executing any financial action.
6. Never fabricate balances — always use get_balance.
7. If unsure, ask a clarifying question instead of acting.
""",
    tools=[get_balance, fx_impact, build_plan, execute_transfer]
)


if __name__ == "__main__" and os.getenv("RUN_AGENT_MAIN") == "1":
    prompt = "Transfer 2000 from Aiman to China Univ X for tuition."
    result = Runner.run_sync(kai, prompt)
    print("\n--- Kai says ---")
    print(result.final_output)

    from agent import supabase

class FXRequest(BaseModel):
    amount_home: float
    pair: str
    shock_pct: float

@app.post("/fx-impact")
def fx_impact_endpoint(req: FXRequest):
    rate_row = supabase.table("fx_rates").select("rate") \
        .eq("pair", req.pair).order("ts", desc=True).limit(1).execute()
    base_rate = float(rate_row.data[0]["rate"]) if rate_row.data else 1.0
    shocked_rate = base_rate * (1 + req.shock_pct / 100)
    extra = req.amount_home * (shocked_rate - base_rate)
    return {
        "pair": req.pair,
        "base_rate": round(base_rate, 6),
        "shocked_rate": round(shocked_rate, 6),
        "base_cost_dest": round(req.amount_home * base_rate, 2),
        "shocked_cost_dest": round(req.amount_home * shocked_rate, 2),
        "extra_cost": round(extra, 2),
        "recommended_buffer_pct": round(max(5, abs(req.shock_pct) * 1.5), 2),
    }