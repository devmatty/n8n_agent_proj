"""
tools.py — The hands of the agent.

Each tool has two parts:
  1. A Python function that does the actual work.
  2. A "schema" dict that describes the function to Claude so Claude
     knows when and how to call it.

The schema is the part that's new. Read the comments on the first
tool carefully — the same pattern applies to all five.
"""

import pandas as pd
from datetime import datetime, timezone

ACCOUNT_BOOK_PATH = "./data/account_book.csv"


def _utc_now_iso() -> str:
    """Timezone-aware UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_book() -> pd.DataFrame:
    """Load the book and ensure the agent-log columns are strings,
    so writing into them never trips pandas' dtype coercion."""
    df = pd.read_csv(ACCOUNT_BOOK_PATH, dtype={"last_action": "string",
                                               "last_action_at": "string"})
    df["last_action"] = df["last_action"].fillna("")
    df["last_action_at"] = df["last_action_at"].fillna("")
    return df


# ---------------------------------------------------------------------------
# TOOL 1: get_accounts — the agent's eyes
# ---------------------------------------------------------------------------

def get_accounts() -> list[dict]:
    """Return the current state of every account in the book."""
    df = _load_book()
    return df.to_dict(orient="records")


# ----- THE TOOL SCHEMA — read this comment carefully -----
#
# This dict is what Claude actually sees when deciding whether to use
# this tool. Claude never sees the Python function above — only this.
#
# Three pieces matter:
#
#   "name"        : the string the agent uses to call the tool. Must
#                   match the Python function name exactly so your
#                   dispatcher (in Stage 3) can route the call.
#
#   "description" : the most important field. This is Claude's only
#                   hint about WHEN to use this tool. Write it like a
#                   tooltip for a new hire: what does it do, when
#                   should I reach for it, what does it give me back?
#                   Vague descriptions = Claude ignoring or misusing
#                   your tool. Be specific.
#
#   "input_schema": JSON-Schema describing the arguments. For tools
#                   that take no arguments (like this one), you still
#                   pass an empty object — required by the API.
#
# This is the same shape every tool in this file follows.

get_accounts_schema = {
    "name": "get_accounts",
    "description": (
        "Returns the current state of every account in the customer "
        "success book. Each account includes real support signals "
        "(open_tickets, critical_tickets, avg_satisfaction, "
        "days_since_activity) and contract fields (plan_tier__SYNTH, "
        "mrr__SYNTH, days_to_renewal__SYNTH). Call this FIRST at the "
        "start of any review — you need the state of the book before "
        "you can reason about which accounts need attention."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


# ---------------------------------------------------------------------------
# TOOL 2: score_risk — analytical brain for one account
# ---------------------------------------------------------------------------

def score_risk(account_id: str) -> dict:
    """
    Score one account's churn risk on a 1-5 scale using its real
    support signals and contract proximity. Returns score + reason.
    """
    df = _load_book()
    row = df[df["product_line"] == account_id]
    if row.empty:
        return {"error": f"Account '{account_id}' not found."}
    r = row.iloc[0]

    score = 1
    reasons = []

    # Real signals — each tier of trouble adds to the score
    if r["avg_satisfaction"] <= 2.7:
        score += 2; reasons.append(f"low CSAT ({r['avg_satisfaction']})")
    elif r["avg_satisfaction"] <= 3.0:
        score += 1; reasons.append(f"soft CSAT ({r['avg_satisfaction']})")

    if r["critical_rate"] >= 0.50:
        score += 1; reasons.append(f"critical rate {r['critical_rate']:.0%}")

    if r["open_tickets"] >= 150:
        score += 1; reasons.append(f"{int(r['open_tickets'])} open tickets")

    # Renewal proximity raises urgency
    if r["days_to_renewal__SYNTH"] <= 45:
        score += 1; reasons.append(
            f"renewal in {int(r['days_to_renewal__SYNTH'])} days"
        )

    return {
        "account_id": account_id,
        "risk_score": min(score, 5),
        "reason": "; ".join(reasons) if reasons else "no risk signals",
    }


score_risk_schema = {
    "name": "score_risk",
    "description": (
        "Compute a 1-5 churn risk score for ONE account based on its "
        "support signals (CSAT, critical-ticket rate, open tickets) "
        "and how close its renewal is. Returns the score and a "
        "short human-readable reason. Use this after get_accounts() "
        "to evaluate accounts that look concerning. A score of 4 or "
        "5 warrants outreach."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": (
                    "The product_line value identifying the account, "
                    "e.g. 'GoPro Hero' or 'Dell XPS'."
                ),
            }
        },
        "required": ["account_id"],
    },
}


# ---------------------------------------------------------------------------
# TOOL 3: draft_outreach — the writing hand
# ---------------------------------------------------------------------------

def draft_outreach(account_id: str, reason: str) -> dict:
    """
    Build a structured prompt-pack the agent loop will pass to Claude
    to generate a tailored re-engagement email. We don't generate the
    email text here — Claude does, inside the loop in Stage 3.
    """
    df = _load_book()
    row = df[df["product_line"] == account_id]
    if row.empty:
        return {"error": f"Account '{account_id}' not found."}
    r = row.iloc[0]

    return {
        "account_id": account_id,
        "context": {
            "plan_tier": r["plan_tier__SYNTH"],
            "mrr": int(r["mrr__SYNTH"]),
            "open_tickets": int(r["open_tickets"]),
            "csat": float(r["avg_satisfaction"]),
            "days_to_renewal": int(r["days_to_renewal__SYNTH"]),
        },
        "trigger_reason": reason,
        "tone_guidance": (
            "Warm, brief, and specific. Reference the actual support "
            "load and proximity to renewal. Offer a concrete next "
            "step (15-min call). Three short paragraphs, no buzzwords."
        ),
    }


draft_outreach_schema = {
    "name": "draft_outreach",
    "description": (
        "Build a structured outreach package for a single at-risk "
        "account: the account's context, the trigger reason, and "
        "tone guidance. Use this when you've decided an account "
        "needs proactive outreach (typically risk score 4 or 5). "
        "The output is passed to flag_for_approval next."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "The product_line value identifying the account.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why this account is being contacted, in one "
                    "sentence. E.g. 'CSAT 2.6 with renewal in 30 days.'"
                ),
            },
        },
        "required": ["account_id", "reason"],
    },
}


# ---------------------------------------------------------------------------
# TOOL 4: flag_for_approval — the human-in-the-loop gate
# ---------------------------------------------------------------------------

# In-memory queue. In Stage 4 (n8n), this becomes a real notification.
APPROVAL_QUEUE: list[dict] = []


def flag_for_approval(account_id: str, action: str, draft: str) -> dict:
    """
    Queue a proposed action for human review. Nothing is sent.
    The agent NEVER takes a customer-facing action directly — it
    always routes through this gate.
    """
    item = {
        "account_id": account_id,
        "action": action,
        "draft": draft,
        "queued_at": _utc_now_iso(),
        "status": "awaiting_approval",
    }
    APPROVAL_QUEUE.append(item)
    return {"queued": True, "position": len(APPROVAL_QUEUE), "item": item}


flag_for_approval_schema = {
    "name": "flag_for_approval",
    "description": (
        "Queue a proposed customer-facing action (email, call, "
        "escalation) for human approval before anything sends. "
        "ALWAYS use this for outbound actions — never send "
        "directly. This is the safety gate of the system."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string"},
            "action": {
                "type": "string",
                "description": (
                    "Short label for what's proposed, e.g. "
                    "'send_re_engagement_email'."
                ),
            },
            "draft": {
                "type": "string",
                "description": "Full text of the proposed message.",
            },
        },
        "required": ["account_id", "action", "draft"],
    },
}


# ---------------------------------------------------------------------------
# TOOL 5: log_action — the agent's memory
# ---------------------------------------------------------------------------

def log_action(account_id: str, action: str) -> dict:
    """Persist what the agent did for this account, with a timestamp."""
    df = _load_book()
    mask = df["product_line"] == account_id
    if not mask.any():
        return {"error": f"Account '{account_id}' not found."}

    ts = _utc_now_iso()
    df.loc[mask, "last_action"] = action
    df.loc[mask, "last_action_at"] = ts
    df.to_csv(ACCOUNT_BOOK_PATH, index=False)
    return {"logged": True, "account_id": account_id,
            "action": action, "at": ts}


log_action_schema = {
    "name": "log_action",
    "description": (
        "Record an action taken for an account in the account book. "
        "Call this after flag_for_approval so the book reflects "
        "that this account was already handled this run — prevents "
        "duplicate outreach on the next loop iteration."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string"},
            "action": {
                "type": "string",
                "description": "Short label, e.g. 'flagged_re_engagement'.",
            },
        },
        "required": ["account_id", "action"],
    },
}


# ---------------------------------------------------------------------------
# Registries the agent loop (Stage 3) will import
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    get_accounts_schema,
    score_risk_schema,
    draft_outreach_schema,
    flag_for_approval_schema,
    log_action_schema,
]

TOOL_FUNCTIONS = {
    "get_accounts":      get_accounts,
    "score_risk":        score_risk,
    "draft_outreach":    draft_outreach,
    "flag_for_approval": flag_for_approval,
    "log_action":        log_action,
}


# ---------------------------------------------------------------------------
# Quick self-test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Tool self-test ===\n")

    accts = get_accounts()
    print(f"get_accounts -> {len(accts)} accounts loaded")
    print(f"  first: {accts[0]['product_line']} "
          f"(CSAT {accts[0]['avg_satisfaction']}, "
          f"{accts[0]['open_tickets']} open)\n")

    # Score the riskiest-looking few
    for a in accts[:3]:
        s = score_risk(a["product_line"])
        print(f"score_risk({a['product_line']!r}) -> {s}")
    print()

    pack = draft_outreach("GoPro Hero", "Low CSAT, renewal nearing")
    print(f"draft_outreach -> context keys: {list(pack['context'].keys())}")
    print(f"  trigger: {pack['trigger_reason']}\n")

    q = flag_for_approval("GoPro Hero",
                          "send_re_engagement_email",
                          "Hi team, just checking in...")
    print(f"flag_for_approval -> queued at position {q['position']}\n")

    lg = log_action("GoPro Hero", "flagged_re_engagement")
    print(f"log_action -> {lg}")
