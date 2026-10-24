"""
RepoRadar: Agent 3 - GitHub Discovery and Verification Agent
Discovers GitHub repositories in real time and performs evidence-based feature verification.

Part of the RepoRadar Multi-Agent System for Fetch-A-Thon 3.0 (ASI:One Agent Challenge).
Independently deployable and copy-pasteable into Agentverse.
"""

import os
import sys
import json
import time
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

import urllib.request
import urllib.parse
import urllib.error

# Try requests, fallback to urllib
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
    format="%(asctime)s [%(levelname)s] [DiscoveryAgent] %(message)s",
)
logger = logging.getLogger("reporadar_discovery_agent")


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
# Configuration Helpers
# ---------------------------------------------------------------------------
def get_config_int(var_name: str, default: int) -> int:
    try:
        return int(os.getenv(var_name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Agent Initialization
# ---------------------------------------------------------------------------
agent_seed = os.getenv("AGENT_SEED")
if agent_seed:
    agent = Agent(name="reporadar_discovery_agent", seed=agent_seed)
else:
    agent = Agent(name="reporadar_discovery_agent")

logger.info("Initializing RepoRadar GitHub Discovery and Verification Agent...")


# ---------------------------------------------------------------------------
# GitHub REST API Client with Bounded Retries & Rate-Limit Handling
# ---------------------------------------------------------------------------
KNOWN_OSI_LICENSES = {
    "mit", "apache-2.0", "gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0",
    "bsd-2-clause", "bsd-3-clause", "mpl-2.0", "isc", "unlicense", "epl-2.0",
}


def make_github_request(url: str, raw_content: bool = False) -> Tuple[Optional[Any], Optional[str], Optional[dict]]:
    """
    Executes a GitHub REST API request with timeouts, rate limit detection, and retries.
    Returns: (parsed_data_or_text, error_message, headers_dict)
    """
    token = os.getenv("GITHUB_TOKEN", "").strip()
    timeout_sec = get_config_int("REQUEST_TIMEOUT_SECONDS", 6)
    max_retries = get_config_int("MAX_RETRIES", 1)

    headers = {
        "User-Agent": "RepoRadar-Discovery-Agent-FetchAThon",
        "Accept": "application/vnd.github.raw+json" if raw_content else "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempt = 0
    backoff = 1.0

    while attempt <= max_retries:
        attempt += 1
        try:
            if HAS_REQUESTS:
                resp = requests.get(url, headers=headers, timeout=timeout_sec)
                resp_headers = dict(resp.headers)
                status_code = resp.status_code

                # Rate limiting detection
                remaining = resp_headers.get("x-ratelimit-remaining")
                if remaining == "0" or status_code in (403, 429):
                    reset_time = resp_headers.get("x-ratelimit-reset", "unknown")
                    err = f"GitHub API rate limit exceeded (reset at {reset_time})."
                    logger.warning(err)
                    return None, err, resp_headers

                if status_code == 200:
                    if raw_content:
                        return resp.text, None, resp_headers
                    return resp.json(), None, resp_headers
                elif status_code == 404:
                    return None, "Not Found (404)", resp_headers
                elif status_code >= 500:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                else:
                    return None, f"GitHub HTTP {status_code}", resp_headers

            else:
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                        resp_headers = dict(resp.headers)
                        content = resp.read().decode("utf-8", errors="replace")
                        if raw_content:
                            return content, None, resp_headers
                        return json.loads(content), None, resp_headers
                except urllib.error.HTTPError as he:
                    resp_headers = dict(he.headers)
                    if he.code in (403, 429):
                        return None, "GitHub API rate limit exceeded.", resp_headers
                    if he.code == 404:
                        return None, "Not Found (404)", resp_headers
                    if he.code >= 500:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    return None, f"GitHub HTTP {he.code}", resp_headers

        except Exception as e:
            if attempt > max_retries:
                return None, f"Request failure: {str(e)}", None
            time.sleep(backoff)
            backoff *= 2.0

    return None, "Max retries reached without response.", None


# ---------------------------------------------------------------------------
# Feature Verification Engine
# ---------------------------------------------------------------------------
def verify_platform_support(candidate: dict, target_platform: str, corpus: str) -> dict:
    """Verifies support for a target platform (e.g. Linux)."""
    p_lower = target_platform.lower()
    html_url = candidate["html_url"]

    if p_lower == "linux":
        linux_keywords = [
            "linux", "ubuntu", "debian", "fedora", "arch linux", "flatpak",
            "appimage", "snapcraft", "apt install", "apt-get", "pacman -s",
            "dnf install", "rpm", "tar.gz", "posix",
        ]
        has_positive = any(re.search(rf"\b{re.escape(k)}\b", corpus) for k in linux_keywords)
        has_negative = bool(re.search(r"\b(windows\s*only|only\s*supported\s*on\s*windows|macos\s*only)\b", corpus))

        if has_negative and not has_positive:
            return {
                "status": "not_supported",
                "explanation": "Documentation indicates project is restricted to non-Linux platforms.",
                "evidence_url": f"{html_url}#readme",
            }
        if has_positive:
            return {
                "status": "verified",
                "explanation": "Repository documentation or packaging instructions verify Linux compatibility.",
                "evidence_url": f"{html_url}#readme",
            }
        return {
            "status": "unknown",
            "explanation": "No explicit Linux compatibility or packaging instructions found in documentation.",
            "evidence_url": html_url,
        }

    # For other platforms (Windows, macOS, etc.)
    has_pos = bool(re.search(rf"\b{re.escape(p_lower)}\b", corpus))
    if has_pos:
        return {
            "status": "verified",
            "explanation": f"Documentation mentions support for {target_platform}.",
            "evidence_url": f"{html_url}#readme",
        }
    return {
        "status": "unknown",
        "explanation": f"Documentation does not explicitly verify support for {target_platform}.",
        "evidence_url": html_url,
    }


def verify_license(candidate: dict, corpus: str) -> dict:
    """Verifies open source licensing status using metadata and README evidence."""
    html_url = candidate["html_url"]
    lic = candidate.get("license") or {}
    spdx = (lic.get("spdx_id") or "").lower()

    if spdx and spdx != "noassertion":
        if spdx in KNOWN_OSI_LICENSES or any(k in spdx for k in ["gpl", "mit", "apache", "bsd", "mpl", "lgpl", "agpl"]):
            return {
                "status": "verified",
                "license_name": lic.get("name") or spdx.upper(),
                "spdx_id": spdx.upper(),
                "explanation": f"GitHub metadata identifies verified open-source license: {lic.get('name', spdx)}.",
                "evidence_url": lic.get("url") or f"{html_url}/blob/{candidate.get('default_branch', 'main')}/LICENSE",
            }

    # Inspect README for license mentions
    license_match = re.search(r"(?:##\s*License|###\s*License)[\s\S]{0,300}?(mit|apache|gpl|bsd|mpl|agpl|lgpl)", corpus)
    if license_match:
        lic_found = license_match.group(1).upper()
        return {
            "status": "verified",
            "license_name": f"{lic_found} (documented in README)",
            "spdx_id": lic_found,
            "explanation": f"README documentation includes license section referencing {lic_found}.",
            "evidence_url": f"{html_url}#readme",
        }

    return {
        "status": "unknown",
        "license_name": "Unknown",
        "spdx_id": None,
        "explanation": "No recognized open-source license detected in GitHub metadata or repository overview.",
        "evidence_url": html_url,
    }


def verify_feature(feature_name: str, corpus: str, html_url: str) -> dict:
    """Verifies whether a requested feature is supported based on documentation evidence."""
    feat_lower = feature_name.lower().strip()

    # Domain synonyms dictionary for common engineering capabilities
    synonyms = {
        "schematic design": ["schematic", "schematics", "eeschema", "circuit diagram", "schematic capture"],
        "circuit simulation": ["simulation", "simulate", "simulator", "spice", "ngspice", "transient analysis"],
        "pcb layout": ["pcb", "pcb layout", "pcbnew", "board design", "board layout", "gerber"],
        "gerber viewer": ["gerber", "gerbv", "drill files", "cam"],
        "headless browser support": ["headless", "playwright", "puppeteer", "selenium", "webdriver"],
        "multitrack editing": ["multitrack", "multi-track", "timeline", "tracks"],
        "video effects": ["effects", "filters", "color grading", "transitions"],
    }

    terms_to_check = synonyms.get(feat_lower, [feat_lower])
    if feat_lower not in terms_to_check:
        terms_to_check.append(feat_lower)

    # Check for negative statements (e.g. "simulation is not supported")
    for term in terms_to_check:
        neg_patterns = [
            rf"no\s+support\s+for\s+{re.escape(term)}",
            rf"{re.escape(term)}\s+is\s+not\s+supported",
            rf"does\s+not\s+support\s+{re.escape(term)}",
            rf"unsupported\s*:\s*{re.escape(term)}",
            rf"lacks\s+{re.escape(term)}",
        ]
        for np in neg_patterns:
            if re.search(np, corpus):
                return {
                    "status": "not_supported",
                    "explanation": f"Documentation explicitly indicates lack of support for '{feature_name}'.",
                    "evidence_url": f"{html_url}#readme",
                }

    # Check for positive evidence
    for term in terms_to_check:
        match = re.search(rf"([^.\n]{{0,60}}\b{re.escape(term)}\b[^.\n]{{0,60}})", corpus)
        if match:
            snippet = match.group(1).strip()
            # Clean up markdown chars
            snippet = re.sub(r"[#*`]", "", snippet).strip()
            return {
                "status": "verified",
                "explanation": f"Verified in project documentation: \"...{snippet}...\"",
                "evidence_url": f"{html_url}#readme",
            }

    return {
        "status": "unknown",
        "explanation": f"Feature '{feature_name}' is not explicitly documented in repository overview.",
        "evidence_url": html_url,
    }


# ---------------------------------------------------------------------------
# Discovery Workflow
# ---------------------------------------------------------------------------
def execute_discovery(requirements: dict) -> Tuple[List[dict], List[str], str]:
    """
    Executes live GitHub search queries and gathers verified candidate evidence.
    Returns: (candidates, warnings, error)
    """
    search_queries = requirements.get("search_queries") or []
    if not search_queries:
        norm_task = requirements.get("normalized_task") or "developer tools"
        search_queries = [norm_task]

    max_queries = get_config_int("GITHUB_MAX_QUERIES", 3)
    results_per_query = get_config_int("GITHUB_RESULTS_PER_QUERY", 5)

    active_queries = search_queries[:max_queries]
    collected_repos: Dict[str, dict] = {}
    warnings: List[str] = []
    STOP_WORDS = {"open", "source", "free", "the", "and", "for", "with", "that", "this", "suite", "software", "tool", "tools", "need"}

    # 1. Run GitHub Repository Searches with Concise, High-Recall Queries
    for query in active_queries:
        # Prune stop words to keep queries concise (2-3 words max) so GitHub AND search doesn't return 0
        clean_words = [w for w in query.split() if w.lower() not in STOP_WORDS]
        clean_q = " ".join(clean_words[:3]) if clean_words else query

        encoded_q = urllib.parse.quote_plus(clean_q)
        url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=stars&order=desc&per_page={results_per_query}"

        data, err, headers = make_github_request(url, raw_content=False)
        if err:
            warnings.append(f"Query '{clean_q}' warning: {err}")
            if "rate limit" in err.lower():
                break
            continue

        if not data or not isinstance(data.get("items"), list):
            continue

        for item in data["items"]:
            repo_id = item.get("full_name", "").lower()
            if repo_id and repo_id not in collected_repos:
                collected_repos[repo_id] = item

    # 1b. Resilient Fallback: If no repositories found, run broad feature/domain search
    if not collected_repos:
        req_feats = requirements.get("required_features") or []
        category = (requirements.get("category") or "").replace("-", " ")
        fallback_source = req_feats[0] if req_feats else category
        fb_words = [w for w in fallback_source.split() if w.lower() not in STOP_WORDS][:2]
        fallback_query = " ".join(fb_words) if fb_words else "eda"

        encoded_fb = urllib.parse.quote_plus(fallback_query)
        fb_url = f"https://api.github.com/search/repositories?q={encoded_fb}&sort=stars&order=desc&per_page={results_per_query}"
        fb_data, fb_err, _ = make_github_request(fb_url, raw_content=False)
        if fb_data and isinstance(fb_data.get("items"), list):
            for item in fb_data["items"]:
                repo_id = item.get("full_name", "").lower()
                if repo_id and repo_id not in collected_repos:
                    collected_repos[repo_id] = item

    if not collected_repos and warnings:
        return [], warnings, "Unable to discover repositories from GitHub due to API limits or errors."

    # 2. Inspect top deduplicated candidates (cap at 4 to conserve API budget and keep search fast)
    sorted_items = sorted(
        collected_repos.values(),
        key=lambda x: x.get("stargazers_count", 0),
        reverse=True,
    )[:4]

    verified_candidates = []
    required_features = requirements.get("required_features") or []
    target_platforms = requirements.get("target_platforms") or []

    for item in sorted_items:
        owner_repo = item.get("full_name")
        html_url = item.get("html_url")
        desc = item.get("description") or ""
        topics = " ".join(item.get("topics") or [])

        # Fetch README content
        readme_url = f"https://api.github.com/repos/{owner_repo}/readme"
        readme_text, readme_err, _ = make_github_request(readme_url, raw_content=True)

        if not readme_text:
            readme_text = ""
            if readme_err and "rate limit" not in readme_err.lower():
                pass  # 404 is normal for repos without README

        # Build corpus for inspection (untrusted text data)
        corpus = f"{desc}\n{topics}\n{readme_text[:15000]}".lower()

        # Evidence evaluation
        feature_evidence = {}
        for feat in required_features:
            feature_evidence[feat] = verify_feature(feat, corpus, html_url)

        platform_evidence = {}
        for plat in target_platforms:
            platform_evidence[plat] = verify_platform_support(item, plat, corpus)

        license_eval = verify_license(item, corpus)

        # Usability indicators
        beginner_signals = bool(re.search(r"\b(easy to use|gui|tutorial|beginner|getting started|quickstart)\b", corpus))

        candidate_record = {
            "name": item.get("name"),
            "full_name": owner_repo,
            "html_url": html_url,
            "description": desc,
            "language": item.get("language") or "Unknown",
            "stars": item.get("stargazers_count", 0),
            "forks": item.get("forks_count", 0),
            "open_issues": item.get("open_issues_count", 0),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "pushed_at": item.get("pushed_at"),
            "archived": item.get("archived", False),
            "default_branch": item.get("default_branch", "main"),
            "license": license_eval,
            "feature_evidence": feature_evidence,
            "platform_evidence": platform_evidence,
            "has_readme": bool(readme_text),
            "beginner_signals": beginner_signals,
        }
        verified_candidates.append(candidate_record)

    logger.info(f"Discovery complete. Collected {len(verified_candidates)} verified candidates.")
    return verified_candidates, warnings, ""


# ---------------------------------------------------------------------------
# Startup Event Handler
# ---------------------------------------------------------------------------
@agent.on_event("startup")
async def on_startup(ctx: Context):
    token_set = bool(os.getenv("GITHUB_TOKEN", "").strip())
    ctx.logger.info("==================================================================")
    ctx.logger.info("🔍 RepoRadar GitHub Discovery & Verification Agent is ACTIVE!")
    ctx.logger.info(f"📍 Agent Address: {ctx.agent.address}")
    ctx.logger.info(f"🔑 GITHUB_TOKEN:  {'Configured (5,000 req/hr rate limit)' if token_set else 'Not set (Public unauthenticated 60 req/hr)'}")
    ctx.logger.info("==================================================================")


# ---------------------------------------------------------------------------
# Message Handler
# ---------------------------------------------------------------------------
discovery_proto = Protocol(name="RepoRadarDiscoveryProtocol", version="1.0.0")


@discovery_proto.on_message(model=DiscoveryRequest, replies={DiscoveryResponse})
async def handle_discovery_request(ctx: Context, sender: str, msg: DiscoveryRequest):
    """Processes DiscoveryRequest by searching GitHub and assembling evidence."""
    ctx.logger.info(f"📥 Processing DiscoveryRequest for task {msg.task_id} from {sender[:12]}...")

    reqs = msg.requirements or {}
    try:
        candidates, warnings, err = execute_discovery(reqs)
    except Exception as e:
        ctx.logger.error(f"Error during discovery execution: {e}")
        candidates, warnings, err = [], [], f"Discovery execution error: {str(e)}"

    ctx.logger.info(f"✅ Task {msg.task_id}: Discovery completed. Found {len(candidates)} verified candidate(s).")

    await ctx.send(
        sender,
        DiscoveryResponse(
            task_id=msg.task_id,
            candidates=candidates,
            warnings=warnings,
            error=err,
        ),
    )


# ---------------------------------------------------------------------------
# Attach Protocol
# ---------------------------------------------------------------------------
agent.include(discovery_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent.run()
