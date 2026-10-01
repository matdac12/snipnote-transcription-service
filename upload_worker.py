"""Lightweight separate process; never invokes a paid transcription provider."""
import signal
import threading
from upload_reconciler import reconcile_uploads

def main():
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    while not stop.is_set():
        try:
            report=reconcile_uploads()
            print(f'Upload reconciliation: checked={report.checked} queued={report.queued} expired={report.expired} errors={report.errors}',flush=True)
        except Exception:print('Upload reconciliation unavailable; retrying',flush=True)
        stop.wait(20)

if __name__=='__main__':main()
