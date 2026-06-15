"""
Sudhanwa's Surgical Bug Sniper — sbk.py
Pipeline: HUNT → CLONE → COMPREHEND → FIX → VERIFY → PUSH → PR

v6: Quality-first — comprehension gates, patch validation, clean logging,
    context engineering, AI-artifact stripping.
"""

import os, re, sys, shutil, subprocess, time, requests, random, json, ast, metrics
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(override=True)


# ───────────────────────────────────────────────────────────────────────────────
# STRUCTURED LOGGING — one-line summaries, fixed-width phase labels, no noise.
# LLM raw output goes to sbk_llm.log (debug only), never to main log.
# ───────────────────────────────────────────────────────────────────────────────
LLM_LOG_FILE = "sbk_llm.log"


def emit(phase: str, msg: str, sym: str = " "):
    """Structured one-liner:  ` PHASE    ✓ message`"""
    print(f" {phase.upper():<8} {sym} {msg}", flush=True)


def emit_ok(phase, msg):
    emit(phase, msg, "✓")


def emit_fail(phase, msg):
    emit(phase, msg, "✗")


def emit_skip(phase, msg):
    emit(phase, msg, "→")


def _llm_log(text: str):
    """Append raw LLM output to debug log file (not user-facing)."""
    try:
        with open(LLM_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


WHITELIST = [
    "langchain-ai/langgraph",
    "joaomdmoura/crewAI",
    "run-llama/llama_index",
    "qdrant/qdrant",
    "ollama/ollama",
    "vllm-project/vllm",
]

# Extra label variants to try per repo
BUG_LABELS = ["bug", "Bug", "type:bug", "kind/bug", "bug report"]


# ───────────────────────────────────────────────────────────────────────────────
# SHARED SESSION — persistent TCP pool + auto-retry
# ───────────────────────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=30)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — HUNT (with difficulty scoring)
# ═══════════════════════════════════════════════════════════════════════════════
class RepoHunter:
    STOP = {"the","and","for","with","that","this","from","are","has","not",
            "bug","fix","error","issue","using","when","use","causes","cause",
            "fails","fail","crash","weird","strange","also","into","does","its",
            "have","been","please","after","before","then","just","only","some",
            "which","they","them","their","there","these","those","more","will",
            "what","how","why","who","can","should","could","would","may","might"}

    # Keywords signaling a bug is too complex for a 7B model
    HARD_SIGNALS = {
        "race condition", "deadlock", "memory leak", "segfault", "segmentation",
        "cuda", "gpu", "nccl", "distributed", "multi-node", "multi-gpu",
        "kubernetes", "docker", "networking", "ssl", "certificate",
        "authentication", "flaky test", "non-deterministic", "concurrency",
        "thread-safe", "async race", "oom", "out of memory",
    }

    def __init__(self, session: requests.Session):
        self.session = session

    def get_repo(self, name):
        try:
            r = self.session.get(f"https://api.github.com/repos/{name}", timeout=10)
            if r.status_code == 200:
                d = r.json()
                return {"full_name": d["full_name"], "clone_url": d["clone_url"]}
            return None
        except Exception:
            return None

    def _is_actionable(self, title: str, body: str) -> bool:
        """
        Permissive actionability — accepts any bug with meaningful technical content.
        """
        if not body:
            return False
        total = (body or "").strip()
        if len(total) < 100:
            return False
        bl = body.lower()
        tl = title.lower()

        # Hard reject — questions and feature requests
        if any(w in tl for w in ("feature request", "enhancement", "question",
                                  "how to", "[question]", "[feat]", "[feature]")):
            return False

        # Strong positive signals
        has_traceback  = "traceback" in bl or 'file "' in bl
        has_error_line = bool(re.search(r'\b\w+error:\s', bl, re.I)) or "error:" in bl
        has_code_block = "```" in body
        has_file_ref   = bool(re.search(r'[\w\-/]+\.(?:py|js|rs|ts|go|cpp|h)', body))
        has_assert     = "assertionerror" in bl or "assert" in bl
        has_exception  = bool(re.search(r'\b(exception|raise|throws?)\b', bl))

        positives = sum([has_traceback, has_error_line, has_code_block,
                         has_file_ref, has_assert, has_exception])
        return positives >= 1

    def _is_vague(self, title: str) -> bool:
        vague = {"weird", "strange", "unexpected", "sometimes", "intermittent",
                 "random", "flaky", "occasionally"}
        tl = title.lower()
        return any(v in tl for v in vague)

    def _score_difficulty(self, issue: dict) -> int:
        """Score issue solvability for a 7B model. Higher = easier to fix.
        Returns 0 or negative for bugs that should be skipped."""
        title = issue.get("title", "")
        body  = issue.get("body") or ""
        combined = (title + " " + body).lower()
        score = 0

        # Strong positive: has Python traceback with file + line number
        if re.search(r'file ".*\.py", line \d+', combined):
            score += 30

        # Positive: has explicit error type
        if re.search(r'\b(TypeError|ValueError|KeyError|AttributeError|'
                     r'IndexError|ImportError|NameError|RuntimeError)\b', body):
            score += 20

        # Positive: mentions specific function or class
        if re.search(r'\b(def |class |function |method )\w+', combined):
            score += 10

        # Positive: has code block showing the problem
        if "```" in body:
            score += 10

        # Positive: short, focused issue (< 2000 chars)
        if len(body) < 2000:
            score += 5

        # Negative: too complex for 7B
        for signal in self.HARD_SIGNALS:
            if signal in combined:
                score -= 25

        # Negative: too many files referenced (multi-file bug)
        file_refs = re.findall(r'[\w\-/]+\.(?:py|js|rs|ts|go)', body)
        if len(set(file_refs)) > 3:
            score -= 15

        # Negative: too many comments (complex discussion, likely already triaged)
        if issue.get("comments", 0) > 10:
            score -= 20

        # Negative: issue already has linked PRs
        if "pull request" in combined or re.search(r'\bpr\s*#\d+', combined):
            score -= 10

        return score

    def scan_bugs(self, repo_full_name: str) -> list:
        """
        Multi-strategy bug scan with difficulty ranking.
        1. Try known bug labels one by one
        2. If nothing found, pull top-30 open issues and self-filter
        3. Rank by difficulty score (easiest first)
        """
        bugs = []

        # Strategy A — labelled bugs
        for label in BUG_LABELS:
            try:
                r = self.session.get(
                    f"https://api.github.com/repos/{repo_full_name}/issues"
                    f"?state=open&labels={label}&per_page=20&sort=created&direction=desc",
                    timeout=10)
                if r.status_code == 200:
                    for issue in r.json():
                        if issue.get("pull_request"):
                            continue
                        metrics.increment_metric("issues_scanned")
                        if self._is_vague(issue.get("title", "")):
                            continue
                        if self._is_actionable(issue.get("title", ""),
                                               issue.get("body") or ""):
                            bugs.append(issue)
                if bugs:
                    break
            except Exception:
                continue

        # Strategy B — any open issue that looks like a bug
        if not bugs:
            try:
                r = self.session.get(
                    f"https://api.github.com/repos/{repo_full_name}/issues"
                    f"?state=open&per_page=30&sort=created&direction=desc",
                    timeout=10)
                if r.status_code == 200:
                    for issue in r.json():
                        if issue.get("pull_request"):
                            continue
                        metrics.increment_metric("issues_scanned")
                        title = issue.get("title", "")
                        if self._is_vague(title):
                            continue
                        if self._is_actionable(title, issue.get("body") or ""):
                            bugs.append(issue)
            except Exception:
                pass

        # Rank by difficulty — easiest first, filter out hopeless ones
        bugs.sort(key=lambda b: self._score_difficulty(b), reverse=True)
        bugs = [b for b in bugs if self._score_difficulty(b) > 0]

        emit("hunt", f"Scanned {repo_full_name} → {len(bugs)} solvable bug(s)")
        return bugs[:5]


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — CLONE
# ═══════════════════════════════════════════════════════════════════════════════
class AutoCloner:
    def __init__(self):
        if sys.platform == "win32":
            self.base = "C:\\temp\\sbk_hunts"
        else:
            self.base = os.path.join(os.environ.get("TEMP", "/tmp"), "sbk_hunts")
        os.makedirs(self.base, exist_ok=True)

    def _rmdir(self, path):
        for _ in range(4):
            try:
                shutil.rmtree(path); return True
            except Exception:
                time.sleep(0.4)
        return False

    def clone(self, repo) -> str | None:
        url  = repo["clone_url"]
        slug = repo["full_name"].replace("/", "_")
        target = os.path.join(self.base, slug)

        if os.path.exists(target):
            if not self._rmdir(target) or os.path.exists(target):
                target = os.path.join(self.base, f"{slug}_{int(time.time())}")

        emit("clone", f"Fetching {repo['full_name']} (shallow blobless)...")
        t0 = time.time()
        try:
            r = subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 "--filter=blob:none", url, target],
                timeout=180, capture_output=True, text=True,
                encoding="utf-8", errors="replace"
            )
            if r.returncode == 0:
                elapsed = time.time() - t0
                emit_ok("clone", f"Ready — {elapsed:.1f}s")
                return target
            emit_fail("clone", f"Git error: {r.stderr.strip()[:120]}")
            return None
        except subprocess.TimeoutExpired:
            emit_fail("clone", "Timed out (180s)")
            self._rmdir(target)
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SURGERY (comprehension gate + quality validation + AI stripping)
# ═══════════════════════════════════════════════════════════════════════════════

# Zero-tolerance output format — the LLM must output ONLY the patch block.
SURGERY_SYSTEM = """\
You are a precise code repair tool. You receive a bug report and a source file.
You must output EXACTLY one SEARCH/REPLACE block to fix the bug.

OUTPUT FORMAT (no other text allowed):
<<<SEARCH>>>
[exact lines from the file, character-for-character including whitespace]
<<<REPLACE>>>
[corrected lines — minimal change only]
<<<END>>>

ABSOLUTE RULES:
1. Your ENTIRE response must begin with <<<SEARCH>>> and end with <<<END>>>
2. Do NOT write ANY text before <<<SEARCH>>> or after <<<END>>>
3. Do NOT add comments like "# Fixed", "# Added", "# Ensure", "# TODO"
4. Do NOT add explanations, markdown, code fences, or conversational text
5. Do NOT wrap code in ```python``` or any markdown blocks
6. Do NOT refactor, rename, or restructure code beyond the fix
7. The SEARCH block must match existing file content EXACTLY
8. Change the FEWEST lines possible to resolve the bug
9. If the file is unrelated to the bug, output exactly: NO_FIX

VIOLATIONS CAUSE SYSTEM FAILURE. Output ONLY the raw block."""

# Lightweight comprehension prompt — asks the LLM to explain the root cause
# before attempting a fix. If it can't, we skip (saves a wasted fix attempt).
COMPREHENSION_PROMPT = """\
You are a senior engineer triaging a bug report.
Analyze the bug and respond with EXACTLY this format (one line per field):

ROOT_CAUSE: [one sentence — the specific code defect causing the bug]
AFFECTED_FUNCTION: [function or class name most likely containing the bug]
FIX_TYPE: [one of: logic_error, missing_check, wrong_value, missing_import, type_error, api_misuse, other]
CONFIDENCE: [high, medium, low]

If you cannot determine a clear root cause, respond with exactly: INCOMPREHENSIBLE

Bug Report:
{bug_desc}

Relevant file paths in the repository:
{file_hints}"""


class Surgeon:
    def __init__(self, model: str, ollama_base: str, session: requests.Session):
        self.model       = model.split("/")[-1]
        self.ollama_base = ollama_base.rstrip("/")
        self.ctx_chars   = int(os.getenv("SURGERY_CONTEXT_CHARS", "35000"))
        self.timeout     = int(os.getenv("SURGERY_TIMEOUT_SEC",  "180"))
        self.session     = session
        self._repo_root  = None
        self.last_skip_reason = None  # set by operate() for summary tracking

    # ── Health check ──────────────────────────────────────────────────────────
    def _ollama_ok(self) -> bool:
        try:
            r = self.session.get(f"{self.ollama_base}/api/tags", timeout=5)
            if r.status_code != 200:
                emit_fail("fix", f"Ollama error {r.status_code} — is it running?")
                return False
            models = [m["name"] for m in r.json().get("models", [])]
            tag  = self.model if ":" in self.model else self.model + ":latest"
            base = tag.split(":")[0]
            if not any(m.startswith(base) for m in models):
                emit_fail("fix", f"Model '{tag}' not found — run: ollama pull {tag}")
                return False
            return True
        except Exception as e:
            emit_fail("fix", f"Cannot reach Ollama: {e}")
            return False

    # ── Comprehension Gate ────────────────────────────────────────────────────
    def _comprehend_bug(self, bug_desc: str, file_hints: list) -> dict | None:
        """Ask LLM to explain the root cause BEFORE attempting a fix.
        Returns parsed comprehension dict, or None if incomprehensible."""
        prompt = COMPREHENSION_PROMPT.format(
            bug_desc=bug_desc[:3000],
            file_hints=", ".join(file_hints[:5]) if file_hints else "none found"
        )
        payload = {
            "model":   self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":  False,
            "options": {"temperature": 0.0, "num_predict": 256},
        }
        try:
            r = self.session.post(f"{self.ollama_base}/api/chat",
                                  json=payload, timeout=60)
            if r.status_code != 200:
                return None
            text = r.json().get("message", {}).get("content", "")
            _llm_log(f"\n--- COMPREHENSION ({time.strftime('%H:%M:%S')}) ---\n{text}\n")

            if "INCOMPREHENSIBLE" in text.upper():
                return None

            result = {}
            for line in text.strip().splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip().upper().replace(" ", "_")
                    if key in ("ROOT_CAUSE", "AFFECTED_FUNCTION", "FIX_TYPE", "CONFIDENCE"):
                        result[key] = val.strip()

            if not result.get("ROOT_CAUSE"):
                return None
            if result.get("CONFIDENCE", "").lower() == "low":
                return None

            return result
        except Exception:
            return None

    # ── Fetch issue comments for richer context ───────────────────────────────
    def _fetch_issue_comments(self, repo_full: str, issue_number: int) -> str:
        """Fetch first 5 comments — they often contain the actual diagnosis."""
        try:
            r = self.session.get(
                f"https://api.github.com/repos/{repo_full}/issues/{issue_number}/comments"
                f"?per_page=5&sort=created&direction=asc",
                timeout=10)
            if r.status_code != 200:
                return ""
            comments = r.json()
            parts = []
            for c in comments[:5]:
                body = (c.get("body") or "").strip()
                if body and len(body) > 20:
                    author = c.get("user", {}).get("login", "?")
                    parts.append(f"Comment by {author}: {body[:500]}")
            return "\n\n".join(parts)
        except Exception:
            return ""

    # ── File scoring ──────────────────────────────────────────────────────────
    def _score_file(self, filepath: str, bug_text: str) -> int:
        path_lower = filepath.replace("\\", "/").lower()
        stop = {"the","and","for","with","that","this","from","are","has","not",
                "bug","fix","error","issue","using","when","use","causes","cause",
                "fails","fail","crash","weird","strange"}
        words = re.findall(r'\b[a-z][a-z0-9_]{2,}\b', bug_text.lower())
        kws = [w for w in words if w not in stop]
        return sum(1 for kw in kws if kw in path_lower)

    def _extract_identifiers(self, title: str, body: str) -> list:
        stop = {"the","and","for","with","that","this","from","are","has","not",
                "bug","fix","error","issue","when","use","list","single","string",
                "addition","between","weird","behavior","behaviour","return","import",
                "none","true","false","type","value","name","args","kwargs","self"}
        snake  = re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', title + " " + body)
        words  = re.findall(r'\b[a-z][a-z0-9_]{3,}\b', (title + " " + body).lower())
        combined = list(dict.fromkeys(snake + [w for w in words if w not in stop]))
        return combined[:12]

    # ── Parallel file content scanning ────────────────────────────────────────
    def _find_files_by_content(self, identifiers: list, repo_root: str) -> dict:
        scores = {}
        if not identifiers:
            return scores
        try:
            ls = subprocess.run(["git", "ls-files", "--", "*.py", "*.go", "*.rs", "*.ts", "*.js"],
                                capture_output=True, text=True, timeout=15)
            rel_files = [f.strip() for f in ls.stdout.splitlines() if f.strip()]
        except Exception:
            return scores

        def _score_one(rel_path):
            abs_path = os.path.join(repo_root, rel_path)
            try:
                text = _cached_read(abs_path)
            except Exception:
                return rel_path, 0
            score = 0
            for ident in identifiers:
                score += text.count(f"def {ident}") * 6
                score += text.count(f"fn {ident}") * 6      # Rust/Go
                score += text.count(f"func {ident}") * 6    # Go
                score += text.count(f"self.{ident}") * 3
                score += text.count(f"cls.{ident}") * 3
                score += text.count(ident)
            return rel_path, score

        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(_score_one, f): f for f in rel_files}
            for fut in as_completed(futs):
                rel_path, score = fut.result()
                if score > 0:
                    scores[rel_path] = score
        return scores

    def _find_files(self, title: str, body: str, repo_root: str) -> list:
        found = {}
        bug_text = title + " " + body

        # Priority 1: exact traceback paths (Python File "..." lines)
        for raw in re.findall(r'[Ff]ile ["\'](.+?\.py)["\']', body):
            raw_norm = raw.replace("\\", "/")
            for root, dirs, files in os.walk("."):
                dirs[:] = [d for d in dirs
                           if d not in (".git","__pycache__","node_modules",".venv","dist","build")]
                for fn in files:
                    full = os.path.join(root, fn).replace("\\", "/")
                    if full.endswith(raw_norm) or raw_norm.endswith(os.path.basename(fn)):
                        found[full] = self._score_file(full, bug_text) + 20

        # Priority 2: parallel content search
        identifiers = self._extract_identifiers(title, body)
        if identifiers:
            content_hits = self._find_files_by_content(identifiers, repo_root)
            for rel_path, score in content_hits.items():
                norm = rel_path.replace("\\", "/")
                combined = score + self._score_file(norm, bug_text)
                found[norm] = max(found.get(norm, 0), combined)

        # Priority 3: filenames mentioned in body
        if not found:
            clean = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
            names = {os.path.basename(p) for p in
                     re.findall(r"[\w\-/]+\.(?:py|js|rs|ts|cpp|h|go)", clean)}
            names -= {"utils.py","helpers.py","common.py","base.py","__init__.py",
                       "types.py","constants.py","config.py"}
            for root, dirs, files in os.walk("."):
                dirs[:] = [d for d in dirs
                           if d not in (".git","__pycache__","node_modules",".venv")]
                for fn in files:
                    if fn in names:
                        full = os.path.join(root, fn).replace("\\", "/")
                        found[full] = self._score_file(full, bug_text) + 5

        if not found:
            return []

        ranked = sorted(found.items(), key=lambda x: x[1], reverse=True)
        result = [p for p, _ in ranked[:3]]
        return result

    # ── Smart context window ──────────────────────────────────────────────────
    def _extract_context(self, content: str, title: str, body: str) -> str:
        if len(content) <= self.ctx_chars:
            return content

        identifiers = self._extract_identifiers(title, body)
        lines = content.splitlines()
        if not lines:
            return content[:self.ctx_chars]

        if not identifiers:
            return content[:self.ctx_chars]

        # Score every line; bonus for def/class/func signatures
        best_line, best_score = 0, 0
        for i, line in enumerate(lines):
            ll = line.lower()
            score = sum(1 for ident in identifiers if ident in ll)
            if re.match(r'\s*(def |async def |class |func |fn )', line):
                score += 2
            if score > best_score:
                best_score, best_line = score, i

        if best_score == 0:
            return content[:self.ctx_chars]

        # Extract a generous window centred on the best match
        start = max(0, best_line - 200)
        end, chars = start, 0
        while end < len(lines) and chars < self.ctx_chars:
            chars += len(lines[end]) + 1
            end += 1

        return "\n".join(lines[start:end])

    # ── Streamed Ollama call (output to llm log only, not stdout) ─────────────
    def _call_ollama_endpoint(self, payload: dict) -> str:
        try:
            emit("fix", "LLM generating patch...")
            _llm_log(f"\n--- LLM CALL ({time.strftime('%H:%M:%S')}) ---\n")
            r = self.session.post(f"{self.ollama_base}/api/chat",
                                  json=payload, timeout=self.timeout, stream=True)
            if r.status_code != 200:
                emit_fail("fix", f"Ollama HTTP {r.status_code}")
                return ""
            chunks = []
            for raw in r.iter_lines():
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                tok = obj.get("message", {}).get("content", "")
                if tok:
                    _llm_log(tok)  # debug log only — not user-facing
                    chunks.append(tok)
                if obj.get("done"):
                    break
            _llm_log("\n--- END ---\n")
            return "".join(chunks)
        except requests.Timeout:
            emit_fail("fix", f"Timed out after {self.timeout}s")
            return ""
        except Exception as e:
            emit_fail("fix", f"Ollama error: {e}")
            return ""

    def _call_ollama(self, bug_desc: str, file_path: str, file_content: str) -> str:
        user_msg = (
            f"Bug Report:\n{bug_desc}\n\n"
            f"File: {file_path}\n"
            f"```\n{file_content}\n```\n\n"
            "Produce the SEARCH/REPLACE block to fix this bug:"
        )
        payload = {
            "model":   self.model,
            "messages": [
                {"role": "system", "content": SURGERY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            "stream": True,
            "options": {"temperature": 0.05, "num_predict": 2048},
        }
        return self._call_ollama_endpoint(payload)

    def _call_ollama_retry(self, bug_desc: str, file_path: str, file_content: str, feedback: str) -> str:
        user_msg = (
            f"Bug Report:\n{bug_desc}\n\n"
            f"File: {file_path}\n"
            f"```\n{file_content}\n```\n\n"
            f"Your previous attempt failed:\n{feedback}\n\n"
            "Produce a corrected SEARCH/REPLACE block. Start with <<<SEARCH>>> immediately:"
        )
        payload = {
            "model":   self.model,
            "messages": [
                {"role": "system", "content": SURGERY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            "stream": True,
            "options": {"temperature": 0.05, "num_predict": 2048},
        }
        return self._call_ollama_endpoint(payload)

    # ── Strip AI artifacts from patch content ─────────────────────────────────
    def _strip_ai_artifacts(self, replace_text: str, search_text: str) -> str:
        """Remove LLM-generated comments not present in the original code."""
        orig_lines = set(l.strip() for l in search_text.splitlines())
        cleaned = []
        for line in replace_text.splitlines():
            stripped = line.strip()
            # Only filter comment lines that are NEW (not in original)
            if stripped.startswith("#") and stripped not in orig_lines:
                comment_lower = stripped.lower()
                ai_patterns = [
                    "# added", "# fixed", "# ensure", "# optimized",
                    "# todo", "# this will", "# this ensures",
                    "# handle the", "# check for", "# noqa",
                    "# as an ai", "# surgically", "# updated",
                    "# corrected", "# resolved", "# patched",
                ]
                if any(comment_lower.startswith(p) for p in ai_patterns):
                    continue
            cleaned.append(line)
        return "\n".join(cleaned)

    # ── Validate patch quality before writing to disk ─────────────────────────
    def _validate_patch_quality(self, search_text: str, replace_text: str) -> str | None:
        """Returns None if valid, or an error string if patch should be rejected."""
        search_lines = [l for l in search_text.splitlines() if l.strip()]
        replace_lines = [l for l in replace_text.splitlines() if l.strip()]

        # Reject if SEARCH block is completely empty
        if len(search_lines) < 1:
            return "SEARCH block is empty"

        # Reject if REPLACE is dramatically larger (LLM is rewriting, not fixing)
        max_allowed = max(len(search_lines) * 3, 12)
        if len(replace_lines) > max_allowed:
            return (f"REPLACE too large ({len(replace_lines)} lines vs "
                    f"{len(search_lines)} SEARCH lines) — not a surgical fix")

        # Reject if REPLACE contains conversational/markdown artifacts
        replace_lower = replace_text.lower()
        bad_artifacts = [
            "```python", "```javascript", "```typescript",
            "**search", "**replace",
            "to fix the bug", "here's the", "here is the",
            "this will ensure", "this ensures that",
            "the corrected", "updated implementation",
            "as an ai", "note:",
        ]
        for artifact in bad_artifacts:
            if artifact in replace_lower:
                return f"REPLACE contains LLM artifact: '{artifact}'"

        # Reject if REPLACE adds too many new imports (scope creep)
        new_imports = []
        search_stripped = {l.strip() for l in search_text.splitlines()}
        for line in replace_text.splitlines():
            if re.match(r'\s*(import |from \S+ import )', line):
                if line.strip() not in search_stripped:
                    new_imports.append(line.strip())
        if len(new_imports) > 2:
            return f"REPLACE adds {len(new_imports)} new imports (scope creep)"

        return None  # valid

    # ── Apply SEARCH/REPLACE with fuzzy fallback ──────────────────────────────
    def _apply_patch(self, llm_output: str, target_file: str) -> bool:
        if not llm_output or "NO_FIX" in llm_output:
            emit_skip("fix", "Model says file is unrelated (NO_FIX)")
            return False

        search_text = ""
        replace_text = ""

        # Strategy A — <<<SEARCH>>> / <<<REPLACE>>> / <<<END>>>
        m = re.search(
            r'<<<SEARCH>>>\s*\n?(.*?)\n?<<<REPLACE>>>\s*\n?(.*?)\n?<<<END>>>',
            llm_output, re.DOTALL
        )
        if m:
            search_text = m.group(1)
            replace_text = m.group(2)
        else:
            # Strategy B — Tolerant SEARCH / REPLACE parser
            pattern = (r'(?:\*?\*?SEARCH\*?\*?:?\s*\n?)(.*?)'
                       r'(?:\n?\*?\*?REPLACE\*?\*?:?\s*\n?)(.*?)'
                       r'(?:\n?<<<END>>>|\n?```|\n?$$|\Z)')
            m_tol = re.search(pattern, llm_output, re.DOTALL | re.IGNORECASE)
            if m_tol:
                search_text = m_tol.group(1)
                replace_text = m_tol.group(2)
            else:
                # Strategy C — conflict-marker diff format
                pattern_cm = r'<<<<<<< SEARCH\s*\n(.*?)\n=======\n(.*?)\n>>>>>>>'
                m_cm = re.search(pattern_cm, llm_output, re.DOTALL)
                if m_cm:
                    search_text = m_cm.group(1)
                    replace_text = m_cm.group(2)

        if not search_text and not replace_text:
            emit_fail("fix", "No valid SEARCH/REPLACE block in LLM output")
            _llm_log(f"\n--- UNPARSEABLE ---\n{llm_output[:500]}\n")
            return False

        # Clean up markdown code blocks the LLM may have wrapped around content
        def clean_fences(text: str) -> str:
            text = text.strip()
            text = re.sub(r'^```[a-zA-Z0-9_-]*\n', '', text)
            text = re.sub(r'\n```$', '', text)
            return text

        search_text = clean_fences(search_text)
        replace_text = clean_fences(replace_text)

        if not search_text.strip():
            emit_fail("fix", "SEARCH block is empty after cleaning")
            return False

        # ── Quality Gate — validate before writing ────────────────────────────
        rejection = self._validate_patch_quality(search_text, replace_text)
        if rejection:
            emit_fail("fix", f"Patch rejected → {rejection}")
            return False

        # ── Strip AI artifacts from REPLACE ───────────────────────────────────
        replace_text = self._strip_ai_artifacts(replace_text, search_text)

        try:
            original = open(target_file, encoding="utf-8", errors="replace").read()
        except Exception as e:
            emit_fail("fix", f"Cannot read {target_file}: {e}")
            return False

        # Exact match
        new_content = None
        match_type  = None

        if search_text in original:
            new_content = original.replace(search_text, replace_text, 1)
            match_type = "exact"
        else:
            # Fuzzy: strip trailing whitespace per line
            def strip_lines(s): return "\n".join(l.rstrip() for l in s.splitlines())
            s_stripped = strip_lines(search_text)
            o_stripped = strip_lines(original)
            if s_stripped in o_stripped:
                new_content = o_stripped.replace(s_stripped, replace_text.rstrip(), 1)
                match_type = "fuzzy-ws"
            else:
                # Last resort: indent-agnostic
                def strip_all(s):
                    return "\n".join(l.strip() for l in s.splitlines() if l.strip())
                s2 = strip_all(search_text)
                lines_orig = original.splitlines()
                for i, line in enumerate(lines_orig):
                    window = "\n".join(l.strip() for l in
                                       lines_orig[i:i+len(search_text.splitlines())] if l.strip())
                    if window == s2:
                        n_lines = len(search_text.splitlines())
                        new_lines = lines_orig[:i] + replace_text.splitlines() + lines_orig[i+n_lines:]
                        new_content = "\n".join(new_lines)
                        match_type = "indent-agnostic"
                        break

        if new_content is None:
            emit_fail("fix", "SEARCH block not found in file (3 strategies)")
            return False

        # ── Pre-write AST validation for Python files ─────────────────────────
        if target_file.endswith(".py"):
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                emit_fail("fix", f"Patch would break syntax at line {e.lineno} — rejected pre-write")
                return False

        with open(target_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)

        search_n = len(search_text.strip().splitlines())
        replace_n = len(replace_text.strip().splitlines())
        basename = os.path.basename(target_file)
        emit_ok("fix", f"Patched {basename} ({match_type}, {search_n}→{replace_n} lines)")
        return True

    # ── Main surgery loop ──────────────────────────────────────────────────────
    def operate(self, bug: dict, repo_root: str, repo_full: str = "") -> bool:
        self.last_skip_reason = None

        if not self._ollama_ok():
            self.last_skip_reason = "ollama"
            return False

        self._repo_root = repo_root
        title = bug.get("title", "")
        body  = bug.get("body") or ""

        # ── Enrich context with issue comments ────────────────────────────────
        if repo_full and bug.get("number"):
            comments_text = self._fetch_issue_comments(repo_full, bug["number"])
            if comments_text:
                n_comments = comments_text.count("Comment by")
                emit("think", f"Loaded {n_comments} issue comment(s) for context")
                body = body + "\n\n--- Discussion ---\n" + comments_text

        bug_desc = f"Title: {title}\n\n{body[:3000]}"

        targets = self._find_files(title, body, repo_root)
        if not targets:
            emit_fail("target", "No candidate files found in repository")
            self.last_skip_reason = "no_files"
            return False

        target_names = [os.path.basename(t) for t in targets]
        emit("target", f"Candidates: {', '.join(target_names)}")

        # ── Comprehension Gate — does the LLM understand the bug? ─────────────
        emit("think", "Analyzing root cause before attempting fix...")
        comprehension = self._comprehend_bug(bug_desc, targets)
        if not comprehension:
            emit_fail("think", "Cannot determine root cause — skipping this bug")
            self.last_skip_reason = "comprehension"
            return False

        root_cause = comprehension.get("ROOT_CAUSE", "?")
        confidence = comprehension.get("CONFIDENCE", "?")
        fix_type   = comprehension.get("FIX_TYPE", "?")
        emit_ok("think", f"{root_cause[:80]}")
        emit("think", f"Confidence: {confidence} | Type: {fix_type}")

        # Enrich bug description with the LLM's own analysis
        bug_desc += f"\n\nRoot cause analysis: {root_cause}"

        for tf in targets[:3]:   # try top 3 files
            # Resolve to absolute path for the cache
            if not os.path.isabs(tf):
                abs_tf = os.path.join(repo_root, tf.lstrip("./"))
            else:
                abs_tf = tf

            try:
                raw = _cached_read(abs_tf)
            except Exception as e:
                emit_fail("target", f"Cannot read {os.path.basename(abs_tf)}: {e}")
                continue

            context = self._extract_context(raw, title, body)

            # --- AGENTIC SELF-CORRECTION LOOP ---
            attempts = 2
            error_feedback = ""

            for attempt in range(attempts):
                if error_feedback:
                    emit("fix", f"Retry {attempt+1}/{attempts} with error feedback...")
                    llm_out = self._call_ollama_retry(bug_desc, tf, context, error_feedback)
                else:
                    llm_out = self._call_ollama(bug_desc, tf, context)

                if not llm_out:
                    break

                if self._apply_patch(llm_out, abs_tf):
                    changed = subprocess.run(
                        ["git", "diff", "--name-only"],
                        capture_output=True, text=True
                    ).stdout.strip()
                    if changed:
                        return True
                    else:
                        emit("fix", "Patch written but git shows no diff — next file")
                        break
                else:
                    error_feedback = (
                        "The SEARCH block was not found in the file. "
                        "Copy the exact lines from the file character-for-character, "
                        "including all whitespace and indentation."
                    )

        emit_fail("fix", "All target files exhausted — no valid fix produced")
        self.last_skip_reason = "all_exhausted"
        return False


# Module-level LRU cache — keyed on ABSOLUTE path to survive os.chdir()
@lru_cache(maxsize=256)
def _cached_read(abs_path: str) -> str:
    return open(abs_path, encoding="utf-8", errors="replace").read(80000)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — VERIFY (enhanced: py_compile + ast.parse)
# ═══════════════════════════════════════════════════════════════════════════════
class Verifier:
    def run(self) -> bool:
        emit("verify", "Running syntax + structure checks...")
        diff = subprocess.run(["git", "diff", "--name-only"],
                              capture_output=True, text=True)
        py_files = [f.strip() for f in diff.stdout.strip().splitlines()
                    if f.strip().endswith(".py") and os.path.isfile(f.strip())]
        if not py_files:
            emit_ok("verify", "No Python files changed")
            return True

        errors = []
        for f in py_files:
            # Check 1: py_compile
            r = subprocess.run([sys.executable, "-m", "py_compile", f],
                               capture_output=True, text=True)
            if r.returncode != 0:
                errors.append(f"  {f}: {r.stderr.strip()[:200]}")
                continue

            # Check 2: ast.parse for structural validation
            try:
                source = open(f, encoding="utf-8", errors="replace").read()
                tree = ast.parse(source)
            except SyntaxError as e:
                errors.append(f"  {f}: AST parse error at line {e.lineno}")
                continue

        if errors:
            emit_fail("verify", f"{len(errors)} error(s) found:")
            for e in errors:
                print(e, flush=True)
            return False

        emit_ok("verify", f"{len(py_files)} file(s) clean — syntax + AST validated")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — PUSH + PR
# ═══════════════════════════════════════════════════════════════════════════════
class Committer:
    JUNK = {".gitignore", "sbk_run.log", "sbk_llm.log", "metrics.json"}

    def __init__(self, session: requests.Session):
        self.session = session
        self.last_pr_url = None  # set by push() for summary tracking

    def _wait_for_fork(self, github_user, repo_short, headers, max_wait=20):
        for i in range(max_wait // 2):
            chk = self.session.get(
                f"https://api.github.com/repos/{github_user}/{repo_short}",
                headers=headers, timeout=10)
            if chk.status_code == 200:
                emit_ok("push", f"Fork ready ({(i+1)*2}s)")
                return chk.json()
            time.sleep(2)
        return None

    def _create_pr(self, repo_full, branch, bug_number, bug_title,
                   github_user, headers):
        """Open a pull request from the fork branch to upstream default branch."""
        try:
            pr_body = (
                f"Fixes #{bug_number}\n\n"
                f"This PR resolves: **{bug_title}**\n\n"
                f"---\n*Verified: syntax + AST checks passed ✓*"
            )
            r = self.session.post(
                f"https://api.github.com/repos/{repo_full}/pulls",
                headers=headers,
                json={
                    "title": f"fix: resolve issue #{bug_number} — {bug_title[:60]}",
                    "body":  pr_body,
                    "head":  f"{github_user}:{branch}",
                    "base":  "main",   # try main first
                },
                timeout=15
            )
            if r.status_code in (200, 201):
                pr_url = r.json().get("html_url", "")
                emit_ok("pr", f"Opened → {pr_url}")
                metrics.increment_metric("prs_opened")
                return pr_url
            # Retry with master
            if r.status_code == 422:
                r2 = self.session.post(
                    f"https://api.github.com/repos/{repo_full}/pulls",
                    headers=headers,
                    json={
                        "title": f"fix: resolve issue #{bug_number} — {bug_title[:60]}",
                        "body":  pr_body,
                        "head":  f"{github_user}:{branch}",
                        "base":  "master",
                    },
                    timeout=15
                )
                if r2.status_code in (200, 201):
                    pr_url = r2.json().get("html_url", "")
                    emit_ok("pr", f"Opened → {pr_url}")
                    metrics.increment_metric("prs_opened")
                    return pr_url
            emit_fail("pr", f"PR creation returned {r.status_code}: {r.text[:100]}")
            return None
        except Exception as e:
            emit_fail("pr", f"Could not open PR: {e}")
            return None

    def push(self, repo, bug, github_user, token, headers, user_email="sbk@sudhanwa.dev", user_name="Sudhanwa-git") -> bool:
        self.last_pr_url = None
        repo_full  = repo["full_name"]
        repo_short = repo_full.split("/")[-1]
        bug_number = bug["number"]
        bug_title  = bug.get("title", "")
        branch     = f"fix/issue-{bug_number}"

        emit("push", f"Forking {repo_full}...")
        r = self.session.post(f"https://api.github.com/repos/{repo_full}/forks",
                              headers=headers, json={}, timeout=15)
        if r.status_code not in [200, 202]:
            emit_fail("push", f"Fork failed ({r.status_code})")
            return False

        fork_data = self._wait_for_fork(github_user, repo_short, headers)
        if not fork_data:
            emit_fail("push", "Fork not ready — giving up")
            return False

        fork_url    = fork_data["clone_url"]
        auth_remote = fork_url.replace("https://", f"https://{github_user}:{token}@")

        try:
            subprocess.run(["git", "config", "user.email", user_email],
                           check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", user_name],
                           check=True, capture_output=True)
            subprocess.run(["git", "remote", "set-url", "origin", auth_remote],
                           check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", branch],
                           check=True, capture_output=True)

            diff_files = subprocess.run(["git", "diff", "--name-only"],
                                        capture_output=True, text=True
                                        ).stdout.strip().splitlines()
            src = [
                f.strip() for f in diff_files
                if f.strip()
                and os.path.basename(f.strip()) not in self.JUNK
                and not f.strip().startswith(".")
                and os.path.isfile(f.strip())
            ]
            if not src:
                emit_fail("push", "Nothing to commit")
                return False

            staged = ", ".join(os.path.basename(s) for s in src)
            emit("push", f"Staging: {staged}")
            subprocess.run(["git", "add", "--"] + src, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"fix: resolve issue #{bug_number}"],
                check=True, capture_output=True
            )
            push = subprocess.run(
                ["git", "push", "--force", "--set-upstream", "origin", branch],
                capture_output=True, text=True
            )
            if push.returncode != 0:
                emit_fail("push", f"Push failed: {push.stderr.strip()[:200]}")
                return False

            emit_ok("push", f"Branch live → {github_user}/{repo_short}/tree/{branch}")

            # Open a PR automatically
            pr_url = self._create_pr(repo_full, branch, bug_number, bug_title,
                                     github_user, headers)
            self.last_pr_url = pr_url
            if not pr_url:
                emit("push", "Branch pushed — PR creation skipped")
            return True

        except subprocess.CalledProcessError as e:
            emit_fail("push", f"Git error: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR (with mission summary + score-based selection)
# ═══════════════════════════════════════════════════════════════════════════════
class SurgicalBugSniper:
    def __init__(self):
        self.model       = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.ollama_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
        self.token       = os.getenv("GITHUB_TOKEN", "")

        self.session = _make_session()
        self.session.headers.update({
            "Authorization": f"token {self.token}",
            "Accept":        "application/vnd.github.v3+json",
        })
        self.headers = dict(self.session.headers)

        self.github_user = None
        try:
            r = self.session.get("https://api.github.com/user", timeout=10)
            if r.status_code == 200:
                self.github_user = r.json().get("login")
                emit_ok("init", f"GitHub: {self.github_user}")
            else:
                emit_fail("init", f"Auth failed ({r.status_code}) — push disabled")
        except Exception as e:
            emit_fail("init", f"GitHub error: {e}")

        # Get local Git email/name for contribution attribution
        self.user_email = "sbk@sudhanwa.dev"
        self.user_name = "Sudhanwa-git"
        try:
            email_res = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
            if email_res.returncode == 0 and email_res.stdout.strip():
                self.user_email = email_res.stdout.strip()
            name_res = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
            if name_res.returncode == 0 and name_res.stdout.strip():
                self.user_name = name_res.stdout.strip()
        except Exception:
            pass

        self.hunter    = RepoHunter(self.session)
        self.cloner    = AutoCloner()
        self.surgeon   = Surgeon(self.model, self.ollama_base, self.session)
        self.verifier  = Verifier()
        self.committer = Committer(self.session)

        # Mission summary tracking
        self.summary = {
            "repo": None,
            "bugs_scanned": 0,
            "bugs_attempted": 0,
            "skipped_comprehension": 0,
            "skipped_verify": 0,
            "skipped_no_fix": 0,
            "fix_file": None,
            "pr_url": None,
        }

    def _cleanup(self):
        d = self.cloner.base
        if os.path.exists(d):
            try:
                shutil.rmtree(d, ignore_errors=True)
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass

    def _parallel_scan(self, candidates: list) -> tuple:
        """
        Parallel-scan all repos. Instead of random selection,
        pick the repo with the highest-scoring individual bug.
        """
        emit("hunt", f"Scanning {len(candidates)} repos in parallel...")

        def _fetch(name):
            repo = self.hunter.get_repo(name)
            if not repo:
                return None, []
            bugs = self.hunter.scan_bugs(repo["full_name"])
            return repo, bugs

        results = []
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futs = {pool.submit(_fetch, n): n for n in candidates}
            for fut in as_completed(futs):
                repo, bugs = fut.result()
                if repo and bugs:
                    results.append((repo, bugs))

        if not results:
            return None, []

        # Pick repo with the highest single-bug solvability score
        results.sort(
            key=lambda r: max(self.hunter._score_difficulty(b) for b in r[1]),
            reverse=True
        )
        selected = results[0]
        best_score = max(self.hunter._score_difficulty(b) for b in selected[1])
        emit_ok("hunt", f"Best target: {selected[0]['full_name']} "
                        f"({len(selected[1])} bugs, top score: {best_score})")
        return selected

    def _print_summary(self):
        """Print a clean mission summary at the end of every run."""
        s = self.summary
        print(flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print("  MISSION SUMMARY", flush=True)
        print(" ───────────────────────────────────────────────────────────", flush=True)
        if s["repo"]:
            print(f"  Repo              {s['repo']}", flush=True)
        print(f"  Bugs scanned      {s['bugs_scanned']}", flush=True)
        print(f"  Bugs attempted    {s['bugs_attempted']}", flush=True)
        if s["skipped_comprehension"]:
            print(f"  Skipped (unclear) {s['skipped_comprehension']}", flush=True)
        if s["skipped_verify"]:
            print(f"  Skipped (syntax)  {s['skipped_verify']}", flush=True)
        if s["skipped_no_fix"]:
            print(f"  Skipped (no fix)  {s['skipped_no_fix']}", flush=True)
        if s["fix_file"]:
            print(f"  Fix applied       {s['fix_file']}", flush=True)
        if s["pr_url"]:
            print(f"  PR opened         {s['pr_url']}", flush=True)
        elif s["fix_file"]:
            print(f"  PR opened         push failed", flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)

    def run(self):
        print(flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print("  SURGICAL BUG SNIPER  v6  ·  Quality-First", flush=True)
        print("  HUNT → CLONE → COMPREHEND → FIX → VERIFY → PUSH", flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print(flush=True)

        self._cleanup()

        # Clear LLM debug log for this run
        try:
            open(LLM_LOG_FILE, "w").close()
        except Exception:
            pass

        candidates = WHITELIST.copy()
        random.shuffle(candidates)
        emit("init", f"Targets: {' · '.join(c.split('/')[-1] for c in candidates)}")

        repo, bugs = self._parallel_scan(candidates)
        if not repo or not bugs:
            emit_fail("done", "No solvable bugs found across all repos")
            self._print_summary()
            return

        self.summary["repo"] = repo["full_name"]
        self.summary["bugs_scanned"] = len(bugs)

        local = self.cloner.clone(repo)
        if not local:
            self._print_summary()
            return

        orig = os.getcwd()
        os.chdir(local)
        repo_root = local   # absolute — passed to surgeon so LRU cache stays valid

        max_bugs = int(os.getenv("MAX_BUGS_PER_REPO", "3"))

        try:
            for bug in bugs[:max_bugs]:
                print(flush=True)
                title_short = bug["title"][:65]
                emit("bug", f"#{bug['number']}: {title_short}")

                metrics.increment_metric("issues_attempted")
                self.summary["bugs_attempted"] += 1

                if not self.surgeon.operate(bug, repo_root, repo_full=repo["full_name"]):
                    subprocess.run(["git", "checkout", "."], capture_output=True)
                    # Track skip reason
                    reason = self.surgeon.last_skip_reason
                    if reason == "comprehension":
                        self.summary["skipped_comprehension"] += 1
                    elif reason in ("no_files", "all_exhausted", "ollama"):
                        self.summary["skipped_no_fix"] += 1
                    continue

                if not self.verifier.run():
                    subprocess.run(["git", "checkout", "."], capture_output=True)
                    self.summary["skipped_verify"] += 1
                    continue

                # Record what was fixed
                diff_output = subprocess.run(
                    ["git", "diff", "--name-only"],
                    capture_output=True, text=True
                ).stdout.strip()
                self.summary["fix_file"] = diff_output.replace("\n", ", ")

                if not self.github_user:
                    emit_fail("push", "No GitHub auth — cannot push")
                    continue

                if self.committer.push(
                    repo=repo, bug=bug,
                    github_user=self.github_user,
                    token=self.token,
                    headers=self.headers,
                    user_email=self.user_email,
                    user_name=self.user_name,
                ):
                    self.summary["pr_url"] = self.committer.last_pr_url or "pushed (no PR)"
                    print(flush=True)
                    emit_ok("done", "MISSION COMPLETE")
                    self._print_summary()
                    return

        except Exception as e:
            emit_fail("error", str(e))
        finally:
            os.chdir(orig)

        emit("done", "All bugs tried — no fix committed this run")
        self._print_summary()


if __name__ == "__main__":
    SurgicalBugSniper().run()
