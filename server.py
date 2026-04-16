#!/usr/bin/env python3
"""Lightweight server for browsing Claude Code session history."""

import json
import os
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8080
CONFIG_FILE = Path(__file__).parent / "config.json"

# ── Config persistence ──

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def get_claude_dir():
    """Return configured claude dir, or auto-detect ~/.claude."""
    cfg = load_config()
    custom = cfg.get("claude_dir")
    if custom and Path(custom).is_dir():
        return Path(custom)
    default = Path.home() / ".claude"
    if default.is_dir():
        return default
    return None


def load_app_session_meta():
    """Scan Claude desktop app state to map cliSessionId → {isArchived, title}.

    Claude app stores per-session metadata (archive flag, user-edited title)
    under ~/Library/Application Support/Claude/{claude-code,local-agent-mode}-sessions/
    as nested local_*.json files. The cliSessionId field there matches the JSONL file stem.
    """
    meta = {}
    roots = [
        Path.home() / "Library/Application Support/Claude/claude-code-sessions",
        Path.home() / "Library/Application Support/Claude/local-agent-mode-sessions",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for f in root.rglob("local_*.json"):
            try:
                obj = json.loads(f.read_text())
                cli_id = obj.get("cliSessionId")
                if not cli_id:
                    continue
                meta[cli_id] = {
                    "isArchived": bool(obj.get("isArchived")),
                    "title": obj.get("title", "") or "",
                }
            except Exception:
                continue
    return meta


class SessionHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/status":
            self._json_response(self._get_status())
        elif path == "/api/projects":
            self._json_response(self._list_projects())
        elif path == "/api/sessions":
            project = qs.get("project", [""])[0]
            self._json_response(self._list_sessions(project))
        elif path == "/api/session":
            filepath = qs.get("path", [""])[0]
            self._json_response(self._read_session(filepath))
        elif path == "/api/search":
            project = qs.get("project", [""])[0]
            query = qs.get("q", [""])[0]
            self._json_response(self._search_sessions(project, query))
        elif path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html")
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/set-path":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._json_response(self._set_path(body.get("path", "")))
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        filepath = Path(__file__).parent / filename
        if not filepath.exists():
            self.send_error(404)
            return
        content = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _get_status(self):
        claude_dir = get_claude_dir()
        cfg = load_config()
        return {
            "detected": claude_dir is not None,
            "claude_dir": str(claude_dir) if claude_dir else None,
            "is_custom": bool(cfg.get("claude_dir")),
            "home": str(Path.home()),
        }

    def _set_path(self, path_str):
        p = Path(path_str).expanduser()
        projects = p / "projects"
        if not p.is_dir():
            return {"ok": False, "error": f"路径不存在: {path_str}"}
        if not projects.is_dir():
            return {"ok": False, "error": f"未找到 projects 目录: {projects}"}
        cfg = load_config()
        cfg["claude_dir"] = str(p)
        save_config(cfg)
        return {"ok": True, "claude_dir": str(p)}

    def _list_projects(self):
        claude_dir = get_claude_dir()
        if not claude_dir:
            return []
        projects_dir = claude_dir / "projects"
        if not projects_dir.exists():
            return []
        home = str(Path.home())
        projects = []
        for d in sorted(projects_dir.iterdir()):
            if d.is_dir():
                jsonl_count = len(list(d.glob("*.jsonl")))
                if jsonl_count > 0:
                    # Recover real path: dir name is path with / replaced by -
                    # e.g. "-Users-bytedance-Documents-format_toolbox"
                    raw = d.name
                    if raw.startswith("-"):
                        real_path = "/" + raw[1:].replace("-", "/")
                        # Try to find the actual existing path by checking longest match
                        # Heuristic: the dir name encodes the real path with - as /
                        # but folder names may contain -, so try to reconstruct
                        candidate = Path(real_path)
                        if not candidate.exists():
                            # Walk segments to find the real boundary
                            segs = raw[1:].split("-")
                            real_path = ""
                            for seg in segs:
                                test = real_path + "/" + seg
                                if Path(test).exists():
                                    real_path = test
                                elif real_path:
                                    # Try merging with hyphen (original name had -)
                                    test2 = real_path + "-" + seg
                                    if Path(test2).exists():
                                        real_path = test2
                                    else:
                                        real_path = real_path + "/" + seg
                                else:
                                    real_path = "/" + seg
                        else:
                            real_path = str(candidate)
                    else:
                        real_path = raw.replace("-", "/")

                    # Create display name: strip home prefix, show as ~/...
                    if real_path.startswith(home):
                        display = "~" + real_path[len(home):]
                    else:
                        display = real_path
                    projects.append({
                        "id": d.name,
                        "name": display,
                        "fullPath": real_path,
                        "sessions": jsonl_count,
                    })
        return projects

    def _list_sessions(self, project_id):
        claude_dir = get_claude_dir()
        if not claude_dir:
            return []
        project_dir = claude_dir / "projects" / project_id
        if not project_dir.exists():
            return []
        app_meta = load_app_session_meta()
        sessions = []
        for f in sorted(project_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = f.stat()
            custom_title, first_msg, last_msg, slug, entrypoint, session_type = self._extract_title(f)
            app_info = app_meta.get(f.stem, {})
            # Prefer Claude app's user-edited title if the JSONL didn't have one
            if not custom_title and app_info.get("title"):
                custom_title = app_info["title"]
            sessions.append({
                "id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "customTitle": custom_title,
                "firstMessage": first_msg,
                "lastMessage": last_msg,
                "slug": slug,
                "entrypoint": entrypoint,
                "sessionType": session_type,
                "isArchived": app_info.get("isArchived", False),
            })
        return sessions

    @staticmethod
    def _is_meta_content(text):
        """Check if message content is a meta/system message, not real user input."""
        if not text:
            return True
        stripped = text.strip()
        meta_prefixes = (
            "<local-command-caveat>", "<command-name>", "<local-command-stdout>",
            "<local-command-stderr>", "<local-command-error>",
            "<task-notification>", "<<autonomous-loop",
        )
        meta_exact = (
            "[Request interrupted by user]",
        )
        if any(stripped.startswith(tag) for tag in meta_prefixes):
            return True
        if stripped in meta_exact:
            return True
        # Image metadata from paste
        if stripped.startswith("[Image: original "):
            return True
        return False

    def _extract_title(self, filepath):
        """Extract custom title, first real user message, last user query, slug, entrypoint, and session type."""
        custom_title = ""
        first_user_msg = ""
        last_user_msg = ""
        entrypoint = ""
        slug = ""
        session_type = "manual"  # "manual" or "scheduled"
        try:
            with open(filepath, "r") as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "custom-title" and not custom_title:
                        custom_title = obj.get("customTitle", "") or obj.get("title", "")
                    if not entrypoint and obj.get("entrypoint"):
                        entrypoint = obj.get("entrypoint", "")
                    if not slug and obj.get("slug"):
                        slug = obj.get("slug", "")
                    if obj.get("type") == "user":
                        # Skip meta messages (isMeta, local-command-caveat, etc.)
                        if obj.get("isMeta"):
                            continue
                        content = obj.get("message", {}).get("content", "")
                        raw_text = ""
                        if isinstance(content, str):
                            raw_text = content
                        elif isinstance(content, list):
                            for c in content:
                                if c.get("type") == "text":
                                    raw_text = c.get("text", "")
                                    break
                        if not raw_text:
                            continue
                        stripped = raw_text.strip()
                        # Detect scheduled task
                        if stripped.startswith("<scheduled-task"):
                            session_type = "scheduled"
                            # Extract scheduled task name for preview
                            m = re.search(r'name="([^"]+)"', stripped[:500])
                            task_name = m.group(1) if m else "scheduled task"
                            display_text = f"⏱ {task_name}"
                            if not first_user_msg:
                                first_user_msg = display_text
                            last_user_msg = display_text
                            continue
                        if self._is_meta_content(raw_text):
                            continue
                        text = raw_text[:150]
                        if text:
                            if not first_user_msg:
                                first_user_msg = text[:100]
                            last_user_msg = text
        except Exception:
            pass
        return custom_title, first_user_msg, last_user_msg, slug, entrypoint, session_type

    def _read_session(self, filepath):
        claude_dir = get_claude_dir()
        if not claude_dir:
            return {"error": "未配置 Claude 目录"}
        fp = Path(filepath)
        if not fp.exists() or not str(fp).startswith(str(claude_dir)):
            return {"error": "Invalid path"}

        messages = []
        metadata = {}
        try:
            with open(fp, "r") as f:
                for line in f:
                    obj = json.loads(line)
                    msg_type = obj.get("type")

                    if msg_type in ("file-history-snapshot", "progress", "queue-operation", "last-prompt"):
                        continue

                    if msg_type == "custom-title":
                        metadata["title"] = obj.get("customTitle", "") or obj.get("title", "")
                        continue

                    if msg_type == "agent-name":
                        metadata["agent"] = obj.get("agentName", "")
                        continue

                    if msg_type == "system":
                        messages.append({
                            "type": "system",
                            "content": obj.get("message", {}).get("content", ""),
                            "uuid": obj.get("uuid", ""),
                            "timestamp": obj.get("timestamp", ""),
                        })
                        continue

                    if msg_type in ("user", "assistant"):
                        # Skip meta/command messages
                        if obj.get("isMeta"):
                            continue
                        role = obj.get("message", {}).get("role", msg_type)
                        raw_content = obj.get("message", {}).get("content", "")
                        # Skip local-command-caveat etc. for user messages
                        if msg_type == "user" and isinstance(raw_content, str) and self._is_meta_content(raw_content):
                            continue
                        parts = []

                        if isinstance(raw_content, str):
                            parts.append({"type": "text", "text": raw_content})
                        elif isinstance(raw_content, list):
                            for c in raw_content:
                                ct = c.get("type")
                                if ct == "text":
                                    parts.append({"type": "text", "text": c.get("text", "")})
                                elif ct == "thinking":
                                    parts.append({"type": "thinking", "text": c.get("thinking", "")})
                                elif ct == "tool_use":
                                    parts.append({
                                        "type": "tool_use",
                                        "name": c.get("name", ""),
                                        "input": c.get("input", {}),
                                        "id": c.get("id", ""),
                                    })
                                elif ct == "tool_result":
                                    tr_content = c.get("content", "")
                                    if isinstance(tr_content, list):
                                        texts = [x.get("text", "") for x in tr_content if x.get("type") == "text"]
                                        tr_content = "\n".join(texts)
                                    parts.append({
                                        "type": "tool_result",
                                        "tool_use_id": c.get("tool_use_id", ""),
                                        "content": tr_content[:5000] if isinstance(tr_content, str) else str(tr_content)[:5000],
                                    })

                        if parts:
                            messages.append({
                                "type": role,
                                "parts": parts,
                                "uuid": obj.get("uuid", ""),
                                "timestamp": obj.get("timestamp", ""),
                                "model": obj.get("message", {}).get("model", ""),
                            })
        except Exception as e:
            return {"error": str(e)}

        return {"metadata": metadata, "messages": messages}

    def _search_sessions(self, project_id, query):
        """Full-text search across all sessions in a project."""
        if not query or not query.strip():
            return []
        claude_dir = get_claude_dir()
        if not claude_dir:
            return []
        project_dir = claude_dir / "projects" / project_id
        if not project_dir.exists():
            return []

        query_lower = query.lower()
        results = []
        app_meta = load_app_session_meta()

        for f in sorted(project_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            matches = []
            try:
                with open(f, "r") as fh:
                    for line in fh:
                        obj = json.loads(line)
                        msg_type = obj.get("type")
                        if msg_type not in ("user", "assistant"):
                            continue
                        if obj.get("isMeta"):
                            continue
                        raw = obj.get("message", {}).get("content", "")
                        texts = []
                        if isinstance(raw, str):
                            texts.append(raw)
                        elif isinstance(raw, list):
                            for c in raw:
                                if c.get("type") == "text":
                                    texts.append(c.get("text", ""))
                                elif c.get("type") == "thinking":
                                    texts.append(c.get("thinking", ""))
                        for t in texts:
                            if query_lower in t.lower():
                                # Extract snippet around the match
                                idx = t.lower().index(query_lower)
                                start = max(0, idx - 40)
                                end = min(len(t), idx + len(query) + 40)
                                snippet = ("..." if start > 0 else "") + t[start:end] + ("..." if end < len(t) else "")
                                matches.append({
                                    "role": msg_type,
                                    "snippet": snippet,
                                    "uuid": obj.get("uuid", ""),
                                    "timestamp": obj.get("timestamp", ""),
                                })
                                if len(matches) >= 5:  # Max 5 matches per session
                                    break
                        if len(matches) >= 5:
                            break
            except Exception:
                continue

            if matches:
                stat = f.stat()
                custom_title, first_msg, last_msg, slug, entrypoint, session_type = self._extract_title(f)
                app_info = app_meta.get(f.stem, {})
                if not custom_title and app_info.get("title"):
                    custom_title = app_info["title"]
                results.append({
                    "id": f.stem,
                    "path": str(f),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "customTitle": custom_title,
                    "firstMessage": first_msg,
                    "lastMessage": last_msg,
                    "slug": slug,
                    "entrypoint": entrypoint,
                    "sessionType": session_type,
                    "isArchived": app_info.get("isArchived", False),
                    "matches": matches,
                    "matchCount": len(matches),
                })

        return results

    def log_message(self, format, *args):
        pass


def find_free_port(start=8080):
    """Find an available port starting from `start`."""
    import socket
    for port in range(start, start + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start


def open_browser(port):
    """Open browser after a short delay."""
    import threading
    import webbrowser
    def _open():
        import time
        time.sleep(0.5)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=_open, daemon=True).start()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else find_free_port(PORT)
    server = HTTPServer(("127.0.0.1", port), SessionHandler)
    claude_dir = get_claude_dir()
    if claude_dir:
        print(f"Claude dir: {claude_dir}")
    else:
        print("未检测到 ~/.claude，请在浏览器中手动配置路径")
    print(f"Claude Session Viewer running at http://localhost:{port}")
    open_browser(port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
