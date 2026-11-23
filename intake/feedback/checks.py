"""Deterministic checks: on the raw text before any model sees it, and on the reader's task before a builder does.

Every check returns (name, ok, detail). The first failure names the rejection; all of them go to the log.
"""

import difflib
import fnmatch
import json
import posixpath
import re
import unicodedata

Check = tuple[str, bool, str]

TASK_KEYS = {"type", "summary", "acceptance", "allowed_paths"}

# Raw text ---------------------------------------------------------------------------------------------------

_VISIBLE_FORMAT_CHARS = {"‍"}  # zero-width joiner: emoji sequences use it
_HTML_COMMENT = re.compile(r"<!--")
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}[\s:]?){32,}")
_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|%[0-9a-fA-F]{2}){8,}")
_PHRASES = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier|your) (?:instructions|rules|prompt)"
    r"|\b(?:system|developer) (?:prompt|message)\b|\byou are now\b|ignora (?:las |tus )?instrucciones",
    re.IGNORECASE)


def _hidden_chars(text: str) -> list[str]:
    return sorted({f"U+{ord(ch):04X}" for ch in text
                   if unicodedata.category(ch) in ("Cf", "Co") and ch not in _VISIBLE_FORMAT_CHARS})


def _mixed(blob: str) -> bool:
    return bool(re.search(r"[a-z]", blob) and re.search(r"[A-Z]", blob) and re.search(r"[0-9]", blob))


def raw(text: str, limits: dict) -> list[Check]:
    size = len(text.encode("utf-8"))
    hidden = _hidden_chars(text)
    blobs = [b for b in _BASE64.findall(text) if _mixed(b)] + _HEX.findall(text) + _ESCAPES.findall(text)
    phrase = _PHRASES.search(text)
    return [
        ("too-large", size <= limits["raw_max_bytes"], f"{size} bytes"),
        ("hidden-text", not hidden and not _HTML_COMMENT.search(text),
         f"invisible characters {', '.join(hidden[:5])}" if hidden else
         "an HTML comment (hidden when GitHub renders it)" if _HTML_COMMENT.search(text) else "none"),
        ("encoded-blob", not blobs, f"{len(blobs)} encoded run(s), e.g. {blobs[0][:24]}…" if blobs else "none"),
        ("injection-phrase", phrase is None, f"\"{phrase.group(0)}\"" if phrase else "none"),
    ]


# The reader's task ------------------------------------------------------------------------------------------

_URL = re.compile(r"https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|dev|sh|xyz|ru|cn|me|co|app|site|link|ly|es)\b",
                  re.IGNORECASE)
_COMMAND = re.compile(
    r"\$\(|\|\s*(?:sh|bash|zsh|python3?)\b|&&"
    r"|\b(?:curl|wget|chmod|chown|sudo|eval|exec|ssh|scp|rsync|netcat|nc|base64|xxd|openssl|rm -rf|pip install|npm)\b",
    re.IGNORECASE)
_LINK = re.compile(  # a pointer to something outside the repo that only the raw text names
    r"\b(?:external|third[- ]party|outside|their|author's|reporter's|user's|maintainer's)\b(?:\W+\w+){0,3}\W+"
    r"(?:links?|urls?|sites?|websites?|pages?|guides?|docs|resources?|services?|accounts?)\b"
    r"|\b(?:links?|urls?)\b(?:\W+\w+){0,3}\W+(?:external|third[- ]party|outside|their|author's|reporter's)\b"
    r"|\b(?:that|this|the given|the provided|the requested) (?:link|url|address|site|page)\b",
    re.IGNORECASE)
_SECRETS = re.compile(
    r"\b(?:env|environ|environment|secrets?|tokens?|api[ _-]?keys?|credentials?|passwords?|cookies?|private keys?"
    r"|ssh|keychain|anthropic\w*|github_token|claude_config\w*|home directory)\b|\.env\b|~/",
    re.IGNORECASE)
_META = re.compile(
    r"\b(?:factory|verifier|gate|workflows?|github actions|pipeline|pre-commit|git hooks?|hooks?|prompts?"
    r"|instructions?|system message|maintainer|reviewers?|approv\w*|llm|agents?|assistant|claude|codex|openai"
    r"|allowed_paths|forbidden_paths|conftest|fakes\.py|pyproject|uv\.lock|lockfile|xfail|mutants?|coverage"
    r"|(?:skip|delete|remove|disable|loosen|relax)\w* (?:\w+ ){0,2}tests?)\b"
    r"|\b(?:CI|AI)\b",
    re.IGNORECASE)
_META_CASED = re.compile(r"\b(?:CI|AI)\b")  # only upper case: "ci" or "ai" inside Spanish words isn't a hit


def _texts(task: dict) -> list[str]:
    return [task["summary"], *task["acceptance"]]


def _hits(pattern: re.Pattern, texts: list[str]) -> list[str]:
    return sorted({m.group(0) for t in texts for m in pattern.finditer(t)})


def _meta_hits(texts: list[str]) -> list[str]:
    found = set()
    for t in texts:
        for m in _META.finditer(t):
            word = m.group(0)
            if word.lower() in ("ci", "ai") and not _META_CASED.fullmatch(word):
                continue
            found.add(word)
    return sorted(found)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def longest_copy(texts: list[str], raw_text: str) -> str:
    source = _norm(raw_text)
    best = ""
    for t in texts:
        t = _norm(t)
        m = difflib.SequenceMatcher(None, t, source, autojunk=False).find_longest_match(0, len(t), 0, len(source))
        if m.size > len(best):
            best = t[m.a:m.a + m.size]
    return best


def _path_problem(path: str, allowed: list[str], forbidden: list[str], files: set[str]) -> str | None:
    if not isinstance(path, str) or not path:
        return "not a path"
    if path.startswith(("/", "~")) or "\\" in path or ".." in path.split("/") or re.search(r"[*?\[\]\s]", path):
        return f"{path!r}: not a plain relative path"
    norm = posixpath.normpath(path) + ("/" if path.endswith("/") else "")
    for f in forbidden:
        if norm == f or (f.endswith("/") and norm.startswith(f)) or fnmatch.fnmatch(norm, f) \
                or (norm.endswith("/") and f.startswith(norm)):
            return f"{path!r}: forbidden ({f})"
    if not any(norm == a or (a.endswith("/") and norm.startswith(a)) for a in allowed):
        return f"{path!r}: outside {allowed}"
    exists = norm in files or (norm.endswith("/") and any(x.startswith(norm) for x in files))
    if not exists and not re.fullmatch(r"tests/test_[a-z0-9_]+\.py", norm):
        return f"{path!r}: not in the repository (only a new tests/test_*.py may be created)"
    return None


def parse_answer(answer: str) -> dict | None:
    text = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def task(doc: dict, raw_text: str, policy: dict, files: set[str]) -> list[Check]:
    """Checks on a task the reader proposed. `doc` is already parsed and is not a refusal."""
    lim, target = policy["limits"], policy["target"]
    keys = set(doc)
    shape_ok = (keys == TASK_KEYS and isinstance(doc.get("summary"), str) and isinstance(doc.get("acceptance"), list)
                and all(isinstance(a, str) for a in doc["acceptance"]) and isinstance(doc.get("allowed_paths"), list))
    out: list[Check] = [("schema", shape_ok, "ok" if shape_ok else
                         f"keys {sorted(keys)}, expected {sorted(TASK_KEYS)} with the contract's types")]
    if not shape_ok:
        return out
    texts = _texts(doc)
    sizes = (len(doc["summary"]) <= lim["summary_max_chars"] and "\n" not in doc["summary"]
             and 1 <= len(doc["acceptance"]) <= lim["acceptance_max_items"]
             and all(len(a) <= lim["acceptance_item_max_chars"] for a in doc["acceptance"])
             and 1 <= len(doc["allowed_paths"]) <= lim["paths_max"])
    path_problems = [p for p in (_path_problem(x, target["allowed"], target["forbidden"], files)
                                 for x in doc["allowed_paths"]) if p]
    hidden = [c for t in texts for c in _hidden_chars(t)]
    urls, commands, secrets_, meta = (_hits(_URL, texts) + _hits(_LINK, texts), _hits(_COMMAND, texts),
                                      _hits(_SECRETS, texts), _meta_hits(texts))
    copy = longest_copy(texts, raw_text)
    out += [
        ("type", doc["type"] in target["types"], f"{doc['type']!r}"),
        ("size", sizes, f"summary {len(doc['summary'])} chars (max {lim['summary_max_chars']}), "
                        f"{len(doc['acceptance'])} acceptance item(s) of {[len(a) for a in doc['acceptance']]} chars "
                        f"(max {lim['acceptance_max_items']} of {lim['acceptance_item_max_chars']}), "
                        f"{len(doc['allowed_paths'])} path(s) (max {lim['paths_max']})"),
        ("paths", not path_problems, "; ".join(path_problems) or ", ".join(doc["allowed_paths"])),
        ("hidden-text", not hidden, ", ".join(hidden) or "none"),
        ("url", not urls, ", ".join(urls) or "none"),
        ("command", not commands, ", ".join(commands) or "none"),
        ("secrets", not secrets_, ", ".join(secrets_) or "none"),
        ("about-the-factory", not meta, ", ".join(meta) or "none"),
        ("verbatim-copy", len(copy) <= lim["verbatim_max_chars"],
         f"longest stretch copied from the feedback: {len(copy)} chars"),
    ]
    return out
