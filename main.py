import os
import re
import json
import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from groq import Groq
from openai import OpenAI

from prompt import SYSTEM_PROMPT

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# App
# ─────────────────────────────────────────
app = FastAPI(title="Just Proposed API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# AI Client Setup
# Switch via AI_PROVIDER env variable:
#   AI_PROVIDER=groq   → uses Groq (default, for testing)
#   AI_PROVIDER=openai → uses OpenAI (for production)
# ─────────────────────────────────────────
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()

if AI_PROVIDER == "openai":
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    logger.info("Using OpenAI — model: %s", MODEL)
else:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    logger.info("Using Groq — model: %s", MODEL)


# ─────────────────────────────────────────
# Request / Response Models
# ─────────────────────────────────────────
class Message(BaseModel):
    role: str       # 'user' or 'assistant'
    content: str


class LeadProfile(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    phase: Optional[str] = "CREATED"
    source: Optional[str] = None
    ad_name: Optional[str] = None


class ChatRequest(BaseModel):
    messages: List[Message]
    lead_profile: LeadProfile
    message_count: int = 0


class ChatResponse(BaseModel):
    reply: str
    new_phase: str
    call_requested: bool
    stop_responding: bool
    lost_reason: Optional[str] = None
    intent: str
    ai_summary: str


# ─────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Just Proposed API",
        "version": "2.0.0",
        "provider": AI_PROVIDER,
        "model": MODEL,
    }

@app.get("/health")
def health():
    return {"status": "healthy"}


# ─────────────────────────────────────────
# Main Chat Endpoint
# ─────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    logger.info(
        "Incoming — phone: %s | phase: %s | msg_count: %d",
        req.lead_profile.phone,
        req.lead_profile.phase,
        req.message_count,
    )

    # Build lead profile string for prompt
    lead_profile_str = (
        f"Name: {req.lead_profile.name or 'Unknown'} | "
        f"Phone: {req.lead_profile.phone or 'Unknown'} | "
        f"Phase: {req.lead_profile.phase or 'CREATED'} | "
        f"Source: {req.lead_profile.source or 'Unknown'} | "
        f"Ad: {req.lead_profile.ad_name or 'Unknown'}"
    )

    # Inject lead profile + message count into system prompt
    system = SYSTEM_PROMPT.replace("{lead_profile}", lead_profile_str)
    system = system.replace("{message_count}", str(req.message_count))

    # Build messages array for AI
    messages = [{"role": "system", "content": system}]
    for msg in req.messages:
        messages.append({"role": msg.role, "content": msg.content})

    # Call AI
    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=500,
            temperature=0.4,
        )
        raw = completion.choices[0].message.content.strip()
        logger.info("AI raw response: %s", raw)
    except Exception as e:
        logger.error("AI call failed: %s", str(e))
        raise HTTPException(status_code=502, detail=f"AI provider error: {str(e)}")

    # Parse JSON response
    try:
        # Strip markdown fences if AI added them
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        # Fallback: extract JSON block if there's extra text around it
        match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)

        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed: %s | raw: %s", str(e), raw)
        raise HTTPException(status_code=500, detail=f"AI returned invalid JSON: {raw}")

    # Validate and extract fields with safe defaults
    new_phase = data.get("new_phase", req.lead_profile.phase or "CREATED")
    stop_responding = data.get("stop_responding", False)
    call_requested = data.get("call_requested", False)
    lost_reason = data.get("lost_reason", None)

    # Safety: force stop_responding if phase is INTERESTED or LOST
    if new_phase in ("INTERESTED", "LOST"):
        stop_responding = True

    # Safety: force call_requested if phase is INTERESTED
    if new_phase == "INTERESTED":
        call_requested = True

    # Safety: validate lost_reason values
    if lost_reason not in (None, "not_interested", "budget"):
        lost_reason = None

    response = ChatResponse(
        reply=data.get("reply", "Sorry, something went wrong. Please try again."),
        new_phase=new_phase,
        call_requested=call_requested,
        stop_responding=stop_responding,
        lost_reason=lost_reason,
        intent=data.get("intent", "unknown"),
        ai_summary=data.get("ai_summary", ""),
    )

    logger.info(
        "Response — new_phase: %s | stop: %s | call: %s | intent: %s",
        response.new_phase,
        response.stop_responding,
        response.call_requested,
        response.intent,
    )

    return response
