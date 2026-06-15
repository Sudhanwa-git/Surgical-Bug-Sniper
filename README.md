# Surgical Bug Sniper

**An autonomous AI agent that hunts open bugs in major open-source AI repositories, generates minimal code patches using a local LLM, verifies them, and opens pull requests — fully automated, zero API fees.**

Built to run on consumer hardware (6 GB VRAM, RTX 3050). No cloud APIs. No LangChain. No external agentic frameworks. Just Python, Git, and a local Ollama model.

---

## Why This Exists

Open-source AI infrastructure projects accumulate hundreds of open bug reports. Most are well-documented — complete with tracebacks, error types, and file references — yet sit unresolved because maintainers are focused on feature development.

This project automates the entire bug-fix contribution cycle:

1. **Scan** GitHub for actionable bugs across multiple repositories simultaneously
2. **Rank** bugs by solvability (filtering out multi-file, GPU, and concurrency issues a 7B model can't handle)
3. **Clone** the repository, **locate** the relevant source file, and **build context** around the bug
4. **Generate** a minimal SEARCH/REPLACE patch via local LLM inference
5. **Validate** the patch (AST parsing, syntax checks, AI artifact stripping)
6. **Fork**, **commit**, **push**, and **open a pull request** — all via GitHub REST API

The result is a system that can go from "zero" to "PR opened" in under 3 minutes, entirely on a laptop.

---

## System Architecture

```
                          ┌─────────────────────────────┐
                          │    Streamlit Control Panel   │
                          │   FIRE / ABORT / Live Feed   │
                          └─────────────┬───────────────┘
                                        │
                    ┌───────────────────────────────────────┐
                    │          ORCHESTRATOR (sbk.py)        │
                    │   Custom stateful pipeline — no       │
                    │   LangChain, no LangGraph, pure       │
                    │   Python class composition            │
                    └───────────────────┬───────────────────┘
                                        │
          ┌─────────────────────────────────────────────────────┐
          │                                                     │
    ┌─────▼─────┐  ┌─────────┐  ┌──────────┐  ┌────────┐  ┌───▼───┐
    │  1. HUNT  │→ │ 2. CLONE│→ │ 3. FIX   │→ │4.VERIFY│→ │5.PUSH │
    │           │  │         │  │          │  │        │  │       │
    │ GitHub    │  │ Git CLI │  │ Ollama   │  │ AST +  │  │ Fork  │
    │ REST API  │  │ shallow │  │ qwen2.5  │  │ py_    │  │ Push  │
    │ parallel  │  │ blobless│  │ -coder   │  │ compile│  │ PR    │
    │ scan      │  │ clone   │  │ 7B local │  │ check  │  │       │
    └───────────┘  └─────────┘  └──────────┘  └────────┘  └───────┘
```

---

## Pipeline Deep-Dive

### Stage 1 — Hunt

**Tool:** GitHub REST API v3 with `ThreadPoolExecutor` (parallel)

Scans a whitelist of 6 repositories concurrently:
- `langchain-ai/langgraph`
- `joaomdmoura/crewAI`
- `run-llama/llama_index`
- `qdrant/qdrant`
- `ollama/ollama`
- `vllm-project/vllm`

**Strategy A:** Query issues by bug labels (`bug`, `type:bug`, `kind/bug`, etc.)
**Strategy B (fallback):** Pull the 30 most recent open issues and self-filter using heuristic scoring

**Actionability filter** rejects:
- Feature requests, questions, enhancement proposals
- Issues with < 100 characters of body text
- Vague titles ("weird", "intermittent", "flaky")

**Difficulty scoring** ranks every issue on a numeric scale:
| Signal | Score |
|---|---|
| Python traceback with file + line number | +30 |
| Explicit error type (TypeError, KeyError, etc.) | +20 |
| Mentions specific function or class | +10 |
| Contains code block | +10 |
| Short, focused issue (< 2000 chars) | +5 |
| GPU/CUDA/distributed/race-condition keywords | −25 |
| References > 3 files (multi-file bug) | −15 |
| More than 10 comments (complex discussion) | −20 |
| Already has linked PRs | −10 |

Bugs scoring ≤ 0 are **discarded**. The repo with the highest single-bug score wins.

---

### Stage 2 — Clone

**Tool:** Git CLI

```
git clone --depth 1 --single-branch --filter=blob:none <url> <target>
```

- **`--depth 1`** — single commit, no history
- **`--filter=blob:none`** — download file contents on demand (blobless clone)
- Clones to a temp directory (`C:\temp\sbk_hunts` on Windows)
- Old clones are wiped on startup to prevent stale context

---

### Stage 3 — Fix (Comprehension-Gated)

**Tool:** Ollama running `qwen2.5-coder:7b` (Q4_K_M quantization)

This is the core of the system and has **three sub-phases:**

#### 3a. Comprehension Gate
Before attempting any patch, the LLM is asked to explain the root cause:
```
ROOT_CAUSE: [one sentence]
AFFECTED_FUNCTION: [function name]
FIX_TYPE: [logic_error | missing_check | wrong_value | ...]
CONFIDENCE: [high | medium | low]
```
If the LLM responds `INCOMPREHENSIBLE` or `low` confidence → **skip the bug entirely**. This prevents wasted inference on bugs the model doesn't understand.

#### 3b. File Targeting
Three-tier file discovery:
1. **Traceback paths** — extract `File "..."` references from the bug body
2. **Content scoring** — parallel scan of all source files, scoring by identifier frequency (`def func_name` = 6 pts, `self.attr` = 3 pts, raw mention = 1 pt)
3. **Filename matching** — match filenames mentioned in the bug body

#### 3c. Patch Generation
The LLM receives a **zero-tolerance system prompt** that demands output in strict `<<<SEARCH>>>` / `<<<REPLACE>>>` / `<<<END>>>` format. No markdown, no explanations, no comments.

**Smart context windowing:** If the target file exceeds 35,000 characters, the system extracts a focused window centered on the best-matching identifiers rather than truncating from the top.

**Self-correction loop:** If the first patch fails to apply (SEARCH block doesn't match), the LLM gets error feedback and retries once with explicit instructions to copy lines character-for-character.

**Patch application** uses three match strategies:
1. Exact string match
2. Fuzzy whitespace-normalized match
3. Indent-agnostic line-by-line match

---

### Stage 4 — Verify

**Tool:** `py_compile` + `ast.parse`

Every modified Python file gets two checks:
1. **`py_compile`** — catches syntax errors that would crash at import time
2. **`ast.parse`** — validates structural integrity of the abstract syntax tree

Additionally, a **pre-write AST check** runs before any patch is written to disk. If the patched content would break syntax, the patch is rejected before the file is modified.

---

### Stage 5 — Push + PR

**Tool:** Git CLI + GitHub REST API v3

1. Fork the upstream repository to the authenticated user's account
2. Wait for the fork to become available (polling with backoff)
3. Configure git identity, create a fix branch (`fix/issue-<N>`)
4. Stage only modified source files (explicitly excludes `.aider*`, logs, `.gitignore`)
5. Commit with a conventional message: `fix: resolve issue #<N>`
6. Force-push the branch to the fork
7. Open a pull request targeting `main` (falls back to `master` if 422)

---

## Quality Gates

The system has **five layers of protection** against bad patches:

| Gate | What It Catches |
|---|---|
| **Comprehension Gate** | Skips bugs the LLM can't explain — prevents blind fix attempts |
| **Patch Size Validation** | Rejects patches where REPLACE is > 3× larger than SEARCH (rewrites, not fixes) |
| **AI Artifact Stripping** | Removes LLM-generated comments (`# Fixed`, `# Added`, `# Ensure`, etc.) not present in original code |
| **Import Scope Check** | Rejects patches adding more than 2 new imports (scope creep) |
| **AST + Syntax Validation** | Pre-write and post-write structural checks on every modified Python file |

---

## Design Decisions

### Why no LangChain / LangGraph?

LangChain adds ~150 MB of dependencies, abstract chain types, callback plumbing, and token-counting overhead — all for a pipeline that is fundamentally **linear**. Each stage either succeeds or fails, and the orchestrator decides what to do next. A Python class with five methods does this in ~1,400 lines with zero abstraction tax.

### Why Ollama + 7B model?

This project is designed to run on a personal laptop with a 6 GB VRAM GPU. Cloud APIs cost money per token. Ollama runs `qwen2.5-coder:7b` (Q4_K_M) locally with zero fees. The comprehension gate and difficulty scoring compensate for the model's limitations by only feeding it bugs within its capability range.

### Why SEARCH/REPLACE over unified diff?

Small LLMs are bad at generating correct unified diffs — they hallucinate line numbers, get hunk headers wrong, and produce off-by-one context lines. The `<<<SEARCH>>>` / `<<<REPLACE>>>` format only requires the model to copy existing code and write the replacement — no line counting, no hunk math. This produces a dramatically higher success rate on 7B models.

### Why custom file scoring instead of embeddings?

Embedding-based retrieval requires a separate model, vector store, and chunking pipeline. The keyword-frequency scorer in `_find_files_by_content()` is 50 lines of Python, runs in under a second via `ThreadPoolExecutor`, and is sufficient for matching tracebacks and function names to source files.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Orchestrator | Custom pipeline class (`sbk.py`) — no frameworks |
| LLM | Ollama → Qwen 2.5 Coder 7B (Q4_K_M) |
| UI | Streamlit 1.37+ with fragment-scoped live refresh |
| Version Control | Git CLI + GitHub REST API v3 |
| HTTP | `requests` with TCP pooling + auto-retry (`urllib3`) |
| Concurrency | `ThreadPoolExecutor` for parallel repo scanning + file scoring |

---

## Project Structure

```
surgical-bug-sniper/
├── sbk.py              # Core pipeline — Hunt, Clone, Fix, Verify, Push
├── sbk_ui.py           # Streamlit web dashboard with live operation feed
├── metrics.py          # Thread-safe JSON metrics tracker
├── ship.bat            # One-command git commit + push to GitHub
├── requirements.txt    # Runtime dependencies (5 packages)
├── .env.example        # Environment variable template
├── .streamlit/
│   └── config.toml     # Streamlit dark theme configuration
└── sample/
    ├── core.py          # Demo buggy file (ZeroDivisionError)
    └── test_core.py     # Demo tests that expose the bug
```

---

## Setup

### Prerequisites
- Python 3.10+
- Git CLI (authenticated to GitHub)
- [Ollama](https://ollama.com) installed and running

### Install

```bash
git clone https://github.com/Sudhanwa-git/Surgical-Bug-Sniper.git
cd Surgical-Bug-Sniper

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### Pull the Model

```bash
ollama pull qwen2.5-coder:7b
```

### Configure

```bash
cp .env.example .env
# Edit .env — add your GITHUB_TOKEN
```

---

## Usage

### Web UI (Recommended)

```bash
streamlit run sbk_ui.py
```

Open `http://localhost:8501`. Click **FIRE** to start a hunt. The live feed streams every pipeline stage in real time with a step-by-step progress tracker.

### CLI

```bash
python sbk.py
```

Runs the full pipeline in your terminal. Outputs a structured mission summary at the end.

### Ship Changes

```bash
ship "your commit message"
```

Stages all changes, commits, and pushes to `origin main` in one command.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | GitHub PAT with `repo` and `workflow` scopes |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Model identifier (must match `ollama list`) |
| `OLLAMA_API_BASE` | `http://localhost:11434` | Ollama API endpoint |
| `SURGERY_CONTEXT_CHARS` | `35000` | Max source characters sent to LLM per file |
| `SURGERY_TIMEOUT_SEC` | `180` | LLM inference timeout in seconds |
| `MAX_BUGS_PER_REPO` | `3` | Max bugs attempted per repository per run |

---

## Metrics

The system tracks cumulative statistics across runs in `metrics.json` (gitignored):

- **Issues Scanned** — total GitHub issues evaluated
- **Issues Attempted** — bugs that passed all filters and entered the fix pipeline
- **PRs Opened** — pull requests successfully created
- **PRs Merged / Closed** — downstream tracking (manual update)

---

## Limitations

- **Single-file fixes only** — the system targets one file per bug. Multi-file refactors are out of scope.
- **Python-centric verification** — AST/syntax checks only run on `.py` files. Go/Rust/JS patches are applied without structural validation.
- **7B model ceiling** — complex bugs involving distributed systems, GPU kernels, or deep architectural issues are filtered out by design.
- **No test execution** — verification is static (syntax + AST). The system does not run the project's test suite.
- **Rate limits** — GitHub API rate limits (5,000 req/hr authenticated) can throttle scanning on rapid successive runs.

---

## License

MIT
