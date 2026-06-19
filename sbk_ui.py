"""
Surgical Bug Sniper — sbk_ui.py
Streamlit 1.37+ · Fragment-scoped live refresh · Zero full-page rerenders during hunt
"""

import streamlit as st
import subprocess, os, sys, time, re, psutil, collections, requests
from dotenv import load_dotenv, dotenv_values
import db

load_dotenv(override=True)

st.set_page_config(
    page_title="Surgical Bug Sniper",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── One-time session init ─────────────────────────────────────────────────────
if "session_started" not in st.session_state:
    st.session_state.session_started = True
    st.session_state.process_pid     = None
    st.session_state.last_log_hash   = ""
    db.init_db()   # ensure sniper.db + tables exist on first load
    try:
        open("sbk_run.log", "w", encoding="utf-8").close()
    except Exception:
        pass

# ── CSS — injected on every rerun to ensure visual sync ───────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Michroma&family=Share+Tech+Mono&display=swap');

/* ── Chrome ── */
#MainMenu, footer, header { visibility: hidden }
html, body, [class*="css"] { background: #000000 !important }
.block-container { padding-top: 2rem; padding-bottom: 1rem; max-width: 1060px }
* { font-family: 'Helvetica Neue', Arial, sans-serif }

/* ── Title ── */
.title-wrap { margin-bottom: 0.15rem }
.title-text {
  font-family: 'Michroma', sans-serif;
  font-size: 1.9rem; color: #ffffff;
  text-transform: uppercase; letter-spacing: 4px;
  margin: 0; padding: 0;
}
.subtitle-text {
  color: #444444; font-size: .7rem; letter-spacing: 5px;
  text-transform: uppercase; margin-top: .25rem; margin-bottom: 1.8rem;
}
.accent { color: #ffffff; font-family: 'Michroma', sans-serif; font-weight: normal; }

/* ── Step Tracker ── */
.tracker-container {
  position: relative;
  margin: 0 0 2rem 0;
  padding-top: 28px;
}
.tracker-progress-bg {
  position: absolute;
  top: 39px;
  left: 9%;
  right: 9%;
  height: 1px;
  background: #222222;
  z-index: 1;
}
.tracker-progress-fill {
  position: absolute;
  top: 39px;
  left: 9%;
  height: 1px;
  background: #ffffff;
  z-index: 1;
  transition: width 0.6s ease-in-out;
}
.tracker {
  display: flex; justify-content: space-between; align-items: flex-start;
  position: relative; z-index: 2;
}
.step-wrap {
  display: flex; flex-direction: column; align-items: center;
  z-index: 2; flex: 1;
}
.step-dot {
  width: 22px; height: 22px; border-radius: 50%;
  border: 1.5px solid #222222; background: #000000;
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 900; color: transparent;
  margin-bottom: 9px;
  transition: all 0.4s ease;
}
.step-dot.done  {
  background: #ffffff; border-color: #ffffff; color: #000000;
}
.step-dot.active{
  background: #000000; border-color: #ffffff; border-width: 2px;
  color: #ffffff;
}
.step-dot.pulse { animation: pulse-ring 1.4s ease-in-out infinite }
@keyframes pulse-ring {
  0%   { box-shadow: 0 0 0 0 rgba(255,255,255,.4) }
  70%  { box-shadow: 0 0 0 8px rgba(255,255,255,0) }
  100% { box-shadow: 0 0 0 0 rgba(255,255,255,0)  }
}
.step-label {
  font-family: 'Michroma', sans-serif;
  font-size: .6rem; font-weight: 700; letter-spacing: 2px;
  text-transform: uppercase; color: #333333; text-align: center;
}
.step-wrap.done   .step-label { color: #ffffff; font-weight: bold; }
.step-wrap.active .step-label { color: #ffffff }

/* ── Buttons ── */
.stButton > button {
  width: 100%; background: #ffffff; color: #000000; border: none;
  border-radius: 0; padding: .8rem .5rem;
  font-weight: 900; font-family: 'Michroma', sans-serif;
  letter-spacing: 2px; text-transform: uppercase;
  font-size: .65rem;
  transition: background 0.2s ease;
}
.stButton > button:hover { background: #cccccc }
.abort-btn > button { background: #000000; color: #ffffff; border: 1px solid #ffffff }
.abort-btn > button:hover { background: #ffffff; color: #000000 }

/* ── Log Feed ── */
.feed-wrap {
  background: #000000;
  border: 1px solid #222222;
  padding: 14px 16px;
  max-height: 620px;
  overflow-y: auto;
  font-family: 'Share Tech Mono', 'Courier New', monospace;
  font-size: .72rem;
  line-height: 1.55;
  scroll-behavior: smooth;
}
.ll-base  { display:block; white-space:pre-wrap; word-break:break-all; padding: 1px 0; }
.ll-ok    { color: #4ade80; }   /* green  — success */
.ll-fail  { color: #f87171; }   /* red    — failure */
.ll-think { color: #fbbf24; }   /* amber  — LLM reasoning */
.ll-patch { color: #a78bfa; }   /* violet — patch summary */
.ll-diff-hdr  { color: #60a5fa; padding: 3px 0 1px 0; }  /* blue — diff box header/footer */
.ll-diff-rem  { color: #f87171; background: #1c0a0a; display:block; white-space:pre-wrap; padding:0 4px; }
.ll-diff-add  { color: #4ade80; background: #071c0f; display:block; white-space:pre-wrap; padding:0 4px; }
.ll-diff-ctx  { color: #555555; display:block; white-space:pre-wrap; padding:0 4px; }
.ll-info  { color: #888888; }   /* grey   — neutral info */
.ll-ts    { color: #333333; }   /* dimmed — timestamps */
.ll-phase { color: #ffffff; font-weight: bold; margin-right: 4px; }
.ll-sep   { color: #333333; display:block; }

.stCodeBlock, pre {
  background: #000000 !important;
  border: 1px solid #222222; border-radius: 0;
  font-size: .72rem !important;
  font-family: 'Share Tech Mono', 'Courier New', monospace !important;
}
code { color: #888888 !important }
hr  { border-color: #222222 }

.feed-label {
  font-family: 'Michroma', sans-serif;
  font-size: .6rem; letter-spacing: 3px; color: #444444;
  text-transform: uppercase; margin-bottom: .4rem;
}
.status-badge {
  display: inline-block;
  font-family: 'Michroma', sans-serif; font-size: .6rem;
  letter-spacing: 2px; text-transform: uppercase;
  padding: 2px 10px; border-radius: 0; margin-left: 12px;
}
.badge-running { background: #000000; color: #ffffff; border: 1px solid #ffffff }
.badge-idle    { background: #000000; color: #444444; border: 1px solid #222222 }
.badge-done    { background: #ffffff; color: #000000 }

</style>
""", unsafe_allow_html=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
LOG_FILE = "sbk_run.log"


def _tail_log(n: int) -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return list(collections.deque(f, maxlen=n))
    except Exception:
        return []


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ── Phase colour mapping ──────────────────────────────────────────────────────
_PHASE_CLASS = {
    "HUNT"    : "ll-ok",
    "TARGET"  : "ll-ok",
    "FETCH"   : "ll-info",
    "FIX"     : "ll-ok",
    "THINK"   : "ll-think",
    "PATCH"   : "ll-patch",
    "DIFF"    : "ll-diff-hdr",
    "VERIFY"  : "ll-ok",
    "PUSH"    : "ll-ok",
    "PR"      : "ll-ok",
    "EMBED"   : "ll-info",
    "INIT"    : "ll-info",
    "BUG"     : "ll-think",
    "RATE"    : "ll-fail",
    "DONE"    : "ll-ok",
    "DRY-RUN" : "ll-think",
    "ABORT"   : "ll-fail",
    "ERROR"   : "ll-fail",
    "TRIAGE"  : "ll-info",
    "STYLE"   : "ll-info",
    "TEST"    : "ll-patch",
}

# Lines whose phase should force red styling regardless of ✓/✗
_ALWAYS_FAIL = {"RATE", "ABORT", "ERROR"}


def _render_line(raw: str) -> str:
    """
    Convert one log line into a styled HTML span.
    Handles:
      • Normal emit lines:  " PHASE    [sym] [HH:MM:SS] message"
      • DIFF box lines:     " DIFF      │ - old code" / " DIFF      │ + new code"
      • DIFF borders:       " DIFF      ┌─..."
      • PATCH summary:      " PATCH      ✎  [file]  ..."
      • Section separators: "═" or "─"
    """
    line = ANSI.sub("", raw).rstrip("\n\r")
    if not line.strip():
        return "<br>"

    # ─── Section separator lines ────────────────────────────────────
    if line.strip().startswith("═") or line.strip().startswith("─"):
        return f'<span class="ll-sep ll-base">{_esc(line)}</span>'

    # ─── DIFF box lines ────────────────────────────────────────
    if " DIFF " in line:
        # Header / footer of the box  (┌─ filename  OR  └─────)
        if "┌" in line or "└" in line or ("│" in line and " - " not in line and " + " not in line):
            return f'<span class="ll-diff-hdr ll-base">{_esc(line)}</span>'
        # Removed line
        if "│ -" in line:
            code = _esc(line.split("│ -", 1)[1]) if "│ -" in line else _esc(line)
            return f'<span class="ll-diff-rem">  − {code}</span>'
        # Added line
        if "│ +" in line:
            code = _esc(line.split("│ +", 1)[1]) if "│ +" in line else _esc(line)
            return f'<span class="ll-diff-add">  + {code}</span>'
        # Context / ellipsis
        return f'<span class="ll-diff-ctx">{_esc(line)}</span>'

    # ─── Standard emit line: " PHASE    sym [ts] msg" ────────────────────────
    # Strip leading space, split on whitespace up to 3 tokens: PHASE, SYM, rest
    stripped = line.lstrip()
    parts    = stripped.split(None, 2)       # ["PHASE", "sym_or_[ts]", "rest..."]
    if len(parts) < 2:
        return f'<span class="ll-info ll-base">{_esc(line)}</span>'

    phase = parts[0].rstrip(":")
    rest  = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Extract timestamp [HH:MM:SS] if present
    ts_html = ""
    ts_match = re.match(r"([✓✗ ]?)\s*(\[\d{2}:\d{2}:\d{2}\])\s*(.*)", rest, re.DOTALL)
    if ts_match:
        sym, ts, msg = ts_match.group(1), ts_match.group(2), ts_match.group(3)
        ts_html = f'<span class="ll-ts">{_esc(ts)} </span>'
    else:
        sym_match = re.match(r"([✓✗ ]?)\s*(.*)", rest, re.DOTALL)
        sym = sym_match.group(1) if sym_match else " "
        msg = sym_match.group(2) if sym_match else rest

    # Choose colour class
    if sym == "✓" and phase not in _ALWAYS_FAIL:
        cls = "ll-ok"
    elif sym == "✗" or phase in _ALWAYS_FAIL:
        cls = "ll-fail"
    else:
        cls = _PHASE_CLASS.get(phase, "ll-info")

    sym_html = f'<span style="margin-right:4px">{_esc(sym)}</span>' if sym.strip() else ""
    phase_html = f'<span class="ll-phase" style="min-width:54px;display:inline-block">{_esc(phase)}</span>'
    msg_html   = f'<span class="{cls}">{_esc(msg)}</span>'

    return f'<span class="ll-base">{phase_html} {sym_html}{ts_html}{msg_html}</span>'


def render_feed_html(lines: list) -> str:
    """Turn the full list of log lines into a scrollable styled HTML block."""
    if not lines:
        return '<div class="feed-wrap"><span class="ll-info">No active feed.</span></div>'
    inner = "\n".join(_render_line(l) for l in lines)
    # Auto-scroll anchor
    return (
        f'<div class="feed-wrap" id="log-feed">{inner}'
        f'<span id="feed-bottom"></span></div>'
        f'<script>document.getElementById("feed-bottom")?.scrollIntoView({{behavior:"smooth"}});</script>'
    )


def kill_hunt():
    pid = st.session_state.get("process_pid")
    if pid:
        try:
            p = psutil.Process(pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except psutil.NoSuchProcess:
            pass
        st.session_state.process_pid = None
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("\n[ ABORT ] OPERATION TERMINATED BY USER.\n")
        except Exception:
            pass


def current_step(lines: list) -> int:
    for line in reversed(lines):
        line = line.upper()
        if " PUSH " in line or " PR " in line or "[ PUSH ]" in line: return 5
        if " VERIFY " in line or "[ VERIFY ]" in line: return 4
        if " FIX " in line or " THINK " in line or " BUG " in line or "[ SURGERY ]" in line: return 3
        if " CLONE " in line or "[ CLONE ]" in line: return 2
        if " HUNT " in line or "[ HUNT ]" in line: return 1
    return 0


def is_done(lines: list) -> bool:
    return any("MISSION COMPLETE" in line for line in lines)


def tracker_html(step: int, running: bool) -> str:
    STEPS = ["Hunt", "Clone", "Analyze", "Verify", "Commit"]
    # Calculate progress width percentage (from Hunt [step 1] to Commit [step 5])
    if step <= 1:
        progress_pct = 0
    elif step >= 5:
        progress_pct = 100
    else:
        progress_pct = int(((step - 1) / 4) * 100)

    html = f'<div class="tracker-container"><div class="tracker-progress-bg"></div><div class="tracker-progress-fill" style="width: {progress_pct}%;"></div><div class="tracker">'
    for i, label in enumerate(STEPS):
        idx = i + 1
        if idx < step:
            cls, dot_cls, mark = "done", "done", "✓"
        elif idx == step:
            pulse = " pulse" if running else ""
            cls, dot_cls, mark = "active", f"active{pulse}", ""
        else:
            cls, dot_cls, mark = "", "", ""
        html += f'<div class="step-wrap {cls}"><div class="step-dot {dot_cls}">{mark}</div><div class="step-label">{label}</div></div>'
    html += "</div></div>"
    return html


# ── Static header — only runs on full page load ───────────────────────────────
st.markdown('<div class="title-wrap">'
            '<p class="title-text">Surgical Bug <span class="accent">Sniper</span></p>'
            '</div>', unsafe_allow_html=True)
st.markdown('<p class="subtitle-text">Autonomous · Local · Parallel Hunt · Surgical Fix · Auto PR</p>',
            unsafe_allow_html=True)

# ── Controls ──────────────────────────────────────────────────────────────────
col_cap, col_btn = st.columns([5, 1])
with col_cap:
    st.caption("Scans LangGraph · CrewAI · LlamaIndex · Qdrant · Ollama · vLLM in parallel")

pid = st.session_state.get("process_pid")
with col_btn:
    if pid is None:
        if st.button("FIRE", key="hunt_btn"):
            with open(LOG_FILE, "w", encoding="utf-8") as _f:
                _f.write("DEPLOYING...\n")
            fresh_env = {
                **os.environ,
                **dotenv_values(".env"),
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "TQDM_DISABLE": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "sbk.py"],
                stdout=open(LOG_FILE, "a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                env=fresh_env,
            )
            st.session_state.process_pid = proc.pid
            st.rerun()
    else:
        st.markdown('<div class="abort-btn">', unsafe_allow_html=True)
        if st.button("ABORT", key="abort_btn"):
            kill_hunt()
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

st.markdown("---")

# ── Live Feed Fragment ────────────────────────────────────────────────────────
# @st.fragment is a Streamlit 1.37+ feature.
# - When called from INSIDE the fragment, st.rerun() reruns ONLY the fragment
#   (not the full page), so the header/CSS/buttons are untouched.
# - We self-schedule via time.sleep(0.7) + st.rerun() inside the fragment
#   rather than using run_every= so we can stop refreshing when idle.
# - Only 2 DOM elements update per tick: the tracker HTML + the code block.
@st.fragment
def live_feed():
    current_pid = st.session_state.get("process_pid")

    # ── Detect process death ──────────────────────────────────────────────────
    if current_pid is not None and not psutil.pid_exists(current_pid):
        st.session_state.process_pid = None
        st.rerun(scope="app")   # full page rerun once to swap ABORT → FIRE button

    running  = current_pid is not None
    # Single file-read per tick: feed all helpers from this one list
    log_lines = _tail_log(300)
    step     = current_step(log_lines)
    done     = is_done(log_lines)

    # ── Step tracker ──────────────────────────────────────────────────────────
    st.markdown(tracker_html(step, running), unsafe_allow_html=True)

    # ── Contribution Metrics (from SQLite db) ───────────────────────────────
    m = db.get_stats()
    import metrics as _m
    mc = _m.get_metrics()   # still read issues_scanned from metrics.json
    metrics_html = f"""
    <div style="border: 1px solid #222222; padding: 12px 20px; background-color: #000000; margin: 1rem 0 1.2rem 0;">
        <div style="font-family: 'Michroma', sans-serif; font-size: 0.65rem; color: #ffffff; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px; border-bottom: 1px solid #222222; padding-bottom: 6px;">Contribution Metrics</div>
        <div style="display: flex; justify-content: space-between; font-family: 'Share Tech Mono', monospace; font-size: 0.8rem; flex-wrap: wrap; gap: 10px;">
            <div><span style="color: #888888;">Issues Scanned:</span> <span style="color: #ffffff; font-weight: bold;">{mc.get('issues_scanned', 0)}</span></div>
            <div><span style="color: #888888;">PRs Opened:</span> <span style="color: #ffffff; font-weight: bold;">{m['prs_opened']}</span></div>
            <div><span style="color: #888888;">PRs Merged:</span> <span style="color: #ffffff; font-weight: bold;">{m['prs_merged']}</span></div>
            <div><span style="color: #888888;">Total Runs:</span> <span style="color: #ffffff; font-weight: bold;">{m['total_runs']}</span></div>
        </div>
    </div>
    """
    st.markdown(metrics_html, unsafe_allow_html=True)



    # ── Feed header ──────────────────────────────────────────────────────────

    badge_cls = "badge-done" if done else ("badge-running" if running else "badge-idle")
    badge_txt = "COMPLETE" if done else ("RUNNING" if running else "IDLE")
    st.markdown(
        f'<p class="feed-label">Live Operation Feed'
        f'<span class="status-badge {badge_cls}">{badge_txt}</span></p>',
        unsafe_allow_html=True
    )

    # ── Log ───────────────────────────────────────────────────────────────────
    st.markdown(render_feed_html(log_lines), unsafe_allow_html=True)

    # ── Self-refresh while running ─────────────────────────────────────────────
    if running:
        time.sleep(0.7)          # 0.7s tick ─ smooth without hammering CPU
        st.rerun()               # fragment-only rerun (Streamlit 1.37+)


live_feed()
