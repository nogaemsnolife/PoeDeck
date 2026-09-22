"""Official trade site live search.

A live search is a WebSocket the trade site opens for a saved search; the server pushes the
ids of new listings, and their details are then fetched over plain HTTP. Both need the user's
POESESSID cookie. Everything here runs in worker threads and reports to the UI through a queue:

    ("live_status",   (search_key, text))
    ("live_listings", [Listing, ...])

Only the standard library is used; the WebSocket client below implements the small subset
of RFC 6455 the trade server needs (text frames, ping/pong, close).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import HTTP_TIMEOUT, MAX_LIVE_SEARCHES, USER_AGENT

TRADE_HOST = "www.pathofexile.com"
TRADE_ORIGIN = f"https://{TRADE_HOST}"
LIVE_PATH = "/api/trade/live/{league}/{search_id}"
FETCH_URL = f"{TRADE_ORIGIN}/api/trade/fetch/{{ids}}?query={{search_id}}"
SEARCH_PAGE_URL = f"{TRADE_ORIGIN}/trade/search/{{league}}/{{search_id}}"
SEARCH_URL_RE = re.compile(r"pathofexile\.com/trade/search/(?P<league>[^/?#]+)/(?P<id>[^/?#]+)")

FETCH_BATCH = 10            # ids per fetch request (server maximum)
FETCH_MIN_INTERVAL_S = 1.0  # spacing between fetch requests on top of the server's rate limit headers
IDLE_PING_S = 30            # send a ping after this much silence
RECONNECT_MIN_S = 5
RECONNECT_MAX_S = 120


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
@dataclass
class LiveSearch:
    league: str
    id: str
    label: str = ""
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.league}/{self.id}"

    @property
    def page_url(self) -> str:
        return SEARCH_PAGE_URL.format(league=urllib.parse.quote(self.league), search_id=self.id)

    @classmethod
    def from_url(cls, url: str, label: str = "") -> "LiveSearch | None":
        m = SEARCH_URL_RE.search(url.strip())
        if not m:
            return None
        league = urllib.parse.unquote(m.group("league"))
        return cls(league=league, id=m.group("id"), label=label.strip() or f"{league} search")

    @classmethod
    def from_dict(cls, d: dict) -> "LiveSearch":
        return cls(league=str(d.get("league", "")), id=str(d.get("id", "")),
                   label=str(d.get("label", "")), enabled=bool(d.get("enabled", True)))

    def to_dict(self) -> dict:
        return {"league": self.league, "id": self.id, "label": self.label, "enabled": self.enabled}


@dataclass
class Listing:
    search_key: str
    search_label: str
    listing_id: str
    name: str
    price: str
    seller: str
    online: str          # "online", "afk", "offline" or "" when unknown
    whisper: str         # ready-to-paste whisper text; empty when the API did not include it
    hideout_token: str   # present for NPC-market listings when logged in; unused for now
    indexed: str
    received: float = field(default_factory=time.time)


# ----------------------------------------------------------------------------
# Minimal WebSocket client
# ----------------------------------------------------------------------------
class WebSocketError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class WebSocket:
    """Client side of RFC 6455 over TLS: handshake, frame parsing, ping/pong, close."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, timeout: float = IDLE_PING_S):
        self.timeout = timeout
        self.sock: ssl.SSLSocket | None = None
        self.buf = b""
        self.send_lock = threading.Lock()

    def connect(self, host: str, path: str, headers: dict[str, str]) -> None:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, 443), timeout=HTTP_TIMEOUT)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            resp += chunk
            if len(resp) > 65536:
                raise WebSocketError("handshake response too large")
        head, _, rest = resp.partition(b"\r\n\r\n")
        header_lines = head.decode("latin-1").split("\r\n")
        parts = header_lines[0].split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if status != 101:
            reason = parts[2] if len(parts) > 2 else ""
            raise WebSocketError(f"HTTP {status} {reason}".strip(), status=status)
        accept = next((l.split(":", 1)[1].strip() for l in header_lines[1:]
                       if l.lower().startswith("sec-websocket-accept:")), "")
        expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if accept != expected:
            raise WebSocketError("bad Sec-WebSocket-Accept")
        self.buf = rest
        self.sock.settimeout(self.timeout)

    # -- receiving ---------------------------------------------------------------
    def _fill(self, n: int) -> None:
        """Ensure at least n bytes are buffered. Raises socket.timeout without consuming anything."""
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self.buf += chunk

    def _read_frame(self) -> tuple[bool, int, bytes]:
        self._fill(2)
        b1, b2 = self.buf[0], self.buf[1]
        fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        offset = 2
        if length == 126:
            self._fill(4)
            length = struct.unpack(">H", self.buf[2:4])[0]
            offset = 4
        elif length == 127:
            self._fill(10)
            length = struct.unpack(">Q", self.buf[2:10])[0]
            offset = 10
        if masked:
            self._fill(offset + 4)
            mask = self.buf[offset:offset + 4]
            offset += 4
        else:
            mask = None
        self._fill(offset + length)
        payload = self.buf[offset:offset + length]
        self.buf = self.buf[offset + length:]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self) -> str | None:
        """Return the next text message, or None if nothing arrived within the timeout.

        Control frames are handled here: pings are answered, a close frame raises WebSocketError.
        """
        fragments: list[bytes] = []
        while True:
            try:
                fin, opcode, payload = self._read_frame()
            except socket.timeout:
                return None
            if opcode == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 0
                reason = payload[2:].decode("utf-8", "replace")
                raise WebSocketError(f"closed by server ({code} {reason})".rstrip(), status=0)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x0, 0x1, 0x2):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")

    # -- sending -------------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(header + mask + masked)

    def ping(self) -> None:
        self._send_frame(0x9, b"poedeck")

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


# ----------------------------------------------------------------------------
# Rate limiting and listing fetch
# ----------------------------------------------------------------------------
class RateLimiter:
    """Honours the trade API's X-Rate-Limit-* headers for one policy.

    Rules look like "12:4:10" (max requests : period seconds : penalty seconds) and the matching
    state like "3:4:0" (requests made : period : active penalty). We stay one request below every
    rule and wait out any penalty the server reports.
    """

    def __init__(self, min_interval: float = FETCH_MIN_INTERVAL_S):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.blocked_until = 0.0
        self.rules: list[tuple[int, int, int]] = []
        self.state: list[tuple[int, int, int]] = []
        self.state_time = 0.0   # when `state` was reported; a rule's count is stale once its period passed

    @staticmethod
    def _parse(value: str) -> list[tuple[int, int, int]]:
        out = []
        for part in value.split(","):
            nums = part.strip().split(":")
            if len(nums) == 3 and all(n.isdigit() for n in nums):
                out.append((int(nums[0]), int(nums[1]), int(nums[2])))
        return out

    def update(self, headers) -> None:
        rules_header = headers.get("X-Rate-Limit-Rules", "")
        with self.lock:
            for rule in (r.strip() for r in rules_header.split(",") if r.strip()):
                limits = headers.get(f"X-Rate-Limit-{rule}")
                state = headers.get(f"X-Rate-Limit-{rule}-State")
                if limits and state:
                    self.rules, self.state = self._parse(limits), self._parse(state)
                    self.state_time = time.time()
                    for (_, _, _), (_, _, penalty) in zip(self.rules, self.state):
                        if penalty > 0:
                            self.blocked_until = max(self.blocked_until, time.time() + penalty)
                    break

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)

    def wait(self) -> None:
        """Block until a request may be sent."""
        while True:
            with self.lock:
                now = time.time()
                delay = max(self.blocked_until - now, self.last_request + self.min_interval - now)
                for (limit, period, _), (used, _, _) in zip(self.rules, self.state):
                    elapsed = now - self.state_time
                    if used >= limit - 1 and elapsed < period:
                        # the window resets at an unknown moment within `period`; wait out the rest
                        # of a short period, for long rules a fraction is enough to stay under the limit
                        delay = max(delay, min(period - elapsed, 15))
                if delay <= 0:
                    self.last_request = now
                    return
            time.sleep(min(delay, 5))


def _trade_headers(session_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Origin": TRADE_ORIGIN, "Cookie": f"POESESSID={session_id}"}
    if extra:
        h.update(extra)
    return h


def _online_text(account: dict) -> str:
    online = account.get("online")
    if online is None:
        return "offline"
    if isinstance(online, dict) and online.get("status") == "afk":
        return "afk"
    return "online"


def parse_listing(entry: dict, search: LiveSearch) -> Listing:
    li = entry.get("listing") or {}
    it = entry.get("item") or {}
    price = li.get("price") or {}
    if price.get("amount") is not None and price.get("currency"):
        amount = price["amount"]
        amount_text = f"{amount:g}" if isinstance(amount, (int, float)) else str(amount)
        price_text = f"{amount_text} {price['currency']}"
    else:
        price_text = "no price"
    account = li.get("account") or {}
    name = it.get("name") or ""
    type_line = it.get("typeLine") or it.get("baseType") or ""
    full_name = f"{name} {type_line}".strip() if name and type_line and name != type_line else (name or type_line)
    return Listing(
        search_key=search.key, search_label=search.label, listing_id=str(entry.get("id", "")),
        name=full_name or "item", price=price_text, seller=str(account.get("name", "")),
        online=_online_text(account), whisper=str(li.get("whisper") or ""),
        hideout_token=str(li.get("hideout_token") or ""), indexed=str(li.get("indexed") or ""),
    )


def fetch_listings(ids: list[str], search: LiveSearch, session_id: str, limiter: RateLimiter) -> list[Listing]:
    """Fetch listing details in batches of FETCH_BATCH, honouring the rate limit. Raises on HTTP errors."""
    out: list[Listing] = []
    for i in range(0, len(ids), FETCH_BATCH):
        batch = ids[i:i + FETCH_BATCH]
        url = FETCH_URL.format(ids=",".join(batch), search_id=search.id)
        req = urllib.request.Request(url, headers=_trade_headers(session_id, {"Accept": "application/json"}))
        for attempt in range(2):
            limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    limiter.update(resp.headers)
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                limiter.update(e.headers)
                if e.code == 429 and attempt == 0:
                    retry = e.headers.get("Retry-After", "")
                    limiter.penalize(float(retry) if retry.replace(".", "", 1).isdigit() else 60.0)
                    continue
                raise
        for entry in data.get("result") or []:
            if entry:
                out.append(parse_listing(entry, search))
    return out


# ----------------------------------------------------------------------------
# Workers
# ----------------------------------------------------------------------------
class LiveSearchWorker(threading.Thread):
    """Keeps one live-search WebSocket open, reconnecting with backoff; hands new ids to the manager."""

    def __init__(self, search: LiveSearch, session_id: str, manager: "LiveManager"):
        super().__init__(daemon=True, name=f"live:{search.label}")
        self.search = search
        self.session_id = session_id
        self.manager = manager
        self.stop_event = threading.Event()
        self.ws: WebSocket | None = None

    def stop(self) -> None:
        self.stop_event.set()
        ws, self.ws = self.ws, None
        if ws:
            ws.close()

    def _status(self, text: str) -> None:
        self.manager.out.put(("live_status", (self.search.key, text)))

    def run(self) -> None:
        delay = RECONNECT_MIN_S
        while not self.stop_event.is_set():
            ws = WebSocket(timeout=IDLE_PING_S)
            self.ws = ws
            try:
                self._status("connecting…")
                path = LIVE_PATH.format(league=urllib.parse.quote(self.search.league), search_id=self.search.id)
                ws.connect(TRADE_HOST, path, _trade_headers(self.session_id))
                self._status("connected")
                delay = RECONNECT_MIN_S
                while not self.stop_event.is_set():
                    msg = ws.recv_text()
                    if msg is None:
                        ws.ping()
                        continue
                    self._handle(msg)
            except WebSocketError as e:
                if self.stop_event.is_set():
                    break
                if e.status in (401, 403):
                    self._status("auth failed — check POESESSID")
                    return
                if e.status == 404:
                    self._status("search not found (expired?)")
                    return
                if e.status == 429:
                    delay = max(delay, 60)
                self._status(f"{e}; retry in {delay}s")
            except (OSError, ValueError) as e:
                if self.stop_event.is_set():
                    break
                self._status(f"{type(e).__name__}: {e}; retry in {delay}s")
            finally:
                ws.close()
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _handle(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        new_ids = data.get("new")
        if isinstance(new_ids, list) and new_ids:
            self.manager.enqueue_fetch(self.search, [str(i) for i in new_ids])
        err = data.get("error")
        if isinstance(err, dict):
            self._status(f"server: {err.get('message') or err.get('code')}")


class LiveManager:
    """Owns the live-search workers and the single fetch thread. Thread-safe entry points only."""

    def __init__(self, out: "queue.Queue[tuple[str, object]]"):
        self.out = out
        self.workers: dict[str, LiveSearchWorker] = {}
        self.limiter = RateLimiter()
        self.fetch_queue: "queue.Queue[tuple[LiveSearch, list[str]] | None]" = queue.Queue()
        self.fetcher = threading.Thread(target=self._fetch_loop, daemon=True, name="live:fetch")
        self.fetcher.start()

    def apply(self, session_id: str, searches: list[LiveSearch]) -> None:
        """Start/stop workers so they match the enabled searches (capped at MAX_LIVE_SEARCHES)."""
        wanted = {s.key: s for s in searches if s.enabled}
        for key in list(wanted)[MAX_LIVE_SEARCHES:]:
            self.out.put(("live_status", (key, f"not started: limit of {MAX_LIVE_SEARCHES}")))
            del wanted[key]
        for key, worker in list(self.workers.items()):
            if key not in wanted or worker.session_id != session_id or not worker.is_alive():
                worker.stop()
                del self.workers[key]
        for key, search in wanted.items():
            if key in self.workers:
                continue
            if not session_id:
                self.out.put(("live_status", (key, "POESESSID not set")))
                continue
            worker = LiveSearchWorker(search, session_id, self)
            self.workers[key] = worker
            worker.start()
        for s in searches:
            if not s.enabled:
                self.out.put(("live_status", (s.key, "off")))

    def enqueue_fetch(self, search: LiveSearch, ids: list[str]) -> None:
        self.fetch_queue.put((search, ids))

    def _fetch_loop(self) -> None:
        while True:
            job = self.fetch_queue.get()
            if job is None:
                return
            search, ids = job
            worker = self.workers.get(search.key)
            session_id = worker.session_id if worker else ""
            if not session_id:
                continue
            try:
                listings = fetch_listings(ids, search, session_id, self.limiter)
                if listings:
                    self.out.put(("live_listings", listings))
            except urllib.error.HTTPError as e:
                self.out.put(("live_status", (search.key, f"fetch failed: HTTP {e.code}")))
            except (OSError, ValueError) as e:
                self.out.put(("live_status", (search.key, f"fetch failed: {e}")))

    def shutdown(self) -> None:
        for worker in self.workers.values():
            worker.stop()
        self.workers.clear()
        self.fetch_queue.put(None)
