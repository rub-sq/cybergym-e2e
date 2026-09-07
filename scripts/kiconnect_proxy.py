#!/usr/bin/env python3
"""OpenAI-compatible proxy that pools several KIConnect keys and rotates on quota.

OpenHands receives one stable OPENAI_BASE_URL and never learns that more than one
key exists. All key selection, lock state and retrying happens here.

Locking is driven purely by what upstream returns - there is no local request
counting, so the pool cannot drift out of sync with the provider's own accounting.

Two different 429s must not be confused:
  * "too many concurrent requests" is KIConnect's concurrency cap (3 per key).
    Transient - briefly back off and reuse the same key.
  * quota / message-limit language means the key's window is spent.
    Lock it for --lock-seconds and move to the next key.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_DEFAULT = "https://chat.kiconnect.nrw/api/v1"

# Genuine window exhaustion -> lock the key.
QUOTA_PATTERNS = (
    r"\b(?:usage|weekly|session|rate|message)\s*limit\b",
    r"\blimit\s+(?:reached|exceeded|will\s+reset)\b",
    r"\bquota\b",
    r"\bout of\b[^\n]{0,40}\b(?:usage|credit|message)",
    r"\brate[-\s]limited\b",
    r"\btoo many requests\b",
)
# Concurrency cap -> short retry on the SAME key, never a lock.
CONCURRENCY_PATTERNS = (
    r"too many concurrent",
    r"concurrent request",
)

QUOTA_RE = re.compile("|".join(QUOTA_PATTERNS), re.IGNORECASE)
CONCURRENCY_RE = re.compile("|".join(CONCURRENCY_PATTERNS), re.IGNORECASE)


def classify(status, body_text):
    """Return 'quota', 'concurrency', 'ok' or 'error' for an upstream response."""
    if 200 <= status < 300:
        return "ok"
    if CONCURRENCY_RE.search(body_text):
        return "concurrency"
    if status in (429, 402, 403) or QUOTA_RE.search(body_text):
        if QUOTA_RE.search(body_text) or status in (402, 403):
            return "quota"
        # A bare 429 with no usable text: treat as concurrency, the safer guess -
        # a wrong lock costs 2h, a wrong retry costs seconds.
        return "concurrency"
    return "error"


class KeyPool:
    def __init__(self, keys, lock_seconds, state_file, max_concurrent=3):
        self._lock = threading.Condition()
        self._keys = list(keys)
        self._locked_until = {k: 0.0 for k in self._keys}
        self._inflight = {k: 0 for k in self._keys}
        self._lock_seconds = lock_seconds
        self._state_file = state_file
        self._max_concurrent = max_concurrent
        self._load()

    def _label(self, key):
        return key.split(":")[0][:8]

    def _load(self):
        if not self._state_file or not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as fh:
                saved = json.load(fh)
            for key in self._keys:
                until = saved.get(self._label(key))
                if until and until > time.time():
                    self._locked_until[key] = until
                    left = int(until - time.time())
                    log(f"restored lock on {self._label(key)} ({left}s remaining)")
        except Exception as exc:
            log(f"could not read state file: {exc}")

    def _save(self):
        if not self._state_file:
            return
        try:
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(
                    {self._label(k): v for k, v in self._locked_until.items()}, fh
                )
            os.replace(tmp, self._state_file)
        except Exception as exc:
            log(f"could not write state file: {exc}")

    def acquire(self, timeout=None):
        """Block until a key is free, then reserve a concurrency slot on it."""
        deadline = None if timeout is None else time.time() + timeout
        with self._lock:
            while True:
                now = time.time()
                candidates = [
                    k
                    for k in self._keys
                    if self._locked_until[k] <= now
                    and self._inflight[k] < self._max_concurrent
                ]
                if candidates:
                    # Prefer the least busy key so load spreads across the pool.
                    key = min(candidates, key=lambda k: self._inflight[k])
                    self._inflight[key] += 1
                    return key
                waits = [
                    self._locked_until[k] - now
                    for k in self._keys
                    if self._locked_until[k] > now
                ]
                if len(waits) == len(self._keys):
                    soonest = int(min(waits))
                    log(f"all {len(self._keys)} keys locked; soonest frees in {soonest}s")
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("no key became available in time")
                self._lock.wait(timeout=min(5.0, remaining or 5.0))

    def release(self, key):
        with self._lock:
            self._inflight[key] = max(0, self._inflight[key] - 1)
            self._lock.notify_all()

    def lock_out(self, key):
        with self._lock:
            self._locked_until[key] = time.time() + self._lock_seconds
            self._save()
            log(f"LOCKED {self._label(key)} for {self._lock_seconds}s (quota exhausted)")
            self._lock.notify_all()

    def status(self):
        now = time.time()
        with self._lock:
            return {
                self._label(k): {
                    "locked_for": max(0, int(self._locked_until[k] - now)),
                    "inflight": self._inflight[k],
                }
                for k in self._keys
            }


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] proxy: {msg}", flush=True)


class Handler(BaseHTTPRequestHandler):
    pool = None
    upstream = UPSTREAM_DEFAULT
    max_attempts = 12

    def log_message(self, *args):
        pass  # our own logging only

    def do_GET(self):
        if self.path.rstrip("/") == "/_pool":
            payload = json.dumps(self.pool.status(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._proxy(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._proxy(self.rfile.read(length) if length else b"")

    def _proxy(self, body):
        target = self.upstream.rstrip("/") + self.path.replace("/v1", "", 1)
        for attempt in range(1, self.max_attempts + 1):
            try:
                key = self.pool.acquire()
            except TimeoutError:
                self._fail(503, "no key available")
                return
            try:
                status, text, headers = self._forward(target, body, key)
            finally:
                self.pool.release(key)

            verdict = classify(status, text)
            if verdict == "quota":
                self.pool.lock_out(key)
                continue                       # straight to another key
            if verdict == "concurrency":
                wait = min(30, 2 ** min(attempt, 4))
                log(f"concurrency cap on {key.split(':')[0][:8]}; retry in {wait}s")
                time.sleep(wait)
                continue
            self._respond(status, text, headers)
            return
        self._fail(503, f"exhausted {self.max_attempts} attempts across key pool")

    def _forward(self, target, body, key):
        req = urllib.request.Request(target, data=body or None, method=self.command)
        for name, value in self.headers.items():
            if name.lower() in ("host", "authorization", "content-length", "connection"):
                continue
            req.add_header(name, value)
        req.add_header("Authorization", f"Bearer {key}")
        if body:
            req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
        except Exception as exc:
            return 502, f"proxy upstream error: {exc}", {}

    def _respond(self, status, text, headers):
        payload = text.encode("utf-8")
        self.send_response(status)
        ctype = headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, status, message):
        payload = json.dumps({"error": {"message": message, "type": "proxy_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8817)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--upstream", default=os.getenv("KICONNECT_UPSTREAM", UPSTREAM_DEFAULT))
    ap.add_argument("--lock-seconds", type=int, default=7200, help="lock duration after quota exhaustion")
    ap.add_argument("--max-concurrent", type=int, default=3, help="per-key concurrent request cap")
    ap.add_argument("--state-file", default="kiconnect_pool_state.json")
    ap.add_argument("--key", action="append", default=[], help="repeatable; else KICONNECT_KEY1/2 from env")
    args = ap.parse_args()

    keys = list(args.key)
    if not keys:
        for var in ("KICONNECT_KEY1", "KICONNECT_KEY2", "KICONNECT_API_KEY"):
            val = os.getenv(var)
            if val and val not in keys:
                keys.append(val)
    if not keys:
        sys.exit("no keys: pass --key or set KICONNECT_KEY1/KICONNECT_KEY2")

    Handler.pool = KeyPool(keys, args.lock_seconds, args.state_file, args.max_concurrent)
    Handler.upstream = args.upstream
    log(f"listening on {args.bind}:{args.port} -> {args.upstream}")
    log(f"{len(keys)} key(s): {', '.join(k.split(':')[0][:8] for k in keys)}")
    log(f"lock={args.lock_seconds}s  max_concurrent={args.max_concurrent}/key")
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
