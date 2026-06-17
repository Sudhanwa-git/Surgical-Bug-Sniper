"""
Sudhanwa's Surgical Bug Sniper — sbk.py
Pipeline: HUNT → FETCH → FIX → VERIFY → PUSH → PR

v9: Lean. Zero-disk. No subprocess. No git clone.
    GitHub Tree API → in-memory patch + AST guard → GitHub Data API commit.
"""

import os, re, sys, time, requests, random, json, ast, metrics, pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(override=True)


# ── Logging ───────────────────────────────────────────────────────────────────
LLM_LOG = "sbk_llm.log"

def emit(phase: str, msg: str, sym: str = " "):
    print(f" {phase.upper():<8} {sym} {msg}", flush=True)

def emit_ok(phase, msg):   emit(phase, msg, "✓")
def emit_fail(phase, msg): emit(phase, msg, "✗")

def _llm_log(text: str):
    try:
        with open(LLM_LOG, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
WHITELIST = [
    "langchain-ai/langgraph",
    "joaomdmoura/crewAI",
    "run-llama/llama_index",
    "qdrant/qdrant",
    "ollama/ollama",
    "vllm-project/vllm",
]

BUG_LABELS = ["bug", "Bug", "type:bug", "kind/bug", "bug report"]

_STOP = frozenset({
    "the","and","for","with","that","this","from","are","has","not",
    "bug","fix","error","issue","using","when","use","causes","cause",
    "fails","fail","crash","weird","strange","also","into","does","its",
    "have","been","please","after","before","then","just","only","some",
    "which","they","them","their","there","these","those","more","will",
    "what","how","why","who","can","should","could","would","may","might",
    "list","single","string","addition","between","behavior","behaviour",
    "return","import","none","true","false","type","value","name",
    "args","kwargs","self",
})

_HARD_SIGNALS = frozenset({
    "race condition","deadlock","memory leak","segfault","segmentation",
    "cuda","gpu","nccl","distributed","multi-node","multi-gpu",
    "kubernetes","docker","networking","ssl","certificate",
    "authentication","flaky test","non-deterministic","concurrency",
    "thread-safe","async race","oom","out of memory",
})


# ── Pre-compiled Regexes ──────────────────────────────────────────────────────
RE_ERROR = re.compile(r'\b\w+error:\s', re.I)
RE_FILE_EXT = re.compile(r'[\w\-/]+\.(?:py|js|rs|ts|go|cpp|h)')
RE_EXC = re.compile(r'\b(exception|raise|throws?)\b')
RE_FILE_LINE = re.compile(r'file ".*\.py", line \d+')
RE_EXC_TYPES = re.compile(r'\b(TypeError|ValueError|KeyError|AttributeError|'
                          r'IndexError|ImportError|NameError|RuntimeError)\b')
RE_CODE_ELEM = re.compile(r'\b(def |class |function |method )\w+')
RE_PR = re.compile(r'\bpr\s*#\d+')
RE_WORDS = re.compile(r'\b[a-z][a-z0-9_]{2,}\b')
RE_WORDS_CTX = re.compile(r'\b[a-z][a-z0-9_]{3,}\b')
RE_FUNC_DEF = re.compile(r'\s*(def |async def |class |func |fn )')
RE_SEARCH_REPLACE = re.compile(r'<<<SEARCH>>>\s*\n?(.*?)\n?<<<REPLACE>>>\s*\n?(.*?)\n?<<<END>>>', re.DOTALL)
RE_SEARCH_REPLACE_FALLBACK = re.compile(
    r'(?:\*?\*?SEARCH\*?\*?:?\s*\n?)(.*?)(?:\n?\*?\*?REPLACE\*?\*?:?\s*\n?)'
    r'(.*?)(?:\n?<<<END>>>|\n?```|\Z)', re.DOTALL | re.IGNORECASE)
RE_FILE_TB = re.compile(r'[Ff]ile ["\'](.+?\.py)["\']')

# ── HTTP session ──────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],   # 429 handled manually below
        allowed_methods=["GET", "POST"], raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=30)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s


def _gh_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET with automatic Retry-After respect for GitHub 429 / secondary rate limits."""
    for attempt in range(3):
        r = session.get(url, **kwargs)
        if r.status_code == 429 or (r.status_code == 403 and
                                    "rate limit" in r.text.lower()):
            wait = int(r.headers.get("Retry-After", 60))
            emit("rate", f"GitHub rate-limited — waiting {wait}s (attempt {attempt+1}/3)")
            time.sleep(wait)
            continue
        return r
    return r  # return last response after exhausting retries


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — HUNT
# ═══════════════════════════════════════════════════════════════════════════════
class RepoHunter:
    def __init__(self, session: requests.Session):
        self.session = session

    def get_repo(self, name: str) -> dict | None:
        try:
            r = _gh_get(self.session, f"https://api.github.com/repos/{name}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                return {
                    "full_name":      data["full_name"],
                    "default_branch": data.get("default_branch", "main"),
                }
        except Exception:
            pass
        return None

    @staticmethod
    def _is_actionable(title: str, body: str) -> bool:
        if not body or len(body.strip()) < 100:
            return False
        tl = title.lower()
        if any(w in tl for w in ("feature request","enhancement","question",
                                  "how to","[question]","[feat]","[feature]")):
            return False
        bl = body.lower()
        return any([
            "traceback" in bl or 'file "' in bl,
            bool(RE_ERROR.search(bl)),
            "```" in body,
            bool(RE_FILE_EXT.search(body)),
            "assertionerror" in bl or "assert" in bl,
            bool(RE_EXC.search(bl)),
        ])

    @staticmethod
    def _is_vague(title: str) -> bool:
        return any(v in title.lower() for v in
                   ("weird","strange","unexpected","sometimes","intermittent",
                    "random","flaky","occasionally"))

    @staticmethod
    def _score(issue: dict) -> int:
        title = issue.get("title", "")
        body  = issue.get("body") or ""
        combined = (title + " " + body).lower()
        score = 0
        if RE_FILE_LINE.search(combined): score += 30
        if RE_EXC_TYPES.search(body): score += 20
        if RE_CODE_ELEM.search(combined): score += 10
        if "```" in body: score += 10
        if len(body) < 2000: score += 5
        for sig in _HARD_SIGNALS:
            if sig in combined:
                score -= 25
        if len(set(RE_FILE_EXT.findall(body))) > 3: score -= 15
        if issue.get("comments", 0) > 10: score -= 20
        if "pull request" in combined or RE_PR.search(combined): score -= 10
        return score

    def scan_bugs(self, repo_full: str) -> list:
        bugs = []
        # Strategy A — labelled
        for label in BUG_LABELS:
            try:
                r = _gh_get(self.session,
                    f"https://api.github.com/repos/{repo_full}/issues"
                    f"?state=open&labels={label}&per_page=20&sort=created&direction=desc",
                    timeout=10)
                if r.status_code == 200:
                    for issue in r.json():
                        if not issue.get("pull_request") and \
                           not self._is_vague(issue.get("title","")) and \
                           self._is_actionable(issue.get("title",""), issue.get("body") or ""):
                            metrics.increment_metric("issues_scanned")
                            bugs.append(issue)
                    if bugs:
                        break
            except Exception:
                continue

        # Strategy B — unlabelled fallback
        if not bugs:
            try:
                r = _gh_get(self.session,
                    f"https://api.github.com/repos/{repo_full}/issues"
                    f"?state=open&per_page=30&sort=created&direction=desc",
                    timeout=10)
                if r.status_code == 200:
                    for issue in r.json():
                        if not issue.get("pull_request") and \
                           not self._is_vague(issue.get("title","")) and \
                           self._is_actionable(issue.get("title",""), issue.get("body") or ""):
                            metrics.increment_metric("issues_scanned")
                            bugs.append(issue)
            except Exception:
                pass

        bugs = [b for b in bugs if self._score(b) >= 20]
        bugs.sort(key=self._score, reverse=True)
        emit("hunt", f"Scanned {repo_full} → {len(bugs)} solvable bug(s)")
        return bugs[:5]


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — LOCATE + FETCH (GitHub API, zero disk)
# ═══════════════════════════════════════════════════════════════════════════════
class FileLocator:
    def __init__(self, session: requests.Session):
        self.session = session
        self._tree_cache: dict[str, list] = {}

    def get_tree(self, repo_full: str, default_branch: str = "") -> list:
        if repo_full in self._tree_cache:
            return self._tree_cache[repo_full]
        # Use the repo's actual default branch first, then fall back
        branches = ([default_branch] if default_branch else []) + ["main", "master"]
        seen = set()
        for branch in branches:
            if branch in seen:
                continue
            seen.add(branch)
            r = _gh_get(self.session,
                f"https://api.github.com/repos/{repo_full}/git/trees/{branch}?recursive=1",
                timeout=15)
            if r.status_code == 200:
                nodes = [n["path"] for n in r.json().get("tree", []) if n.get("type") == "blob"]
                self._tree_cache[repo_full] = nodes
                return nodes
        emit_fail("fetch", f"Cannot fetch tree for {repo_full}")
        return []

    def fetch(self, repo_full: str, path: str) -> str:
        for branch in ("main", "master"):
            r = self.session.get(
                f"https://raw.githubusercontent.com/{repo_full}/{branch}/{path}",
                timeout=15)
            if r.status_code == 200:
                return r.text[:100_000]
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SURGERY
# ═══════════════════════════════════════════════════════════════════════════════
SURGERY_SYSTEM = """\
You are a precise code repair tool. You receive a bug report and a source file.
Output the fix in a single response.

Line 1 MUST BE:
ROOT_CAUSE: [one sentence — the exact bug]
If you cannot understand the bug, output only: ROOT_CAUSE: INCOMPREHENSIBLE

Then output EXACTLY one SEARCH/REPLACE block:
<<<SEARCH>>>
[exact lines from the file, character-for-character]
<<<REPLACE>>>
[corrected lines — minimal change only]
<<<END>>>

RULES: No prose before ROOT_CAUSE or after <<<END>>>. No markdown fences. No "# Fixed" comments. Match SEARCH exactly."""


_AI_COMMENT_PREFIXES = (
    "# added","# fixed","# ensure","# optimized","# todo","# this will",
    "# this ensures","# handle the","# check for","# noqa","# as an ai",
    "# surgically","# updated","# corrected","# resolved","# patched",
)


class Surgeon:
    def __init__(self, model: str, ollama_base: str, session: requests.Session):
        self.model       = model.split("/")[-1]
        self.ollama_base = ollama_base.rstrip("/")
        self.ctx_chars   = int(os.getenv("SURGERY_CONTEXT_CHARS", "35000"))
        self.timeout     = int(os.getenv("SURGERY_TIMEOUT_SEC",  "180"))
        self.session     = session
        self.last_skip_reason: str | None = None

    def _ollama_ok(self) -> bool:
        try:
            r = self.session.get(f"{self.ollama_base}/api/tags", timeout=5)
            if r.status_code != 200:
                emit_fail("fix", f"Ollama {r.status_code} — is it running?")
                return False
            models = [m["name"] for m in r.json().get("models", [])]
            tag  = self.model if ":" in self.model else self.model + ":latest"
            if not any(m.startswith(tag.split(":")[0]) for m in models):
                emit_fail("fix", f"Model '{tag}' not found — run: ollama pull {tag}")
                return False
            return True
        except Exception as e:
            emit_fail("fix", f"Cannot reach Ollama: {e}")
            return False

    def _fetch_comments(self, repo_full: str, issue_num: int) -> str:
        try:
            r = _gh_get(self.session,
                f"https://api.github.com/repos/{repo_full}/issues/{issue_num}/comments"
                f"?per_page=5&sort=created&direction=asc",
                timeout=10)
            if r.status_code != 200:
                return ""
            parts = []
            for c in r.json()[:5]:
                body = (c.get("body") or "").strip()
                if len(body) > 20:
                    parts.append(f"@{c.get('user',{}).get('login','?')}: {body[:500]}")
            return "\n\n".join(parts)
        except Exception:
            return ""

    def _score_path(self, path: str, kws: list) -> int:
        pl = path.lower()
        return sum(1 for kw in kws if kw in pl)

    def _find_targets(self, title: str, body: str, tree: list) -> list:
        bug_text = (title + " " + body).lower()
        kws = set(w for w in RE_WORDS.findall(bug_text) if w not in _STOP)

        # Names from tracebacks (highest priority)
        tb_names = {os.path.basename(raw.replace("\\", "/").lower())
                    for raw in RE_FILE_TB.findall(body)}

        # Filenames explicitly mentioned in code blocks or body
        clean = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
        mentioned = {os.path.basename(p).lower()
                     for p in RE_FILE_EXT.findall(clean)}
        mentioned -= {"utils.py","helpers.py","common.py","base.py","__init__.py",
                      "types.py","constants.py","config.py"}

        found = {}
        for path in tree:
            name = os.path.basename(path).lower()
            score = self._score_path(path, kws)
            if name in tb_names:     score += 20
            elif name in mentioned:  score += 5
            if score > 0:
                found[path] = score

        return [p for p, _ in sorted(found.items(), key=lambda x: x[1], reverse=True)[:3]]

    def _extract_context(self, content: str, title: str, body: str) -> str:
        if len(content) <= self.ctx_chars:
            return content
        kws = set(w for w in RE_WORDS_CTX.findall((title+" "+body).lower())
                  if w not in _STOP)
        if not kws:
            return content[:self.ctx_chars]
        lines = content.splitlines()
        best_i, best_score = 0, 0
        for i, line in enumerate(lines):
            if not line.strip(): continue
            ll = line.lower()
            score = sum(1 for kw in kws if kw in ll)
            if RE_FUNC_DEF.match(line):
                score += 2
            if score > best_score:
                best_score, best_i = score, i
        if best_score == 0:
            return content[:self.ctx_chars]
        start = max(0, best_i - 200)
        end, chars = start, 0
        while end < len(lines) and chars < self.ctx_chars:
            chars += len(lines[end]) + 1
            end += 1
        return "\n".join(lines[start:end])

    def _call_ollama(self, bug_desc: str, file_path: str,
                     file_content: str, feedback: str = "") -> str:
        user_msg = f"Bug Report:\n{bug_desc}\n\nFile: {file_path}\n```\n{file_content}\n```\n\n"
        if feedback:
            user_msg += f"Previous attempt failed:\n{feedback}\n\nProduce a corrected SEARCH/REPLACE. Start with <<<SEARCH>>> immediately:"
        else:
            user_msg += "Produce the SEARCH/REPLACE block to fix this bug:"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SURGERY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            "stream": True,
            "options": {"temperature": 0.05, "num_predict": 2048},
        }
        try:
            emit("fix", "LLM generating patch...")
            _llm_log(f"\n--- LLM ({time.strftime('%H:%M:%S')}) ---\n")
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
                    _llm_log(tok)
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

    @staticmethod
    def _strip_ai_comments(replace: str, search: str) -> str:
        orig = {l.strip() for l in search.splitlines()}
        out = []
        for line in replace.splitlines():
            s = line.strip()
            if s.startswith("#") and s not in orig:
                if any(s.lower().startswith(p) for p in _AI_COMMENT_PREFIXES):
                    continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _validate(s: str, r: str) -> str | None:
        sl = [l for l in s.splitlines() if l.strip()]
        rl = [l for l in r.splitlines() if l.strip()]
        if not sl:                             return "SEARCH is empty"
        if s.strip() == r.strip():             return "REPLACE identical to SEARCH"
        if "print(" in r and "print(" not in s: return "REPLACE introduces debug print()"
        rep_code = "\n".join(l for l in rl if not l.strip().startswith("#")).strip()
        if rep_code in ("pass","return","return None") and \
           "\n".join(l for l in sl if not l.strip().startswith("#")).strip() not in \
           ("pass","return","return None",""):  return "REPLACE deletes logic with pass/return"
        if len(rl) > max(len(sl) * 3, 12):    return f"REPLACE too large ({len(rl)} vs {len(sl)} lines)"
        rl_low = r.lower()
        for art in ("```python","```javascript","**search","**replace",
                    "to fix the bug","here's the","here is the",
                    "this will ensure","as an ai","note:"):
            if art in rl_low:                  return f"LLM artifact: '{art}'"
        new_imports = [l.strip() for l in r.splitlines()
                       if re.match(r'\s*(import |from \S+ import )', l)
                       and l.strip() not in {x.strip() for x in s.splitlines()}]
        if len(new_imports) > 2:               return f"Adds {len(new_imports)} new imports"
        return None

    def _apply_mem(self, llm_out: str, original: str, path: str) -> str | None:
        if not llm_out or "NO_FIX" in llm_out:
            return None

        s = r = ""
        m = RE_SEARCH_REPLACE.search(llm_out)
        if m:
            s, r = m.group(1), m.group(2)
        else:
            m2 = RE_SEARCH_REPLACE_FALLBACK.search(llm_out)
            if m2:
                s, r = m2.group(1), m2.group(2)

        if not s:
            emit_fail("fix", "No SEARCH/REPLACE found")
            _llm_log(f"\n--- UNPARSEABLE ---\n{llm_out[:400]}\n")
            return None

        def _clean(t):
            t = t.strip()
            t = re.sub(r'^```[a-zA-Z0-9_-]*\n', '', t)
            return re.sub(r'\n```$', '', t)

        s, r = _clean(s), _clean(r)
        if not s:
            emit_fail("fix", "SEARCH empty after cleaning")
            return None

        err = self._validate(s, r)
        if err:
            emit_fail("fix", f"Rejected: {err}")
            return None

        r = self._strip_ai_comments(r, s)

        # Match (exact → fuzzy-ws → indent-agnostic)
        new, mt = None, None
        if s in original:
            new, mt = original.replace(s, r, 1), "exact"
        else:
            def rstrip(x): return "\n".join(l.rstrip() for l in x.splitlines())
            ss, oo = rstrip(s), rstrip(original)
            if ss in oo:
                new, mt = oo.replace(ss, r.rstrip(), 1), "fuzzy-ws"
            else:
                def norm(x): return "\n".join(l.strip() for l in x.splitlines() if l.strip())
                s2, lo, ns = norm(s), original.splitlines(), len(s.splitlines())
                for i in range(len(lo)):
                    if "\n".join(l.strip() for l in lo[i:i+ns] if l.strip()) == s2:
                        new = "\n".join(lo[:i] + r.splitlines() + lo[i+ns:])
                        mt = "indent-agnostic"
                        break

        if new is None:
            emit_fail("fix", "SEARCH not matched (3 strategies)")
            return None

        # AST guard (Python only)
        if path.endswith(".py"):
            try:
                ot, nt = ast.parse(original), ast.parse(new)
                if ast.dump(ot) == ast.dump(nt):
                    emit_fail("fix", "Patch only modifies formatting/comments (AST identical) - REJECTED BS FIX")
                    return None
                def loaded(t): return {n.id for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                def defined(t):
                    d = {n.id for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                    d |= {n.name for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))}
                    d |= {a.asname or a.name for n in ast.walk(t) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
                    return d
                builtins = set(dir(__builtins__)) | {"self","cls","args","kwargs","_"}
                hall = (loaded(nt) - loaded(ot)) - defined(nt) - builtins
                if hall:
                    emit_fail("fix", f"Undefined vars in patch: {', '.join(hall)}")
                    return None
            except SyntaxError as e:
                emit_fail("fix", f"Syntax broken at line {e.lineno}")
                return None

        sn, rn = len(s.strip().splitlines()), len(r.strip().splitlines())
        emit_ok("fix", f"Patched {os.path.basename(path)} ({mt}, {sn}→{rn} lines)")
        return new

    def operate(self, bug: dict, repo_full: str,
                tree: list, locator: "FileLocator") -> dict:
        """Returns {path: new_content} on success, {} on failure."""
        self.last_skip_reason = None

        if not self._ollama_ok():
            self.last_skip_reason = "ollama"
            return {}

        title = bug.get("title", "")
        body  = bug.get("body") or ""

        if bug.get("number"):
            ctx = self._fetch_comments(repo_full, bug["number"])
            if ctx:
                emit("think", f"Loaded {ctx.count('@')} comment(s) for context")
                body += "\n\n--- Discussion ---\n" + ctx

        bug_desc = f"Title: {title}\n\n{body[:3000]}"
        targets  = self._find_targets(title, body, tree)

        if not targets:
            emit_fail("target", "No candidate files found")
            self.last_skip_reason = "no_files"
            return {}

        emit("target", f"Candidates: {', '.join(os.path.basename(t) for t in targets)}")

        for tf in targets:
            raw = locator.fetch(repo_full, tf)
            if not raw:
                emit_fail("fetch", f"Cannot fetch {os.path.basename(tf)}")
                continue

            context = self._extract_context(raw, title, body)
            feedback = ""

            for attempt in range(2):
                if attempt:
                    emit("fix", "Retry with error feedback...")
                llm_out = self._call_ollama(bug_desc, tf, context, feedback)
                if not llm_out:
                    break

                first = llm_out.strip().splitlines()[0] if llm_out.strip() else ""
                if first.upper().startswith("ROOT_CAUSE:"):
                    rc = first.split(":", 1)[1].strip()
                    if "INCOMPREHENSIBLE" in rc.upper():
                        emit_fail("think", "LLM cannot comprehend — skipping")
                        self.last_skip_reason = "comprehension"
                        break
                    emit_ok("think", rc[:80])

                new = self._apply_mem(llm_out, raw, tf)
                if new is not None:
                    return {tf: new}
                feedback = "SEARCH not found. Copy exact lines including all whitespace."

        emit_fail("fix", "All candidates exhausted — no fix produced")
        self.last_skip_reason = "all_exhausted"
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — VERIFY (in-memory AST)
# ═══════════════════════════════════════════════════════════════════════════════
class Verifier:
    def run(self, results: dict) -> bool:
        emit("verify", "AST-validating patch...")
        errors = []
        for path, content in results.items():
            if not path.endswith(".py"):
                continue
            try:
                ast.parse(content)
            except SyntaxError as e:
                errors.append(f"  {path}: line {e.lineno}: {e.msg}")
        if errors:
            emit_fail("verify", f"{len(errors)} file(s) broken:")
            for e in errors:
                print(e, flush=True)
            return False
        emit_ok("verify", f"{len(results)} file(s) clean")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — PUSH + PR (GitHub Data API, zero local git)
# ═══════════════════════════════════════════════════════════════════════════════
class Committer:
    def __init__(self, session: requests.Session):
        self.session = session
        self.last_pr_url: str | None = None

    def _wait_for_fork(self, github_user: str, repo_short: str,
                       headers: dict, max_wait: int = 20) -> dict | None:
        for i in range(max_wait // 2):
            r = self.session.get(
                f"https://api.github.com/repos/{github_user}/{repo_short}",
                headers=headers, timeout=10)
            if r.status_code == 200:
                emit_ok("push", f"Fork ready ({(i+1)*2}s)")
                return r.json()
            time.sleep(2)
        return None

    def _create_pr(self, repo_full: str, branch: str, bug_number: int,
                   bug_title: str, github_user: str, headers: dict) -> str | None:
        body = (f"Fixes #{bug_number}\n\n"
                f"Resolves: **{bug_title}**\n\n"
                f"---\n*Verified: AST checks passed ✓*")
        title = f"fix: resolve issue #{bug_number} — {bug_title[:60]}"
        for base in ("main", "master"):
            try:
                r = self.session.post(
                    f"https://api.github.com/repos/{repo_full}/pulls",
                    headers=headers,
                    json={"title": title, "body": body,
                          "head": f"{github_user}:{branch}", "base": base},
                    timeout=15)
                if r.status_code in (200, 201):
                    pr_url = r.json().get("html_url", "")
                    emit_ok("pr", f"Opened → {pr_url}")
                    metrics.increment_metric("prs_opened")
                    return pr_url
                if r.status_code != 422:
                    emit_fail("pr", f"PR {r.status_code}: {r.text[:80]}")
                    return None
            except Exception as e:
                emit_fail("pr", f"PR error: {e}")
                return None
        return None

    def push(self, repo: dict, bug: dict, github_user: str,
             token: str, headers: dict, results: dict) -> bool:
        self.last_pr_url = None
        repo_full  = repo["full_name"]
        repo_short = repo_full.split("/")[-1]
        bug_number = bug["number"]
        branch     = f"fix/issue-{bug_number}"

        # Fork
        emit("push", f"Forking {repo_full}...")
        r = self.session.post(f"https://api.github.com/repos/{repo_full}/forks",
                              headers=headers, json={}, timeout=15)
        if r.status_code not in (200, 202):
            emit_fail("push", f"Fork failed ({r.status_code})")
            return False

        fork = self._wait_for_fork(github_user, repo_short, headers)
        if not fork:
            emit_fail("push", "Fork not ready")
            return False

        try:
            base_branch = fork.get("default_branch", "main")

            # HEAD SHA
            r = _gh_get(self.session,
                f"https://api.github.com/repos/{github_user}/{repo_short}"
                f"/git/refs/heads/{base_branch}",
                headers=headers, timeout=10)
            if r.status_code != 200:
                base_branch = "master"
                r = _gh_get(self.session,
                    f"https://api.github.com/repos/{github_user}/{repo_short}"
                    f"/git/refs/heads/{base_branch}",
                    headers=headers, timeout=10)
                if r.status_code != 200:
                    emit_fail("push", "Cannot find HEAD SHA")
                    return False
            head_sha = r.json()["object"]["sha"]

            # Base tree SHA
            r = self.session.get(
                f"https://api.github.com/repos/{github_user}/{repo_short}"
                f"/git/commits/{head_sha}",
                headers=headers, timeout=10)
            if r.status_code != 200:
                emit_fail("push", f"Cannot fetch commit {head_sha[:8]}: HTTP {r.status_code}")
                return False
            base_tree_sha = r.json()["tree"]["sha"]

            # Blobs → tree
            tree_items = []
            for file_path, content in results.items():
                rb = self.session.post(
                    f"https://api.github.com/repos/{github_user}/{repo_short}/git/blobs",
                    headers=headers,
                    json={"content": content, "encoding": "utf-8"},
                    timeout=20)
                if rb.status_code not in (200, 201):
                    emit_fail("push", f"Blob creation failed for {file_path}: HTTP {rb.status_code}")
                    return False
                tree_items.append({"path": file_path, "mode": "100644",
                                   "type": "blob", "sha": rb.json()["sha"]})

            rt = self.session.post(
                f"https://api.github.com/repos/{github_user}/{repo_short}/git/trees",
                headers=headers,
                json={"base_tree": base_tree_sha, "tree": tree_items},
                timeout=15)
            if rt.status_code not in (200, 201):
                emit_fail("push", f"Tree creation failed: HTTP {rt.status_code}")
                return False
            new_tree_sha = rt.json()["sha"]

            # Commit
            rc = self.session.post(
                f"https://api.github.com/repos/{github_user}/{repo_short}/git/commits",
                headers=headers,
                json={"message": f"fix: resolve issue #{bug_number}",
                      "tree": new_tree_sha, "parents": [head_sha]},
                timeout=15)
            if rc.status_code not in (200, 201):
                emit_fail("push", f"Commit creation failed: HTTP {rc.status_code}")
                return False
            new_commit_sha = rc.json()["sha"]

            # Branch ref
            rr = self.session.post(
                f"https://api.github.com/repos/{github_user}/{repo_short}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": new_commit_sha},
                timeout=15)
            if rr.status_code not in (200, 201):
                emit_fail("push", f"Branch creation failed: {rr.text[:80]}")
                return False

            emit_ok("push", f"Branch live → {github_user}/{repo_short}/tree/{branch}")

            pr_url = self._create_pr(repo_full, branch, bug_number,
                                     bug.get("title",""), github_user, headers)
            self.last_pr_url = pr_url
            return True

        except Exception as e:
            emit_fail("push", f"API error: {e}")
            return False


# ── Attempted-issues cache ────────────────────────────────────────────────────
_CACHE_FILE = pathlib.Path(__file__).parent / "attempted_issues.json"

def _load_cache() -> set:
    try:
        return set(json.loads(_CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()

def _save_cache(cache: set):
    try:
        _CACHE_FILE.write_text(json.dumps(sorted(cache)), encoding="utf-8")
    except Exception:
        pass

def _mark_attempted(repo_full: str, issue_num: int):
    key = f"{repo_full}#{issue_num}"
    cache = _load_cache()
    cache.add(key)
    _save_cache(cache)

def _already_attempted(repo_full: str, issue_num: int) -> bool:
    return f"{repo_full}#{issue_num}" in _load_cache()


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════
class SurgicalBugSniper:
    def __init__(self):
        self.model       = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.ollama_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
        self.token       = os.getenv("GITHUB_TOKEN", "")
        self.dry_run     = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

        self.session = _make_session()
        self.session.headers.update({
            "Authorization": f"token {self.token}",
            "Accept":        "application/vnd.github.v3+json",
        })
        self.headers = dict(self.session.headers)

        self.github_user: str | None = None
        try:
            r = self.session.get("https://api.github.com/user", timeout=10)
            if r.status_code == 200:
                self.github_user = r.json().get("login")
                emit_ok("init", f"GitHub: {self.github_user}")
            else:
                emit_fail("init", f"Auth failed ({r.status_code}) — push disabled")
        except Exception as e:
            emit_fail("init", f"GitHub error: {e}")

        self.hunter    = RepoHunter(self.session)
        self.locator   = FileLocator(self.session)
        self.surgeon   = Surgeon(self.model, self.ollama_base, self.session)
        self.verifier  = Verifier()
        self.committer = Committer(self.session)

        self.summary = {
            "repo": None, "bugs_scanned": 0, "bugs_attempted": 0,
            "skipped_comprehension": 0, "skipped_verify": 0, "skipped_no_fix": 0,
            "fix_file": None, "pr_url": None,
        }

    def _parallel_scan(self, candidates: list) -> tuple:
        emit("hunt", f"Scanning {len(candidates)} repos in parallel...")

        def _fetch(name):
            repo = self.hunter.get_repo(name)
            if not repo:
                return None, []
            return repo, self.hunter.scan_bugs(repo["full_name"])

        results = []
        max_workers = min(len(candidates), 8)   # cap thread pool — don't spin up unbounded threads
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for repo, bugs in [f.result() for f in as_completed(
                    [pool.submit(_fetch, n) for n in candidates])]:
                if repo and bugs:
                    results.append((repo, bugs))

        if not results:
            return None, []

        results.sort(key=lambda x: max(self.hunter._score(b) for b in x[1]), reverse=True)
        best = results[0]
        emit_ok("hunt", f"Target: {best[0]['full_name']} "
                        f"({len(best[1])} bug(s), score: {self.hunter._score(best[1][0])})")
        return best

    def _print_summary(self):
        s = self.summary
        print(flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print("  MISSION SUMMARY", flush=True)
        print(" ───────────────────────────────────────────────────────────", flush=True)
        if s["repo"]:          print(f"  Repo              {s['repo']}", flush=True)
        print(f"  Bugs scanned      {s['bugs_scanned']}", flush=True)
        print(f"  Bugs attempted    {s['bugs_attempted']}", flush=True)
        if s["skipped_comprehension"]: print(f"  Skipped (unclear) {s['skipped_comprehension']}", flush=True)
        if s["skipped_verify"]:        print(f"  Skipped (syntax)  {s['skipped_verify']}", flush=True)
        if s["skipped_no_fix"]:        print(f"  Skipped (no fix)  {s['skipped_no_fix']}", flush=True)
        if s["fix_file"]:     print(f"  Fix applied       {s['fix_file']}", flush=True)
        if s["pr_url"]:       print(f"  PR opened         {s['pr_url']}", flush=True)
        elif s["fix_file"]:   print("  PR opened         push failed", flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)

    def run(self):
        print(flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print("  SURGICAL BUG SNIPER  v9  ·  Zero-Disk · Quality-First", flush=True)
        print("  HUNT → FETCH → FIX → VERIFY → PUSH → PR", flush=True)
        print(" ═══════════════════════════════════════════════════════════", flush=True)
        print(flush=True)

        try:
            open(LLM_LOG, "w", encoding="utf-8").close()  # fix: always specify encoding
        except Exception:
            pass

        candidates = WHITELIST.copy()
        random.shuffle(candidates)
        emit("init", f"Targets: {' · '.join(c.split('/')[-1] for c in candidates)}")

        repo, bugs = self._parallel_scan(candidates)
        if not repo or not bugs:
            emit_fail("done", "No solvable bugs found")
            self._print_summary()
            return

        self.summary["repo"] = repo["full_name"]
        self.summary["bugs_scanned"] = len(bugs)

        tree = self.locator.get_tree(repo["full_name"],
                                     default_branch=repo.get("default_branch", ""))
        if not tree:
            emit_fail("fetch", "Cannot fetch repo tree")
            self._print_summary()
            return

        max_bugs = int(os.getenv("MAX_BUGS_PER_REPO", "3"))

        if self.dry_run:
            emit("dry-run", "DRY_RUN=true — will hunt/fix but NOT fork/push/PR")

        try:
            for bug in bugs[:max_bugs]:
                print(flush=True)
                issue_id = bug['number']
                repo_full = repo["full_name"]

                # Skip if we've already attempted this issue in a previous run
                if _already_attempted(repo_full, issue_id):
                    emit("hunt", f"#{issue_id} already attempted in a prior run — skipping")
                    continue

                emit("bug", f"#{issue_id}: {bug['title'][:65]}")
                metrics.increment_metric("issues_attempted")
                self.summary["bugs_attempted"] += 1
                _mark_attempted(repo_full, issue_id)  # mark before attempt to avoid re-runs on crash

                results = self.surgeon.operate(bug, repo_full, tree, self.locator)
                if not results:
                    reason = self.surgeon.last_skip_reason
                    self.summary["skipped_comprehension" if reason == "comprehension" else "skipped_no_fix"] += 1
                    continue

                if not self.verifier.run(results):
                    self.summary["skipped_verify"] += 1
                    continue

                self.summary["fix_file"] = ", ".join(results.keys())

                if self.dry_run:
                    emit_ok("dry-run", f"Fix verified. Skipping push (DRY_RUN). Files: {self.summary['fix_file']}")
                    print(flush=True)
                    emit_ok("done", "MISSION COMPLETE (dry run)")
                    self._print_summary()
                    return

                if not self.github_user:
                    emit_fail("push", "No GitHub auth — cannot push")
                    continue

                if self.committer.push(repo=repo, bug=bug, github_user=self.github_user,
                                       token=self.token, headers=self.headers, results=results):
                    self.summary["pr_url"] = self.committer.last_pr_url or "pushed (no PR)"
                    print(flush=True)
                    emit_ok("done", "MISSION COMPLETE")
                    self._print_summary()
                    return

        except Exception as e:
            emit_fail("error", str(e))

        emit("done", "All bugs tried — no fix committed this run")
        self._print_summary()


if __name__ == "__main__":
    SurgicalBugSniper().run()
