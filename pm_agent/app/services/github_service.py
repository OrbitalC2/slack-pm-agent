"""
GitHub read-only integration service.

Reads commits, PRs, issues, and check status from configured repos.
No write operations in MVP — only read methods are exposed.
"""

import re
from dataclasses import dataclass

import httpx

from pm_agent.app.config import get_settings
from pm_agent.app.logging_config import get_logger

logger = get_logger("github")

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class RepoInfo:
    owner: str
    repo: str
    full_name: str
    default_branch: str = "main"
    description: str = ""


@dataclass
class CommitSummary:
    sha: str
    message: str
    author: str
    date: str
    url: str


@dataclass
class PRSummary:
    number: int
    title: str
    state: str
    author: str
    branch: str
    url: str
    merged: bool = False


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    """Parse owner/repo from GitHub URL."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if match:
        return match.group(1), match.group(2)
    return None


def _get_headers() -> dict:
    """Get GitHub API headers with auth if token is available."""
    settings = get_settings()
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


async def get_repo(github_url: str) -> RepoInfo | None:
    """Get repository info from GitHub."""
    parsed = _parse_repo_url(github_url)
    if not parsed:
        logger.warning(f"Could not parse GitHub URL: {github_url}")
        return None

    owner, repo = parsed
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
                headers=_get_headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"GitHub API error {resp.status_code} for {owner}/{repo}")
                return None

            data = resp.json()
            return RepoInfo(
                owner=owner, repo=repo,
                full_name=data.get("full_name", f"{owner}/{repo}"),
                default_branch=data.get("default_branch", "main"),
                description=data.get("description", ""),
            )
    except Exception as e:
        logger.error(f"GitHub API error: {e}")
        return None


async def get_recent_commits(
    github_url: str, since: str | None = None, limit: int = 20
) -> list[CommitSummary]:
    """Get recent commits from the default branch."""
    parsed = _parse_repo_url(github_url)
    if not parsed:
        return []

    owner, repo = parsed
    try:
        params = {"per_page": limit}
        if since:
            params["since"] = since

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits",
                headers=_get_headers(), params=params, timeout=10,
            )
            if resp.status_code != 200:
                return []

            return [
                CommitSummary(
                    sha=c["sha"][:7],
                    message=c["commit"]["message"].split("\n")[0][:100],
                    author=c["commit"]["author"].get("name", "unknown"),
                    date=c["commit"]["author"].get("date", ""),
                    url=c["html_url"],
                )
                for c in resp.json()
            ]
    except Exception as e:
        logger.error(f"Failed to fetch commits: {e}")
        return []


async def get_open_prs(github_url: str) -> list[PRSummary]:
    """Get open pull requests."""
    parsed = _parse_repo_url(github_url)
    if not parsed:
        return []

    owner, repo = parsed
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls",
                headers=_get_headers(),
                params={"state": "open", "per_page": 20},
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            return [
                PRSummary(
                    number=pr["number"], title=pr["title"], state="open",
                    author=pr["user"]["login"],
                    branch=pr["head"]["ref"], url=pr["html_url"],
                )
                for pr in resp.json()
            ]
    except Exception as e:
        logger.error(f"Failed to fetch PRs: {e}")
        return []


async def get_recent_merged_prs(
    github_url: str, since: str | None = None
) -> list[PRSummary]:
    """Get recently merged pull requests."""
    parsed = _parse_repo_url(github_url)
    if not parsed:
        return []

    owner, repo = parsed
    try:
        params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 20}
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls",
                headers=_get_headers(), params=params, timeout=10,
            )
            if resp.status_code != 200:
                return []

            merged = []
            for pr in resp.json():
                if pr.get("merged_at"):
                    merged.append(PRSummary(
                        number=pr["number"], title=pr["title"], state="merged",
                        author=pr["user"]["login"],
                        branch=pr["head"]["ref"], url=pr["html_url"], merged=True,
                    ))
            return merged
    except Exception as e:
        logger.error(f"Failed to fetch merged PRs: {e}")
        return []


async def get_github_summary(github_url: str) -> str:
    """Get a formatted summary of GitHub activity for project updates."""
    if not github_url:
        return ""

    parts = []

    commits = await get_recent_commits(github_url, limit=10)
    if commits:
        commit_lines = [f"  • `{c.sha}` {c.message} ({c.author})" for c in commits[:5]]
        parts.append("*Recent Commits:*\n" + "\n".join(commit_lines))

    open_prs = await get_open_prs(github_url)
    if open_prs:
        pr_lines = [f"  • #{p.number} {p.title} ({p.author})" for p in open_prs[:5]]
        parts.append("*Open PRs:*\n" + "\n".join(pr_lines))

    merged = await get_recent_merged_prs(github_url)
    if merged:
        merged_lines = [f"  • #{p.number} {p.title}" for p in merged[:5]]
        parts.append("*Recently Merged:*\n" + "\n".join(merged_lines))

    return "\n\n".join(parts) if parts else "No recent GitHub activity found."
