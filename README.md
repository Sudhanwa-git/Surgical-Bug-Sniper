# Surgical Bug Sniper

An autonomous AI agent that scans open-source AI repositories for bugs, generates minimal code patches using a local LLM, validates them, and opens pull requests — fully automated, zero API fees, runs on a laptop.

---

## How It Works

```
  ┌──────────────┐     ┌─────────┐     ┌──────────────┐     ┌────────┐     ┌──────────┐
  │   1. HUNT    │────▶│ 2. CLONE│────▶│   3. FIX     │────▶│4.VERIFY│────▶│ 5. PUSH  │
  │              │     │         │     │              │     │        │     │          │
  │ GitHub API   │     │ Git CLI │     │ Ollama LLM   │     │ AST +  │     │ Fork →   │
  │ parallel     │     │ shallow │     │ qwen2.5-coder│     │ syntax │     │ Push →   │
  │ scan         │     │ blobless│     │ 7B local     │     │ check  │     │ Open PR  │
  └──────────────┘     └─────────┘     └──────────────┘     └────────┘     └──────────┘
```

**Hunt** → Scans 6 repos in parallel via GitHub REST API. Scores each bug by solvability — tracebacks, error types, and code blocks score high; GPU/distributed/concurrency issues are filtered out.

**Clone** → Shallow blobless clone (`--depth 1 --filter=blob:none`) to a temp directory. Fast, minimal disk usage.

**Fix** → Three sub-phases:
1. **Comprehension Gate** — LLM must explain the root cause before attempting a fix. If it can't → skip.
2. **File Targeting** — Parallel content scoring finds the most relevant source file by matching identifiers from the bug report.
3. **Patch Generation** — LLM produces a `SEARCH/REPLACE` block. Self-correction loop retries once with error feedback if the first attempt fails.

**Verify** → Every patched Python file is validated with `py_compile` + `ast.parse` before and after writing to disk.

**Push** → Forks the repo, creates a fix branch, commits only source files, pushes, and opens a PR via GitHub API.

---

## Quality Gates

| Gate | What It Catches |
|---|---|
| Comprehension Gate | Skips bugs the LLM can't explain — prevents blind fix attempts |
| Patch Size Validation | Rejects patches where REPLACE is 3× larger than SEARCH (rewrites, not fixes) |
| AI Artifact Stripping | Removes LLM-generated comments (`# Fixed`, `# Added`, etc.) not in original code |
| Import Scope Check | Rejects patches adding > 2 new imports (scope creep) |
| AST Validation | Pre-write and post-write structural checks on modified files |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Orchestrator | Custom pipeline — no LangChain, no frameworks |
| LLM | Ollama → Qwen 2.5 Coder 7B (Q4_K_M, 6 GB VRAM) |
| UI | Streamlit 1.37+ with fragment-scoped live refresh |
| Git | Git CLI + GitHub REST API v3 |
| HTTP | `requests` with TCP pooling + auto-retry |
| Concurrency | `ThreadPoolExecutor` — parallel repo scanning + file scoring |

---

## Design Decisions

**Why no LangChain?** — The pipeline is linear: each stage succeeds or fails. LangChain adds ~150 MB of dependencies and abstraction overhead for something that works as five Python classes. Zero framework tax.

**Why SEARCH/REPLACE over unified diff?** — 7B models are bad at unified diffs — they hallucinate line numbers and get hunk headers wrong. SEARCH/REPLACE only requires copying existing code and writing the fix. Dramatically higher success rate.

**Why a comprehension gate?** — Without it, the model blindly patches files it doesn't understand. The gate costs one cheap inference call and saves multiple wasted patch attempts.

**Why local inference?** — Runs on a laptop with 6 GB VRAM. Zero API fees. Zero token costs. The difficulty scoring compensates for the model's limitations by only feeding it bugs within its capability range.

---

## Project Structure

```
├── sbk.py              Core pipeline — Hunt, Clone, Fix, Verify, Push (1,365 lines)
├── sbk_ui.py           Streamlit dashboard — live feed, step tracker, metrics
├── metrics.py          Thread-safe JSON metrics tracker
├── ship.bat            One-command commit + push to GitHub
├── requirements.txt    5 runtime dependencies
├── .env.example        Environment variable template
├── .streamlit/
│   └── config.toml     Dark theme config
└── sample/
    ├── core.py          Demo bug (ZeroDivisionError in normalize_tensors)
    └── test_core.py     Tests that expose the bug
```

---

## Setup

**Prerequisites:** Python 3.10+, Git (authenticated), [Ollama](https://ollama.com)

```bash
# Clone and install
git clone https://github.com/Sudhanwa-git/Surgical-Bug-Sniper.git
cd Surgical-Bug-Sniper
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# Pull the model
ollama pull qwen2.5-coder:7b

# Configure
cp .env.example .env
# Edit .env — set your GITHUB_TOKEN
```

---

## Usage

```bash
# Web UI — real-time dashboard with step tracker and live feed
streamlit run sbk_ui.py

# CLI — full pipeline in terminal
python sbk.py

# Ship changes to GitHub
ship "your commit message"
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | GitHub PAT with `repo` scope |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Ollama model name |
| `OLLAMA_API_BASE` | `http://localhost:11434` | Ollama endpoint |
| `SURGERY_CONTEXT_CHARS` | `35000` | Max source chars sent to LLM |
| `SURGERY_TIMEOUT_SEC` | `180` | LLM timeout (seconds) |
| `MAX_BUGS_PER_REPO` | `3` | Max bugs attempted per run |

---

## Target Repositories

The agent currently scans:
- `langchain-ai/langgraph`
- `joaomdmoura/crewAI`
- `run-llama/llama_index`
- `qdrant/qdrant`
- `ollama/ollama`
- `vllm-project/vllm`

---

## Limitations

- **Single-file fixes only** — multi-file refactors are out of scope
- **Python-centric verification** — AST checks only run on `.py` files
- **No test execution** — verification is static (syntax + structure)
- **7B model ceiling** — complex architectural bugs are filtered out by design

---

## License

MIT
