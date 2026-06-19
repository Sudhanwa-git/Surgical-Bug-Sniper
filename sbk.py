"""
Sudhanwa's Surgical Bug Sniper — sbk.py
Pipeline: HUNT → FETCH → FIX → VERIFY → PUSH → PR

v9: Lean. Zero-disk. No subprocess. No git clone.
    GitHub Tree API → in-memory patch + AST guard → GitHub Data API commit.
"""

import os, re, sys, time, requests, random, json, ast, metrics, pathlib, db
from datetime import datetime, timezone
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
    ts = time.strftime("%H:%M:%S")
    print(f" {phase.upper():<8} {sym} [{ts}] {msg}", flush=True)

def emit_ok(phase, msg):   emit(phase, msg, "✓")
def emit_fail(phase, msg): emit(phase, msg, "✗")

def emit_diff(search: str, replace: str, path: str):
    """Write a compact, human-readable diff block to the live log."""
    fname = os.path.basename(path)
    s_lines = search.strip().splitlines()
    r_lines = replace.strip().splitlines()

    # Show at most 8 lines on each side to keep feed readable
    MAX = 8
    s_show = s_lines[:MAX]
    r_show = r_lines[:MAX]
    s_more = len(s_lines) - len(s_show)
    r_more = len(r_lines) - len(r_show)

    print(f" DIFF      ┌─ {fname} ─────────────────────────────", flush=True)
    for line in s_show:
        print(f" DIFF      │ - {line}", flush=True)
    if s_more:
        print(f" DIFF      │   ... ({s_more} more line(s) removed)", flush=True)
    print(f" DIFF      │", flush=True)
    for line in r_show:
        print(f" DIFF      │ + {line}", flush=True)
    if r_more:
        print(f" DIFF      │   ... ({r_more} more line(s) added)", flush=True)
    print(f" DIFF      └─────────────────────────────────────────", flush=True)


def emit_patch_summary(path: str, search: str, replace: str, match_type: str):
    """One-liner human summary + full diff block."""
    fname  = os.path.basename(path)
    s_lines = [l for l in search.strip().splitlines() if l.strip()]
    r_lines = [l for l in replace.strip().splitlines() if l.strip()]
    removed = len(s_lines)
    added   = len(r_lines)

    if added > removed:
        action = f"added {added - removed} line(s)"
    elif removed > added:
        action = f"removed {removed - added} line(s)"
    else:
        action = f"rewrote {removed} line(s)"

    print(f" PATCH      ✎  [{fname}]  {action}  (match: {match_type})", flush=True)
    emit_diff(search, replace, path)

def _llm_log(text: str):
    try:
        with open(LLM_LOG, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
# Python-only AI repos with responsive maintainers & active community PRs
WHITELIST = [
    "langchain-ai/langgraph",
    "BerriAI/litellm",          # high issue throughput, quick merges
    "joaomdmoura/crewAI",
    "run-llama/llama_index",
    "vllm-project/vllm",
    "microsoft/autogen",
]

# Maintainer-blessed labels — these signal "we WANT a PR for this"
PREFERRED_LABELS = ["good first issue", "help wanted", "good-first-issue", "help-wanted"]
BUG_LABELS       = ["bug", "Bug", "type:bug", "kind/bug", "bug report"]

# Issue age sweet spot: not too fresh (maintainer on it), not abandoned
AGE_MIN_DAYS, AGE_MAX_DAYS = 7, 365

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
RE_TEST_BLOCK  = re.compile(r'<<<TEST>>>\s*\n?(.*?)\n?<<<END_TEST>>>', re.DOTALL)
RE_FILE_TB     = re.compile(r'[Ff]ile ["\'](.+?\.py)["\']')

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


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# STEP 1 \u2014 HUNT + TRIAGE
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
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

    # ── Triage helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _issue_age_ok(issue: dict) -> bool:
        """Return True if issue is in the age sweet-spot (not too fresh, not abandoned)."""
        try:
            dt  = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).days
            return AGE_MIN_DAYS <= age <= AGE_MAX_DAYS
        except Exception:
            return True   # don't penalise if unparseable

    @staticmethod
    def _is_unclaimed(issue: dict) -> bool:
        """Skip issues already assigned to someone — maintainer has it."""
        return issue.get("assignee") is None

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
        title    = issue.get("title", "")
        body     = issue.get("body") or ""
        labels   = [l.get("name","").lower() for l in issue.get("labels", [])]
        combined = (title + " " + body).lower()
        score    = 0
        # Maintainer-blessed labels are a strong positive signal
        if any(l in ("good first issue","help wanted","good-first-issue","help-wanted")
               for l in labels):
            score += 15
        if RE_FILE_LINE.search(combined): score += 30
        if RE_EXC_TYPES.search(body):     score += 20
        if RE_CODE_ELEM.search(combined): score += 10
        if "```" in body:                 score += 10
        if len(body) < 2000:              score += 5
        for sig in _HARD_SIGNALS:
            if sig in combined:           score -= 25
        if len(set(RE_FILE_EXT.findall(body))) > 3: score -= 15
        if issue.get("comments", 0) > 10:           score -= 20
        if "pull request" in combined or RE_PR.search(combined): score -= 10
        return score

    def _fetch_label(self, repo_full: str, label: str) -> list:
        """Fetch issues for a single label — used by parallel scanner."""
        try:
            r = _gh_get(self.session,
                f"https://api.github.com/repos/{repo_full}/issues"
                f"?state=open&labels={label}&per_page=20&sort=created&direction=desc",
                timeout=10)
            if r.status_code == 200:
                found = []
                for issue in r.json():
                    if (not issue.get("pull_request")
                            and self._is_unclaimed(issue)
                            and self._issue_age_ok(issue)
                            and not self._is_vague(issue.get("title",""))
                            and self._is_actionable(issue.get("title",""),
                                                     issue.get("body") or "")):
                        metrics.increment_metric("issues_scanned")
                        found.append(issue)
                return found
        except Exception:
            pass
        return []

    def scan_bugs(self, repo_full: str) -> list:
        bugs: list   = []
        seen_ids: set[int] = set()

        def _collect(labels: list):
            with ThreadPoolExecutor(max_workers=len(labels)) as pool:
                for fut in as_completed([pool.submit(self._fetch_label, repo_full, lbl)
                                         for lbl in labels]):
                    for issue in fut.result():
                        if issue["id"] not in seen_ids:
                            seen_ids.add(issue["id"])
                            bugs.append(issue)

        # Round 1 — maintainer-blessed labels (best merge signal)
        _collect(PREFERRED_LABELS)

        # Round 2 — generic bug labels if nothing preferred found
        if not bugs:
            _collect(BUG_LABELS)

        # Round 3 — unlabelled fallback
        if not bugs:
            try:
                r = _gh_get(self.session,
                    f"https://api.github.com/repos/{repo_full}/issues"
                    f"?state=open&per_page=30&sort=created&direction=desc",
                    timeout=10)
                if r.status_code == 200:
                    for issue in r.json():
                        if (not issue.get("pull_request")
                                and self._is_unclaimed(issue)
                                and self._issue_age_ok(issue)
                                and not self._is_vague(issue.get("title",""))
                                and self._is_actionable(issue.get("title",""),
                                                         issue.get("body") or "")):
                            metrics.increment_metric("issues_scanned")
                            bugs.append(issue)
            except Exception:
                pass

        bugs = [b for b in bugs if self._score(b) >= 20]
        bugs.sort(key=self._score, reverse=True)
        emit("hunt", f"Scanned {repo_full} \u2192 {len(bugs)} solvable bug(s)")
        return bugs[:5]


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





# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — LOCATE + FETCH (GitHub API, zero disk)
# ═══════════════════════════════════════════════════════════════════════════════
class FileLocator:
    def __init__(self, session: requests.Session):
        self.session = session
        self._tree_cache: dict[str, list] = {}
        self._branch_cache: dict[str, str] = {}   # repo_full → resolved branch

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
                self._branch_cache[repo_full] = branch   # remember working branch
                return nodes
        emit_fail("fetch", f"Cannot fetch tree for {repo_full}")
        return []

    def fetch(self, repo_full: str, path: str) -> str:
        # Try the known-good branch first (avoids a wasted HTTP round-trip)
        known = self._branch_cache.get(repo_full)
        branches = ([known] if known else []) + [b for b in ("main", "master") if b != known]
        for branch in branches:
            r = self.session.get(
                f"https://raw.githubusercontent.com/{repo_full}/{branch}/{path}",
                timeout=15)
            if r.status_code == 200:
                if branch != known:          # cache the newly discovered branch
                    self._branch_cache[repo_full] = branch
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


# ── Style Sampler ───────────────────────────────────────────────────────────────────
class StyleSampler:
    """Sample a few files from the repo to detect code style conventions."""

    @staticmethod
    def detect(contents: list[str]) -> str:
        """Return a one-line style hint from up to 3 sampled file contents."""
        combined = "\n".join(contents[:3])
        lines    = combined.splitlines()

        # Indentation
        indent = "4-space"
        tab_lines   = sum(1 for l in lines if l.startswith("\t"))
        two_lines   = sum(1 for l in lines if l.startswith("  ") and not l.startswith("    "))
        four_lines  = sum(1 for l in lines if l.startswith("    "))
        if tab_lines > max(two_lines, four_lines):
            indent = "tab"
        elif two_lines > four_lines:
            indent = "2-space"

        # Quote style (look at string literals)
        single = combined.count("'") 
        double = combined.count('"')
        quotes = "single quotes" if single > double else "double quotes"

        return f"Style hint: use {indent} indentation, {quotes}."


# ── Test Finder ───────────────────────────────────────────────────────────────────
class TestFinder:
    """Locate or derive the test file path for a source file."""

    def find(self, source_path: str, tree: list) -> str | None:
        fname = os.path.basename(source_path).removesuffix(".py")
        candidates = {f"test_{fname}.py", f"{fname}_test.py"}
        for path in tree:
            if os.path.basename(path) in candidates and "test" in path.lower():
                return path
        return None

    def derive_path(self, source_path: str, tree: list) -> str:
        """Return existing test path or a sensible new path."""
        existing = self.find(source_path, tree)
        if existing:
            return existing
        fname = os.path.basename(source_path).removesuffix(".py")
        # Mirror the source dir under tests/
        parts = source_path.replace("\\", "/").split("/")
        sub   = "/".join(parts[1:-1]) if len(parts) > 2 else ""
        return f"tests/{sub + '/' if sub else ''}test_{fname}.py"


class Surgeon:
    def __init__(self, model: str, ollama_base: str, session: requests.Session):
        self.model       = model.split("/")[-1]
        self.ollama_base = ollama_base.rstrip("/")
        self.ctx_chars   = int(os.getenv("SURGERY_CONTEXT_CHARS", "18000"))  # was 35000
        self.timeout     = int(os.getenv("SURGERY_TIMEOUT_SEC",  "180"))
        self.session     = session
        self.last_skip_reason: str | None = None
        self.last_root_cause:  str        = ""
        self.last_test_code:   str        = ""
        self.last_test_path:   str        = ""

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
        tb_hits = []   # fast path: collect traceback hits immediately
        for path in tree:
            name = os.path.basename(path).lower()
            if name in tb_names:
                tb_hits.append(path)
                found[path] = 20 + self._score_path(path, kws)
            else:
                score = self._score_path(path, kws)
                if name in mentioned:
                    score += 5
                if score > 0:
                    found[path] = score

        # Short-circuit: if we have high-confidence traceback hits, skip low-signal candidates
        if len(tb_hits) >= 2:
            tb_hits.sort(key=lambda p: found[p], reverse=True)
            return tb_hits[:5]

        return [p for p, _ in sorted(found.items(), key=lambda x: x[1], reverse=True)[:5]]

    def _semantic_rerank(self, candidates: list, bug_desc: str,
                         repo_full: str, locator: "FileLocator") -> list:
        """
        Re-rank candidate files using Ollama embedding cosine similarity.
        Only active when USE_EMBEDDINGS=true.  Falls back gracefully on any error.
        Adds ~1-2s but dramatically improves file targeting accuracy.
        """
        if not candidates:
            return candidates
        if os.getenv("USE_EMBEDDINGS", "false").lower() not in ("1", "true", "yes"):
            return candidates[:3]   # default: return top-3 keyword candidates unchanged

        embed_model = os.getenv("EMBED_MODEL", "nomic-embed-text")

        def _embed(text: str) -> list:
            try:
                r = self.session.post(
                    f"{self.ollama_base}/api/embeddings",
                    json={"model": embed_model, "prompt": text[:4000]},
                    timeout=30)
                if r.status_code == 200:
                    return r.json().get("embedding", [])
            except Exception:
                pass
            return []

        bug_vec = _embed(bug_desc)
        if not bug_vec:
            emit("embed", "Embedding unavailable — using keyword ranking")
            return candidates[:3]

        # Precompute query magnitude once — reused for every file comparison
        import math as _math
        bug_mag = _math.sqrt(_math.fsum(x * x for x in bug_vec)) if bug_vec else 0.0

        scored = []
        for path in candidates:
            content = locator.fetch(repo_full, path)
            if not content:
                continue
            vec = _embed(content[:4000])
            sim = db._cosine(bug_vec, vec, mag_a=bug_mag)
            scored.append((path, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        reranked = [p for p, _ in scored[:3]]
        if reranked:
            emit_ok("embed", f"Semantic rerank → {', '.join(os.path.basename(p) for p in reranked)}")
        return reranked or candidates[:3]

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
                     file_content: str, feedback: str = "",
                     style_hint: str = "") -> str:
        user_msg = f"Bug Report:\n{bug_desc}\n\nFile: {file_path}\n```\n{file_content}\n```\n"
        if style_hint:
            user_msg += f"\n{style_hint}\n"
        if feedback:
            user_msg += f"\nPrevious attempt failed:\n{feedback}\n\nProduce a corrected SEARCH/REPLACE. Start with <<<SEARCH>>> immediately:"
        else:
            user_msg += "\nProduce the fix:"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SURGERY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            "stream": True,
            "options": {"temperature": 0.05, "num_predict": 1400},  # was 2048
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
        emit_patch_summary(path, s, r, mt)

        return new

    def operate(self, bug: dict, repo_full: str,
                 tree: list, locator: "FileLocator",
                 style_hint: str = "", test_finder: "TestFinder | None" = None) -> dict:
        """Returns {path: new_content} on success, {} on failure."""
        self.last_skip_reason = None
        self.last_root_cause  = ""
        self.last_test_code   = ""
        self.last_test_path   = ""

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

        bug_desc   = f"Title: {title}\n\n{body[:3000]}"
        kw_targets = self._find_targets(title, body, tree)

        if not kw_targets:
            emit_fail("target", "No candidate files found")
            self.last_skip_reason = "no_files"
            return {}

        # Semantic re-ranking (fast no-op unless USE_EMBEDDINGS=true)
        targets = self._semantic_rerank(kw_targets, bug_desc, repo_full, locator)
        emit("target", f"Candidates: {', '.join(os.path.basename(t) for t in targets)}")

        for tf in targets:
            raw = locator.fetch(repo_full, tf)
            if not raw:
                emit_fail("fetch", f"Cannot fetch {os.path.basename(tf)}")
                continue

            context  = self._extract_context(raw, title, body)
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
                    emit_ok("think", f"Root cause → {rc[:120]}")

                new = self._apply_mem(llm_out, raw, tf)
                if new is not None:
                    # Record match details to DB
                    try:
                        m2 = RE_SEARCH_REPLACE.search(llm_out)
                        s_block = m2.group(1).strip() if m2 else ""
                        sl = len(s_block.splitlines())
                        rl = len(new.splitlines()) - len(raw.splitlines()) + sl
                        db.patch_done(repo_full, bug.get("number", 0),
                                      patch_file=tf, lines_before=sl, lines_after=max(rl,0))
                    except Exception:
                        pass
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

    def _has_existing_pr(self, repo_full: str, issue_num: int, headers: dict) -> bool:
        """Return True if any open PR already references this issue — avoids duplicate PRs."""
        try:
            r = self.session.get(
                f"https://api.github.com/repos/{repo_full}/pulls?state=open&per_page=50",
                headers=headers, timeout=10)
            if r.status_code != 200:
                return False
            pattern = re.compile(rf'\b#{issue_num}\b')
            for pr in r.json():
                combined = (pr.get("body") or "") + pr.get("title", "")
                if pattern.search(combined):
                    emit("push", f"Existing open PR already covers #{issue_num} — skipping")
                    return True
        except Exception:
            pass
        return False

    def push(self, repo: dict, bug: dict, github_user: str,
             token: str, headers: dict, results: dict) -> bool:
        self.last_pr_url = None
        repo_full  = repo["full_name"]
        repo_short = repo_full.split("/")[-1]
        bug_number = bug["number"]
        branch     = f"fix/issue-{bug_number}"

        # Pre-PR duplicate check — don't open a PR if one already exists for this issue
        if self._has_existing_pr(repo_full, bug_number, headers):
            return False

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

            # Extract PR number from URL for DB tracking
            if pr_url:
                try:
                    pr_num = int(pr_url.rstrip("/").split("/")[-1])
                    db.pr_add(pr_url, repo_full, bug_number, pr_num)
                except Exception:
                    pass

            return True

        except Exception as e:
            emit_fail("push", f"API error: {e}")
            return False


# ── Attempted-issues cache (now delegated to db.py) ─────────────────────────
# The JSON-file cache functions have been replaced by db.already_attempted()
# and db.mark_attempted(). The db module is imported at the top of this file.
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
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        t_start = time.time()
        db.init_db()
        db.run_start(run_id)

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
                if db.already_attempted(repo_full, issue_id):
                    emit("hunt", f"#{issue_id} already attempted in a prior run — skipping")
                    continue

                emit("bug", f"#{issue_id}: {bug['title'][:65]}")
                metrics.increment_metric("issues_attempted")
                self.summary["bugs_attempted"] += 1
                db.mark_attempted(repo_full, issue_id,   # mark before attempt — avoids re-runs on crash
                                  title=bug.get("title",""), run_id=run_id)

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
                    db.patch_done(repo_full, issue_id, pr_url=self.committer.last_pr_url)
                    db.run_finish(run_id, outcome="success",
                                  repo=repo_full,
                                  duration_sec=round(time.time()-t_start, 1),
                                  bugs_scanned=len(bugs),
                                  bugs_attempted=self.summary["bugs_attempted"],
                                  pr_url=self.committer.last_pr_url)
                    print(flush=True)
                    emit_ok("done", "MISSION COMPLETE")
                    self._print_summary()
                    return

        except Exception as e:
            emit_fail("error", str(e))
            db.run_finish(run_id, outcome="error",
                          duration_sec=round(time.time()-t_start, 1))

        db.run_finish(run_id, outcome="no_fix",
                      repo=repo.get("full_name") if repo else None,
                      duration_sec=round(time.time()-t_start, 1),
                      bugs_scanned=len(bugs) if bugs else 0,
                      bugs_attempted=self.summary["bugs_attempted"])
        emit("done", "All bugs tried — no fix committed this run")
        self._print_summary()


if __name__ == "__main__":
    SurgicalBugSniper().run()
