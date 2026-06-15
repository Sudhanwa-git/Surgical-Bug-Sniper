"""
Sudhanwa's Surgical Bug Sniper — sbk.py
Pipeline: HUNT → CLONE → SURGERY → VERIFY → PUSH → PR

v5: Full fix — broader bug discovery, robust surgery, PR creation,
    fast parallel scan, LRU-safe absolute paths, streamed inference.
"""

import os, re, sys, shutil, subprocess, time, requests, random, json, metrics
from concurrent.futures import ThreadPoolExecutor, as_completed, FIRST_COMPLETED, wait
from functools import lru_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(override=True)


def log(msg: str):
    print(msg, flush=True)


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
# STEP 1 — HUNT
# ═══════════════════════════════════════════════════════════════════════════════
class RepoHunter:
    STOP = {"the","and","for","with","that","this","from","are","has","not",
            "bug","fix","error","issue","using","when","use","causes","cause",
            "fails","fail","crash","weird","strange","also","into","does","its",
            "have","been","please","after","before","then","just","only","some",
            "which","they","them","their","there","these","those","more","will",
            "what","how","why","who","can","should","could","would","may","might"}

    def __init__(self, session: requests.Session):
        self.session = session

    def get_repo(self, name):
        try:
            r = self.session.get(f"https://api.github.com/repos/{name}", timeout=10)
            if r.status_code == 200:
                d = r.json()
                return {"full_name": d["full_name"], "clone_url": d["clone_url"]}
            log(f"[ HUNT ] ✗ {name} (HTTP {r.status_code})")
            return None
        except Exception as e:
            log(f"[ HUNT ] ✗ {name} — {e}")
            return None

    def _is_actionable(self, title: str, body: str) -> bool:
        """
        Permissive actionability — accepts any bug with meaningful technical content.
        Previously required a traceback; now accepts code blocks, error messages,
        or simply a sufficiently detailed description.
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
        return positives >= 1   # just ONE signal is enough

    def _is_vague(self, title: str) -> bool:
        vague = {"weird", "strange", "unexpected", "sometimes", "intermittent",
                 "random", "flaky", "occasionally"}
        tl = title.lower()
        return any(v in tl for v in vague)

    def scan_bugs(self, repo_full_name: str) -> list:
        """
        Multi-strategy bug scan:
        1. Try known bug labels one by one
        2. If nothing found, pull top-30 open issues and self-filter
        This ensures we always find something even when repos use custom labels.
        """
        log(f"[ HUNT ] Scanning {repo_full_name}...")
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
                    log(f"[ HUNT ] {len(bugs)} bug(s) via label '{label}' in {repo_full_name}")
                    return bugs[:5]
            except Exception:
                continue

        # Strategy B — any open issue that looks like a bug
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

        log(f"[ HUNT ] {len(bugs)} actionable issue(s) in {repo_full_name} (unlabelled scan)")
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
            log("[ CLONE ] Removing stale clone...")
            if not self._rmdir(target) or os.path.exists(target):
                target = os.path.join(self.base, f"{slug}_{int(time.time())}")

        log(f"[ CLONE ] Fetching {repo['full_name']} (blobless shallow)...")
        try:
            r = subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 "--filter=blob:none", url, target],
                timeout=180, capture_output=True, text=True,
                encoding="utf-8", errors="replace"
            )
            if r.returncode == 0:
                log("[ CLONE ] ✓ Ready")
                return target
            log(f"[ CLONE ] ✗ Failed: {r.stderr.strip()[:200]}")
            return None
        except subprocess.TimeoutExpired:
            log("[ CLONE ] ✗ Timed out")
            self._rmdir(target)
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SURGERY
# ═══════════════════════════════════════════════════════════════════════════════
SURGERY_SYSTEM = """\
You are an expert software engineer specializing in surgical code fixes. 
Your task is to analyze the bug description and the provided file context, identify the exact root cause, and produce a SEARCH/REPLACE block to fix it.

Format your output EXACTLY as shown in this block. Output ONLY the block, with no conversational prefix, suffix, introduction, or explanations:
<<<SEARCH>>>
[exact lines to change, copied verbatim from the file including all whitespace and indentation]
<<<REPLACE>>>
[corrected lines]
<<<END>>>

CRITICAL INSTRUCTIONS:
- You must locate the bug in the provided context and fix it. Be precise.
- The <<<SEARCH>>> block must match the existing lines in the file character-for-character (same indentation, trailing spaces, and line breaks).
- Only change what is absolutely broken to resolve the bug. Do not refactor, rewrite, add comments, or introduce unrelated imports.
- Make the SMALLEST and most precise change possible.
- Output ONLY raw search/replace block. Do NOT explain your changes.
- If and ONLY if the provided file has absolutely no relation to the bug, output exactly: NO_FIX"""


class Surgeon:
    def __init__(self, model: str, ollama_base: str, session: requests.Session):
        self.model       = model.split("/")[-1]
        self.ollama_base = ollama_base.rstrip("/")
        self.ctx_chars   = int(os.getenv("SURGERY_CONTEXT_CHARS", "35000"))
        self.timeout     = int(os.getenv("SURGERY_TIMEOUT_SEC",  "180"))
        self.session     = session
        self._repo_root  = None   # set by operate() so cache keys are absolute

    # ── Health check ──────────────────────────────────────────────────────────
    def _ollama_ok(self) -> bool:
        try:
            r = self.session.get(f"{self.ollama_base}/api/tags", timeout=5)
            if r.status_code != 200:
                log(f"[ SURGERY ] Ollama error {r.status_code}. Is it running?")
                return False
            models = [m["name"] for m in r.json().get("models", [])]
            tag  = self.model if ":" in self.model else self.model + ":latest"
            base = tag.split(":")[0]
            if not any(m.startswith(base) for m in models):
                log(f"[ SURGERY ] Model '{tag}' not found. Run: ollama pull {tag}")
                return False
            log(f"[ SURGERY ] Ollama ✓ — {tag} ready")
            return True
        except Exception as e:
            log(f"[ SURGERY ] Cannot reach Ollama: {e}")
            return False

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
                        abs_f = os.path.join(repo_root, full.lstrip("./"))
                        found[full] = self._score_file(full, bug_text) + 20

        # Priority 2: parallel content search
        identifiers = self._extract_identifiers(title, body)
        if identifiers:
            log(f"[ SURGERY ] Key identifiers: {identifiers[:6]}")
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
            log("[ SURGERY ] No target files found — skipping")
            return []

        ranked = sorted(found.items(), key=lambda x: x[1], reverse=True)
        result = [p for p, _ in ranked[:3]]
        log(f"[ SURGERY ] Ranked targets: {result}")
        return result

    # ── Smart context window ──────────────────────────────────────────────────
    def _extract_context(self, content: str, title: str, body: str) -> str:
        if len(content) <= self.ctx_chars:
            log(f"[ SURGERY ] File is small ({len(content)} chars) — providing complete file context")
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
        # Let's take up to 200 lines above and 250 lines below
        start = max(0, best_line - 200)
        end, chars = start, 0
        while end < len(lines) and chars < self.ctx_chars:
            chars += len(lines[end]) + 1
            end += 1

        log(f"[ SURGERY ] Generous Window: lines {start+1}–{end} (peak at {best_line+1})")
        return "\n".join(lines[start:end])

    # ── Streamed Ollama call ──────────────────────────────────────────────────
    def _call_ollama_endpoint(self, payload: dict) -> str:
        try:
            log(f"[ SURGERY ] Calling {self.model} (stream)...")
            r = self.session.post(f"{self.ollama_base}/api/chat",
                                  json=payload, timeout=self.timeout, stream=True)
            if r.status_code != 200:
                log(f"[ SURGERY ] Ollama HTTP {r.status_code}")
                return ""
            chunks = []
            print("[ SURGERY ] ▶ ", end="", flush=True)
            for raw in r.iter_lines():
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                tok = obj.get("message", {}).get("content", "")
                if tok:
                    print(tok, end="", flush=True)
                    chunks.append(tok)
                if obj.get("done"):
                    break
            print()
            return "".join(chunks)
        except requests.Timeout:
            log(f"\n[ SURGERY ] ✗ Timed out after {self.timeout}s")
            return ""
        except Exception as e:
            log(f"\n[ SURGERY ] ✗ Ollama error: {e}")
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
            f"Your previous SEARCH/REPLACE block failed to apply:\n{feedback}\n\n"
            "Please produce a corrected SEARCH/REPLACE block where the SEARCH block exists in the file content character-for-character:"
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

    # ── Apply SEARCH/REPLACE with fuzzy fallback ──────────────────────────────
    def _apply_patch(self, llm_output: str, target_file: str) -> bool:
        if not llm_output or "NO_FIX" in llm_output:
            log("[ SURGERY ] Model said NO_FIX")
            return False

        search_text = ""
        replace_text = ""

        # Strategy A — Try custom <<<SEARCH>>> and <<<REPLACE>>> tags
        m = re.search(
            r'<<<SEARCH>>>\s*\n?(.*?)\n?<<<REPLACE>>>\s*\n?(.*?)\n?<<<END>>>',
            llm_output, re.DOTALL
        )
        if m:
            search_text = m.group(1)
            replace_text = m.group(2)
        else:
            # Strategy B — Tolerant SEARCH / REPLACE parser (with or without colon/bolding/code blocks)
            pattern = r'(?:\*?\*?SEARCH\*?\*?:?\s*\n?)(.*?)(?:\n?\*?\*?REPLACE\*?\*?:?\s*\n?)(.*?)(?:\n?<<<END>>>|\n?```|\n?$$|\Z)'
            m_tol = re.search(pattern, llm_output, re.DOTALL | re.IGNORECASE)
            if m_tol:
                search_text = m_tol.group(1)
                replace_text = m_tol.group(2)
            else:
                # Strategy C — Aider diff format (<<<<<<< SEARCH / ======= / >>>>>>>)
                pattern_aider = r'<<<<<<< SEARCH\s*\n(.*?)\n=======\n(.*?)\n>>>>>>>'
                m_aider = re.search(pattern_aider, llm_output, re.DOTALL)
                if m_aider:
                    search_text = m_aider.group(1)
                    replace_text = m_aider.group(2)

        if not search_text and not replace_text:
            log(f"[ SURGERY ] ✗ No valid block found — raw output:\n{llm_output[:300]}")
            return False

        # Clean up markdown code blocks if the LLM wrapped the search/replace contents in them
        def clean_fences(text: str) -> str:
            text = text.strip()
            text = re.sub(r'^```[a-zA-Z0-9_-]*\n', '', text)
            text = re.sub(r'\n```$', '', text)
            return text

        search_text = clean_fences(search_text)
        replace_text = clean_fences(replace_text)

        if not search_text.strip():
            log("[ SURGERY ] ✗ SEARCH block is empty after cleaning")
            return False

        try:
            original = open(target_file, encoding="utf-8", errors="replace").read()
        except Exception as e:
            log(f"[ SURGERY ] ✗ Cannot read {target_file}: {e}")
            return False

        # Exact match
        if search_text in original:
            new_content = original.replace(search_text, replace_text, 1)
            log("[ SURGERY ] ✓ Exact match — patching")
        else:
            # Fuzzy: strip trailing whitespace per line
            def strip_lines(s): return "\n".join(l.rstrip() for l in s.splitlines())
            s_stripped = strip_lines(search_text)
            o_stripped = strip_lines(original)
            if s_stripped in o_stripped:
                new_content = o_stripped.replace(s_stripped, replace_text.rstrip(), 1)
                log("[ SURGERY ] ✓ Fuzzy whitespace match — patching")
            else:
                # Last resort: try stripping ALL leading whitespace
                def strip_all(s):
                    return "\n".join(l.strip() for l in s.splitlines() if l.strip())
                s2 = strip_all(search_text)
                lines_orig = original.splitlines()
                for i, line in enumerate(lines_orig):
                    window = "\n".join(l.strip() for l in lines_orig[i:i+len(search_text.splitlines())] if l.strip())
                    if window == s2:
                        # Replace those exact lines in original
                        n_lines = len(search_text.splitlines())
                        new_lines = lines_orig[:i] + replace_text.splitlines() + lines_orig[i+n_lines:]
                        new_content = "\n".join(new_lines)
                        log("[ SURGERY ] ✓ Indent-agnostic match — patching")
                        break
                else:
                    log(f"[ SURGERY ] ✗ SEARCH not found (3 strategies tried)")
                    log(f"[ SURGERY ] Searched:\n{search_text[:150]}")
                    return False

        with open(target_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
        log(f"[ SURGERY ] ✓ Patched: {target_file}")
        return True

    # ── Main surgery loop ──────────────────────────────────────────────────────
    def operate(self, bug: dict, repo_root: str) -> bool:
        if not self._ollama_ok():
            return False

        self._repo_root = repo_root
        title = bug.get("title", "")
        body  = bug.get("body") or ""
        bug_desc = f"Title: {title}\n\n{body[:2000]}"

        targets = self._find_files(title, body, repo_root)
        if not targets:
            return False

        for tf in targets[:3]:   # try top 3 files
            # Resolve to absolute path for the cache
            if not os.path.isabs(tf):
                abs_tf = os.path.join(repo_root, tf.lstrip("./"))
            else:
                abs_tf = tf

            try:
                raw = _cached_read(abs_tf)
            except Exception as e:
                log(f"[ SURGERY ] Cannot read {abs_tf}: {e}")
                continue

            context = self._extract_context(raw, title, body)

            # --- AGENTIC SELF-CORRECTION LOOP ---
            attempts = 2
            error_feedback = ""

            for attempt in range(attempts):
                if error_feedback:
                    log(f"[ SURGERY ] Retrying {tf} (Attempt {attempt+1}/{attempts}) with error feedback...")
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
                        log(f"[ SURGERY ] ✓ Modified: {changed}")
                        return True
                    else:
                        log("[ SURGERY ] Patch written but git shows no diff — retrying next file")
                        break
                else:
                    error_feedback = (
                        "The SEARCH block you provided was not found in the file. "
                        "Please make sure the SEARCH block matches the file content character-for-character, "
                        "including all whitespace and indentation exactly."
                    )

        log("[ SURGERY ] ✗ All targets exhausted")
        return False


# Module-level LRU cache — keyed on ABSOLUTE path to survive os.chdir()
@lru_cache(maxsize=256)
def _cached_read(abs_path: str) -> str:
    return open(abs_path, encoding="utf-8", errors="replace").read(80000)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — VERIFY
# ═══════════════════════════════════════════════════════════════════════════════
class Verifier:
    def run(self) -> bool:
        log("[ VERIFY ] Checking syntax...")
        diff = subprocess.run(["git", "diff", "--name-only"],
                              capture_output=True, text=True)
        py_files = [f.strip() for f in diff.stdout.strip().splitlines()
                    if f.strip().endswith(".py") and os.path.isfile(f.strip())]
        if not py_files:
            log("[ VERIFY ] ✓ No Python files changed")
            return True
        errors = []
        for f in py_files:
            r = subprocess.run([sys.executable, "-m", "py_compile", f],
                               capture_output=True, text=True)
            if r.returncode != 0:
                errors.append(f"  {f}: {r.stderr.strip()}")
        if errors:
            log("[ VERIFY ] ✗ Syntax errors:\n" + "\n".join(errors))
            return False
        log(f"[ VERIFY ] ✓ {len(py_files)} file(s) clean")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — PUSH + PR
# ═══════════════════════════════════════════════════════════════════════════════
class Committer:
    JUNK = {".aider.chat.history.md", ".aider.input.history",
            ".gitignore", ".aiderignore", "sbk_run.log"}

    def __init__(self, session: requests.Session):
        self.session = session

    def _wait_for_fork(self, github_user, repo_short, headers, max_wait=20):
        for i in range(max_wait // 2):
            chk = self.session.get(
                f"https://api.github.com/repos/{github_user}/{repo_short}",
                headers=headers, timeout=10)
            if chk.status_code == 200:
                log(f"[ PUSH ] Fork ready in ~{(i+1)*2}s")
                return chk.json()
            log(f"[ PUSH ] Waiting for fork... {(i+1)*2}s")
            time.sleep(2)
        return None

    def _create_pr(self, repo_full, branch, bug_number, bug_title,
                   github_user, headers):
        """Open a pull request from the fork branch to upstream default branch."""
        try:
            pr_body = (
                f"Fixes #{bug_number}\n\n"
                f"This PR resolves the reported issue: **{bug_title}**\n\n"
                f"Changes were identified and applied automatically by "
                f"[Surgical Bug Sniper](https://github.com/{github_user}).\n\n"
                f"---\n*Verified: Python syntax check passed ✓*"
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
                log(f"[ PR ] ✓ Pull request opened: {pr_url}")
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
                    log(f"[ PR ] ✓ Pull request opened: {pr_url}")
                    metrics.increment_metric("prs_opened")
                    return pr_url
            log(f"[ PR ] ⚠ PR creation returned {r.status_code}: {r.text[:150]}")
            return None
        except Exception as e:
            log(f"[ PR ] ⚠ Could not open PR: {e}")
            return None

    def push(self, repo, bug, github_user, token, headers, user_email="sbk@sudhanwa.dev", user_name="Sudhanwa-git") -> bool:
        repo_full  = repo["full_name"]
        repo_short = repo_full.split("/")[-1]
        bug_number = bug["number"]
        bug_title  = bug.get("title", "")
        branch     = f"fix/issue-{bug_number}"

        log(f"[ PUSH ] Forking {repo_full}...")
        r = self.session.post(f"https://api.github.com/repos/{repo_full}/forks",
                              headers=headers, json={}, timeout=15)
        if r.status_code not in [200, 202]:
            log(f"[ PUSH ] ✗ Fork failed ({r.status_code}): {r.text[:150]}")
            return False

        fork_data = self._wait_for_fork(github_user, repo_short, headers)
        if not fork_data:
            log("[ PUSH ] ✗ Fork not ready — giving up")
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
                and not f.strip().startswith(".aider")
                and os.path.isfile(f.strip())
            ]
            if not src:
                log("[ PUSH ] ✗ Nothing to commit")
                return False

            log(f"[ PUSH ] Staging: {src}")
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
                log(f"[ PUSH ] ✗ Push failed: {push.stderr.strip()[:300]}")
                return False

            log(f"[ PUSH ] ✓ Branch live: github.com/{github_user}/{repo_short}/tree/{branch}")

            # Open a PR automatically
            pr_url = self._create_pr(repo_full, branch, bug_number, bug_title,
                                     github_user, headers)
            if pr_url:
                log(f"[ PR   ] ✓ STRIKE COMPLETE → {pr_url}")
            else:
                log(f"[ PUSH ] ✓ Branch pushed (PR creation skipped)")
            return True

        except subprocess.CalledProcessError as e:
            log(f"[ PUSH ] ✗ Git error: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════
class SurgicalBugSniper:
    def __init__(self):
        self.model       = os.getenv("OLLAMA_MODEL",
                           os.getenv("AIDER_MODEL", "ollama_chat/qwen2.5-coder:7b"))
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
                log(f"[ INIT ] GitHub: {self.github_user}")
            else:
                log(f"[ INIT ] Auth failed ({r.status_code}) — push disabled")
        except Exception as e:
            log(f"[ INIT ] GitHub error: {e}")

        # Get local Git email/name to ensure contributions are attributed to the user (green tiles)
        self.user_email = "sbk@sudhanwa.dev"
        self.user_name = "Sudhanwa-git"
        try:
            email_res = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
            if email_res.returncode == 0 and email_res.stdout.strip():
                self.user_email = email_res.stdout.strip()
                log(f"[ INIT ] Git email: {self.user_email}")
            name_res = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
            if name_res.returncode == 0 and name_res.stdout.strip():
                self.user_name = name_res.stdout.strip()
                log(f"[ INIT ] Git name: {self.user_name}")
        except Exception:
            pass

        self.hunter    = RepoHunter(self.session)
        self.cloner    = AutoCloner()
        self.surgeon   = Surgeon(self.model, self.ollama_base, self.session)
        self.verifier  = Verifier()
        self.committer = Committer(self.session)

    def _cleanup(self):
        d = self.cloner.base
        if os.path.exists(d):
            try:
                shutil.rmtree(d, ignore_errors=True)
                os.makedirs(d, exist_ok=True)
                log("[ INIT ] ✓ Old clones cleared")
            except Exception as e:
                log(f"[ INIT ] ⚠ Cleanup: {e}")

    def _parallel_scan(self, candidates: list) -> tuple:
        """
        Fire off all repo scans in parallel. Return the FIRST repo that
        comes back with ≥1 actionable bug — don't wait for the rest.
        """
        log(f"[ HUNT ] Parallel-scanning {len(candidates)} repos...")

        def _fetch(name):
            repo = self.hunter.get_repo(name)
            if not repo:
                return None, []
            bugs = self.hunter.scan_bugs(repo["full_name"])
            return repo, bugs

        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futs = {pool.submit(_fetch, n): n for n in candidates}
            # Return the first future that gives us usable bugs
            done_first = []
            for fut in as_completed(futs):
                repo, bugs = fut.result()
                if repo and bugs:
                    done_first.append((repo, bugs))
                    # Don't break — collect all so we can pick the richest
                    # (avoids always picking whichever repo is fastest to respond)

        if not done_first:
            return None, []
        # Pick a target repository randomly from the ones that returned bugs
        selected = random.choice(done_first)
        log(f"[ HUNT ] Selected random target: {selected[0]['full_name']} with {len(selected[1])} bug(s)")
        return selected

    def run(self):
        log("=" * 62)
        log("  SURGICAL BUG SNIPER  v5  · Parallel · Streamed · Auto-PR")
        log("  HUNT → CLONE → SURGERY → VERIFY → PUSH → PR")
        log("=" * 62)

        self._cleanup()

        candidates = WHITELIST.copy()
        random.shuffle(candidates)
        log(f"[ INIT ] Targets: {' | '.join(c.split('/')[-1] for c in candidates)}")

        repo, bugs = self._parallel_scan(candidates)
        if not repo or not bugs:
            log("\n[ DONE ] No actionable bugs found across all repos.")
            return

        log(f"\n[ HUNT ] ✓ Target: {repo['full_name']} — {len(bugs)} bug(s) queued")

        local = self.cloner.clone(repo)
        if not local:
            log("[ CLONE ] ✗ Failed — exiting")
            return

        orig = os.getcwd()
        os.chdir(local)
        repo_root = local   # absolute — passed to surgeon so LRU cache stays valid

        max_bugs = int(os.getenv("MAX_BUGS_PER_REPO", "3"))

        try:
            for bug in bugs[:max_bugs]:
                log(f"\n{'─'*62}")
                log(f"[ BUG ] #{bug['number']}: {bug['title']}")

                metrics.increment_metric("issues_attempted")

                if not self.surgeon.operate(bug, repo_root):
                    subprocess.run(["git", "checkout", "."], capture_output=True)
                    log("[ SURGERY ] ✗ Could not fix — next bug")
                    continue

                if not self.verifier.run():
                    subprocess.run(["git", "checkout", "."], capture_output=True)
                    log("[ VERIFY ] ✗ Syntax fail — next bug")
                    continue

                if not self.github_user:
                    log("[ PUSH ] ✗ No GitHub auth — cannot push")
                    continue

                if self.committer.push(
                    repo=repo, bug=bug,
                    github_user=self.github_user,
                    token=self.token,
                    headers=self.headers,
                    user_email=self.user_email,
                    user_name=self.user_name,
                ):
                    log("\n>>> MISSION COMPLETE <<<")
                    return

        except Exception as e:
            log(f"[ ERROR ] {e}")
        finally:
            os.chdir(orig)

        log("\n[ DONE ] All bugs tried — no fix committed this run.")


if __name__ == "__main__":
    SurgicalBugSniper().run()
