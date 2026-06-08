Surgical Bug Sniper
===================

Surgical Bug Sniper is an autonomous AI agent designed as a local-first pipeline to Hunt, Clone, Fix, Verify, and Commit pull requests for bugs in AI infrastructure repositories. 

Optimized to run on consumer hardware like a 6GB VRAM RTX 3050, it combines local LLM inference via Ollama with GitHub's developer APIs to run entirely free of API fees.

The system uses a custom, lightweight Python class orchestrator rather than external agentic frameworks like LangChain, keeping dependency overhead to an absolute minimum.

---

Tech Stack
----------

Core Runtime
- Python 3.10+ utilizing ThreadPoolExecutor for concurrent scouting, subprocess for local execution, and requests for API interaction.

Agent Orchestrator
- A custom, pure Python stateful pipeline class defined in sbk.py. No LangChain or LangGraph dependencies.

Coding LLM and Inference Engine
- Ollama running Qwen 2.5 Coder 7B (Q4_K_M).

User Interface
- Streamlit (v1.37+) with fragment-scoped log streaming and custom dark-mode CSS.

Version Control and APIs
- Git CLI and GitHub REST API (v3) with TCP connection pooling and retries.

---

System Architecture
-------------------

```
[ Streamlit Web UI ] (FIRE / ABORT Controls)
                   |
                   v
              [ 1. Hunt ]
         (GitHub REST API Scan)
                   |
                   v
              [ 2. Clone ]
        (Shallow Blobless Clone)
                   |
                   v
               [ 3. Fix ]
        (Ollama qwen2.5-coder:7b)
                   |
                   v
             [ 4. Verify ]
           (py_compile check)
                   |
                   v
             [ 5. Commit ]
          (Git Branch & Stage)
                   |
                   v
         [ Upstream GitHub PR ]
        (Fork, Commit, Push, PR)
```

Execution Pipeline
------------------

1. Hunt
- Tool: GitHub REST API (v3) with concurrent ThreadPoolExecutor.
- Action: Runs parallel scans across whitelisted repositories. Uses heuristics to filter and rank bug reports containing actionable technical indicators (like tracebacks, exceptions, code blocks, or file references).

2. Clone
- Tool: Git CLI.
- Action: Performs a clean, shallow, blobless clone (--depth 1 --filter=blob:none) to target TEMP directories, deleting old repositories on startup to prevent stale context.

3. Fix
- Tool: Ollama running Qwen 2.5 Coder 7B.
- Action: Generates a SEARCH/REPLACE diff patch. Optimizes local context windows by parsing a code segment (up to 35,000 characters) surrounding key identifiers, then patches files using exact matching, fuzzy whitespace matching, or indentation-agnostic logic.

4. Verify
- Tool: Python py_compile compiler.
- Action: Automatically runs compile-time syntax checks on all modified code files. It aborts the pipeline if syntax errors are found, avoiding broken commits.

5. Commit
- Tool: Git CLI and GitHub REST API (v3).
- Action: Sets git configuration, branches, stages modified files, creates atomic commits, forks the repository via API, pushes the branch to your account, and opens a pull request targeting main or master.

---

Setup and Installation
----------------------

Prerequisites
- Python 3.10+
- Git CLI (configured and authenticated to GitHub)
- Ollama running locally

Download the Model
```bash
ollama pull qwen2.5-coder:7b
```

Install Dependencies
```bash
git clone https://github.com/your-username/surgical-bug-sniper.git
cd surgical-bug-sniper
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
On Windows, activate the virtual environment using: .venv\Scripts\activate

Configure Environment
Create a .env file in the root directory:
```ini
GITHUB_TOKEN=ghp_your_github_token_here
AIDER_MODEL=ollama_chat/qwen2.5-coder:7b
OLLAMA_API_BASE=http://localhost:11434
MAX_BUGS_PER_REPO=3
SURGERY_CONTEXT_CHARS=35000
SURGERY_TIMEOUT_SEC=180
```

---

Usage
-----

Web UI
Launch the control panel dashboard:
```bash
streamlit run sbk_ui.py
```
Open http://localhost:8501 to monitor the live operation feed.

CLI Mode
Run the pipeline directly in your terminal:
```bash
python sbk.py
```
