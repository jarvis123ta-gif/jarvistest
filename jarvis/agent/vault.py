"""vault.py — folders on disk become a searchable, linked graph.

Read-only. Nothing in this module ever opens a file for writing.
Standard library only.
"""

from __future__ import annotations

import os
import re
import math
import time
import zlib
import json
import hashlib
from collections import defaultdict

# ---------------------------------------------------------------- limits

MAX_FILE_BYTES = 2 * 1024 * 1024          # 2 MB, per the brief
TEXT_EXTS = {".md", ".markdown", ".txt", ".mdx"}
PDF_EXTS = {".pdf"}
READABLE_EXTS = TEXT_EXTS | PDF_EXTS

SKIP_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "dist", "build", ".cache", ".DS_Store", ".obsidian",
    ".trash", ".idea", ".vscode", "site-packages", ".pytest_cache",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "is", "are", "was", "were", "be", "been", "being", "it",
    "its", "this", "that", "these", "those", "as", "by", "from", "we", "i",
    "you", "he", "she", "they", "them", "his", "her", "our", "your", "their",
    "not", "no", "so", "do", "does", "did", "have", "has", "had", "will",
    "would", "can", "could", "should", "may", "might", "there", "here",
    "what", "when", "where", "which", "who", "how", "all", "any", "than",
    "then", "up", "out", "about", "into", "over", "just", "also",
}

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|([^\]]+))?\]\]")
TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][\w\-/]{1,40})")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'’\-]*")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------- pdf

def _pdf_text(raw: bytes) -> str:
    """Best-effort PDF text extraction with the standard library only.

    Pulls FlateDecode streams, then reads the text-showing operators. This
    handles the ordinary case (text-based PDFs written by normal tools). It
    does not handle scanned images or exotic encodings — those come back
    empty, and the caller marks the note unreadable rather than pretending.
    """
    chunks: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.DOTALL):
        blob = m.group(1)
        try:
            blob = zlib.decompress(blob)
        except zlib.error:
            pass  # may be uncompressed, or an image stream we cannot read
        if b"BT" not in blob:
            continue
        for tm in re.finditer(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>", blob):
            tok = tm.group(0)
            if tok.startswith(b"<"):
                hexs = re.sub(rb"\s", b"", tok[1:-1])
                if len(hexs) % 2:
                    hexs += b"0"
                try:
                    chunks.append(bytes.fromhex(hexs.decode()).decode("utf-16-be", "ignore"))
                except ValueError:
                    continue
            else:
                s = tok[1:-1]
                s = re.sub(rb"\\([nrtbf])", b" ", s)
                s = re.sub(rb"\\([()\\])", rb"\1", s)
                chunks.append(s.decode("latin-1", "ignore"))
    text = " ".join(chunks)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ---------------------------------------------------------------- parsing

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """A deliberately small YAML subset: `key: value` and `key: [a, b]`."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [p.strip().strip("'\"") for p in v[1:-1].split(",") if p.strip()]
        else:
            meta[k] = v.strip("'\"")
    return meta, text[m.end():]


def _title_of(meta: dict, body: str, path: str) -> str:
    if meta.get("title"):
        return str(meta["title"])
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            break
    return os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ")


def _type_of(meta: dict, path: str, root: str) -> str:
    if meta.get("type"):
        return str(meta["type"]).strip().lower()
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)[:-1]
    if parts:
        t = parts[-1].lower().rstrip("s") if len(parts[-1]) > 3 else parts[-1].lower()
        return re.sub(r"[^a-z0-9\-]", "", t) or "note"
    return "note"


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower())
            if len(t) > 1 and t not in STOPWORDS]


# ---------------------------------------------------------------- vault

class Vault:
    """An in-memory, read-only index of one or more folders."""

    def __init__(self, roots: list[str], label: str = "vault"):
        self.roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
        self.label = label
        self.notes: dict[str, dict] = {}
        self.edges: list[tuple[str, str]] = []
        self.adjacency: dict[str, set[str]] = defaultdict(set)
        self.skipped: list[dict] = []
        self.built_at: float = 0.0
        self._df: dict[str, int] = {}
        self._postings: dict[str, dict[str, int]] = defaultdict(dict)
        self._avg_len: float = 1.0

    # -- discovery -------------------------------------------------

    def _walk(self):
        for root in self.roots:
            if not os.path.isdir(root):
                self.skipped.append({"path": root, "reason": "root not found"})
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in READABLE_EXTS:
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                    except OSError as e:
                        self.skipped.append({"path": full, "reason": str(e)})
                        continue
                    if st.st_size > MAX_FILE_BYTES:
                        self.skipped.append(
                            {"path": full, "reason": f"over 2 MB ({st.st_size // 1024} KB)"})
                        continue
                    yield root, full, st

    @staticmethod
    def _nid(path: str) -> str:
        return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]

    # -- build -----------------------------------------------------

    def build(self) -> "Vault":
        t0 = time.time()
        self.notes.clear()
        self.edges.clear()
        self.adjacency.clear()
        self.skipped.clear()

        for root, full, st in self._walk():
            ext = os.path.splitext(full)[1].lower()
            try:
                if ext in PDF_EXTS:
                    body = _pdf_text(open(full, "rb").read())
                    meta: dict = {}
                    unreadable = not body
                else:
                    raw = open(full, "r", encoding="utf-8", errors="replace").read()
                    meta, body = _parse_frontmatter(raw)
                    unreadable = False
            except OSError as e:
                self.skipped.append({"path": full, "reason": str(e)})
                continue

            nid = self._nid(full)
            links = [(m.group(1).strip(), (m.group(2) or "").strip())
                     for m in WIKILINK_RE.finditer(body)]
            self.notes[nid] = {
                "id": nid,
                "path": full,
                "rel": os.path.relpath(full, root),
                "file": os.path.basename(full),
                "root": root,
                "title": _title_of(meta, body, full),
                "type": _type_of(meta, full, root),
                "meta": meta,
                "tags": sorted({t.lower() for t in TAG_RE.findall(body)}
                               | {str(t).lower() for t in (meta.get("tags") or [])
                                  if isinstance(meta.get("tags"), list)}),
                "text": body,
                "excerpt": self._excerpt(body),
                "words": len(body.split()),
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "unreadable": unreadable,
                "_rawlinks": [l[0] for l in links],
                "links": [],
                "backlinks": [],
                "degree": 0,
            }

        self._resolve_links()
        self._build_index()
        self.built_at = time.time()
        self.build_seconds = round(self.built_at - t0, 3)
        return self

    @staticmethod
    def _excerpt(body: str, n: int = 240) -> str:
        clean = re.sub(r"\s+", " ", re.sub(r"^#{1,6}\s*", "", body, flags=re.M)).strip()
        return clean[:n] + ("…" if len(clean) > n else "")

    def _resolve_links(self):
        by_title: dict[str, str] = {}
        by_stem: dict[str, str] = {}
        for nid, n in self.notes.items():
            by_title.setdefault(n["title"].strip().lower(), nid)
            by_stem.setdefault(os.path.splitext(n["file"])[0].strip().lower(), nid)

        seen: set[tuple[str, str]] = set()
        for nid, n in self.notes.items():
            for target in n.pop("_rawlinks"):
                key = target.strip().lower()
                tid = by_title.get(key) or by_stem.get(key)
                if not tid or tid == nid:
                    continue
                n["links"].append(tid)
                self.notes[tid]["backlinks"].append(nid)
                self.adjacency[nid].add(tid)
                self.adjacency[tid].add(nid)
                pair = tuple(sorted((nid, tid)))
                if pair not in seen:
                    seen.add(pair)
                    self.edges.append((nid, tid))

        for nid, n in self.notes.items():
            n["links"] = sorted(set(n["links"]))
            n["backlinks"] = sorted(set(n["backlinks"]))
            n["degree"] = len(self.adjacency[nid])

    # -- search ----------------------------------------------------

    def _build_index(self):
        self._postings = defaultdict(dict)
        lengths: dict[str, int] = {}
        for nid, n in self.notes.items():
            # title and tags weigh more than body prose
            toks = (tokenize(n["title"]) * 3
                    + tokenize(" ".join(n["tags"])) * 2
                    + tokenize(n["type"]) * 2
                    + tokenize(n["text"]))
            lengths[nid] = max(len(toks), 1)
            counts: dict[str, int] = defaultdict(int)
            for t in toks:
                counts[t] += 1
            for t, c in counts.items():
                self._postings[t][nid] = c
        self._df = {t: len(d) for t, d in self._postings.items()}
        self._lengths = lengths
        self._avg_len = (sum(lengths.values()) / len(lengths)) if lengths else 1.0

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """BM25 over the indexed notes. Returns hits with score and a snippet."""
        qt = tokenize(query)
        if not qt or not self.notes:
            return []
        N = len(self.notes)
        k1, b = 1.5, 0.75
        scores: dict[str, float] = defaultdict(float)
        for t in qt:
            postings = self._postings.get(t)
            if not postings:
                continue
            idf = math.log(1 + (N - self._df[t] + 0.5) / (self._df[t] + 0.5))
            for nid, tf in postings.items():
                dl = self._lengths[nid]
                scores[nid] += idf * (tf * (k1 + 1)) / (
                    tf + k1 * (1 - b + b * dl / self._avg_len))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        out = []
        for nid, sc in ranked:
            n = self.notes[nid]
            out.append({
                "id": nid, "title": n["title"], "type": n["type"],
                "file": n["file"], "rel": n["rel"], "path": n["path"],
                "score": round(sc, 3), "degree": n["degree"],
                "snippet": self._snippet(n["text"], qt),
                "mtime": n["mtime"],
            })
        return out

    @staticmethod
    def _snippet(text: str, qt: list[str], width: int = 220) -> str:
        low = text.lower()
        pos = -1
        for t in qt:
            pos = low.find(t)
            if pos >= 0:
                break
        if pos < 0:
            return Vault._excerpt(text, width)
        start = max(0, pos - width // 3)
        frag = re.sub(r"\s+", " ", text[start:start + width]).strip()
        return ("…" if start else "") + frag + "…"

    # -- graph queries ---------------------------------------------

    def hubs(self, n: int = 10) -> list[dict]:
        ranked = sorted(self.notes.values(), key=lambda x: (-x["degree"], x["title"]))
        return [{"id": x["id"], "title": x["title"], "type": x["type"],
                 "degree": x["degree"]} for x in ranked[:n]]

    def counts_by_type(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for n in self.notes.values():
            c[n["type"]] += 1
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def shortest_path(self, a: str, b: str) -> list[str]:
        """Plain BFS. Returns [] when the two nodes are in different components."""
        if a not in self.notes or b not in self.notes:
            return []
        if a == b:
            return [a]
        prev = {a: None}
        frontier = [a]
        while frontier:
            nxt = []
            for cur in frontier:
                for nb in self.adjacency[cur]:
                    if nb in prev:
                        continue
                    prev[nb] = cur
                    if nb == b:
                        path, step = [], b
                        while step is not None:
                            path.append(step)
                            step = prev[step]
                        return list(reversed(path))
                    nxt.append(nb)
            frontier = nxt
        return []

    def note(self, nid: str) -> dict | None:
        return self.notes.get(nid)

    # -- serialisation ---------------------------------------------

    def graph_payload(self) -> dict:
        """What the browser needs to draw. Deliberately excludes full text."""
        return {
            "label": self.label,
            "nodes": [{
                "id": n["id"], "title": n["title"], "type": n["type"],
                "degree": n["degree"], "words": n["words"],
                "mtime": n["mtime"], "file": n["file"],
            } for n in self.notes.values()],
            "edges": [{"s": s, "t": t} for s, t in self.edges],
            "counts": self.counts_by_type(),
            "hubs": self.hubs(8),
            "stats": {
                "notes": len(self.notes),
                "edges": len(self.edges),
                "skipped": len(self.skipped),
                "roots": self.roots,
                "build_seconds": getattr(self, "build_seconds", 0.0),
            },
        }

    def report(self) -> str:
        """Step 1 of the build order: print what was found."""
        lines = [
            f"vault: {self.label}",
            f"roots: {', '.join(self.roots)}",
            f"notes: {len(self.notes)}   edges: {len(self.edges)}   "
            f"skipped: {len(self.skipped)}   in {getattr(self, 'build_seconds', 0)}s",
            "",
            "counts by type",
        ]
        for t, c in self.counts_by_type().items():
            lines.append(f"  {t:<14} {c:>4}")
        lines += ["", "top 10 hubs"]
        for h in self.hubs(10):
            lines.append(f"  {h['degree']:>3}  {h['title']}  ({h['type']})")
        if self.skipped:
            lines += ["", f"skipped ({len(self.skipped)})"]
            for s in self.skipped[:10]:
                lines.append(f"  {s['reason']}: {s['path']}")
        unreadable = [n for n in self.notes.values() if n["unreadable"]]
        if unreadable:
            lines += ["", f"pdfs with no extractable text: {len(unreadable)}"]
            for n in unreadable[:5]:
                lines.append(f"  {n['rel']}")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from data import active_roots, mode_label
    v = Vault(active_roots(), mode_label()).build()
    print(v.report())
