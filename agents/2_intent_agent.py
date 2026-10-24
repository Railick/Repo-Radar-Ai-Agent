"""
RepoRadar: Agent 2 - Intent Analyzer Agent
Extracts and structures user requirements into machine-readable specifications.

Part of the RepoRadar Multi-Agent System for Fetch-A-Thon 3.0 (ASI:One Agent Challenge).
Independently deployable and copy-pasteable into Agentverse.
"""

import os
import sys
import json
import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

# Ensure an asyncio event loop exists across Python 3.10-3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from uagents import Agent, Context, Model, Protocol, Field

import urllib.request
import urllib.error

# Try requests, fallback to urllib.request
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# Logging Configuration (Excludes Secrets)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [IntentAgent] %(message)s",
)
logger = logging.getLogger("reporadar_intent_agent")


# ---------------------------------------------------------------------------
# Duplicated Cross-Agent Message Models (Matching contracts across all agents)
# ---------------------------------------------------------------------------
class IntentRequest(Model):
    task_id: str
    text: str


class IntentResponse(Model):
    task_id: str
    requirements: dict
    error: str = ""


class DiscoveryRequest(Model):
    task_id: str
    requirements: dict


class DiscoveryResponse(Model):
    task_id: str
    candidates: list
    warnings: list = Field(default_factory=list)
    error: str = ""


class RankingRequest(Model):
    task_id: str
    requirements: dict
    candidates: list


class RankingResponse(Model):
    task_id: str
    recommendations: list
    summary: str = ""
    warnings: list = Field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Agent Initialization
# ---------------------------------------------------------------------------
agent_seed = os.getenv("AGENT_SEED")
if agent_seed:
    agent = Agent(name="reporadar_intent_agent", seed=agent_seed)
else:
    agent = Agent(name="reporadar_intent_agent")

logger.info("Initializing RepoRadar Intent Analyzer Agent...")


# ---------------------------------------------------------------------------
# Deterministic Rule-Based Fallback Intent Extractor
# ---------------------------------------------------------------------------
def extract_intent_fallback(text: str) -> Dict[str, Any]:
    """
    Deterministic rule-based intent extractor.
    Used when ASI1_API_KEY is unset or the ASI:One LLM API is unreachable.
    Provides reliable, structured extraction for common technical queries.
    """
    text_lower = text.lower()

    # 1. Target Platforms
    platforms = []
    if re.search(r"\blinux\b|\bubuntu\b|\bdebian\b|\bfedora\b|\barch\b", text_lower):
        platforms.append("linux")
    if re.search(r"\bwindows\b|\bwin11\b|\bwin10\b", text_lower):
        platforms.append("windows")
    if re.search(r"\bmac\b|\bmacos\b|\bosx\b|\bapple\b", text_lower):
        platforms.append("macos")
    if re.search(r"\bdocker\b|\bcontainer\b", text_lower):
        platforms.append("docker")

    # 2. Licensing Constraints
    license_constraints = []
    if "open source" in text_lower or "open-source" in text_lower or "foss" in text_lower or "floss" in text_lower:
        license_constraints.append("open_source")
    if "free" in text_lower:
        license_constraints.append("free")
    if "permissive" in text_lower or "mit" in text_lower or "apache" in text_lower or "bsd" in text_lower:
        license_constraints.append("permissive")
    if "gpl" in text_lower or "copyleft" in text_lower:
        license_constraints.append("copyleft")

    # 3. Skill Level
    skill_level = "any"
    if re.search(r"\bbeginner\b|\bnovice\b|\bnewbie\b|\beasy\b|\bsimple\b", text_lower):
        skill_level = "beginner"
    elif re.search(r"\badvanced\b|\bexpert\b|\bprofessional\b|\bpro\b", text_lower):
        skill_level = "advanced"
    elif re.search(r"\bintermediate\b", text_lower):
        skill_level = "intermediate"

    # 4. Domain & Category Extraction
    category = "developer-tools"
    core_problem = text.strip()
    required_features = []
    preferred_features = []
    queries = []

    # Electronic Design Automation / Circuit Design
    if any(k in text_lower for k in ["circuit", "schematic", "pcb", "eda", "electronics", "spice", "breadboard"]):
        category = "electronic-design-automation"
        core_problem = "Electronic design automation and PCB design suite"

        if "schematic" in text_lower:
            required_features.append("schematic design")
        if "simulation" in text_lower or "simulate" in text_lower or "spice" in text_lower:
            required_features.append("circuit simulation")
        if "pcb" in text_lower or "board layout" in text_lower or "layout" in text_lower:
            required_features.append("pcb layout")
        if "gerber" in text_lower:
            required_features.append("gerber viewer")

        if skill_level == "beginner":
            preferred_features.append("beginner friendly UI")

        # Concise, complementary GitHub search queries (2-3 words each for optimal GitHub matching)
        queries = [
            "schematic pcb",
            "circuit simulator",
            "pcb design eda",
            "kicad eda",
        ]

    # Video Editing / Media
    elif any(k in text_lower for k in ["video edit", "video editor", "cut video", "timeline", "transcode"]):
        category = "video-editing"
        core_problem = "Open source video editing and production software"
        if "multitrack" in text_lower or "multi-track" in text_lower:
            required_features.append("multitrack editing")
        if "effects" in text_lower or "filter" in text_lower:
            required_features.append("video effects")
        required_features.append("video rendering")

        queries = [
            "open source video editor linux",
            "video editing timeline multitrack",
            "kdenlive shotcut video editor",
        ]

    # Web Scraping / Data Mining
    elif any(k in text_lower for k in ["scrape", "scraper", "scraping", "crawler", "crawl"]):
        category = "web-scraping"
        core_problem = "Web scraping and data extraction framework"
        if "headless" in text_lower or "browser" in text_lower:
            required_features.append("headless browser support")
        if "async" in text_lower or "fast" in text_lower:
            required_features.append("asynchronous scraping")

        queries = [
            "web scraper crawler python",
            "web scraping framework async",
            "headless browser data scraper",
        ]

    # Generic technical tool extraction
    else:
        # Extract features by detecting words following 'need', 'require', 'must have'
        match = re.search(r"(?:need|needs|require|requires|want|wants)\s+([^.]+)", text_lower)
        if match:
            found_chunk = match.group(1)
            parts = [p.strip() for p in re.split(r",|\band\b", found_chunk) if len(p.strip()) > 2]
            for p in parts[:4]:
                if not any(k in p for k in ["free", "open source", "linux", "windows", "beginner"]):
                    required_features.append(p)

        # Build search queries from text tokens (keep to 2-3 words for high recall on GitHub)
        words = [w for w in re.findall(r"\b[a-zA-Z]{3,}\b", text_lower)
                 if w not in {"the", "and", "for", "with", "that", "this", "need", "want", "use", "using", "free", "open", "source"}]
        query_words = words[:3]
        if query_words:
            queries.append(" ".join(query_words))
            if len(words) >= 2:
                queries.append(f"{words[0]} {words[1]}")
            if platforms:
                queries.append(f"{words[0]} {platforms[0]}")
        else:
            queries.append(text.strip()[:20])

    # Default queries if none generated
    if not queries:
        queries = [f"{category}", f"{category} tool"]

    # Mandatory constraints
    mandatory_constraints = list(set(platforms + [lc for lc in license_constraints if lc == "open_source"]))

    # Check for empty or unintelligible input
    needs_clarification = len(text.strip()) < 4 or (len(words if 'words' in locals() else []) == 0 and not required_features and category == "developer-tools")

    return {
        "original_request": text,
        "normalized_task": core_problem,
        "category": category,
        "mandatory_constraints": mandatory_constraints,
        "required_features": required_features,
        "preferred_features": preferred_features,
        "target_platforms": platforms,
        "license_constraints": license_constraints,
        "skill_level": skill_level,
        "hardware_constraints": [],
        "integration_requirements": [],
        "offline_or_privacy": [],
        "preferred_technologies": [],
        "search_queries": queries[:4],
        "unresolved_ambiguities": [],
        "needs_clarification": needs_clarification,
        "clarification_question": (
            "Could you specify what kind of software or task you need assistance with "
            "(for example: circuit design, video editing, data scraping)?" if needs_clarification else ""
        ),
    }


# ---------------------------------------------------------------------------
# ASI:One API Intent Analyzer
# ---------------------------------------------------------------------------
def call_asi1_llm(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Calls the ASI:One OpenAI-compatible chat completions API.
    Returns parsed structured specification dict, or None on failure.
    """
    api_key = os.getenv("ASI1_API_KEY", "").strip()
    if not api_key:
        logger.info("ASI1_API_KEY not configured. Falling back to deterministic intent analyzer.")
        return None

    base_url = os.getenv("ASI1_BASE_URL", "https://api.asi1.ai/v1").rstrip("/")
    model_name = os.getenv("ASI1_MODEL", "asi1-mini").strip()
    endpoint = f"{base_url}/chat/completions"

    system_prompt = (
        "You are RepoRadar's Intent Analyzer. Your task is to analyze a user's natural-language request "
        "for software and produce a strict, machine-readable JSON object.\n\n"
        "Rules:\n"
        "1. Distinguish must-haves from preferences:\n"
        "   - Mandatory platform requirements (e.g. Linux, Windows) must go to target_platforms and mandatory_constraints.\n"
        "   - Mandatory license requirements (e.g. open source) must go to license_constraints and mandatory_constraints.\n"
        "   - 'Free' and 'open source' are NOT interchangeable.\n"
        "   - Explicitly requested capabilities are required_features.\n"
        "   - Nice-to-haves and usability requests (e.g. beginner friendly) are preferred_features.\n"
        "2. Generate 3 to 4 distinct, concise, complementary GitHub search queries (2 to 3 words each, e.g. 'schematic pcb', 'circuit simulator').\n"
        "   Do NOT generate long queries or include filler words like 'free' or 'open source' in the queries, as GitHub search treats words as strict AND conditions.\n"
        "3. Output ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "original_request": string,\n'
        '  "normalized_task": string,\n'
        '  "category": string,\n'
        '  "mandatory_constraints": [string],\n'
        '  "required_features": [string],\n'
        '  "preferred_features": [string],\n'
        '  "target_platforms": [string],\n'
        '  "license_constraints": [string],\n'
        '  "skill_level": "beginner" | "intermediate" | "advanced" | "any",\n'
        '  "hardware_constraints": [string],\n'
        '  "integration_requirements": [string],\n'
        '  "offline_or_privacy": [string],\n'
        '  "preferred_technologies": [string],\n'
        '  "search_queries": [string],\n'
        '  "unresolved_ambiguities": [string],\n'
        '  "needs_clarification": boolean,\n'
        '  "clarification_question": string\n'
        "}"
    )

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.1,
        "stream": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        if HAS_REQUESTS:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"ASI:One API returned HTTP status {resp.status_code}")
                return None
            data = resp.json()
        else:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))

        # Extract content from chat completion
        choices = data.get("choices", [])
        if not choices:
            return None

        content = choices[0].get("message", {}).get("content", "").strip()

        # Strip markdown json code fences if present
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n?", "", content)
            content = re.sub(r"\n?```$", "", content)

        parsed = json.loads(content)
        parsed["original_request"] = user_text
        return parsed

    except Exception as e:
        logger.warning(f"Failed to query ASI:One API: {e}. Will use fallback extractor.")
        return None


# ---------------------------------------------------------------------------
# Startup Event Handler
# ---------------------------------------------------------------------------
@agent.on_event("startup")
async def on_startup(ctx: Context):
    api_key_set = bool(os.getenv("ASI1_API_KEY", "").strip())
    ctx.logger.info("==================================================================")
    ctx.logger.info("🧠 RepoRadar Intent Analyzer Agent is ACTIVE and ready!")
    ctx.logger.info(f"📍 Agent Address: {ctx.agent.address}")
    ctx.logger.info(f"🔑 ASI1_API_KEY:  {'Configured (using ASI:One LLM)' if api_key_set else 'Not set (using deterministic NLP fallback)'}")
    ctx.logger.info("==================================================================")


# ---------------------------------------------------------------------------
# Message Handler
# ---------------------------------------------------------------------------
intent_proto = Protocol(name="RepoRadarIntentProtocol", version="1.0.0")


@intent_proto.on_message(model=IntentRequest, replies={IntentResponse})
async def handle_intent_request(ctx: Context, sender: str, msg: IntentRequest):
    """Processes IntentRequest, extracting structured requirements."""
    ctx.logger.info(f"📥 Processing IntentRequest for task {msg.task_id} from {sender[:12]}...")

    if not msg.text or not msg.text.strip():
        await ctx.send(
            sender,
            IntentResponse(
                task_id=msg.task_id,
                requirements={},
                error="Empty request text received.",
            ),
        )
        return

    try:
        # Attempt ASI:One LLM call, fallback to deterministic parser
        requirements = call_asi1_llm(msg.text)
        if not requirements:
            requirements = extract_intent_fallback(msg.text)

        # Sanitize and validate essential fields
        if not isinstance(requirements.get("search_queries"), list) or not requirements["search_queries"]:
            requirements["search_queries"] = [msg.text.strip()[:50]]

        if not isinstance(requirements.get("mandatory_constraints"), list):
            requirements["mandatory_constraints"] = []

        if not isinstance(requirements.get("required_features"), list):
            requirements["required_features"] = []

        err_msg = ""
    except Exception as e:
        ctx.logger.error(f"Error processing intent: {e}")
        requirements = extract_intent_fallback(msg.text)
        err_msg = ""

    ctx.logger.info(
        f"✅ Task {msg.task_id}: Extracted category '{requirements.get('category')}' with "
        f"{len(requirements.get('search_queries', []))} search queries."
    )

    await ctx.send(
        sender,
        IntentResponse(
            task_id=msg.task_id,
            requirements=requirements,
            error=err_msg,
        ),
    )


# ---------------------------------------------------------------------------
# Attach Protocol
# ---------------------------------------------------------------------------
agent.include(intent_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent.run()
