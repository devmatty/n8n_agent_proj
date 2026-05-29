"""
agent.py — The brain of the autonomous Customer Success Agent.

This is the agent loop. It is the conceptual heart of every agent
framework you'll ever encounter (LangChain, AutoGen, CrewAI, the
OpenAI Assistants API). Read the comments — they teach the pattern.

WHAT THIS FILE DOES, in one paragraph:
Sends the task to Claude with the list of tools from tools.py.
Claude replies with either (a) a tool it wants to call, or
(b) a final answer. If (a), we run the tool and feed the result
back. If (b), we stop. That observe-think-act cycle, repeated
until Claude is done, is what makes this an "agent."

HOW TO RUN:
  1. pip install anthropic python-dotenv pandas
  2. Create a file named `.env` in the same folder with one line:
        ANTHROPIC_API_KEY=sk-ant-...your key from console.anthropic.com...
  3. python3 agent.py

You will then see a live trace of the agent reasoning through
your 42-account book.
"""

import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from anthropic import Anthropic

# Import the tools we built in Stage 2
from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, APPROVAL_QUEUE


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()  # reads ANTHROPIC_API_KEY from .env

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key:
    sys.exit(
        "ERROR: ANTHROPIC_API_KEY not set.\n"
        "Create a .env file with: ANTHROPIC_API_KEY=sk-ant-..."
    )

client = Anthropic(api_key=api_key)
MODEL = "claude-sonnet-4-5"   # The model the agent uses to reason
MAX_TURNS = 25                # Safety limit on loop iterations


# ---------------------------------------------------------------------------
# The system prompt — the agent's job description
# ---------------------------------------------------------------------------
#
# This is the single most important piece of prose in the whole project.
# It tells Claude WHO it is, WHAT it's doing, and HOW it should decide.
# In interviews you'll be asked "how did you design the agent" — this
# system prompt is the answer.
#
# Note the explicit decision framework. Vague prompts produce vague
# agents. We tell Claude exactly when to flag and when to skip.

SYSTEM_PROMPT = """\
You are an autonomous Customer Success agent for a SaaS company.
Your job is to review the book of accounts and proactively flag
those at risk of churn, drafting outreach for human approval.

You have these tools available:
  - get_accounts: pull the current state of all accounts
  - score_risk: score one account 1-5 with reason
  - draft_outreach: build an outreach package for one account
  - flag_for_approval: queue a proposed action for human review
  - log_action: record what you did on an account

DECISION FRAMEWORK:
  1. Start by calling get_accounts() to see the book.
  2. Identify the 3-5 accounts most worth scoring based on a
     combination of MRR (revenue at risk), low satisfaction,
     high open-ticket counts, and renewal proximity. Do not score
     all 42 accounts — be selective like a senior CSM would be.
  3. For each account you score, if risk_score >= 4, call
     draft_outreach, then flag_for_approval with the drafted email
     text, then log_action so we don't re-flag it next run.
  4. When you've worked through the priority accounts, stop calling
     tools and produce a final summary: which accounts were flagged,
     why, and what you'd recommend next.

WRITING THE DRAFT EMAIL:
When you call flag_for_approval, the `draft` argument must be the
ACTUAL email text you would send. Three short paragraphs. Warm,
specific, no buzzwords. Reference the real signals (open tickets,
satisfaction) and propose a 15-minute call. Sign off as
"Matthew, Customer Success".

Be concise in your reasoning between tool calls. Get to actions.
"""


# ---------------------------------------------------------------------------
# Helpers for the trace output
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def short(value, n=200):
    """Truncate long tool results for the printed trace."""
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= n else s[:n] + f"... [{len(s)-n} more chars]"


# ---------------------------------------------------------------------------
# The agent loop — the part that matters
# ---------------------------------------------------------------------------

def run_agent():
    banner("AUTONOMOUS CS AGENT — RUN STARTED")
    print(f"Started at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"Model:      {MODEL}")
    print(f"Tools:      {[t['name'] for t in TOOL_SCHEMAS]}")

    # The conversation history. Every turn, we send the FULL history
    # to Claude so it has memory of what's happened so far. This is
    # how agents "remember" — there's no hidden state, just the
    # growing transcript.
    messages = [
        {
            "role": "user",
            "content": "Review the account book and act on at-risk accounts.",
        }
    ]

    for turn in range(1, MAX_TURNS + 1):
        # ---- Step 1: Ask Claude what to do next ----
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # ---- Step 2: Print any reasoning text Claude included ----
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n[turn {turn}] agent reasoning:")
                print(f"  {block.text.strip()}")

        # ---- Step 3: Check why Claude stopped ----
        #
        # "end_turn"  → Claude is done; it gave a final answer.
        # "tool_use"  → Claude wants us to run one or more tools.
        # anything else → unexpected, bail.
        if response.stop_reason == "end_turn":
            banner("AGENT FINISHED")
            return _summarize_run()

        if response.stop_reason != "tool_use":
            print(f"\nUnexpected stop_reason: {response.stop_reason}")
            return

        # ---- Step 4: Run every tool Claude asked for ----
        #
        # A single response can request multiple tools in parallel.
        # We dispatch each one through the TOOL_FUNCTIONS registry
        # from tools.py.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_args = block.input

            print(f"\n[turn {turn}] agent → {tool_name}({short(tool_args, 120)})")

            fn = TOOL_FUNCTIONS.get(tool_name)
            if fn is None:
                result = {"error": f"Unknown tool '{tool_name}'"}
            else:
                try:
                    result = fn(**tool_args)
                except Exception as e:
                    result = {"error": str(e)}

            print(f"[turn {turn}]   result: {short(result)}")

            # Each tool result must reference the tool_use_use_id Claude
            # gave us, so Claude can match results to its requests.
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        # ---- Step 5: Append BOTH sides to the history, then loop ----
        #
        # The assistant's tool-call request goes in as-is, then we add
        # a user message containing the tool results. That's the
        # protocol the API expects.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"\nReached MAX_TURNS={MAX_TURNS} without finishing.")


# ---------------------------------------------------------------------------
# After the run — show what's queued for approval
# ---------------------------------------------------------------------------

def _summarize_run():
    banner("APPROVAL QUEUE — items awaiting your review")
    if not APPROVAL_QUEUE:
        print("(nothing queued)")
        return

    for i, item in enumerate(APPROVAL_QUEUE, 1):
        print(f"\n--- Item {i} ---")
        print(f"Account: {item['account_id']}")
        print(f"Action:  {item['action']}")
        print(f"Queued:  {item['queued_at']}")
        print(f"Draft:\n{item['draft']}")
        print("-" * 50)

    print(f"\nTotal queued: {len(APPROVAL_QUEUE)}")
    print("In Stage 4 these will be routed to you via n8n for approve/reject.")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_agent()
