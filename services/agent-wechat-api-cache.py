#!/usr/bin/env python3
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


UPSTREAM_URL = os.environ.get("AGENT_WECHAT_UPSTREAM_URL", "http://127.0.0.1:6174").rstrip("/")
BIND_HOST = os.environ.get("AGENT_WECHAT_CACHE_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("AGENT_WECHAT_CACHE_PORT", "6175"))
TOKEN_FILE = Path(os.environ.get("AGENT_WECHAT_TOKEN_FILE", "/opt/docker/agent-wechat/secrets/token"))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "180"))
AUTH_TTL_SECONDS = float(os.environ.get("AUTH_CACHE_TTL_SECONDS", "30"))
CHATS_TTL_SECONDS = float(os.environ.get("CHATS_CACHE_TTL_SECONDS", "20"))
MESSAGES_TTL_SECONDS = float(os.environ.get("MESSAGES_CACHE_TTL_SECONDS", "12"))
MAX_STALE_SECONDS = float(os.environ.get("MAX_STALE_SECONDS", "60"))
CHATS_PREFETCH_LIMIT = int(os.environ.get("CHATS_PREFETCH_LIMIT", "50"))
MESSAGES_PREFETCH_LIMIT = int(os.environ.get("MESSAGES_PREFETCH_LIMIT", "50"))
ALLOWED_CHAT_IDS = {
    item.strip()
    for item in os.environ.get("AGENT_WECHAT_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}

MESSAGE_PATH_RE = re.compile(r"^/api/messages/[^/]+$")


class Cache:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.stats = {
            "hits": 0,
            "misses": 0,
            "bypasses": 0,
            "stale": 0,
            "errors": 0,
            "invalidations": 0,
        }

    def get(self, key):
        now = time.time()
        with self.lock:
            item = self.items.get(key)
            if not item:
                self.stats["misses"] += 1
                return None, "miss"
            if item["expires_at"] >= now:
                self.stats["hits"] += 1
                return item, "hit"
            self.stats["misses"] += 1
            return item, "expired"

    def set(self, key, ttl, status, headers, body):
        now = time.time()
        with self.lock:
            self.items[key] = {
                "status": status,
                "headers": headers,
                "body": body,
                "created_at": now,
                "expires_at": now + ttl,
            }

    def invalidate_messages(self):
        with self.lock:
            before = len(self.items)
            self.items = {
                key: value
                for key, value in self.items.items()
                if not (key.startswith("GET /api/messages/") or key.startswith("GET /api/chats"))
            }
            removed = before - len(self.items)
            if removed:
                self.stats["invalidations"] += removed
            return removed

    def snapshot(self):
        with self.lock:
            return {"items": len(self.items), "stats": dict(self.stats)}


CACHE = Cache()


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def read_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def cache_ttl(path):
    path_only = path.split("?", 1)[0]
    if path_only in {"/api/status", "/api/status/auth"}:
        return AUTH_TTL_SECONDS
    if path_only == "/api/chats":
        return CHATS_TTL_SECONDS
    if MESSAGE_PATH_RE.match(path_only):
        return MESSAGES_TTL_SECONDS
    return 0.0


def requested_limit(query):
    values = urllib.parse.parse_qs(query, keep_blank_values=True)
    if set(values) - {"limit"}:
        return None
    raw = values.get("limit", [None])[0]
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def canonical_get(path):
    split = urllib.parse.urlsplit(path)
    limit = requested_limit(split.query)
    if limit is None:
        return {
            "key": "GET " + path,
            "fetch_path": path,
            "limit": None,
            "ttl": cache_ttl(path),
        }

    if split.path == "/api/chats":
        fetch_limit = max(limit, CHATS_PREFETCH_LIMIT)
        fetch_path = split.path + "?" + urllib.parse.urlencode({"limit": fetch_limit})
        return {
            "key": f"GET {split.path}?prefetch={fetch_limit}",
            "fetch_path": fetch_path,
            "limit": limit,
            "ttl": CHATS_TTL_SECONDS,
        }

    if MESSAGE_PATH_RE.match(split.path):
        fetch_limit = max(limit, MESSAGES_PREFETCH_LIMIT)
        fetch_path = split.path + "?" + urllib.parse.urlencode({"limit": fetch_limit})
        return {
            "key": f"GET {split.path}?prefetch={fetch_limit}",
            "fetch_path": fetch_path,
            "limit": limit,
            "ttl": MESSAGES_TTL_SECONDS,
        }

    return {
        "key": "GET " + path,
        "fetch_path": path,
        "limit": None,
        "ttl": cache_ttl(path),
    }


def trim_json_list(body, limit):
    if limit is None:
        return body
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return body
    if not isinstance(data, list):
        return body
    return json.dumps(data[:limit], ensure_ascii=False).encode("utf-8")


def filter_chats_json_list(body):
    if not ALLOWED_CHAT_IDS:
        return body
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return body
    if not isinstance(data, list):
        return body
    filtered = []
    for item in data:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("id") or item.get("chatId") or "")
        if chat_id in ALLOWED_CHAT_IDS:
            filtered.append(item)
    return json.dumps(filtered, ensure_ascii=False).encode("utf-8")


def response_headers(upstream_headers):
    content_type = upstream_headers.get("Content-Type") or upstream_headers.get("content-type")
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def build_forward_headers(handler):
    skip = {"host", "content-length", "connection", "accept-encoding"}
    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower() not in skip
    }
    if "Authorization" not in headers:
        token = read_token()
        if token:
            headers["Authorization"] = "Bearer " + token
    return headers


def forward(method, path, headers, body=None):
    req = urllib.request.Request(
        UPSTREAM_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
            data = resp.read()
            return resp.status, response_headers(resp.headers), data
    except urllib.error.HTTPError as exc:
        data = exc.read()
        return exc.code, response_headers(exc.headers), data


def json_body(body):
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (AttributeError, json.JSONDecodeError):
        return None


def should_retry_send(status, body):
    if status != 200:
        return False
    data = json_body(body)
    if not isinstance(data, dict) or data.get("success") is not False:
        return False
    error = str(data.get("error") or "")
    return "No action selected" in error


def maybe_retry_send(path, headers, body, status, response_body):
    if path.split("?", 1)[0] != "/api/messages/send":
        return status, response_body
    if not should_retry_send(status, response_body):
        return status, response_body

    payload = json_body(body)
    chat_id = str((payload or {}).get("chatId") or "").strip()
    if not chat_id:
        return status, response_body

    open_path = "/api/chats/" + urllib.parse.quote(chat_id, safe="") + "/open"
    log(f"send returned No action selected; opening chat then retrying chat={chat_id}")
    open_status, _open_headers, open_body = forward("POST", open_path, headers)
    log(f"open before retry chat={chat_id} status={open_status} body={open_body[:200]!r}")
    time.sleep(1.0)
    retry_status, _retry_headers, retry_body = forward("POST", path, headers, body)
    return retry_status, retry_body


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentWechatApiCache/1.0"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def write_response(self, status, headers, body, cache_status="bypass"):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Agent-WeChat-Cache", cache_status)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({
                "ok": True,
                "upstream": UPSTREAM_URL,
                **CACHE.snapshot(),
            }, ensure_ascii=False).encode("utf-8")
            self.write_response(200, {"Content-Type": "application/json"}, body)
            return

        plan = canonical_get(self.path)
        ttl = plan["ttl"]
        key = plan["key"]
        if ttl <= 0:
            CACHE.stats["bypasses"] += 1
            status, headers, body = forward("GET", self.path, build_forward_headers(self))
            self.write_response(status, headers, body)
            return

        cached, state = CACHE.get(key)
        if cached and state == "hit":
            body = cached["body"]
            if urllib.parse.urlsplit(self.path).path == "/api/chats":
                body = filter_chats_json_list(body)
            body = trim_json_list(body, plan["limit"])
            self.write_response(cached["status"], cached["headers"], body, "hit")
            return

        try:
            status, headers, body = forward("GET", plan["fetch_path"], build_forward_headers(self))
        except Exception as exc:
            CACHE.stats["errors"] += 1
            if cached and time.time() - cached["created_at"] <= MAX_STALE_SECONDS:
                CACHE.stats["stale"] += 1
                log(f"serving stale path={self.path} error={exc!r}")
                body = cached["body"]
                if urllib.parse.urlsplit(self.path).path == "/api/chats":
                    body = filter_chats_json_list(body)
                body = trim_json_list(body, plan["limit"])
                self.write_response(cached["status"], cached["headers"], body, "stale")
                return
            body = json.dumps({"error": repr(exc)}, ensure_ascii=False).encode("utf-8")
            self.write_response(502, {"Content-Type": "application/json"}, body, "error")
            return

        if status == 200:
            CACHE.set(key, ttl, status, headers, body)
        if urllib.parse.urlsplit(self.path).path == "/api/chats":
            body = filter_chats_json_list(body)
        body = trim_json_list(body, plan["limit"])
        self.write_response(status, headers, body, "miss")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        forward_headers = build_forward_headers(self)
        status, headers, response_body = forward("POST", self.path, forward_headers, body)
        status, response_body = maybe_retry_send(
            self.path, forward_headers, body, status, response_body
        )
        if self.path.split("?", 1)[0] == "/api/messages/send":
            removed = CACHE.invalidate_messages()
            log(f"invalidated cache entries after send count={removed}")
        self.write_response(status, headers, response_body)

    def do_PUT(self):
        self.forward_with_body("PUT")

    def do_PATCH(self):
        self.forward_with_body("PATCH")

    def do_DELETE(self):
        self.forward_with_body("DELETE")

    def forward_with_body(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        status, headers, response_body = forward(method, self.path, build_forward_headers(self), body)
        self.write_response(status, headers, response_body)


def main():
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    log(
        "starting agent-wechat api cache "
        f"bind={BIND_HOST}:{BIND_PORT} upstream={UPSTREAM_URL} "
        f"ttl(auth={AUTH_TTL_SECONDS},chats={CHATS_TTL_SECONDS},messages={MESSAGES_TTL_SECONDS}) "
        f"prefetch(chats={CHATS_PREFETCH_LIMIT},messages={MESSAGES_PREFETCH_LIMIT}) "
        f"allowedChats={len(ALLOWED_CHAT_IDS)}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
