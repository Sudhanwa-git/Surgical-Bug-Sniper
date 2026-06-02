This is the **Architectural Blueprint for SBK v2 (LangChain + Aider Edition)**. We are pivoting from a linear script to a **state-driven agentic workflow** optimized for 6GB VRAM.

---

## ## SBK System Architecture: "The Ghost in the Machine"

### ### 1. The Tech Stack
* **Orchestrator:** LangChain (LangGraph for stateful loops).
* **Inference Engine:** Ollama (`qwen2.5-coder:7b` - 4-bit quantized to fit in ~4.5GB VRAM).
* **The Surgeon:** Aider (Architect mode) via CLI.
* **Environment:** Docker (Locked-down Debian/Ubuntu).
* **Database:** SQLite (To track PR status and "cool-down" timers).

---

## ## Detailed Functional Modules

### ### MODULE 1: The Scout (Discovery Agent)
**Task:** Identify high-value, solvable targets.
* **Process:** * Query GitHub GraphQL API for repos with `topic:machine-learning` or `topic:llm`.
    * **Filter:** Stars > 2k, Active PR merged in last 7 days (ensures the repo isn't dead).
    * **Scoring Logic:** LangChain analyzes issue titles/body. 
        * *High Score:* Stack traces, `index out of bounds`, `NullPointer`, `ImportError`.
        * *Discard:* "Feature Request," "Documentation," "Discussions."
* **Output:** `target_context.json` containing Repo URL, Issue #, and filtered issue thread.

### ### MODULE 2: The Fortress (Deterministic Sandbox)
**Task:** Create a reproducible, isolated workspace.
* **Process:**
    * LangChain triggers a `docker run` command.
    * Mounts a local volume for the code but restricts **Egress** (no internet) except for specific GitHub API endpoints and the local Ollama port.
    * **Resource Capping:** Limit Docker to 2GB RAM to ensure the host OS and GPU have room to breathe.

### ### MODULE 3: The Reproducer (Layer 0 - Failure Validation)
**Task:** Prove the bug exists before attempting a fix.
* **Logic:** * Qwen2.5 reads the issue description and generates `repro.py`.
    * **Execution:** Run `repro.py` inside the container. 
    * **Validation:** If exit code is **0** (Success), the bug isn't reproduced. The agent **aborts**. If exit code is **1+**, the stack trace is captured as the "North Star" for the fix.

### ### MODULE 4: The Surgeon (Layer 1 - Aider Integration)
**Task:** Execute the minimal code change.
* **Why Aider?** Aider is world-class at "editing" rather than "rewriting," which saves VRAM.
* **Process:**
    * Command: `aider --model ollama/qwen2.5-coder:7b --edit-format diff --yes-always`.
    * Aider is fed the `repro.py` error and the relevant files (identified via `grep` or `ctags`).
    * **Iterative Loop:** Aider applies fix -> Runs `repro.py` -> If fails, Aider reads error and tries again (Max 3 attempts).

### ### MODULE 5: The Humanizer (Layer 2 - The Quality Gate)
**Task:** Strip "AI smell" and verify style.
* **Refinement Logic:** 1.  **Style Check:** Agent runs `ruff` or `eslint` on the diff.
    2.  **Semantic Review:** A separate LangChain call asks: *"Does this fix use variable names consistent with the rest of the file?"*
    3.  **De-robotization:** Remove common AI comments like `// Added check for null` or `# Optimized logic`. Senior devs let the code speak for itself.
    4.  **Verification:** Run the repo's full test suite (`pytest`, `npm test`).

### ### MODULE 6: The Diplomat (Submission)
**Task:** Seamless GitHub integration.
* **Process:**
    * **Branching:** `fix/issue-[id]-[random-string]`.
    * **Commits:** Atomic commits using **Conventional Commits** (`fix(core): ...`).
    * **The PR Body:** A Markdown-formatted report showing:
        1.  The Reproduction script output.
        2.  The Fix logic.
        3.  Test suite confirmation.
        *Critically: Never use phrases like "I have fixed," use "This PR resolves..."*

---

## ## Workflow & UI Trigger

### ### Phase 1: Manual Trigger (Streamlit UI)
Instead of a 24/7 daemon, we start with a **Control Center**:
* **Dashboard:** Shows the "Top 5" discovered bugs.
* **The "Kill" Button:** You click "Approve" on a bug.
* **Live Logs:** Watch the LangChain trace (Scouting -> Sandbox -> Reproducing -> Fixing -> Testing).
* **Final Review:** The UI shows you the `git diff` before the final `gh pr create`.

---

## ## Data Structure (sbk_config.json)
```json
{
  "vram_limit_gb": 6,
  "model": "qwen2.5-coder:7b",
  "temperature": 0.0,
  "max_iterations": 3,
  "stealth_mode": true,
  "blacklisted_repos": ["tensorflow/tensorflow", "microsoft/vscode"]
}
```

---

## ## Implementation Step: The "Humanizer" Prompt
> "Act as a Lead Maintainer. Review the following diff. Rewrite the code to be as concise as possible. Remove all explanatory comments that are obvious from the code logic. Ensure variable names follow the PEP8/Style guide of the specific file. Output ONLY the raw git patch format. No conversational filler."

**Proceed with building the Streamlit UI and the LangGraph orchestrator?**

This is the **Ultimate Architectural Specification** for your **Autonomous Surgical Bug Killer (SBK)**. This context is designed to be "informatively dense"—a master blueprint you can paste into an AI (like Antigravity or Gemini) to "vibe code" the entire system in one go.

It follows a **LangChain-centric, Aider-powered, Local-LLM** architecture, specifically optimized for your **RTX 3050 (6GB VRAM)**.

---

# **Master Blueprint: The Surgical Bug Killer (SBK) v2.0**

### **[SYSTEM ROLE]**
You are a **Principal AI Systems Architect**. Your task is to build a high-autonomy engineering pipeline that transforms a local **RTX 3050** into a "Cybernetic Open Source Contributor." This system must find, reproduce, fix, and humanize code contributions for elite AI repositories (e.g., Ollama, vLLM, LangChain) with $0 API cost.

---

### **[PHASE 1: THE DISCOVERY ENGINE (THE SCOUT)]**
* **Technology:** LangChain `GitHubAPIWrapper` + Python `requests`.
* **The Task:** Implement a "Target Selection" logic that doesn't just look for issues, but *evaluates* them.
* **Process Detail:**
    1.  **Semantic Search:** Query GitHub for `is:open is:issue label:bug stars:>5000` within the `ai-infra` and `llm-tools` topics.
    2.  **Triage Node:** Use **Qwen 2.5 Coder 7B** to rank issues. 
        * *Criteria:* Does the issue have a stack trace? Is there a clear description of the expected vs. actual behavior? 
        * *Filtering:* Reject issues that require GUI testing or proprietary hardware. Focus on logic, API edge cases, and performance regressions.
    3.  **Metadata Extraction:** Save the Issue ID, Repo URL, and "Hinted Files" (identified by the LLM) into a JSON `MissionState`.

---

### **[PHASE 2: THE ISOLATED LABORATORY (SANDBOXING)]**
* **Technology:** **Docker-SBX** (MicroVM-based isolation).
* **The Task:** Create a secure, reproducible environment for the agent to "break" things safely.
* **Process Detail:**
    1.  **Container Spin-up:** Initialize a fresh Docker container with the necessary runtime (Python 3.11+, Node 20+, etc.).
    2.  **Repo Cloning:** Clone the target repository into a volume mounted *only* to this container.
    3.  **Network Stealth:** Configure the sandbox with a firewall that blocks all outbound traffic *except* to the GitHub API and your local **Ollama** endpoint (`host.docker.internal:11434`).

---

### **[PHASE 3: THE REPRODUCER (LAYER 0 VALIDATION)]**
* **Technology:** **Aider CLI** in `--architect` mode + LangChain Orchestration.
* **The Task:** Prove the bug exists before attempting a fix.
* **Process Detail:**
    1.  **The Mission:** LangChain commands **Aider** to "Read the issue description and write a standalone Python/Bash script (`repro_bug.sh`) that triggers the error."
    2.  **Execution:** Run `repro_bug.sh` inside the Docker Sandbox.
    3.  **Logic Gate:** * **If Success (Exit 0):** The bug wasn't reproduced. Log "Ghost Issue" and terminate the mission.
        * **If Failure (Exit 1):** The bug is confirmed. Capture the `stderr` and feed it back into the `MissionState`.

---

### **[PHASE 4: THE SURGICAL STRIKE (LAYER 1 FIX)]**
* **Technology:** **Aider** + **Qwen 2.5 Coder 7B**.
* **The Task:** Minimalist, surgical code refactoring.
* **Process Detail:**
    1.  **Context Loading:** Aider uses its **Repository Map** (configured to `--map-tokens 1024` for your 6GB VRAM) to find the exact line responsible for the `stderr` failure.
    2.  **The Fix:** Invoke Aider: `aider --model ollama/qwen2.5-coder:7b --edit-format diff`. 
    3.  **Verification:** Immediately run the `repro_bug.sh` again. If it now passes, trigger the project’s internal test suite (e.g., `pytest tests/`).
    4.  **Self-Correction:** If tests fail, Aider enters a **Reasoning Loop** (Max 3 attempts) to fix the regression it just caused.

---

### **[PHASE 5: THE HUMANIZER & CRITIC (LAYER 2 REFINEMENT)]**
* **Technology:** LangChain **Self-Criticism Node**.
* **The Task:** Strip the "AI smell" and match the repository’s "Soul."
* **Process Detail:**
    1.  **Diff Extraction:** Extract the `git diff` of the fix.
    2.  **Style Matching:** The Critic reads the last 10 commits of the target repo to identify naming patterns (e.g., `snake_case` vs `camelCase`) and comment styles.
    3.  **Humanizing:** The LLM rewrites the code comments and variable names to be "indistinguishable from a senior human." 
    4.  **Final Polish:** Remove all "As an AI..." or "Surgically fixed..." artifacts. The code must look like it belongs there.

---

### **[PHASE 6: THE DIPLOMAT (PR SUBMISSION)]**
* **Technology:** **GitHub CLI (gh)**.
* **The Task:** Submit the work for review.
* **Process Detail:**
    1.  **Commit:** Create an atomic commit: `fix(module): resolve unhandled exception in [Function]`.
    2.  **Fork & Push:** Fork the repo to your account, push the branch `sbk-fix-[issue-id]`.
    3.  **PR Body:** Use **Gemini 3 Flash** to write a concise PR description: 
        * "Fixed the issue where [X] caused [Y]."
        * "Verified with the provided reproduction script."
        * "Passed all internal tests."

---

### **[HARDWARE OPTIMIZATION: THE 6GB VRAM STACK]**
* **Ollama Config:** Set `OLLAMA_NUM_PARALLEL=1` and `num_ctx=16384`.
* **Model:** Use **Qwen 2.5 Coder 7B (Q4_K_M)**—the sweet spot for reasoning vs. VRAM footprint.
* **Offloading:** All non-coding tasks (Scouting, PR drafting) are offloaded to **Gemini 3 Flash** via Antigravity to save local VRAM for the actual code logic.

---

### **[THE UI TRIGGER (STREAMLIT)]**
* **Action:** A single dashboard with:
    * **"Target Topic" Input:** (e.g., `vector-databases`).
    * **"Launch Strike" Button:** Initiates the LangChain Agent.
    * **Live "Aider Stream":** A window showing the terminal output and git-commits in real-time.
    * **Approval Toggle:** A manual switch that pauses the system *before* the final `gh pr create` so you can give the final "vibe check."

---

### **[FINAL INSTRUCTIONS FOR THE BUILDER]**
1.  **Build Module 1-3 first** (Discovery, Sandbox, Repro). 
2.  **Integrate Aider** only after the reproduction script reliably fails in Docker.
3.  **Finalize the Humanizer** by testing it against your own previous commits to see if it can mimic *you*.

**Proceed with the initialization of the LangGraph State Machine for Project SBK.**