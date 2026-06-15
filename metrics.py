import os
import json
import threading

METRICS_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_FILE = os.path.join(METRICS_DIR, "metrics.json")
_lock = threading.Lock()

DEFAULT_METRICS = {
    "issues_scanned": 0,
    "issues_attempted": 0,
    "prs_opened": 0,
    "prs_merged": 0,
    "prs_closed": 0
}

def load_metrics() -> dict:
    """Load metrics from metrics.json, initializing if necessary."""
    with _lock:
        return _load_metrics_unlocked()

def _load_metrics_unlocked() -> dict:
    if not os.path.exists(METRICS_FILE):
        _save_metrics_unlocked(DEFAULT_METRICS)
        return DEFAULT_METRICS.copy()
    try:
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Ensure all expected keys are present
            for k, v in DEFAULT_METRICS.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        return DEFAULT_METRICS.copy()

def save_metrics(metrics: dict):
    """Save metrics dict to metrics.json."""
    with _lock:
        _save_metrics_unlocked(metrics)

def _save_metrics_unlocked(metrics: dict):
    try:
        with open(METRICS_FILE, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
    except Exception:
        pass

def increment_metric(name: str, count: int = 1):
    """Increment the value of a specific metric key."""
    with _lock:
        metrics = _load_metrics_unlocked()
        if name in metrics:
            metrics[name] += count
        else:
            metrics[name] = count
        _save_metrics_unlocked(metrics)

def get_metrics() -> dict:
    """Return the current metrics dictionary."""
    return load_metrics()
