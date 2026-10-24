"""
RepoRadar: Agent 4 - Ranking and Decision Support Agent
Applies mandatory constraint filtering, explainable multi-factor scoring, and trade-off analysis.

Part of the RepoRadar Multi-Agent System for Fetch-A-Thon 3.0 (ASI:One Agent Challenge).
Independently deployable and copy-pasteable into Agentverse.
"""

import os
import sys
import json
import time
import math
import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

# Ensure an asyncio event loop exists across Python 3.10-3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from uagents import Agent, Context, Model, Protocol, Field

# ---------------------------------------------------------------------------
# Logging Configuration (Excludes Secrets)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [RankingAgent] %(message)s",
)
logger = logging.getLogger("reporadar_ranking_agent")


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
# Ranking Constants & Weights (Documented as per build specification)
# ---------------------------------------------------------------------------
WEIGHT_RELEVANCE = 0.40          # 40%: Task & domain alignment
WEIGHT_FEATURE_COMPAT = 0.25     # 25%: Verified mandatory and preferred features
WEIGHT_MAINTENANCE = 0.15        # 15%: Commit recency and active maintenance
WEIGHT_POPULARITY = 0.10         # 10%: Community adoption (log-scaled stars)
WEIGHT_DOC_USABILITY = 0.10      # 10%: README completeness, license, beginner fit


# ---------------------------------------------------------------------------
# Agent Initialization
# ---------------------------------------------------------------------------
agent_seed = os.getenv("AGENT_SEED")
if agent_seed:
    agent = Agent(name="reporadar_ranking_agent", seed=agent_seed)
else:
    agent = Agent(name="reporadar_ranking_agent")

logger.info("Initializing RepoRadar Ranking and Decision Support Agent...")


# ---------------------------------------------------------------------------
# Hard Filtering Engine (Step 1)
# ---------------------------------------------------------------------------
def apply_hard_filters(candidate: dict, requirements: dict) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether a candidate satisfies mandatory hard constraints.
    Returns: (is_admissible, rejection_reason)
    """
    # Reject archived projects if active maintenance is expected
    if candidate.get("archived"):
        return False, "Repository is archived and no longer actively maintained."

    # Platform constraints check
    target_platforms = requirements.get("target_platforms") or []
    platform_evidence = candidate.get("platform_evidence") or {}
    for plat in target_platforms:
        p_eval = platform_evidence.get(plat) or {}
        if p_eval.get("status") == "not_supported":
            return False, f"Target platform '{plat}' is explicitly unsupported."

    # Required features check
    required_features = requirements.get("required_features") or []
    feat_evidence = candidate.get("feature_evidence") or {}
    for feat in required_features:
        f_eval = feat_evidence.get(feat) or {}
        if f_eval.get("status") == "not_supported":
            return False, f"Mandatory feature '{feat}' is explicitly unsupported."

    # Open source licensing check
    license_constraints = requirements.get("license_constraints") or []
    if "open_source" in license_constraints:
        lic_eval = candidate.get("license") or {}
        if lic_eval.get("status") == "not_supported":
            return False, "Project license is proprietary or incompatible with open-source requirements."

    return True, None


# ---------------------------------------------------------------------------
# Deterministic Scoring Engine (Step 2)
# ---------------------------------------------------------------------------
def score_candidate(candidate: dict, requirements: dict) -> Tuple[float, dict]:
    """
    Calculates explainable, normalized 0-100 score across 5 weighted dimensions.
    Returns: (final_score, breakdown_dict)
    """
    # 1. Relevance Score (0.0 - 1.0)
    category = requirements.get("category", "").lower()
    norm_task = requirements.get("normalized_task", "").lower()
    desc = (candidate.get("description") or "").lower()
    name = (candidate.get("name") or "").lower()

    relevance_points = 0.0
    if category and category in desc or category in name:
        relevance_points += 0.5
    else:
        # Check token match
        cat_tokens = [t for t in re.split(r"[-_ ]", category) if len(t) > 2]
        if any(t in desc or t in name for t in cat_tokens):
            relevance_points += 0.35
        else:
            relevance_points += 0.15

    # Core problem keyword match
    task_words = [w for w in re.findall(r"\b\w{4,}\b", norm_task) if w not in {"with", "that", "this", "free"}]
    if task_words:
        matched_words = sum(1 for w in task_words if w in desc or w in name)
        relevance_points += 0.5 * min(1.0, matched_words / max(1, len(task_words[:4])))
    else:
        relevance_points += 0.3

    score_relevance = min(1.0, relevance_points)

    # 2. Feature Compatibility Score (0.0 - 1.0)
    required_features = requirements.get("required_features") or []
    feat_evidence = candidate.get("feature_evidence") or {}

    if required_features:
        feat_points = 0.0
        for feat in required_features:
            f_eval = feat_evidence.get(feat) or {}
            st = f_eval.get("status", "unknown")
            if st == "verified":
                feat_points += 1.0
            elif st == "unknown":
                feat_points += 0.25  # Honesty: unverified gets 1/4 point
            # not_supported gets 0.0
        score_feature = feat_points / len(required_features)
    else:
        score_feature = 0.8  # Default if no specific features required

    # 3. Maintenance Recency Score (0.0 - 1.0)
    pushed_at_str = candidate.get("pushed_at")
    score_maintenance = 0.2
    if pushed_at_str:
        try:
            # Handle ISO formats
            clean_date = pushed_at_str.replace("Z", "+00:00")
            push_dt = datetime.fromisoformat(clean_date)
            now_dt = datetime.now(timezone.utc)
            days_ago = max(0, (now_dt - push_dt).days)

            if days_ago <= 90:
                score_maintenance = 1.0
            elif days_ago <= 180:
                score_maintenance = 0.8
            elif days_ago <= 365:
                score_maintenance = 0.6
            elif days_ago <= 730:
                score_maintenance = 0.35
            else:
                score_maintenance = 0.15
        except Exception:
            score_maintenance = 0.4

    # 4. Community Adoption / Popularity (0.0 - 1.0)
    stars = candidate.get("stars", 0)
    score_popularity = min(1.0, math.log10(max(1, stars) + 1) / 4.5)

    # 5. Documentation and Usability (0.0 - 1.0)
    score_doc = 0.0
    if candidate.get("has_readme"):
        score_doc += 0.4

    lic_eval = candidate.get("license") or {}
    if lic_eval.get("status") == "verified":
        score_doc += 0.3
    elif lic_eval.get("status") == "unknown":
        score_doc += 0.1

    skill_level = requirements.get("skill_level", "any")
    if skill_level == "beginner":
        if candidate.get("beginner_signals"):
            score_doc += 0.3
        else:
            score_doc += 0.15
    else:
        score_doc += 0.3

    score_doc = min(1.0, score_doc)

    # Weighted composite score (0 - 100)
    final_score = (
        (score_relevance * WEIGHT_RELEVANCE)
        + (score_feature * WEIGHT_FEATURE_COMPAT)
        + (score_maintenance * WEIGHT_MAINTENANCE)
        + (score_popularity * WEIGHT_POPULARITY)
        + (score_doc * WEIGHT_DOC_USABILITY)
    ) * 100.0

    breakdown = {
        "relevance": round(score_relevance * 100, 1),
        "feature_compatibility": round(score_feature * 100, 1),
        "maintenance": round(score_maintenance * 100, 1),
        "popularity": round(score_popularity * 100, 1),
        "documentation_usability": round(score_doc * 100, 1),
    }

    return round(final_score, 1), breakdown


# ---------------------------------------------------------------------------
# Shortlist & Recommendation Assembly (Step 3)
# ---------------------------------------------------------------------------
def build_shortlist(candidates: List[dict], requirements: dict) -> Tuple[List[dict], str, List[str]]:
    """
    Filters, scores, and structures top recommendations with trade-offs.
    Returns: (recommendations, summary, warnings)
    """
    surviving = []
    rejected = []
    warnings = []

    # Step 1: Apply hard filters
    for cand in candidates:
        admissible, reason = apply_hard_filters(cand, requirements)
        if admissible:
            surviving.append(cand)
        else:
            rejected.append((cand.get("full_name"), reason))

    if rejected:
        for r_name, r_reason in rejected[:3]:
            warnings.append(f"Excluded '{r_name}': {r_reason}")

    if not surviving:
        summary = (
            "All candidate repositories were excluded by mandatory constraints. "
            "Consider relaxing platform, license, or feature constraints."
        )
        return [], summary, warnings

    # Step 2: Score surviving candidates
    scored_candidates = []
    required_features = requirements.get("required_features") or []

    for cand in surviving:
        score, breakdown = score_candidate(cand, requirements)
        scored_candidates.append((score, cand, breakdown))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    # Step 3: Format top recommendations (up to 3)
    top_entries = scored_candidates[:3]
    recommendations = []

    for score, cand, breakdown in top_entries:
        full_name = cand.get("full_name")
        html_url = cand.get("html_url")
        desc = cand.get("description") or "Open source project"
        lic_eval = cand.get("license") or {}
        feat_evidence = cand.get("feature_evidence") or {}
        plat_evidence = cand.get("platform_evidence") or {}

        # Verified features list
        verified_feats = []
        unknown_feats = []
        for f_name, f_data in feat_evidence.items():
            if f_data.get("status") == "verified":
                verified_feats.append({
                    "feature": f_name,
                    "explanation": f_data.get("explanation"),
                    "evidence_url": f_data.get("evidence_url"),
                })
            elif f_data.get("status") == "unknown":
                unknown_feats.append(f_name)

        # Platform verification check
        for p_name, p_data in plat_evidence.items():
            if p_data.get("status") == "verified":
                verified_feats.append({
                    "feature": f"{p_name.capitalize()} Support",
                    "explanation": p_data.get("explanation"),
                    "evidence_url": p_data.get("evidence_url"),
                })
            elif p_data.get("status") == "unknown":
                unknown_feats.append(f"{p_name.capitalize()} compatibility")

        # Maintenance text
        stars = cand.get("stars", 0)
        pushed_at = cand.get("pushed_at") or "Unknown"
        if "T" in pushed_at:
            pushed_date = pushed_at.split("T")[0]
        else:
            pushed_date = pushed_at
        maint_text = f"Active ({stars:,} stars, last commit: {pushed_date})"

        # Requirement coverage calculation
        total_reqs = len(required_features)
        verified_reqs = sum(1 for f in required_features if feat_evidence.get(f, {}).get("status") == "verified")
        coverage_text = f"{verified_reqs}/{total_reqs} mandatory features verified" if total_reqs else "General fit"

        # Confidence rating based on evidence completeness
        if total_reqs > 0 and (verified_reqs / total_reqs) >= 0.75:
            confidence = "High"
        elif total_reqs > 0 and (verified_reqs / total_reqs) >= 0.40:
            confidence = "Medium"
        else:
            confidence = "Moderate"

        # Fit explanation
        why_fits = f"{desc} Matches requirements with score {score}/100."
        if cand.get("beginner_signals") and requirements.get("skill_level") == "beginner":
            why_fits += " Features beginner-accessible documentation and interface."

        # Limitations
        limitations = []
        if cand.get("open_issues", 0) > 500:
            limitations.append(f"High open issue count ({cand.get('open_issues')} issues).")
        if not cand.get("has_readme"):
            limitations.append("Lacks structured README documentation on GitHub.")
        if unknown_feats:
            limitations.append(f"Features pending manual confirmation: {', '.join(unknown_feats)}.")

        evidence_urls = [
            ev["evidence_url"] for ev in verified_feats if ev.get("evidence_url")
        ]
        if lic_eval.get("evidence_url"):
            evidence_urls.append(lic_eval["evidence_url"])

        recommendations.append({
            "repo_name": full_name,
            "repo_url": html_url,
            "score": score,
            "score_breakdown": breakdown,
            "why_it_fits": why_fits,
            "verified_features": verified_feats,
            "limitations": limitations,
            "license_status": lic_eval.get("license_name") or "Unknown",
            "maintenance_indicators": maint_text,
            "requirement_coverage": coverage_text,
            "unknowns": unknown_feats,
            "confidence": confidence,
            "evidence_urls": list(set(evidence_urls)),
        })

    # Summary and trade-off synthesis
    if len(recommendations) >= 2:
        top1 = recommendations[0]
        top2 = recommendations[1]
        summary = (
            f"Selected {len(recommendations)} top candidate(s). "
            f"**{top1['repo_name']}** leads in verified coverage and community adoption ({top1['score']}/100). "
            f"**{top2['repo_name']}** provides a strong alternative ({top2['score']}/100)."
        )
    elif recommendations:
        top1 = recommendations[0]
        summary = (
            f"Identified **{top1['repo_name']}** as the best verified match "
            f"scoring {top1['score']}/100 with {top1['requirement_coverage']}."
        )
    else:
        summary = "No candidates met all evaluation criteria."

    return recommendations, summary, warnings


# ---------------------------------------------------------------------------
# Startup Event Handler
# ---------------------------------------------------------------------------
@agent.on_event("startup")
async def on_startup(ctx: Context):
    ctx.logger.info("==================================================================")
    ctx.logger.info("📊 RepoRadar Ranking & Decision Support Agent is ACTIVE!")
    ctx.logger.info(f"📍 Agent Address: {ctx.agent.address}")
    ctx.logger.info("⚖️  Weights: Relevance 40% | Features 25% | Maintenance 15% | Popularity 10% | Docs 10%")
    ctx.logger.info("==================================================================")


# ---------------------------------------------------------------------------
# Message Handler
# ---------------------------------------------------------------------------
ranking_proto = Protocol(name="RepoRadarRankingProtocol", version="1.0.0")


@ranking_proto.on_message(model=RankingRequest, replies={RankingResponse})
async def handle_ranking_request(ctx: Context, sender: str, msg: RankingRequest):
    """Processes RankingRequest by applying hard filters and scoring."""
    ctx.logger.info(f"📥 Processing RankingRequest for task {msg.task_id} from {sender[:12]}...")

    cands = msg.candidates or []
    reqs = msg.requirements or {}

    if not cands:
        await ctx.send(
            sender,
            RankingResponse(
                task_id=msg.task_id,
                recommendations=[],
                summary="No candidates provided for ranking.",
                warnings=[],
                error="",
            ),
        )
        return

    try:
        recs, summary, warnings = build_shortlist(cands, reqs)
        err_msg = ""
    except Exception as e:
        ctx.logger.error(f"Error during ranking: {e}")
        recs, summary, warnings = [], "Ranking calculation error.", [str(e)]
        err_msg = f"Ranking calculation error: {str(e)}"

    ctx.logger.info(
        f"✅ Task {msg.task_id}: Ranking complete. Selected {len(recs)} recommendation(s)."
    )

    await ctx.send(
        sender,
        RankingResponse(
            task_id=msg.task_id,
            recommendations=recs,
            summary=summary,
            warnings=warnings,
            error=err_msg,
        ),
    )


# ---------------------------------------------------------------------------
# Attach Protocol
# ---------------------------------------------------------------------------
agent.include(ranking_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent.run()
