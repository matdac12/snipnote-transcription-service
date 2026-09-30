"""Server-side minutes debit for worker-processed jobs (audit B). OFF unless
SERVER_MINUTES_DEBIT_ENABLED=true.

Why: transcription done by the worker used to be free. The app only checked the balance and
debited solely on its on-device paths, so a user could queue unlimited server hours.

How (all keyed by meeting_id, the SAME key as the app's `debit_minutes(p_meeting_id)`, so the
app's on-device fallback and this worker can never both charge one meeting):
  1. With the results, the worker stores `billable_seconds` on the job (measured with ffprobe
     whenever possible, never the old len(bytes)/32000 guess when a better number exists).
  2. Right after the results are saved it calls the service-role-only RPC
     `debit_minutes_for_job` (migration 011). The RPC is idempotent, clamps at zero and records
     minutes_debited / debited_at / debit_status on the job.
  3. A failure is logged and NEVER fails the job; `sweep_undebited` (every worker loop, throttled)
     retries completed jobs that still have billable_seconds but no debited_at.
  Minutes = max(1, ceil(seconds / 60)), identical to the app's rounding.

Env (read at call time):
  SERVER_MINUTES_DEBIT_ENABLED                 default false
  SERVER_MINUTES_DEBIT_SWEEP_INTERVAL_SECONDS  default 300
  SERVER_MINUTES_DEBIT_SWEEP_LOOKBACK_HOURS    default 72   only jobs completed this recently are swept
  SERVER_MINUTES_DEBIT_SWEEP_BATCH             default 25
"""
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import supabase_client as db


def debit_enabled() -> bool:
    return os.getenv('SERVER_MINUTES_DEBIT_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, '') or default)
    except ValueError:
        return default
    return value if value > 0 else default


def minutes_for_seconds(seconds: Optional[float]) -> int:
    """max(1, ceil(seconds / 60)); 0 when there is nothing to bill (unknown / non-positive)."""
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return 0
    return max(1, math.ceil(value / 60)) if value > 0 and math.isfinite(value) else 0


def choose_billing_seconds(measured: Optional[float], fallbacks: Iterable[Optional[float]] = ()) -> Optional[float]:
    """Best available audio length: the measured one, else the first positive fallback
    (client-reported duration, then the byte-size estimate). Never negative."""
    for value in (measured, *fallbacks):
        try:
            if value and float(value) > 0 and math.isfinite(float(value)):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def debit_job(job_id: str, user_id: str, meeting_id: str, seconds: Optional[float], provider: Optional[str]) -> Optional[Dict[str, Any]]:
    """Debit one finished job. Idempotent, NEVER raises (a billing problem must not fail a finished job)."""
    if not debit_enabled():
        return None
    minutes = minutes_for_seconds(seconds)
    if minutes <= 0:
        print(f'   💳 Job {job_id}: no billable duration, nothing debited')
        return None
    try:
        result = db.debit_minutes_for_job(user_id, meeting_id, job_id, minutes, seconds, provider)
        print(f'   💳 Job {job_id}: {result.get("status", "?")} ({result.get("minutes_debited", "?")} of {minutes} min)')
        return result
    except Exception as error:
        # Sanitised: class + message only (no payload); the sweep retries.
        print(f'   ⚠️ Job {job_id}: minutes debit failed ({type(error).__name__}: {str(error)[:200]}); the sweep will retry')
        return None


_sweep_lock = threading.Lock()
_last_sweep = 0.0


def sweep_undebited(now: Optional[datetime] = None, force: bool = False) -> Dict[str, int]:
    """Retry debits of completed jobs that have billable_seconds but no debited_at.

    Throttled (SERVER_MINUTES_DEBIT_SWEEP_INTERVAL_SECONDS) and never raises. Only jobs the
    worker itself finished are candidates: rows the app saved for on-device transcriptions have
    billable_seconds NULL, and jobs completed before this feature existed are excluded by the
    lookback window."""
    global _last_sweep
    stats = {'checked': 0, 'debited': 0, 'errors': 0}
    if not debit_enabled():
        return stats
    with _sweep_lock:
        if not force and time.monotonic() - _last_sweep < _env_number('SERVER_MINUTES_DEBIT_SWEEP_INTERVAL_SECONDS', 300):
            return stats
        _last_sweep = time.monotonic()
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=_env_number('SERVER_MINUTES_DEBIT_SWEEP_LOOKBACK_HOURS', 72))).isoformat()
    try:
        jobs = db.list_undebited_completed_jobs(since, int(_env_number('SERVER_MINUTES_DEBIT_SWEEP_BATCH', 25)))
    except Exception as error:
        print(f'⚠️ minutes sweep: could not list jobs ({type(error).__name__}: {str(error)[:200]})')
        stats['errors'] += 1
        return stats
    for job in jobs:
        stats['checked'] += 1
        result = debit_job(job['id'], job['user_id'], job['meeting_id'], job.get('billable_seconds'), job.get('transcription_provider'))
        if result is None:
            stats['errors'] += 1
        else:
            stats['debited'] += 1
    if stats['checked']:
        print(f'💳 minutes sweep: {stats}')
    return stats
