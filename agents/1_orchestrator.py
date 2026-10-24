"""
RepoRadar: Agent 1 - Orchestrator Agent
Public ASI:One chat entry point and multi-agent workflow coordinator.

Part of the RepoRadar Multi-Agent System for Fetch-A-Thon 3.0 (ASI:One Agent Challenge).
Independently deployable and copy-pasteable into Agentverse.
"""

import os
import sys
import time
import json
import logging
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from typing import Dict, List, Any, Optional

# Ensure an asyncio event loop exists across Python 3.10-3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from uagents import Agent, Context, Model, Protocol, Field
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    ChatAcknowledgement,
    TextContent,
    chat_protocol_spec,
)

# ---------------------------------------------------------------------------
# Logging Configuration (Fallback)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Orchestrator] %(message)s",
)


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
# Persistent State Management via ctx.storage (Agentverse Persistent Storage)
# ---------------------------------------------------------------------------
# In-memory fast cache as fallback
_MEMORY_TASKS: Dict[str, Dict[str, Any]] = {}


def save_task(ctx: Context, task_id: str, task_data: dict):
    """Saves task state to persistent Agentverse storage and local cache."""
    _MEMORY_TASKS[task_id] = task_data
    try:
        ctx.storage.set(f"task:{task_id}", task_data)
        # Update list of active task IDs
        active_ids = ctx.storage.get("active_task_ids") or []
        if task_id not in active_ids:
            active_ids.append(task_id)
            ctx.storage.set("active_task_ids", active_ids)
    except Exception as e:
        ctx.logger.warning(f"Error persisting task {task_id}: {e}")


def get_task(ctx: Context, task_id: str) -> Optional[dict]:
    """Retrieves task state from persistent Agentverse storage or local cache."""
    try:
        val = ctx.storage.get(f"task:{task_id}")
        if val:
            _MEMORY_TASKS[task_id] = val
            return val
    except Exception as e:
        ctx.logger.warning(f"Error reading task {task_id} from storage: {e}")
    return _MEMORY_TASKS.get(task_id)


def remove_task(ctx: Context, task_id: str):
    """Cleans up task from persistent storage and local cache."""
    _MEMORY_TASKS.pop(task_id, None)
    try:
        if ctx.storage.has(f"task:{task_id}"):
            ctx.storage.remove(f"task:{task_id}")
        active_ids = ctx.storage.get("active_task_ids") or []
        if task_id in active_ids:
            active_ids.remove(task_id)
            ctx.storage.set("active_task_ids", active_ids)
    except Exception as e:
        ctx.logger.warning(f"Error cleaning task {task_id}: {e}")


def set_sender_task(ctx: Context, sender: str, task_id: str):
    """Maps a user sender address to an active task for follow-up questions."""
    try:
        ctx.storage.set(f"sender:{sender}", task_id)
    except Exception:
        pass


def get_sender_task(ctx: Context, sender: str) -> Optional[str]:
    """Retrieves the active task ID for a sender."""
    try:
        return ctx.storage.get(f"sender:{sender}")
    except Exception:
        return None


def get_task_timeout() -> float:
    """Reads configured timeout in seconds, with safe default."""
    try:
        return float(os.getenv("TASK_TIMEOUT_SECONDS", "90"))
    except ValueError:
        return 90.0


# ---------------------------------------------------------------------------
# Agent Initialization
# ---------------------------------------------------------------------------
agent_seed = os.getenv("AGENT_SEED")
if agent_seed:
    agent = Agent(name="reporadar_orchestrator", seed=agent_seed)
else:
    agent = Agent(name="reporadar_orchestrator")


# ---------------------------------------------------------------------------
# Protocol Definitions
# ---------------------------------------------------------------------------
# 1. ASI:One Chat Protocol (Standard ACP)
chat_proto = Protocol(spec=chat_protocol_spec)

# 2. Worker Workflow Protocol (Cross-Agent Coordination)
workflow_proto = Protocol(name="RepoRadarWorkflowProtocol", version="1.0.0")


# ---------------------------------------------------------------------------
# Startup Event Handler (Provides immediate feedback in Agentverse logs)
# ---------------------------------------------------------------------------
@agent.on_event("startup")
async def on_startup(ctx: Context):
    intent_addr = os.getenv("INTENT_AGENT_ADDRESS", "").strip()
    discovery_addr = os.getenv("DISCOVERY_AGENT_ADDRESS", "").strip()
    ranking_addr = os.getenv("RANKING_AGENT_ADDRESS", "").strip()

    ctx.logger.info("==================================================================")
    ctx.logger.info("🛰️  RepoRadar Orchestrator Agent is ACTIVE and listening for queries!")
    ctx.logger.info(f"📍 Agent Address: {ctx.agent.address}")
    ctx.logger.info(f"🔗 Intent Worker:    {intent_addr if intent_addr else '⚠️ NOT SET (configure INTENT_AGENT_ADDRESS)'}")
    ctx.logger.info(f"🔗 Discovery Worker: {discovery_addr if discovery_addr else '⚠️ NOT SET (configure DISCOVERY_AGENT_ADDRESS)'}")
    ctx.logger.info(f"🔗 Ranking Worker:   {ranking_addr if ranking_addr else '⚠️ NOT SET (configure RANKING_AGENT_ADDRESS)'}")
    ctx.logger.info("👉 Ready to accept messages from ASI:One chat or Agentverse Chat tab.")
    ctx.logger.info("==================================================================")


# ---------------------------------------------------------------------------
# Response Formatting Helper
# ---------------------------------------------------------------------------
def format_final_report(
    user_query: str,
    requirements: dict,
    recommendations: list,
    summary: str,
    warnings: list,
) -> str:
    """Formats a decision-ready markdown response for ASI:One chat."""
    lines = []
    lines.append("## 🛰️ RepoRadar Discovery & Verification Report")
    lines.append("")

    # Brief summary
    if summary:
        lines.append(f"**Executive Summary:** {summary}")
        lines.append("")
    else:
        norm_task = requirements.get("normalized_task") or user_query
        lines.append(f"Analysis complete for: *\"{norm_task}\"*.")
        lines.append("")

    # If warnings present (e.g. rate limits or partial results)
    if warnings:
        clean_warnings = [w for w in warnings if w]
        if clean_warnings:
            lines.append("> [!NOTE]")
            for w in clean_warnings:
                lines.append(f"> ℹ️ {w}")
            lines.append("")

    # Recommendations
    if not recommendations:
        lines.append("### 🔍 No Fully Matching Repositories Found")
        lines.append(
            "None of the discovered repositories met all mandatory constraints "
            "with verified evidence."
        )
        lines.append("")
        lines.append("**Suggestions:**")
        lines.append("- Relax platform or strict licensing requirements.")
        lines.append("- Specify alternative or broader feature requirements.")
        lines.append("")
        lines.append("Would you like me to rerun the search with adjusted criteria?")
        return "\n".join(lines)

    lines.append(f"### 🏆 Top Verified Matches ({len(recommendations)} Found)")
    lines.append("")

    for idx, rec in enumerate(recommendations, 1):
        repo_name = rec.get("repo_name", "Unknown Repository")
        repo_url = rec.get("repo_url", "https://github.com")
        score = rec.get("score", 0.0)
        why_fits = rec.get("why_it_fits", "Matches key requirements.")
        license_status = rec.get("license_status", "Unknown")
        maintenance = rec.get("maintenance_indicators", "Unknown activity")
        coverage = rec.get("requirement_coverage", "N/A")
        confidence = rec.get("confidence", "Medium")

        lines.append(f"#### {idx}. [{repo_name}]({repo_url}) — **Score: {score:.1f}/100**")
        lines.append(f"- **Fit Assessment:** {why_fits}")
        lines.append(f"- **Coverage & Confidence:** {coverage} | Confidence: **{confidence}**")
        lines.append(f"- **License:** {license_status}")
        lines.append(f"- **Maintenance:** {maintenance}")

        # Verified features
        verified_feats = rec.get("verified_features", [])
        if verified_feats:
            lines.append("- **Verified Features:**")
            for vf in verified_feats:
                feat_name = vf.get("feature", "Feature")
                expl = vf.get("explanation", "Supported")
                evidence_url = vf.get("evidence_url")
                if evidence_url:
                    lines.append(f"  - ✅ **{feat_name}**: {expl} ([Evidence]({evidence_url}))")
                else:
                    lines.append(f"  - ✅ **{feat_name}**: {expl}")

        # Limitations & Warnings
        limitations = rec.get("limitations", [])
        if limitations:
            lines.append("- **Limitations & Trade-offs:**")
            for lim in limitations:
                lines.append(f"  - ⚠️ {lim}")

        # Unknowns
        unknowns = rec.get("unknowns", [])
        if unknowns:
            lines.append("- **Unverified / Unknowns:**")
            for unk in unknowns:
                lines.append(f"  - ❓ {unk}")

        lines.append("")

    # Next steps & follow-up
    lines.append("---")
    lines.append(
        "💡 **Next Steps:** Would you like to inspect installation steps for a specific Linux distribution, "
        "or evaluate alternative tools for specific workflows?"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ASI:One Chat Protocol Handlers
# ---------------------------------------------------------------------------
@chat_proto.on_message(ChatMessage)
async def handle_incoming_chat(ctx: Context, sender: str, msg: ChatMessage):
    """Handles user messages originating from ASI:One or Agentverse Chat."""
    ctx.logger.info(f"📨 Incoming ChatMessage received from session sender {sender[:12]}...")

    # 1. Extract user query text
    user_text = msg.text().strip()
    if not user_text:
        welcome_text = (
            "👋 **Welcome to RepoRadar!**\n\n"
            "Search less. Verify more. Build with confidence.\n\n"
            "Please describe the software you are looking for. Include your operating system, "
            "licensing preferences, and must-have features.\n\n"
            "*Example: 'I need free, open-source circuit design software for Linux. "
            "I am a beginner and need schematic design, simulation, and PCB layout.'*"
        )
        await ctx.send(sender, ChatMessage(content=[TextContent(text=welcome_text)]))
        return

    ctx.logger.info(f"📝 User query: '{user_text[:70]}...'")

    # 3. Check for worker agent configuration
    intent_addr = os.getenv("INTENT_AGENT_ADDRESS", "").strip()
    discovery_addr = os.getenv("DISCOVERY_AGENT_ADDRESS", "").strip()
    ranking_addr = os.getenv("RANKING_AGENT_ADDRESS", "").strip()

    missing_workers = []
    if not intent_addr:
        missing_workers.append("INTENT_AGENT_ADDRESS")
    if not discovery_addr:
        missing_workers.append("DISCOVERY_AGENT_ADDRESS")
    if not ranking_addr:
        missing_workers.append("RANKING_AGENT_ADDRESS")

    if missing_workers:
        err_msg = (
            "⚙️ **Configuration Notice**: RepoRadar Orchestrator is missing required worker agent addresses:\n\n"
            + "\n".join(f"- `{w}`" for w in missing_workers)
            + "\n\nPlease configure these addresses as secrets in Agentverse for the Orchestrator agent."
        )
        ctx.logger.error(f"Missing worker configuration: {missing_workers}")
        await ctx.send(sender, ChatMessage(content=[TextContent(text=err_msg)]))
        return

    # 4. Check for active task awaiting clarification from this sender
    existing_task_id = get_sender_task(ctx, sender)
    if existing_task_id:
        existing_task = get_task(ctx, existing_task_id)
        if existing_task and existing_task.get("stage") == "WAITING_CLARIFICATION":
            ctx.logger.info(f"Received clarification for task {existing_task_id}")
            # Combine initial query with clarification
            combined_text = f"{existing_task['user_text']}. Clarification: {user_text}"
            existing_task["user_text"] = combined_text
            existing_task["stage"] = "PENDING_INTENT"
            existing_task["updated_at"] = time.time()
            save_task(ctx, existing_task_id, existing_task)

            await ctx.send(
                intent_addr,
                IntentRequest(task_id=existing_task_id, text=combined_text),
            )
            return

    # 5. Create new workflow task
    task_id = f"task_{uuid4().hex[:12]}"
    now = time.time()

    task_record = {
        "task_id": task_id,
        "session_sender": sender,
        "original_msg_id": str(msg.msg_id),
        "user_text": user_text,
        "stage": "PENDING_INTENT",
        "created_at": now,
        "updated_at": now,
        "requirements": None,
        "candidates": None,
    }
    save_task(ctx, task_id, task_record)
    set_sender_task(ctx, sender, task_id)

    ctx.logger.info(f"🚀 Created task {task_id}. Dispatching IntentRequest to {intent_addr[:12]}...")

    await ctx.send(
        intent_addr,
        IntentRequest(task_id=task_id, text=user_text),
    )


@chat_proto.on_message(ChatAcknowledgement)
async def handle_chat_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    """Handles acknowledgements received from ASI:One chat clients."""
    ctx.logger.debug(f"ChatAcknowledgement received for message ID: {msg.acknowledged_msg_id}")


# ---------------------------------------------------------------------------
# Worker Agent Response Handlers
# ---------------------------------------------------------------------------
@workflow_proto.on_message(model=IntentResponse)
async def handle_intent_response(ctx: Context, sender: str, msg: IntentResponse):
    """Receives structured requirements from Agent 2 (Intent Agent)."""
    task = get_task(ctx, msg.task_id)
    if not task:
        ctx.logger.error(f"IntentResponse received for unknown or pruned task: {msg.task_id}")
        return

    if task["stage"] != "PENDING_INTENT":
        ctx.logger.warning(f"IntentResponse ignored for task {msg.task_id} in stage {task['stage']}")
        return

    # Check for intent extraction errors
    if msg.error:
        task["stage"] = "FAILED"
        task["updated_at"] = time.time()
        save_task(ctx, msg.task_id, task)
        err_msg = (
            f"❌ **Intent Analysis Notice**: {msg.error}\n\n"
            "Please refine your request with more details about your software needs."
        )
        ctx.logger.error(f"Task {msg.task_id} failed in Intent phase: {msg.error}")
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=err_msg)]))
        return

    reqs = msg.requirements or {}
    task["requirements"] = reqs
    task["updated_at"] = time.time()

    # Check if critical clarification is needed
    if reqs.get("needs_clarification"):
        clarification_q = reqs.get("clarification_question") or (
            "Could you specify the primary software category or task you want to accomplish?"
        )
        task["stage"] = "WAITING_CLARIFICATION"
        save_task(ctx, msg.task_id, task)
        ctx.logger.info(f"Task {msg.task_id} requires clarification from user.")
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=clarification_q)]))
        return

    # Dispatch to Discovery Agent (Agent 3)
    discovery_addr = os.getenv("DISCOVERY_AGENT_ADDRESS", "").strip()
    if not discovery_addr:
        task["stage"] = "FAILED"
        save_task(ctx, msg.task_id, task)
        err_msg = "⚠️ Discovery Agent address is not configured on the Orchestrator."
        ctx.logger.error(err_msg)
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=err_msg)]))
        return

    task["stage"] = "PENDING_DISCOVERY"
    save_task(ctx, msg.task_id, task)
    ctx.logger.info(f"Task {msg.task_id}: Requirements extracted. Dispatching DiscoveryRequest to {discovery_addr[:12]}...")

    await ctx.send(
        discovery_addr,
        DiscoveryRequest(task_id=msg.task_id, requirements=reqs),
    )


@workflow_proto.on_message(model=DiscoveryResponse)
async def handle_discovery_response(ctx: Context, sender: str, msg: DiscoveryResponse):
    """Receives verified candidate repositories from Agent 3 (Discovery Agent)."""
    task = get_task(ctx, msg.task_id)
    if not task:
        ctx.logger.error(f"DiscoveryResponse received for unknown task: {msg.task_id}")
        return

    if task["stage"] != "PENDING_DISCOVERY":
        ctx.logger.warning(f"DiscoveryResponse ignored for task {msg.task_id} in stage {task['stage']}")
        return

    # Check for discovery errors
    if msg.error and not msg.candidates:
        task["stage"] = "FAILED"
        task["updated_at"] = time.time()
        save_task(ctx, msg.task_id, task)
        err_msg = (
            f"❌ **GitHub Discovery Notice**: {msg.error}\n\n"
            "Live GitHub discovery encountered an error. Please try again in a few moments."
        )
        ctx.logger.error(f"Task {msg.task_id} failed in Discovery phase: {msg.error}")
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=err_msg)]))
        return

    candidates = msg.candidates or []
    task["candidates"] = candidates
    task["updated_at"] = time.time()

    # If no candidates found
    if not candidates:
        task["stage"] = "COMPLETED"
        save_task(ctx, msg.task_id, task)
        no_cand_msg = (
            "🔍 **No Matching Repositories Discovered**\n\n"
            "RepoRadar searched GitHub for your criteria, but no repositories matched the specified "
            "constraints or keywords.\n\n"
            "**Suggestions to broaden the search:**\n"
            "- Relax strict platform or licensing requirements.\n"
            "- Try describing the core function in different words (e.g. *EDA*, *CAD*, *schematic*)."
        )
        ctx.logger.info(f"Task {msg.task_id}: Zero candidates returned from Discovery.")
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=no_cand_msg)]))
        return

    # Dispatch to Ranking Agent (Agent 4)
    ranking_addr = os.getenv("RANKING_AGENT_ADDRESS", "").strip()
    if not ranking_addr:
        task["stage"] = "FAILED"
        save_task(ctx, msg.task_id, task)
        err_msg = "⚠️ Ranking Agent address is not configured on the Orchestrator."
        ctx.logger.error(err_msg)
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=err_msg)]))
        return

    task["stage"] = "PENDING_RANKING"
    save_task(ctx, msg.task_id, task)
    ctx.logger.info(
        f"Task {msg.task_id}: {len(candidates)} candidates discovered. "
        f"Dispatching RankingRequest to {ranking_addr[:12]}..."
    )

    await ctx.send(
        ranking_addr,
        RankingRequest(
            task_id=msg.task_id,
            requirements=task["requirements"] or {},
            candidates=candidates,
        ),
    )


@workflow_proto.on_message(model=RankingResponse)
async def handle_ranking_response(ctx: Context, sender: str, msg: RankingResponse):
    """Receives scored recommendations from Agent 4 (Ranking Agent)."""
    task = get_task(ctx, msg.task_id)
    if not task:
        ctx.logger.error(f"RankingResponse received for unknown task: {msg.task_id}")
        return

    if task["stage"] != "PENDING_RANKING":
        ctx.logger.warning(f"RankingResponse ignored for task {msg.task_id} in stage {task['stage']}")
        return

    # Check for ranking errors
    if msg.error and not msg.recommendations:
        task["stage"] = "FAILED"
        task["updated_at"] = time.time()
        save_task(ctx, msg.task_id, task)
        err_msg = f"❌ **Ranking Notice**: {msg.error}\n\nUnable to rank candidates at this time."
        ctx.logger.error(f"Task {msg.task_id} failed in Ranking phase: {msg.error}")
        await ctx.send(task["session_sender"], ChatMessage(content=[TextContent(text=err_msg)]))
        return

    task["stage"] = "COMPLETED"
    task["updated_at"] = time.time()
    save_task(ctx, msg.task_id, task)

    ctx.logger.info(
        f"Task {msg.task_id}: Successfully completed ranking with {len(msg.recommendations)} recommendation(s)."
    )

    final_report = format_final_report(
        user_query=task["user_text"],
        requirements=task["requirements"] or {},
        recommendations=msg.recommendations or [],
        summary=msg.summary or "",
        warnings=msg.warnings or [],
    )

    try:
        await ctx.send(
            task["session_sender"],
            ChatMessage(content=[TextContent(text=final_report)]),
        )
    except Exception as e:
        ctx.logger.error(f"Failed to deliver final report to user session: {e}")


# ---------------------------------------------------------------------------
# Periodic Task Timeout and Cleanup Handler
# ---------------------------------------------------------------------------
@agent.on_interval(period=20.0)
async def check_task_timeouts(ctx: Context):
    """Prevents indefinite hangs by timing out stalled tasks and pruning storage."""
    try:
        active_ids = ctx.storage.get("active_task_ids") or []
    except Exception:
        active_ids = list(_MEMORY_TASKS.keys())

    if not active_ids:
        return

    now = time.time()
    timeout_threshold = get_task_timeout()
    expired_ids = []

    for task_id in list(active_ids):
        task = get_task(ctx, task_id)
        if not task:
            expired_ids.append(task_id)
            continue

        stage = task.get("stage", "UNKNOWN")

        # Prune old completed/failed tasks after 15 minutes
        if stage in ("COMPLETED", "FAILED"):
            if now - task.get("updated_at", now) > 900:
                expired_ids.append(task_id)
            continue

        # Keep waiting tasks in WAITING_CLARIFICATION alive up to 10 minutes
        if stage == "WAITING_CLARIFICATION":
            if now - task.get("created_at", now) > 600:
                task["stage"] = "FAILED"
                task["updated_at"] = now
                save_task(ctx, task_id, task)
                expired_ids.append(task_id)
            continue

        # Check for worker timeout in active pipeline stages
        if now - task.get("created_at", now) > timeout_threshold:
            task["stage"] = "FAILED"
            task["updated_at"] = now
            save_task(ctx, task_id, task)
            ctx.logger.warning(f"Task {task_id} timed out during stage '{stage}'.")

            timeout_notice = (
                f"⏱️ **Workflow Timeout**: Your software discovery request timed out during the `{stage}` phase.\n\n"
                "A worker agent did not respond within the allocated time window. "
                "Please verify that the Intent, Discovery, and Ranking agents are deployed and running, "
                "then try your request again."
            )
            try:
                await ctx.send(
                    task["session_sender"],
                    ChatMessage(content=[TextContent(text=timeout_notice)]),
                )
            except Exception as e:
                ctx.logger.error(f"Failed to send timeout notification to user: {e}")

    for tid in expired_ids:
        remove_task(ctx, tid)


# ---------------------------------------------------------------------------
# Attach Protocols to Agent
# ---------------------------------------------------------------------------
agent.include(chat_proto, publish_manifest=True)
agent.include(workflow_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent.run()
