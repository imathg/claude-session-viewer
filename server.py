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
    meta, _ = _load_app_meta_full()
    return meta


def load_local_to_cli_map():
    """Return {local_<sessionId>: cliSessionId} for pin resolution."""
    _, l2c = _load_app_meta_full()
    return l2c


def _load_app_meta_full():
    """Single pass over Claude app session metadata; return (cliId→meta, localId→cliId)."""
    meta = {}
    local_to_cli = {}
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
                # local_<uuid> from filename stem maps to the chapters key in Local Storage
                local_to_cli[f.stem] = cli_id
                if not cli_id:
                    continue
                meta[cli_id] = {
                    "isArchived": bool(obj.get("isArchived")),
                    "title": obj.get("title", "") or "",
                }
            except Exception:
                continue
    return meta, local_to_cli


# ── Claude desktop pins (epitaxy-chapters-v2 in Local Storage leveldb) ──
#
# Claude desktop's bell-icon "Pin chapter" feature stores chapters in a Chrome
# Local Storage key. The leveldb files are at:
#   ~/Library/Application Support/Claude/Local Storage/leveldb/
# Values >threshold are Snappy-compressed at block level. We embed a minimal
# pure-Python Snappy + SSTable reader so the project keeps its no-deps promise.

_LEVELDB_MAGIC = 0xdb4775248b80fb57


def _ldb_varint(buf, pos):
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if b & 0x80 == 0:
            break
        shift += 7
    return val, pos


def _ldb_handle(buf, pos):
    off, pos = _ldb_varint(buf, pos)
    size, pos = _ldb_varint(buf, pos)
    return (off, size), pos


def _snappy_decompress(data):
    """Pure-Python Snappy block-format decoder."""
    _length, pos = _ldb_varint(data, 0)
    out = bytearray()
    n = len(data)
    while pos < n:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:
            lit_len = tag >> 2
            if lit_len < 60:
                lit_len += 1
            else:
                extra = lit_len - 59
                lit_len = int.from_bytes(data[pos:pos+extra], "little") + 1
                pos += extra
            out.extend(data[pos:pos+lit_len])
            pos += lit_len
        elif kind == 1:
            run = ((tag >> 2) & 0x07) + 4
            off = ((tag >> 5) << 8) | data[pos]
            pos += 1
            start = len(out) - off
            for i in range(run):
                out.append(out[start + i])
        elif kind == 2:
            run = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos+2], "little")
            pos += 2
            start = len(out) - off
            for i in range(run):
                out.append(out[start + i])
        else:
            run = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos+4], "little")
            pos += 4
            start = len(out) - off
            for i in range(run):
                out.append(out[start + i])
    return bytes(out)


def _ldb_read_block(data, offset, size):
    block = data[offset:offset+size]
    if offset + size >= len(data):
        return bytes(block)
    comp = data[offset+size]
    if comp == 0:
        return bytes(block)
    if comp == 1:
        return _snappy_decompress(block)
    raise ValueError(f"unknown comp type {comp}")


def _ldb_walk_block(block):
    n = len(block)
    if n < 4:
        return
    num_restarts = int.from_bytes(block[n-4:n], "little")
    end = n - 4 - 4 * num_restarts
    pos = 0
    prev_key = b""
    while pos < end:
        shared, pos = _ldb_varint(block, pos)
        unshared, pos = _ldb_varint(block, pos)
        value_len, pos = _ldb_varint(block, pos)
        key = prev_key[:shared] + bytes(block[pos:pos+unshared])
        pos += unshared
        value = bytes(block[pos:pos+value_len])
        pos += value_len
        yield key, value
        prev_key = key


def _ldb_read_file(path):
    """Yield (key, value) from a LevelDB SSTable file."""
    try:
        data = Path(path).read_bytes()
    except Exception:
        return
    if len(data) < 48:
        return
    footer = data[-48:]
    magic = int.from_bytes(footer[-8:], "little")
    if magic != _LEVELDB_MAGIC:
        return
    _meta, pos = _ldb_handle(footer, 0)
    index_h, _ = _ldb_handle(footer, pos)
    try:
        index_block = _ldb_read_block(data, *index_h)
    except Exception:
        return
    for _ik, ival in _ldb_walk_block(index_block):
        try:
            handle, _ = _ldb_handle(ival, 0)
            dblock = _ldb_read_block(data, *handle)
        except Exception:
            continue
        for k, v in _ldb_walk_block(dblock):
            yield k, v


def _decode_localstorage_value(raw):
    """Local Storage values are 1-byte encoding tag + payload."""
    if not raw:
        return None
    enc = raw[0]
    payload = raw[1:]
    try:
        if enc == 0:
            return payload.decode("utf-16-le", errors="replace")
        return payload.decode("utf-8", errors="replace")
    except Exception:
        return None


def load_claude_pins():
    """Return parsed epitaxy-chapters-v2 JSON, or {}.

    Reads the freshest value across .ldb files in Claude desktop's Local Storage.
    Newer .ldb files override older ones since LevelDB compaction keeps the latest
    write per key.
    """
    root = Path.home() / "Library/Application Support/Claude/Local Storage/leveldb"
    if not root.is_dir():
        return {}
    files = sorted(root.glob("*.ldb"), key=lambda p: p.stat().st_mtime)
    chapter_value = None
    for f in files:
        for k, v in _ldb_read_file(f):
            if b"epitaxy-chapters-v2" in k:
                chapter_value = v
    if chapter_value is None:
        return {}
    text = _decode_localstorage_value(chapter_value)
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except Exception:
        return {}
    by_session = obj.get("state", {}).get("bySession", {}) if isinstance(obj, dict) else {}
    return by_session if isinstance(by_session, dict) else {}


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
        elif path == "/api/pins":
            self._json_response(self._list_pins())
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
        elif parsed.path == "/api/set-archived":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._json_response(self._set_archived(body.get("session_id", ""), body.get("archived", False)))
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

    @staticmethod
    def _read_cwd(jsonls):
        """Return the `cwd` field from the first JSONL line that has one, else None."""
        for f in jsonls:
            try:
                with open(f, "r") as fh:
                    for line in fh:
                        obj = json.loads(line)
                        cwd = obj.get("cwd")
                        if cwd:
                            return cwd
            except Exception:
                continue
        return None

    @staticmethod
    def _decode_dir_name(raw):
        """Fallback path decoder when no JSONL `cwd` is available."""
        if not raw.startswith("-"):
            return raw.replace("-", "/")
        naive = "/" + raw[1:].replace("-", "/")
        if Path(naive).exists():
            return naive
        # Walk segments; when a segment doesn't resolve, try rejoining with
        # the previous path using '-' or '_' (both flatten to '-' on disk).
        segs = raw[1:].split("-")
        real_path = ""
        for seg in segs:
            test = real_path + "/" + seg
            if Path(test).exists():
                real_path = test
            elif real_path:
                merged = None
                for sep in ("-", "_"):
                    cand = real_path + sep + seg
                    if Path(cand).exists():
                        merged = cand
                        break
                real_path = merged if merged else real_path + "/" + seg
            else:
                real_path = "/" + seg
        return real_path

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
                jsonls = list(d.glob("*.jsonl"))
                jsonl_count = len(jsonls)
                if jsonl_count > 0:
                    # Prefer the authoritative `cwd` field from any JSONL line; the dir
                    # name encoding (/ → -) is lossy because folder names can contain
                    # both '-' and '_'.
                    real_path = self._read_cwd(jsonls) or self._decode_dir_name(d.name)

                    # Create display name: strip home prefix, show as ~/...
                    if real_path.startswith(home):
                        display = "~" + real_path[len(home):]
                    else:
                        display = real_path
                    last_modified = max(f.stat().st_mtime for f in jsonls)
                    projects.append({
                        "id": d.name,
                        "name": display,
                        "fullPath": real_path,
                        "sessions": jsonl_count,
                        "lastModified": last_modified,
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
        pin_counts = self._pin_counts_by_cli_id()
        sessions = []
        for f in sorted(project_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = f.stat()
            custom_title, first_msg, last_msg, slug, entrypoint, session_type, fork_root, first_msg_uuid, is_fork_origin, last_query_ts = self._extract_title(f)
            app_info = app_meta.get(f.stem, {})
            # Claude app's user-edited title (titleSource=user) is the latest
            # rename — it should override the JSONL custom-title which may be
            # inherited from a parent fork.
            if app_info.get("title"):
                custom_title = app_info["title"]
            # Skip sessions with no real user input (e.g., only `Unknown command: /x`
            # or raw <command-message> that parsed to nothing) and no user-set title.
            if not custom_title and not first_msg:
                continue
            sessions.append({
                "id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "lastQueryTs": last_query_ts or stat.st_mtime,
                "customTitle": custom_title,
                "firstMessage": first_msg,
                "lastMessage": last_msg,
                "slug": slug,
                "entrypoint": entrypoint,
                "sessionType": session_type,
                "isArchived": app_info.get("isArchived", False),
                "forkRoot": fork_root,
                "firstMsgUuid": first_msg_uuid,
                "isForkOrigin": is_fork_origin,
                "pinCount": pin_counts.get(f.stem, 0),
            })
        return sessions

    @staticmethod
    def _pin_counts_by_cli_id():
        """Return {cliSessionId: number of pinned chapters}."""
        chapters = load_claude_pins()
        local_to_cli = load_local_to_cli_map()
        counts = {}
        for local_id, payload in chapters.items():
            cli_id = local_to_cli.get(local_id)
            if not cli_id:
                continue
            n = len(payload.get("userChapters", []) or []) if isinstance(payload, dict) else 0
            if n:
                counts[cli_id] = counts.get(cli_id, 0) + n
        return counts

    def _list_pins(self):
        """Return list of pinned chapters, joined with session info + resolved msg uuid."""
        claude_dir = get_claude_dir()
        if not claude_dir:
            return []
        chapters = load_claude_pins()
        if not chapters:
            return []
        app_meta, local_to_cli = _load_app_meta_full()
        # Build index: cliSessionId → JSONL path
        cli_to_jsonl = {}
        projects_dir = claude_dir / "projects"
        if projects_dir.is_dir():
            for proj in projects_dir.iterdir():
                if not proj.is_dir():
                    continue
                for jf in proj.glob("*.jsonl"):
                    cli_to_jsonl[jf.stem] = jf

        out = []
        for local_id, payload in chapters.items():
            if not isinstance(payload, dict):
                continue
            user_chapters = payload.get("userChapters") or []
            if not user_chapters:
                continue
            cli_id = local_to_cli.get(local_id)
            jsonl_path = cli_to_jsonl.get(cli_id) if cli_id else None
            session_title = ""
            session_first_msg = ""
            project_id = ""
            project_path = ""
            after_to_uuid = {}
            after_targets = {c.get("afterId", "") for c in user_chapters if c.get("afterId")}
            if jsonl_path and jsonl_path.exists():
                project_id = jsonl_path.parent.name
                project_path = self._read_cwd([jsonl_path]) or self._decode_dir_name(project_id)
                custom_title, first_msg, _last, slug, _ep, _st, _fr, _fmu, _fo, _lq = self._extract_title(jsonl_path)
                meta_info = app_meta.get(cli_id, {})
                session_title = meta_info.get("title") or custom_title or slug or first_msg
                session_first_msg = first_msg
                after_to_uuid = self._map_after_ids_to_uuids(jsonl_path, after_targets)
            chapters_out = []
            for c in user_chapters:
                if not isinstance(c, dict):
                    continue
                after_id = c.get("afterId", "")
                chapters_out.append({
                    "id": c.get("id", ""),
                    "afterId": after_id,
                    "title": c.get("title", ""),
                    "msgUuid": after_to_uuid.get(after_id, ""),
                })
            out.append({
                "localId": local_id,
                "cliSessionId": cli_id or "",
                "sessionPath": str(jsonl_path) if jsonl_path else "",
                "sessionTitle": session_title,
                "firstMessage": session_first_msg,
                "projectId": project_id,
                "projectPath": project_path,
                "isArchived": app_meta.get(cli_id, {}).get("isArchived", False) if cli_id else False,
                "modified": jsonl_path.stat().st_mtime if jsonl_path and jsonl_path.exists() else 0,
                "chapters": chapters_out,
            })
        # Group sessions by descending mtime so most recent pinned conversations surface first
        out.sort(key=lambda x: x["modified"], reverse=True)
        return out

    @staticmethod
    def _map_after_ids_to_uuids(jsonl_path, targets):
        """Scan JSONL once to map each afterId in `targets` to a JSONL line uuid.

        afterId formats from Claude desktop:
          - "msg_<id>-tN"  → matches obj.message.id (strip -tN suffix)
          - "toolu_<id>"   → matches a tool_use part's id within an assistant message
        For msg_*, prefer the LAST line with that message.id (final text chunk).
        """
        # Pre-compute the "msg base" forms we care about
        msg_bases = {}  # base_id → afterId (preserves the -tN suffix the caller asked for)
        toolu_ids = {}  # toolu_id → afterId
        other_ids = {}  # everything else; match against arbitrary content.id
        for a in targets:
            if not a:
                continue
            if a.startswith("msg_"):
                base = a
                # Strip "-t<digits>" suffix
                m = re.match(r"^(msg_[^-\s]+)(-t\d+)?$", a)
                if m:
                    base = m.group(1)
                msg_bases[base] = a
            elif a.startswith("toolu_"):
                toolu_ids[a] = a
            else:
                other_ids[a] = a

        mapping = {}
        try:
            with open(jsonl_path, "r") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message", {}) or {}
                    mid = msg.get("id", "")
                    uuid = obj.get("uuid", "")
                    if mid and mid in msg_bases and uuid:
                        # Take the LAST line with this id (overwrite)
                        mapping[msg_bases[mid]] = uuid
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            cid = c.get("id", "")
                            if cid and cid in toolu_ids and uuid:
                                mapping[toolu_ids[cid]] = uuid
                            elif cid and cid in other_ids and uuid:
                                mapping[other_ids[cid]] = uuid
        except Exception:
            pass
        return mapping

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
        # Typo'd slash command — CLI auto-inserts this as a user message.
        if stripped.startswith("Unknown command: /"):
            return True
        if any(stripped.startswith(tag) for tag in meta_prefixes):
            return True
        if stripped in meta_exact:
            return True
        # Image metadata from paste
        if stripped.startswith("[Image: original "):
            return True
        return False

    @staticmethod
    def _prettify_command_message(text):
        """Slash commands are stored as XML blocks:
            <command-message>plugin</command-message>
            <command-name>/cmd</command-name>
            <command-args>args</command-args>[trailing user text]
        Render them as '/cmd args' plus any trailing text."""
        if not text or "<command-message>" not in text:
            return text
        name_m = re.search(r"<command-name>([^<]*)</command-name>", text)
        args_m = re.search(r"<command-args>([^<]*)</command-args>", text, re.DOTALL)
        cleaned = re.sub(r"<command-(message|name|args)>[^<]*</command-\1>", "", text, flags=re.DOTALL).strip()
        name = name_m.group(1).strip() if name_m else ""
        args = args_m.group(1).strip() if args_m else ""
        pretty = (name + " " + args).strip() if name or args else ""
        if cleaned:
            pretty = (pretty + " " + cleaned).strip() if pretty else cleaned
        return pretty

    def _extract_title(self, filepath):
        """Extract custom title, first real user message, last user query, slug, entrypoint, session type, and fork root."""
        custom_title = ""
        first_user_msg = ""
        last_user_msg = ""
        last_query_ts = 0.0
        entrypoint = ""
        slug = ""
        session_type = "manual"  # "manual" | "scheduled" | "headless"
        # `logicalParentUuid` on the first compact_boundary marks where this session
        # forked from. Sessions sharing it are siblings (fork/compact from same parent).
        fork_root = ""
        first_msg_uuid = ""
        is_fork_origin = False
        session_id = filepath.stem if isinstance(filepath, Path) else Path(filepath).stem
        uuid_to_sid = {}
        try:
            with open(filepath, "r") as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "custom-title":
                        # Take the LAST custom-title event — JSONLs in a shared
                        # conversation tree record the full rename history, and
                        # the latest entry reflects the current name. (Earlier
                        # logic took the first event, which surfaced typo'd
                        # original titles on sibling forks.)
                        latest = obj.get("customTitle", "") or obj.get("title", "")
                        if latest:
                            custom_title = latest
                    if not entrypoint and obj.get("entrypoint"):
                        entrypoint = obj.get("entrypoint", "")
                    if not slug and obj.get("slug"):
                        slug = obj.get("slug", "")
                    if not fork_root and obj.get("logicalParentUuid"):
                        fork_root = obj.get("logicalParentUuid", "")
                    uuid_val = obj.get("uuid", "")
                    sid_val = obj.get("sessionId", "")
                    if uuid_val and sid_val:
                        uuid_to_sid[uuid_val] = sid_val
                    if obj.get("type") == "user":
                        # Skip meta messages (isMeta, local-command-caveat, etc.)
                        if obj.get("isMeta"):
                            continue
                        if not first_msg_uuid and obj.get("uuid"):
                            first_msg_uuid = obj["uuid"]
                        ts = obj.get("timestamp", "")
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
                        # Render slash-command XML blocks as '/cmd args'
                        display_text = self._prettify_command_message(raw_text).strip()
                        if not display_text:
                            continue
                        text = display_text[:150]
                        if text:
                            if not first_user_msg:
                                first_user_msg = text[:100]
                            last_user_msg = text
                            if ts:
                                try:
                                    from datetime import datetime, timezone
                                    last_query_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                                except Exception:
                                    pass
        except Exception:
            pass
        if fork_root and uuid_to_sid.get(fork_root) == session_id:
            is_fork_origin = True
        # Headless one-shot calls (e.g. 破壁人 reader's `claude -p` heartbeats)
        # come through entrypoint=sdk-cli and have no session continuity. They
        # surface in the sidebar as 4 near-identical markdown-titled rows
        # otherwise — mark them so the UI can icon/filter them separately.
        if session_type == "manual" and entrypoint == "sdk-cli":
            session_type = "headless"
        return custom_title, first_user_msg, last_user_msg, slug, entrypoint, session_type, fork_root, first_msg_uuid, is_fork_origin, last_query_ts

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
                custom_title, first_msg, last_msg, slug, entrypoint, session_type, fork_root, first_msg_uuid, _fo, _lq = self._extract_title(f)
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
                    "forkRoot": fork_root,
                    "firstMsgUuid": first_msg_uuid,
                    "matches": matches,
                    "matchCount": len(matches),
                })

        return results

    @staticmethod
    def _set_archived(session_id, archived):
        """Toggle isArchived on the Claude desktop app's local_<uuid>.json for a session.

        Desktop app currently has no unarchive UI, so the viewer exposes it. The
        metadata file is identified by its `cliSessionId` field; format on disk
        is compact single-line JSON, so we round-trip with no indent.
        """
        if not session_id:
            return {"ok": False, "error": "missing session_id"}
        archived = bool(archived)
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
                except Exception:
                    continue
                if obj.get("cliSessionId") != session_id:
                    continue
                if bool(obj.get("isArchived")) == archived:
                    return {"ok": True, "noChange": True}
                obj["isArchived"] = archived
                try:
                    f.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
                    return {"ok": True, "path": str(f)}
                except Exception as e:
                    return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"未在 Claude app 数据中找到 session {session_id[:8]}"}

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
