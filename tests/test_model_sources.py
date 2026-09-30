"""W2: the packs' model "sources" can never drift from docs/MODELS.md, or from what discovery accepts.

(a) Every row of MODELS.md's role tables equals a pack source (role, repo, file, size, ComfyUI folder
    and subfolder, run by us) and every pack source is a table row -- both directions. The same for
    the custom-node packages table against the packs' "nodes". The loader MODELS.md names for a row
    reads the pool the role's rule searches, and every role discovery knows has a source.
(b) Every source, as a one-file synthetic pool, satisfies its role's rule through server.pick_model:
    at <subdir>/<file name> (or the bare name), and where `hf download --local-dir` actually puts it
    (<subdir>/<path in repo>). MODELS.md's "Discovery match" column is right about the bare name
    (FAIL bare <=> pick_model finds nothing), and every FAIL-bare source declares its subfolder.

Run: python3 tests/test_model_sources.py
"""
import importlib.util
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- before server.py's own import-time config read

spec = importlib.util.spec_from_file_location("srv_sources", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
engines = srv.engines

MODELS_MD = open(os.path.join(ROOT, "docs", "MODELS.md"), encoding="utf-8").read()


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_rows():
    """MODELS.md's role tables -> [{role, repo, file, size, folder, subdir, run_by_us, loader, bare_fails,
    licence_note, unlicensed}], one per file (a row naming two roles gives two)."""
    out, header = [], None
    for line in MODELS_MD.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        c = cells(line)
        if c[0] == "Role":
            header = c
            continue
        if header is None or set(line) <= set("|- "):
            continue
        row = dict(zip(header, c))
        roles = re.findall(r"`([a-z0-9_]+)`", row["Role"])
        src = next(v for k, v in row.items() if k.startswith("Source"))
        m = re.search(r"`([^`]+)` · `([^`]+)`((?: \+ `[^`]+`)*)", src)
        files = [m.group(2)] + re.findall(r"`([^`]+)`", m.group(3))
        # "a/b.safetensors + c.safetensors": a later file shares the first one's folder in the repo
        files = [files[0]] + [os.path.join(os.path.dirname(files[0]), f) for f in files[1:]]
        sizes = [int(s.replace(",", "").strip()) for s in row["HF size"].split("+")]
        fm = re.match(r"`models/([^/`]+)/?([^`]*?)/?` \(`([A-Za-z0-9]+)`", row["ComfyUI folder"])
        lic = re.search(r"\(([A-Za-z0-9.\-]+)\)", src[m.end():])
        assert len(roles) == len(files) == len(sizes), row
        for role, f, size in zip(roles, files, sizes):
            out.append({"role": role, "repo": m.group(1), "file": f, "size": size, "folder": fm.group(1),
                        "subdir": fm.group(2) or None, "loader": fm.group(3),
                        "run_by_us": row["Match"].startswith("yes"),
                        "bare_fails": "FAIL bare" in row["Discovery match"],
                        "licence_note": lic.group(1) if lic else None,
                        "unlicensed": "unlicensed" in row["Role"]})
    return out


def node_rows():
    part = MODELS_MD.split("## Custom-node packages", 1)[1].split("\n## ", 1)[0]
    out = set()
    for line in part.splitlines():
        m = re.match(r"\| ([A-Za-z0-9\-]+) \| \[[^\]]+\]\((https://[^)]+)\)", line)
        if m:
            out.add((m.group(1), m.group(2)))
    return out


def key(r):
    return (r["role"], r["repo"], r["file"], r["size"], r["folder"], r["subdir"], r["run_by_us"])


ROWS = table_rows()
PACK_SOURCES = []    # (pack, role, source)
for pack in engines.packs():
    for role, lst in (pack.get("sources") or {}).items():
        for s in lst:
            PACK_SOURCES.append((pack, role, s))

print("(a) MODELS.md's tables and the packs' sources are the same list, both ways")
table = {key(r) for r in ROWS}
packed = {(role, s.get("repo"), s.get("file"), s.get("size"), s.get("folder"), s.get("subdir"), s.get("run_by_us"))
          for _, role, s in PACK_SOURCES}
check("MODELS.md has role rows to compare (%d)" % len(ROWS), len(ROWS) >= 30, len(ROWS))
check("every MODELS.md row is a pack source", table <= packed, sorted(table - packed))
check("every pack source is a MODELS.md row", packed <= table, sorted(packed - table))
check("no source is declared twice", len(PACK_SOURCES) == len(packed), len(PACK_SOURCES))
owners = {}
for pack, role, _ in PACK_SOURCES:
    owners.setdefault(role, set()).add(pack["id"])
check("each role's sources live in exactly one pack", all(len(v) == 1 for v in owners.values()),
      {r: v for r, v in owners.items() if len(v) > 1})
check("every role discovery knows has a source", set(srv.ROLE_RULES) <= set(owners),
      sorted(set(srv.ROLE_RULES) - set(owners)))
check("every source has exactly the manifest's fields",
      all(set(s) - {"subdir"} == {"repo", "file", "size", "folder", "run_by_us", "licence"}
          and isinstance(s["size"], int) and isinstance(s["run_by_us"], bool) for _, _, s in PACK_SOURCES),
      [s for _, _, s in PACK_SOURCES if set(s) - {"subdir"} != {"repo", "file", "size", "folder", "run_by_us",
                                                                   "licence"}])
bad_loader = [r["role"] for r in ROWS
              if r["loader"] not in [n for n, _ in srv.POOL_NODES[srv.ROLE_POOL[r["role"]]]]]
check("the loader MODELS.md names reads the pool the role's rule searches", bad_loader == [], bad_loader)
by_key = {key(r): r for r in ROWS}
bad_lic = []
for pack, role, s in PACK_SOURCES:
    r = by_key.get((role, s["repo"], s["file"], s["size"], s["folder"], s.get("subdir"), s["run_by_us"]))
    names = [e["name"] for e in engines.licences() if e["engine"] == pack["id"]]
    want_ok = (s["licence"] is None) if (r and r["unlicensed"]) else (
        s["licence"] == r["licence_note"] if (r and r["licence_note"]) else s["licence"] in names)
    if not want_ok:
        bad_lic.append((role, s["file"], s["licence"]))
check("each source's licence is its pack's licence name (or the one MODELS.md's row names; none when "
      "the row says unlicensed)", bad_lic == [], bad_lic)
packed_nodes = {(n["name"], n["url"]) for pack in engines.packs() for n in pack.get("nodes") or []}
check("MODELS.md's custom-node packages table has rows (%d)" % len(node_rows()), len(node_rows()) >= 4)
check("every custom-node package MODELS.md lists is in a pack's nodes", node_rows() <= packed_nodes,
      sorted(node_rows() - packed_nodes))
check("every pack node is a MODELS.md custom-node package", packed_nodes <= node_rows(),
      sorted(packed_nodes - node_rows()))

print("(a) licences: a plain summary each, and the link MODELS.md's Licence: line gives")
LICENCE_LINKS = {}   # pack module -> the URLs its MODELS.md section's "Licence:" line links
for section in MODELS_MD.split("\n## ")[1:]:
    head, _, rest = section.partition("\nLicence:")
    if not _:
        continue
    urls = re.findall(r"\]\((https?://[^)]+)\)", rest.split("\n", 1)[0])
    for mod in re.findall(r"engines/(\w+)\.py", head):
        LICENCE_LINKS.setdefault(mod, set()).update(urls)
mod_of = {pid: os.path.splitext(os.path.basename(f))[0] for pid, f in engines._PACK_FILES.items()}
entries = engines.licences()
check("MODELS.md's Licence: lines link licence texts (%d packs)" % sum(1 for v in LICENCE_LINKS.values() if v),
      sum(1 for v in LICENCE_LINKS.values() if v) >= 5, LICENCE_LINKS)
check("every licence entry has a one-line summary",
      all(isinstance(e.get("summary"), str) and e["summary"].strip() and "\n" not in e["summary"] for e in entries),
      [(e["engine"], e["name"]) for e in entries if not (e.get("summary") or "").strip()])
missing = [(mod, u) for mod, urls in LICENCE_LINKS.items() for u in urls
           if u not in [e.get("url") for e in entries if mod_of.get(e["engine"]) == mod]]
check("every licence MODELS.md links is carried, as that URL, by its pack", missing == [], missing)
all_links = set().union(*LICENCE_LINKS.values())
stray = [(e["engine"], e["url"]) for e in entries if e.get("url") and e["url"] not in all_links]
check("every licence URL a pack carries is one MODELS.md links", stray == [], stray)

print("(b) every source satisfies its role's rule through pick_model, as MODELS.md's Discovery match says")
check("there are sources to test (%d)" % len(PACK_SOURCES), len(PACK_SOURCES) == len(ROWS) and PACK_SOURCES)
for pack, role, s in PACK_SOURCES:
    rule = srv.ROLE_RULES[role]
    name = os.path.basename(s["file"])
    placed = s["subdir"] + "/" + name if s.get("subdir") else name
    landed = s["subdir"] + "/" + s["file"] if s.get("subdir") else s["file"]
    check("%s: %s passes" % (role, placed), srv.pick_model([placed], rule, False) == placed, rule)
    check("%s: where hf download puts it (%s) passes" % (role, landed),
          srv.pick_model([landed], rule, False) == landed, rule)
    row = by_key.get((role, s["repo"], s["file"], s["size"], s["folder"], s.get("subdir"), s["run_by_us"]))
    bare_fails = srv.pick_model([name], rule, False) is None
    check("%s: MODELS.md's Discovery match is right about the bare name %s, and a failing one has its "
          "subfolder" % (role, name),
          row is not None and row["bare_fails"] == bare_fails and (not bare_fails or bool(s.get("subdir"))),
          (s.get("subdir"), bare_fails, row and row["bare_fails"]))

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
