#!/usr/bin/env python3
"""
GitHub-backed memory sync — Cross-device persistent memory via GitHub.

Syncs local memories (~/.master_ai_memory) to a GitHub private gist.
Login with GitHub username + token, memories follow you across devices.

Usage:
    from github_memory_sync import GitHubMemorySync
    
    sync = GitHubMemorySync()
    sync.login("ebey317", "ghp_...")
    sync.pull_memories()  # Fetch from GitHub
    sync.push_memories()  # Upload to GitHub
    sync.sync()           # Two-way merge

Environment:
    GITHUB_MEMORY_GIST_ID  — gist ID to sync to (optional; creates new gist if not set)
    GITHUB_TOKEN           — stored in ~/.master_ai_keys["github_token"]
"""

import json
import os
import time
import base64
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import threading

try:
    import httpx
except ImportError:
    httpx = None


class GitHubMemorySync:
    """Sync memories to GitHub private gist."""
    
    def __init__(self, username: Optional[str] = None):
        self.username = username
        self.github_token = None
        self.gist_id = None
        self.local_memory_file = Path.home() / ".master_ai_memory"
        self.account_file = Path.home() / ".master_ai_keys"
        self.last_sync = None
        self.lock = threading.Lock()
        
        self._load_stored_credentials()
    
    def _load_stored_credentials(self) -> bool:
        """Load username, token, gist_id from ~/.master_ai_keys."""
        if not self.account_file.exists():
            return False
        try:
            data = json.loads(self.account_file.read_text())
            self.username = data.get("github_username", self.username)
            self.github_token = data.get("github_token")
            self.gist_id = data.get("github_gist_id")
            return bool(self.github_token)
        except Exception:
            return False
    
    def _save_credentials(self) -> None:
        """Save username, token, gist_id to ~/.master_ai_keys."""
        try:
            data = {}
            if self.account_file.exists():
                data = json.loads(self.account_file.read_text())
            data.update({
                "github_username": self.username,
                "github_token": self.github_token,
                "github_gist_id": self.gist_id,
                "github_sync_saved_at": datetime.now().isoformat(),
            })
            self.account_file.write_text(json.dumps(data, indent=2))
            os.chmod(str(self.account_file), 0o600)
        except Exception as e:
            print(f"[github_sync] failed to save credentials: {e}")
    
    def login(self, username: str, github_token: str) -> bool:
        """
        Authenticate with GitHub and create/find memory gist.
        
        Args:
            username: GitHub username
            github_token: GitHub personal access token (needs gist scope)
        
        Returns: True on success
        """
        if not httpx:
            print("[github_sync] httpx not available")
            return False
        
        self.username = username
        self.github_token = github_token
        
        try:
            # Test token validity
            resp = httpx.get(
                "https://api.github.com/user",
                headers=self._auth_headers(),
                timeout=10.0,
            )
            if resp.status_code != 200:
                print(f"[github_sync] authentication failed: {resp.status_code}")
                return False
            
            user_data = resp.json()
            actual_username = user_data.get("login", "")
            if actual_username.lower() != username.lower():
                print(f"[github_sync] username mismatch: {actual_username} != {username}")
                return False
            
            # Create or find memory gist
            if not self._ensure_gist():
                return False
            
            self._save_credentials()
            print(f"[github_sync] logged in as {username}, gist: {self.gist_id[:8]}...")
            return True
        
        except Exception as e:
            print(f"[github_sync] login error: {e}")
            return False
    
    def _auth_headers(self) -> Dict[str, str]:
        """Build GitHub API auth headers."""
        return {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
    
    def _ensure_gist(self) -> bool:
        """Create or validate memory gist."""
        if self.gist_id:
            # Try to access existing gist
            try:
                resp = httpx.get(
                    f"https://api.github.com/gists/{self.gist_id}",
                    headers=self._auth_headers(),
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            # Gist not found or inaccessible
            self.gist_id = None
        
        # Create new gist
        try:
            payload = {
                "description": "Master AI Memory Sync",
                "public": False,
                "files": {
                    "master_ai_memory.md": {
                        "content": "# Master AI Memory\n\nPersistent memory synced across devices.\n",
                    }
                },
            }
            resp = httpx.post(
                "https://api.github.com/gists",
                json=payload,
                headers=self._auth_headers(),
                timeout=10.0,
            )
            if resp.status_code == 201:
                self.gist_id = resp.json().get("id")
                print(f"[github_sync] created new gist: {self.gist_id}")
                return True
        except Exception as e:
            print(f"[github_sync] failed to create gist: {e}")
        
        return False
    
    def pull_memories(self) -> bool:
        """Fetch memories from GitHub gist."""
        if not self.github_token or not self.gist_id:
            return False
        
        if not httpx:
            print("[github_sync] httpx not available")
            return False
        
        try:
            resp = httpx.get(
                f"https://api.github.com/gists/{self.gist_id}",
                headers=self._auth_headers(),
                timeout=10.0,
            )
            if resp.status_code != 200:
                print(f"[github_sync] pull failed: {resp.status_code}")
                return False
            
            gist_data = resp.json()
            files = gist_data.get("files", {})
            
            # Find memory file (usually master_ai_memory.md or first .md file)
            memory_file = files.get("master_ai_memory.md") or next(
                (f for f in files.values() if f.get("filename", "").endswith(".md")),
                None
            )
            
            if not memory_file:
                print("[github_sync] no memory file found in gist")
                return False
            
            remote_memory = memory_file.get("content", "")
            remote_timestamp = gist_data.get("updated_at", "")
            
            # Merge: keep local if newer, prefer remote if older
            local_memory = self.local_memory_file.read_text() if self.local_memory_file.exists() else ""
            local_timestamp = self._get_local_timestamp()
            
            merged = self._merge_memories(local_memory, remote_memory, local_timestamp, remote_timestamp)
            self.local_memory_file.write_text(merged)
            self.last_sync = time.time()
            
            print(f"[github_sync] pulled {len(remote_memory)} chars from gist")
            return True
        
        except Exception as e:
            print(f"[github_sync] pull error: {e}")
            return False
    
    def push_memories(self) -> bool:
        """Upload memories to GitHub gist."""
        if not self.github_token or not self.gist_id:
            return False
        
        if not httpx:
            print("[github_sync] httpx not available")
            return False
        
        if not self.local_memory_file.exists():
            return True  # Nothing to push
        
        try:
            memory_content = self.local_memory_file.read_text()
            payload = {
                "files": {
                    "master_ai_memory.md": {
                        "content": memory_content,
                    }
                },
            }
            resp = httpx.patch(
                f"https://api.github.com/gists/{self.gist_id}",
                json=payload,
                headers=self._auth_headers(),
                timeout=10.0,
            )
            if resp.status_code != 200:
                print(f"[github_sync] push failed: {resp.status_code}")
                return False
            
            self.last_sync = time.time()
            print(f"[github_sync] pushed {len(memory_content)} chars to gist")
            return True
        
        except Exception as e:
            print(f"[github_sync] push error: {e}")
            return False
    
    def sync(self) -> bool:
        """Two-way sync: pull remote, then push local."""
        with self.lock:
            pulled = self.pull_memories()
            pushed = self.push_memories()
            return pulled and pushed
    
    def sync_if_needed(self, interval_s: int = 300) -> bool:
        """Sync only if enough time has passed (default: 5 min)."""
        if self.last_sync and time.time() - self.last_sync < interval_s:
            return True
        return self.sync()
    
    def _get_local_timestamp(self) -> str:
        """Get ISO timestamp of local memory file."""
        if self.local_memory_file.exists():
            mtime = self.local_memory_file.stat().st_mtime
            return datetime.fromtimestamp(mtime).isoformat()
        return ""
    
    def _merge_memories(self, local: str, remote: str, local_ts: str, remote_ts: str) -> str:
        """
        Simple merge: if local is newer, keep local. Otherwise merge unique lines.
        """
        if not remote:
            return local
        if not local:
            return remote
        
        try:
            local_time = datetime.fromisoformat(local_ts) if local_ts else None
            remote_time = datetime.fromisoformat(remote_ts) if remote_ts else None
            
            if local_time and remote_time and (local_time - remote_time).total_seconds() > 60:
                # Local significantly newer
                return local
            if remote_time and local_time and (remote_time - local_time).total_seconds() > 60:
                # Remote significantly newer
                return remote
        except Exception:
            pass
        
        # Merge: append unique remote lines to local
        local_lines = set(local.splitlines())
        remote_lines = remote.splitlines()
        new_lines = [line for line in remote_lines if line not in local_lines]
        
        if new_lines:
            return local.rstrip() + "\n" + "\n".join(new_lines) + "\n"
        return local


if __name__ == "__main__":
    # Demo
    sync = GitHubMemorySync()
    
    print("GitHub Memory Sync — Demo")
    print("=" * 72)
    
    if not sync.login("ebey317", os.environ.get("GITHUB_TOKEN", "")):
        print("Login failed — provide GITHUB_TOKEN env var")
    else:
        sync.sync()
