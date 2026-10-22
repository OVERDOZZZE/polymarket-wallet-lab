"""Background jobs (trader download / analysis). Each job runs in its OWN PROCESS so pandas work never
holds the API server's GIL - order sending stays fast while an analysis runs. One running job per trader.

Progress comes back over a multiprocessing queue; cancel is a multiprocessing Event checked by the pipeline.
"""
import multiprocessing as mp
import threading
import time
import traceback
import uuid

_ctx = mp.get_context("spawn")


class _ChildJob:
    """The `job` object the pipeline sees inside the child process."""

    def __init__(self, q, cancel_event):
        self.q, self.cancel_event = q, cancel_event

    def log(self, msg):
        self.q.put(("log", f"{time.strftime('%H:%M:%S')}  {msg}"))

    def progress(self, frac, stage=""):
        self.q.put(("progress", float(frac), stage))

    def check_cancel(self):
        if self.cancel_event.is_set():
            from .traders.sources import Cancelled
            raise Cancelled()


def _child(q, cancel_event, tid, kind, kwargs):
    from .traders import pipeline, store
    from .traders.sources import Cancelled
    from .util import write_text_atomic
    job = _ChildJob(q, cancel_event)
    status, err, tb = "done", "", ""
    logs = []
    orig_log = job.log

    def log(msg):
        logs.append(msg)
        orig_log(msg)
    job.log = log
    try:
        pipeline.run_update(tid, job=job, **kwargs)
        job.log("finished")
    except Cancelled:
        status = "cancelled"
        job.log("cancelled - everything downloaded so far is kept")
    except Exception as e:
        status, err, tb = "error", f"{type(e).__name__}: {e}", traceback.format_exc()
        job.log(f"ERROR {err}")
    try:
        t = store.Trader.load(tid)
        write_text_atomic(t.log_path, f"{kind} job {status}\n"
                          + "\n".join(logs) + (f"\n\n{tb}" if tb else ""))
    except Exception:
        pass
    q.put(("end", status, err, tb))


class Job:
    def __init__(self, trader_id, trader_name, kind):
        self.id = uuid.uuid4().hex[:8]
        self.trader_id, self.trader_name, self.kind = trader_id, trader_name, kind
        self.status = "running"          # running | done | error | cancelled
        self.frac, self.stage = 0.0, "Starting"
        self.logs = []
        self.error = self.tb = ""
        self.started, self.finished = time.time(), None
        self.cancel_event = _ctx.Event()
        self.proc = None

    @property
    def active(self):
        return self.status == "running"

    def cancel(self):
        self.cancel_event.set()

    def to_dict(self):
        return {"id": self.id, "trader_id": self.trader_id, "trader_name": self.trader_name, "kind": self.kind,
                "status": self.status, "frac": self.frac, "stage": self.stage, "error": self.error,
                "started": self.started, "finished": self.finished,
                "elapsed": (self.finished or time.time()) - self.started, "logs": self.logs[-60:]}


class JobManager:
    def __init__(self):
        self.jobs = []
        self.lock = threading.Lock()

    def running_for(self, tid):
        return next((j for j in self.jobs if j.trader_id == tid and j.active), None)

    def latest_for(self, tid):
        return next((j for j in reversed(self.jobs) if j.trader_id == tid), None)

    def get(self, jid):
        return next((j for j in self.jobs if j.id == jid), None)

    def start(self, trader, kind, **kwargs):
        """kind: 'download' (kwargs start, end) or 'analysis' (fetch=False)."""
        with self.lock:
            if self.running_for(trader.id):
                raise RuntimeError(f"A job is already running for '{trader.name}'.")
            job = Job(trader.id, trader.name, kind)
            self.jobs.append(job)
            del self.jobs[:-50]
        q = _ctx.Queue()
        job.proc = _ctx.Process(target=_child, args=(q, job.cancel_event, trader.id, kind, kwargs), daemon=True,
                                name=f"job-{job.id}")
        job.proc.start()
        threading.Thread(target=self._pump, args=(job, q), daemon=True, name=f"job-pump-{job.id}").start()
        return job

    def _pump(self, job, q):
        while True:
            try:
                msg = q.get(timeout=1.0)
            except Exception:
                if not job.proc.is_alive():
                    if job.active:
                        job.status, job.error = "error", f"job process exited (code {job.proc.exitcode})"
                        job.finished = time.time()
                    return
                continue
            if msg[0] == "log":
                job.logs.append(msg[1])
                del job.logs[:-500]
            elif msg[0] == "progress":
                job.frac = max(0.0, min(1.0, msg[1]))
                job.stage = msg[2] or job.stage
            elif msg[0] == "end":
                job.status, job.error, job.tb = msg[1], msg[2], msg[3]
                job.finished = time.time()
                job.proc.join(timeout=5)
                return

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j.active]
