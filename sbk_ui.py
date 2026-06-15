"""
Surgical Bug Sniper — sbk_ui.py
Streamlit 1.37+ · Fragment-scoped live refresh · Zero full-page rerenders during hunt
"""

import streamlit as st
import subprocess, os, sys, time, re, psutil
from dotenv import load_dotenv, dotenv_values

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


def get_logs(n: int = 100) -> str:
    if not os.path.exists(LOG_FILE):
        return "No active feed."
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        return "".join(ANSI.sub("", l) for l in lines) or "Initializing..."
    except Exception:
        return "Error reading log."


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


def current_step() -> int:
    if not os.path.exists(LOG_FILE):
        return 0
    try:
        text = open(LOG_FILE, encoding="utf-8", errors="replace").read()
    except Exception:
        return 0
    step = 0
    if "[ HUNT ]"    in text: step = max(step, 1)
    if "[ CLONE ]"   in text: step = max(step, 2)
    if "[ SURGERY ]" in text or "[ BUG ]" in text: step = max(step, 3)
    if "[ VERIFY ]"  in text: step = max(step, 4)
    if "[ PUSH ]"    in text or "[ PR ]" in text: step = max(step, 5)
    return step


def is_done() -> bool:
    try:
        return "MISSION COMPLETE" in open(LOG_FILE, encoding="utf-8", errors="replace").read()
    except Exception:
        return False


def tracker_html(step: int, running: bool) -> str:
    STEPS = ["Hunt", "Clone", "Fixing", "Verify", "Commit"]
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
    st.caption("🎯 Scans LangGraph · CrewAI · LlamaIndex · Qdrant · Ollama · vLLM in parallel")

pid = st.session_state.get("process_pid")
with col_btn:
    if pid is None:
        if st.button("▶  FIRE", key="hunt_btn"):
            open(LOG_FILE, "w", encoding="utf-8").write("DEPLOYING...\n")
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
        if st.button("🛑  ABORT", key="abort_btn"):
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
    step     = current_step()
    done     = is_done()

    # ── Step tracker ──────────────────────────────────────────────────────────
    st.markdown(tracker_html(step, running), unsafe_allow_html=True)

    # ── Contribution Metrics ──────────────────────────────────────────────────
    import metrics
    m = metrics.get_metrics()
    metrics_html = f"""
    <div style="border: 1px solid #222222; padding: 12px 20px; background-color: #000000; margin: 1rem 0 2rem 0;">
        <div style="font-family: 'Michroma', sans-serif; font-size: 0.65rem; color: #ffffff; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px; border-bottom: 1px solid #222222; padding-bottom: 6px;">Contribution Metrics</div>
        <div style="display: flex; justify-content: space-between; font-family: 'Share Tech Mono', monospace; font-size: 0.8rem; flex-wrap: wrap; gap: 10px;">
            <div><span style="color: #888888;">Issues Scanned:</span> <span style="color: #ffffff; font-weight: bold;">{m.get('issues_scanned', 0)}</span></div>
            <div><span style="color: #888888;">Issues Attempted:</span> <span style="color: #ffffff; font-weight: bold;">{m.get('issues_attempted', 0)}</span></div>
            <div><span style="color: #888888;">PRs Opened:</span> <span style="color: #ffffff; font-weight: bold;">{m.get('prs_opened', 0)}</span></div>
            <div><span style="color: #888888;">PRs Merged:</span> <span style="color: #ffffff; font-weight: bold;">{m.get('prs_merged', 0)}</span></div>
            <div><span style="color: #888888;">PRs Closed:</span> <span style="color: #ffffff; font-weight: bold;">{m.get('prs_closed', 0)}</span></div>
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
    st.code(get_logs(), language="bash")

    # ── Self-refresh while running ────────────────────────────────────────────
    if running:
        time.sleep(0.7)          # 0.7s tick — smooth without hammering CPU
        st.rerun()               # fragment-only rerun (Streamlit 1.37+)


live_feed()
