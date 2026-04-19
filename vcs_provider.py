"""VCS provider abstraction — supports GitLab (glab) and GitHub (gh)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from typing import Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class MRInfo:
    """Normalised merge/pull request data."""

    def __init__(
        self,
        url: str,
        number: str,
        title: str,
        state: str,          # "open" | "merged" | "closed"
        approved: bool = False,
    ):
        self.url = url
        self.number = number
        self.title = title
        self.state = state
        self.approved = approved


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class VCSProvider(ABC):
    """Abstract interface for VCS operations used by claude_session_manager."""

    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    # -- factory helper ------------------------------------------------------

    @staticmethod
    def detect(repo_path: str) -> "VCSProvider":
        """Return the right provider by inspecting the git remote URL."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            remote_url = result.stdout.strip()
        except Exception:
            remote_url = ""

        if "github.com" in remote_url:
            return GitHubProvider(repo_path)
        # Default to GitLab (covers self-hosted GitLab instances too)
        return GitLabProvider(repo_path)

    # -- abstract operations -------------------------------------------------

    @abstractmethod
    def list_mr_by_branch(self, branch_name: str) -> list[MRInfo]:
        """Return open MRs whose source branch matches *branch_name*."""

    @abstractmethod
    def get_mr(self, mr_id: str) -> Optional[MRInfo]:
        """Fetch a single MR by its number/iid string."""

    @abstractmethod
    def get_mr_approved(self, mr_id: str) -> bool:
        """Return True if the MR has at least one approval."""

    @abstractmethod
    def add_mr_comment(self, mr_id: str, body: str) -> bool:
        """Post a comment on the MR. Return True on success."""

    @abstractmethod
    def close_mr(self, mr_id: str) -> bool:
        """Close/decline the MR without merging. Return True on success."""

    # -- common helper -------------------------------------------------------

    def extract_mr_id(self, mr_url: str) -> Optional[str]:
        """Parse the MR/PR number out of a URL."""
        # GitLab: .../merge_requests/123
        m = re.search(r'/merge_requests/(\d+)', mr_url)
        if m:
            return m.group(1)
        # GitHub: .../pull/123
        m = re.search(r'/pull/(\d+)', mr_url)
        if m:
            return m.group(1)
        return None


# ---------------------------------------------------------------------------
# GitLab provider (uses glab CLI)
# ---------------------------------------------------------------------------

class GitLabProvider(VCSProvider):
    """Calls glab for all VCS operations."""

    def __init__(self, repo_path: str):
        super().__init__(repo_path)
        # Allow override via env; fall back to .gitlab/ next to this file
        _default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitlab")
        self._config_dir = os.environ.get("GLAB_CONFIG_DIR", _default_config)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["GLAB_CONFIG_DIR"] = self._config_dir
        return env

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            env=self._env(),
        )

    def list_mr_by_branch(self, branch_name: str) -> list[MRInfo]:
        result = self._run(
            ["glab", "mr", "list", "--source-branch", branch_name, "--output", "json"]
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            raw = json.loads(result.stdout)
            if not isinstance(raw, list):
                return []
            out = []
            for mr in raw:
                url = mr.get("web_url", "")
                number = str(mr.get("iid", ""))
                if url and number:
                    out.append(MRInfo(
                        url=url,
                        number=number,
                        title=mr.get("title", ""),
                        state=mr.get("state", "opened").lower(),
                    ))
            return out
        except (json.JSONDecodeError, KeyError):
            return []

    def get_mr(self, mr_id: str) -> Optional[MRInfo]:
        result = self._run(["glab", "mr", "view", mr_id, "--output", "json"])
        if result.returncode != 0:
            return None
        try:
            mr = json.loads(result.stdout)
            state = mr.get("state", "opened").lower()
            # Normalise GitLab states
            if state == "opened":
                state = "open"
            return MRInfo(
                url=mr.get("web_url", ""),
                number=str(mr.get("iid", mr_id)),
                title=mr.get("title", ""),
                state=state,
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def get_mr_approved(self, mr_id: str) -> bool:
        result = self._run(
            ["glab", "api", f"projects/:id/merge_requests/{mr_id}/approvals"]
        )
        if result.returncode != 0:
            return False
        try:
            info = json.loads(result.stdout)
            return bool(info.get("approved", False))
        except (json.JSONDecodeError, KeyError):
            return False

    def add_mr_comment(self, mr_id: str, body: str) -> bool:
        result = self._run(["glab", "mr", "note", mr_id, "-m", body])
        return result.returncode == 0

    def close_mr(self, mr_id: str) -> bool:
        result = self._run(["glab", "mr", "close", mr_id])
        return result.returncode == 0


# ---------------------------------------------------------------------------
# GitHub provider (uses gh CLI)
# ---------------------------------------------------------------------------

class GitHubProvider(VCSProvider):
    """Calls gh (GitHub CLI) for all VCS operations."""

    def __init__(self, repo_path: str):
        super().__init__(repo_path)
        _default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github")
        self._config_dir = os.environ.get("GH_CONFIG_DIR", _default_config)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["GH_CONFIG_DIR"] = self._config_dir
        return env

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            env=self._env(),
        )

    def _normalize_state(self, raw: str) -> str:
        """Map GitHub PR states to our canonical set."""
        raw = raw.upper()
        if raw == "MERGED":
            return "merged"
        if raw in ("CLOSED",):
            return "closed"
        return "open"

    def list_mr_by_branch(self, branch_name: str) -> list[MRInfo]:
        result = self._run([
            "gh", "pr", "list",
            "--head", branch_name,
            "--json", "number,url,title,state",
        ])
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            raw = json.loads(result.stdout)
            out = []
            for pr in raw:
                url = pr.get("url", "")
                number = str(pr.get("number", ""))
                if url and number:
                    out.append(MRInfo(
                        url=url,
                        number=number,
                        title=pr.get("title", ""),
                        state=self._normalize_state(pr.get("state", "OPEN")),
                    ))
            return out
        except (json.JSONDecodeError, KeyError):
            return []

    def get_mr(self, mr_id: str) -> Optional[MRInfo]:
        result = self._run([
            "gh", "pr", "view", mr_id,
            "--json", "number,url,title,state,mergedAt",
        ])
        if result.returncode != 0:
            return None
        try:
            pr = json.loads(result.stdout)
            state = self._normalize_state(pr.get("state", "OPEN"))
            return MRInfo(
                url=pr.get("url", ""),
                number=str(pr.get("number", mr_id)),
                title=pr.get("title", ""),
                state=state,
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def get_mr_approved(self, mr_id: str) -> bool:
        result = self._run([
            "gh", "pr", "view", mr_id,
            "--json", "reviews",
        ])
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
            reviews = data.get("reviews", [])
            return any(r.get("state", "").upper() == "APPROVED" for r in reviews)
        except (json.JSONDecodeError, KeyError):
            return False

    def add_mr_comment(self, mr_id: str, body: str) -> bool:
        result = self._run(["gh", "pr", "comment", mr_id, "-b", body])
        return result.returncode == 0

    def close_mr(self, mr_id: str) -> bool:
        result = self._run(["gh", "pr", "close", mr_id])
        return result.returncode == 0
