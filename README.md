# KampusPay Agent Backend (Kai)

Python AI financial agent built with FastAPI, OpenAI Agents SDK, and Supabase.

## Features
- **Balance & FX Analysis:** Checks balances and computes FX deltas.
- **Deterministic Risk Gate:** Hard-blocks transfers > 5,000 BND. Requires confirmation for new beneficiaries.
- **Sandbox Only:** No real money is moved.

## Setup Instructions

1. **Install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install openai-agents supabase fastapi uvicorn python-dotenv