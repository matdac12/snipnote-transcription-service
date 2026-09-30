"""APNs Live Activity pushes for transcription progress (best effort, never fatal).

The iOS app starts an ActivityKit Live Activity with `pushType: .token` and uploads the
per-activity push token to `live_activity_tokens` (see docs/LIVE_ACTIVITY_CONTRACT.md).
The worker calls `notify_stage(job_id, stage, progress)` at real stage transitions; this
module

  * persists `transcription_jobs.stage` (best effort, tolerant of a missing column),
  * pushes `event: update` to every token of the job over APNs HTTP/2, throttled
    (stage transitions always, in-stage progress at most one push per
    APNS_PROGRESS_INTERVAL_SECONDS),
  * pushes `event: end` with an alert on `done` / `failed`, then forgets the tokens.

Design rules
  * `notify_stage` is synchronous, thread-safe and only enqueues: the worker threads
    never wait for Supabase or APNs. One daemon thread owns an asyncio loop that does
    all the I/O (jobs.py is sync and runs in an executor, main.py is async).
  * Nothing here may raise into a job: every public entry point swallows exceptions,
    all I/O is time-bounded and the queue is bounded (overflow drops the update).
  * Disabled (no thread, no I/O, no logs per call) when any APNS_* variable is
    missing; only stage persistence remains (LIVE_ACTIVITY_ENABLED=false turns that
    off too).
  * Secrets (key, JWT) and full device tokens are never logged.

Env (read once, when the notifier is first used):
  APNS_KEY_P8        path to the .p8 file, or the PEM text itself (literal "\\n" ok)
  APNS_KEY_ID        10-char key id
  APNS_TEAM_ID       10-char Apple team id
  APNS_BUNDLE_ID     app bundle id; topic is "<bundle>.push-type.liveactivity"
  LIVE_ACTIVITY_ENABLED          default true   master switch
  APNS_PROGRESS_INTERVAL_SECONDS default 20     min gap between in-stage pushes
  APNS_STALE_SECONDS             default 1200   stale-date offset for updates
  APNS_DISMISSAL_SECONDS         default 10800  end-event dismissal offset (max 4h)
"""
import asyncio
import atexit
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
import jwt

STAGES = ("queued", "preparing", "transcribing", "summarizing", "done", "failed")
TERMINAL_STAGES = ("done", "failed")

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}

JWT_REFRESH_SECONDS = 50 * 60      # Apple: token valid <= 60 min, must not refresh more than every 20 min
JWT_MIN_FORCED_REFRESH_SECONDS = 5 * 60
MAX_PAYLOAD_BYTES = 4096
MAX_MESSAGE_CHARS = 120
MAX_DISMISSAL_SECONDS = 4 * 3600
REQUEST_TIMEOUT_SECONDS = 10.0
QUEUE_MAX = 200
FLUSH_TIMEOUT_SECONDS = 8.0

# APNs reasons meaning "this token will never work again": delete the row.
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "ExpiredToken"}
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,512}$")  # the token goes into the URL path: hex only

ALERTS = {
    "done": {"title": "Meeting ready", "body": "Your transcript and summary are ready."},
    "failed": {"title": "Transcription failed", "body": "Open SnipNote to try again."},
}


def _log(message: str) -> None:
    print(f"   📡 APNs: {message}", flush=True)


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def _mask(token: str) -> str:
    return f"…{token[-6:]}" if token else "…"


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class ApnsConfig:
    key_pem: str
    key_id: str
    team_id: str
    bundle_id: str

    @classmethod
    def from_env(cls) -> Optional["ApnsConfig"]:
        """Return the config, or None when APNs is not (fully) configured."""
        key = (os.getenv("APNS_KEY_P8") or "").strip()
        key_id = (os.getenv("APNS_KEY_ID") or "").strip()
        team_id = (os.getenv("APNS_TEAM_ID") or "").strip()
        bundle_id = (os.getenv("APNS_BUNDLE_ID") or "").strip()
        if not (key and key_id and team_id and bundle_id):
            return None
        if "BEGIN" in key:
            pem = key.replace("\\n", "\n")
        else:
            try:
                with open(key, "r", encoding="utf-8") as handle:
                    pem = handle.read()
            except OSError as error:
                _log(f"disabled: cannot read APNS_KEY_P8 file ({type(error).__name__})")
                return None
        if "BEGIN" not in pem:
            _log("disabled: APNS_KEY_P8 is not a PEM private key")
            return None
        return cls(pem, key_id, team_id, bundle_id)


class JwtProvider:
    """ES256 provider token, cached and refreshed every ~50 minutes.

    Thread-safe (one lock). A refresh forced by an APNs 403 only happens if the token
    that failed is still the current one (concurrent senders do not stampede) and the
    current one is older than JWT_MIN_FORCED_REFRESH_SECONDS (Apple rate-limits
    provider token refreshes).
    """

    def __init__(self, key_pem: str, key_id: str, team_id: str, clock=time.time):
        self._key = key_pem
        self._key_id = key_id
        self._team_id = team_id
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._issued_at = 0.0

    def _mint(self) -> None:
        now = self._clock()
        self._token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)}, self._key,
            algorithm="ES256", headers={"kid": self._key_id},
        )
        self._issued_at = now

    def get(self) -> str:
        with self._lock:
            if self._token is None or self._clock() - self._issued_at >= JWT_REFRESH_SECONDS:
                self._mint()
            return self._token  # type: ignore[return-value]

    def refresh_after_rejection(self, rejected: str) -> str:
        with self._lock:
            if self._token == rejected and self._clock() - self._issued_at >= JWT_MIN_FORCED_REFRESH_SECONDS:
                self._mint()
            return self._token  # type: ignore[return-value]


# --------------------------------------------------------------------------- payload

def build_payload(
    stage: str,
    progress: Optional[float] = None,
    *,
    chunk: Optional[int] = None,
    total_chunks: Optional[int] = None,
    message: Optional[str] = None,
    now: Optional[float] = None,
    stale_seconds: Optional[float] = None,
    dismissal_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """The `aps` body. `progress` is a fraction 0-1 (or None = indeterminate)."""
    now_ts = int(now if now is not None else time.time())
    terminal = stage in TERMINAL_STAGES
    if stage == "done":
        progress = 1.0
    content: Dict[str, Any] = {
        "stage": stage,
        "progress": None if progress is None else round(min(1.0, max(0.0, float(progress))), 3),
    }
    if chunk is not None:
        content["chunk"] = int(chunk)
    if total_chunks is not None:
        content["totalChunks"] = int(total_chunks)
    if message:
        content["message"] = str(message)[:MAX_MESSAGE_CHARS]

    aps: Dict[str, Any] = {
        "timestamp": now_ts,
        "event": "end" if terminal else "update",
        "content-state": content,
    }
    if terminal:
        dismissal = _env_number("APNS_DISMISSAL_SECONDS", 3 * 3600) if dismissal_seconds is None else dismissal_seconds
        aps["dismissal-date"] = now_ts + int(min(max(dismissal, 0), MAX_DISMISSAL_SECONDS))
        aps["alert"] = {**ALERTS[stage], "sound": "default"}
    else:
        stale = _env_number("APNS_STALE_SECONDS", 1200) if stale_seconds is None else stale_seconds
        aps["stale-date"] = now_ts + int(stale)
    payload = {"aps": aps}
    if len(json.dumps(payload).encode()) > MAX_PAYLOAD_BYTES:  # cannot happen with the caps above; belt and braces
        content.pop("message", None)
    return payload


# --------------------------------------------------------------------------- client

@dataclass
class ApnsResult:
    ok: bool
    status: Optional[int] = None
    reason: Optional[str] = None
    dead_token: bool = False
    retryable: bool = False


class ApnsClient:
    """Minimal async APNs HTTP/2 sender. `send` never raises."""

    def __init__(self, config: ApnsConfig, jwt_provider: Optional[JwtProvider] = None, transport=None):
        self.config = config
        self.jwt = jwt_provider or JwtProvider(config.key_pem, config.key_id, config.team_id)
        self._transport = transport
        self._http: Optional[httpx.AsyncClient] = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=5.0)
            if self._transport is not None:
                self._http = httpx.AsyncClient(transport=self._transport, timeout=timeout)
            else:
                self._http = httpx.AsyncClient(http2=True, timeout=timeout)  # needs the `h2` package
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def send(
        self, device_token: str, environment: str, payload: Dict[str, Any], *,
        bundle_id: Optional[str] = None, now: Optional[float] = None,
    ) -> ApnsResult:
        try:
            return await asyncio.wait_for(
                self._send(device_token, environment, payload, bundle_id, now),
                timeout=REQUEST_TIMEOUT_SECONDS + 2.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # includes timeouts; class name only (messages may embed the token URL)
            return ApnsResult(ok=False, reason=type(error).__name__, retryable=True)

    async def _send(self, device_token, environment, payload, bundle_id, now) -> ApnsResult:
        if not TOKEN_RE.match(device_token or ""):
            return ApnsResult(ok=False, reason="MalformedToken", dead_token=True)
        host = HOSTS.get(environment)
        if host is None:
            return ApnsResult(ok=False, reason="UnknownEnvironment")
        aps = payload["aps"]
        terminal = aps.get("event") == "end"
        now_ts = int(now if now is not None else time.time())
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "apns-push-type": "liveactivity",
            "apns-topic": f"{bundle_id or self.config.bundle_id}.push-type.liveactivity",
            "apns-priority": "10" if terminal else "5",
            # A superseded progress update is worthless after a couple of minutes; the end event must land.
            "apns-expiration": str(now_ts + (3600 if terminal else 120)),
            "content-type": "application/json",
        }
        url = f"{host}/3/device/{device_token}"
        client = self._client()

        for attempt in (0, 1):
            provider_token = self.jwt.get()
            response = await client.post(url, content=body, headers={**headers, "authorization": f"bearer {provider_token}"})
            reason = None
            if response.status_code != 200:
                try:
                    reason = response.json().get("reason")
                except Exception:
                    reason = None
            if response.status_code == 200:
                return ApnsResult(ok=True, status=200)
            if response.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken") and attempt == 0:
                refreshed = self.jwt.refresh_after_rejection(provider_token)
                if refreshed != provider_token:
                    continue
            dead = response.status_code == 410 or reason in DEAD_TOKEN_REASONS
            retryable = response.status_code in (429, 500, 502, 503, 504)
            return ApnsResult(ok=False, status=response.status_code, reason=reason, dead_token=dead, retryable=retryable)
        return ApnsResult(ok=False, status=403, reason="ProviderTokenRejected")


# --------------------------------------------------------------------------- store

class SupabaseStore:
    """Default persistence: thin synchronous wrappers (called via asyncio.to_thread)."""

    def get_tokens(self, job_id: str) -> List[Dict[str, Any]]:
        import supabase_client
        return supabase_client.get_live_activity_tokens(job_id)

    def delete_token(self, job_id: str, token: str) -> None:
        import supabase_client
        supabase_client.delete_live_activity_token(job_id, token)

    def delete_tokens(self, job_id: str) -> None:
        import supabase_client
        supabase_client.delete_live_activity_tokens(job_id)

    def persist_stage(self, job_id: str, stage: str) -> None:
        import supabase_client
        supabase_client.update_job_stage(job_id, stage)


# --------------------------------------------------------------------------- notifier

@dataclass
class _Item:
    job_id: str
    stage: str
    progress: Optional[float]
    extra: Dict[str, Any]
    push: bool
    persist: bool


class LiveActivityNotifier:
    def __init__(
        self,
        client: Optional[ApnsClient],
        store=None,
        *,
        persist_stage: bool = True,
        progress_interval: Optional[float] = None,
        clock=time.monotonic,
    ):
        self.client = client
        self.store = store or SupabaseStore()
        self.persist_stage = persist_stage
        self.progress_interval = _env_number("APNS_PROGRESS_INTERVAL_SECONDS", 20.0) if progress_interval is None else progress_interval
        self._clock = clock
        self._state: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._stage_column_missing = False
        self._token_store_retry_at = 0.0
        self.dropped = 0

    @property
    def push_enabled(self) -> bool:
        return self.client is not None

    # ---- public, called from worker threads -------------------------------------------------

    def notify(self, job_id: str, stage: str, progress: Optional[float] = None, **extra: Any) -> None:
        """`progress` is a percentage 0-100 (same scale as update_job_progress) or None."""
        try:
            if stage not in STAGES or not job_id:
                return
            terminal = stage in TERMINAL_STAGES
            fraction = None if progress is None else float(progress) / 100.0
            push, persist = self._decide(str(job_id), stage, fraction, terminal)
            if not (push or persist):
                return
            self._submit(_Item(str(job_id), stage, fraction, extra, push, persist))
        except Exception as error:
            _log(f"notify ignored error: {type(error).__name__}")

    def flush(self, timeout: float = FLUSH_TIMEOUT_SECONDS) -> None:
        """Wait (bounded) until queued work is done; used at process exit."""
        try:
            if self._loop is None or self._queue is None:
                return
            asyncio.run_coroutine_threadsafe(self._queue.join(), self._loop).result(timeout)
        except Exception:
            pass

    # ---- throttling ------------------------------------------------------------------------

    def _decide(self, job_id: str, stage: str, fraction: Optional[float], terminal: bool):
        now = self._clock()
        with self._state_lock:
            st = self._state.get(job_id)
            push = False
            if self.push_enabled:
                if terminal:
                    push = True
                elif st is None or st["push_stage"] != stage:
                    push = True  # stage transition: always
                elif now - st["push_at"] >= self.progress_interval and fraction is not None and fraction != st["progress"]:
                    push = True  # in-stage progress: rate limited
            persist = self.persist_stage and not self._stage_column_missing and (st is None or st["persisted_stage"] != stage)
            if terminal:
                self._state.pop(job_id, None)
            else:
                if st is None:
                    if len(self._state) >= 1000:  # bounded memory; drop the oldest entry
                        self._state.pop(next(iter(self._state)), None)
                    st = self._state[job_id] = {"push_stage": None, "push_at": 0.0, "progress": None, "persisted_stage": None}
                if push:
                    st.update(push_stage=stage, push_at=now, progress=fraction)
                if persist:
                    st["persisted_stage"] = stage
            return push, persist

    # ---- background loop -------------------------------------------------------------------

    def _submit(self, item: _Item) -> None:
        self._ensure_started()
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        if queue.qsize() >= QUEUE_MAX and item.stage not in TERMINAL_STAGES:  # overflow drops progress, never the final event
            self.dropped += 1
            return
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def _ensure_started(self) -> None:
        if self._thread is not None:
            self._ready.wait(2.0)
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._thread_main, name="live-activity", daemon=True)
                self._thread.start()
        self._ready.wait(2.0)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._consume())
        except Exception as error:
            _log(f"background loop stopped: {type(error).__name__}")

    async def _consume(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready.set()
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except Exception as error:
                _log(f"item failed: {type(error).__name__}")
            finally:
                self._queue.task_done()

    async def _process(self, item: _Item) -> None:
        if item.persist:
            await self._persist(item)
        if not item.push:
            return
        rows = []
        if item.stage in TERMINAL_STAGES or time.monotonic() >= self._token_store_retry_at:  # the final alert always tries
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(self.store.get_tokens, item.job_id), 10.0)
            except Exception as error:
                self._token_store_retry_at = time.monotonic() + 60.0  # e.g. migration not applied: do not hammer
                _log(f"token lookup failed ({type(error).__name__}); pausing lookups 60s")
        if rows:
            payload = build_payload(
                item.stage, item.progress, chunk=item.extra.get("chunk"),
                total_chunks=item.extra.get("total_chunks"), message=item.extra.get("message"),
            )
            await asyncio.gather(*(self._send_row(item, row, payload) for row in rows), return_exceptions=True)
        if item.stage in TERMINAL_STAGES:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.store.delete_tokens, item.job_id), 10.0)
            except Exception as error:
                _log(f"token cleanup failed ({type(error).__name__})")

    async def _persist(self, item: _Item) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self.store.persist_stage, item.job_id, item.stage), 10.0)
        except Exception as error:
            text = str(error).lower()
            if "stage" in text and ("column" in text or "pgrst204" in text):
                self._stage_column_missing = True
                _log("transcription_jobs.stage column missing (run migration 007); stage persistence off")
            else:
                _log(f"stage persist failed ({type(error).__name__})")

    async def _send_row(self, item: _Item, row: Dict[str, Any], payload: Dict[str, Any]) -> None:
        token = row.get("token") or ""
        assert self.client is not None
        result = await self.client.send(
            token, row.get("environment") or "production", payload, bundle_id=row.get("bundle_id") or None,
        )
        if not result.ok and result.retryable and item.stage in TERMINAL_STAGES:
            await asyncio.sleep(2.0)  # the final alert is the one push worth a second try
            result = await self.client.send(
                token, row.get("environment") or "production", payload, bundle_id=row.get("bundle_id") or None,
            )
        if result.ok:
            return
        _log(f"job {item.job_id[:8]} token {_mask(token)} {item.stage}: {result.status} {result.reason}")
        if result.dead_token:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.store.delete_token, item.job_id, token), 10.0)
            except Exception as error:
                _log(f"token delete failed ({type(error).__name__})")


# --------------------------------------------------------------------------- module API

_notifier: Optional[LiveActivityNotifier] = None
_disabled = False
_notifier_lock = threading.Lock()


def _build_default() -> Optional[LiveActivityNotifier]:
    if os.getenv("LIVE_ACTIVITY_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return None
    config = ApnsConfig.from_env()
    client = None
    if config is not None:
        try:
            import h2  # noqa: F401  (httpx http2 extra)
            client = ApnsClient(config)
            _log(f"live activity pushes enabled (topic {config.bundle_id}.push-type.liveactivity)")
        except Exception as error:
            _log(f"disabled: cannot initialise client ({type(error).__name__})")
    else:
        _log("live activity pushes disabled (APNS_* not set)")
    notifier = LiveActivityNotifier(client)
    atexit.register(notifier.flush)
    return notifier


def get_notifier() -> Optional[LiveActivityNotifier]:
    global _notifier, _disabled
    if _notifier is None and not _disabled:
        with _notifier_lock:
            if _notifier is None and not _disabled:
                try:
                    _notifier = _build_default()
                except Exception as error:
                    _log(f"disabled: {type(error).__name__}")
                _disabled = _notifier is None
    return _notifier


def set_notifier(notifier: Optional[LiveActivityNotifier]) -> None:
    """Test hook / explicit wiring; None disables everything."""
    global _notifier, _disabled
    _notifier = notifier
    _disabled = notifier is None


def notify_stage(job_id: str, stage: str, progress: Optional[float] = None, **extra: Any) -> None:
    """Report a real stage transition (or in-stage progress, 0-100). Never raises, never blocks.

    stage: queued | preparing | transcribing | summarizing | done | failed
    extra: chunk=int, total_chunks=int, message=str (short, shown by the app if it wants)
    """
    try:
        notifier = get_notifier()
        if notifier is not None:
            notifier.notify(job_id, stage, progress, **extra)
    except Exception:
        pass
