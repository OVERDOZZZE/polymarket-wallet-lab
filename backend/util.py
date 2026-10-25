"""Small shared helpers: atomic files, clock offset, process checks, percentiles."""
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------- files (OneDrive/AV can briefly lock files)
def replace_retry(tmp, dst, tries=10):
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05 * (i + 1))


def write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")      # per process: parallel writers never collide
    tmp.write_text(text, encoding="utf-8")
    replace_retry(tmp, path)


def write_df_atomic(df, path, row_group_size=None):
    """CSV or parquet by extension."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix == ".parquet":
        df.to_parquet(tmp, index=False, compression="zstd", row_group_size=row_group_size)
    else:
        df.to_csv(tmp, index=False)
    replace_retry(tmp, path)


def read_parquet_where(path, column, value):
    """Rows with column == value, reading only row groups whose min/max stats can contain it.
    (pyarrow.dataset - the usual way to filter - is blocked by Windows Application Control on this PC.)"""
    import pandas as pd
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    ci = pf.schema_arrow.get_field_index(column)
    parts = []
    for i in range(pf.num_row_groups):
        st = pf.metadata.row_group(i).column(ci).statistics
        if st is not None and st.has_min_max and not (st.min <= value <= st.max):
            continue
        df = pf.read_row_group(i).to_pandas()
        parts.append(df[df[column] == value])
    return pd.concat(parts, ignore_index=True) if parts else pf.schema_arrow.empty_table().to_pandas()


def read_json(path, default=None):
    for _ in range(3):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (ValueError, PermissionError, OSError):
            time.sleep(0.02)          # caught mid-replace
    return default


# ---------------------------------------------------------------- time
NTP_HOSTS = (("169.254.169.123", 0.3), ("pool.ntp.org", 2.0))   # AWS Time Sync (link-local, sub-ms) first


def _ntp_sample(host, timeout):
    """(offset ms, round trip ms) from one SNTP query, or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        data, _ = s.recvfrom(48)
        t3 = time.time()
        s.close()
        sec, frac = struct.unpack("!II", data[40:48])
        server = sec - 2208988800 + frac / 2 ** 32
        return (server - (t0 + t3) / 2) * 1000.0, (t3 - t0) * 1000.0
    except Exception:
        return None


def ntp_offset_ms(samples=3):
    """Local clock error in ms (true_time = local_time + offset). None if no time server answers. Uses the first
    host that answers (AWS's local time service on EC2, else pool.ntp.org) and, of a few queries, the one with
    the shortest round trip (the least network noise)."""
    for host, timeout in NTP_HOSTS:
        got = [x for x in (_ntp_sample(host, timeout) for _ in range(samples)) if x]
        if got:
            return min(got, key=lambda x: x[1])[0]
    return None


def pct(seq, q):
    if not seq:
        return None
    s = sorted(seq)
    return s[min(len(s) - 1, int(q * len(s)))]


# ---------------------------------------------------------------- processes
def pid_alive(p):
    if not p:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(p))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259             # STILL_ACTIVE
    try:
        os.kill(int(p), 0)
        return True
    except OSError:
        return False


def kill_pid(p):
    if not pid_alive(p):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"], capture_output=True)   # /T: venv launcher + child
        return
    # SIGTERM first: systemd's Restart=on-failure takes it as a clean stop, but a SIGKILL as a crash and restarts
    for sig, wait_s in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 0)):
        try:
            os.kill(int(p), sig)
        except OSError:
            return
        t0 = time.time()
        while time.time() - t0 < wait_s:
            time.sleep(0.1)
            if not pid_alive(p):
                return


def high_priority():
    """Windows: HIGH_PRIORITY_CLASS. Linux: raising priority needs root - the systemd unit sets Nice= instead."""
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00000080)   # HIGH_PRIORITY_CLASS
        except Exception:
            pass


def spawn(args, cwd, log_path):
    """Start a detached background Python process (no console window), output appended to log_path."""
    flags = 0x00000200 | 0x08000000 if sys.platform == "win32" else 0   # NEW_PROCESS_GROUP | NO_WINDOW
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.flush()
    return subprocess.Popen([sys.executable, *args], cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            start_new_session=sys.platform != "win32")   # Linux: outlive a server restart
