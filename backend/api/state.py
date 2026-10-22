"""Process-wide singletons of the API server (created once in app.py's lifespan)."""
from ..jobs import JobManager
from ..traders.live import LiveHub

jobs = JobManager()
hub = LiveHub()


def records(df):
    """DataFrame -> list of dicts with NaN/NaT as None (JSON-safe)."""
    if df is None:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")
