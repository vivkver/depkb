#!/usr/bin/env python3
"""depkb — a token-cheap dependency knowledge base for Gradle Java builds.

Gradle resolves the real graph (init script, nothing is compiled); depkb stores it in
SQLite with jar package/class indexes, POM metadata, the version catalog and what
every module's code references (compiled bytecode, or imports when not compiled),
linked to the dependency that provides it. Queries
answer in a few lines instead of dumping dependency trees, and refresh the KB first
when build files or sources changed (only the edited modules and their dependents
are re-exported).

  depkb build                   export + index (no-op when nothing changed)
  depkb summary                 one-screen overview, start here
  depkb usage acme-core         per module: declared/transitive, version, override, imported?
  depkb runtime acme-core       what each app module actually runs with, and who uses it
  depkb why acme-core -p app    why that version, who requested what, shortest paths
  depkb imports app             what a module's code imports, by dependency
  depkb users ObjectMapper      source files importing a class or package
  depkb audit                   undeclared / unused / not-on-classpath dependencies
  depkb modules [app]           module graph and impact of a change
  depkb api Core -p app         real API of a class in the version on that classpath
  depkb compat                  callers that break with an overridden version
  depkb impact acme-core 2.1.0  would moving to that version break any caller or lose classes?
  depkb explain <Class|error>   diagnose a ClassNotFoundException / NoClassDefFoundError
  depkb missing                 predict them: referenced classes nothing on the classpath provides
  depkb jvm 21 --from 11        impact of a Java upgrade on modules, jars and JVM options
  depkb version | who-uses | deps | pkg | info | diff
  depkb conflicts | drift | split-packages | unresolved | licenses | catalog
  depkb outdated | vulns        network: Maven metadata / OSV (cached)

Python 3.11+ standard library only. Run `depkb <command> -h` for options.
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict, deque
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classfile  # noqa: E402  (sibling module)
INIT_SCRIPT = HERE / "init" / "depkb.init.gradle"
DEFAULT_CONFIGS = "compileClasspath,runtimeClasspath,testCompileClasspath,testRuntimeClasspath,annotationProcessor"
ABBR = {"compileClasspath": "c", "runtimeClasspath": "r", "testCompileClasspath": "tc",
        "testRuntimeClasspath": "tr", "annotationProcessor": "ap"}
LEGEND = "configs: c=compileClasspath r=runtimeClasspath tc/tr=test compile/runtime ap=annotationProcessor"
SKIP_DIRS = {".git", ".gradle", "build", ".depkb", "node_modules", ".idea"}
HASH_NAMES = {"settings.gradle", "settings.gradle.kts", "gradle.properties", "gradle.lockfile",
              "buildscript-gradle.lockfile", "gradle-wrapper.properties"}
DEFAULT_REPO = "https://repo1.maven.org/maven2/"
OSV_URL = os.environ.get("DEPKB_OSV_URL", "https://api.osv.dev").rstrip("/")
CACHE_TTL = 24 * 3600
SOURCE_CMDS = {"usage", "runtime", "imports", "users", "audit", "api", "compat", "impact", "jvm", "missing", "explain"}

SCHEMA_VERSION = 9
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS projects(path TEXT PRIMARY KEY, name TEXT, dir TEXT, kind TEXT, javalib INTEGER, java TEXT);
CREATE TABLE IF NOT EXISTS jvmscan(src TEXT PRIMARY KEY, stamp TEXT);
CREATE TABLE IF NOT EXISTS jvmrefs(src TEXT, owner TEXT, name TEXT, desc TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS sourcesets(project TEXT, name TEXT, dir TEXT, cp TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS declared(project TEXT, bucket TEXT, id TEXT, version TEXT, platform INTEGER);
CREATE TABLE IF NOT EXISTS evidence(project TEXT, sset TEXT, kind TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS mrefs(file INTEGER, owner TEXT, name TEXT, desc TEXT);
CREATE TABLE IF NOT EXISTS services(ga TEXT, version TEXT, iface TEXT);
CREATE TABLE IF NOT EXISTS jarscan(ga TEXT, version TEXT, PRIMARY KEY(ga, version));
CREATE TABLE IF NOT EXISTS jarrefs(ga TEXT, version TEXT, owner TEXT, name TEXT, desc TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS buildfiles(path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER, sha TEXT, owner TEXT);
CREATE TABLE IF NOT EXISTS srcfiles(id INTEGER PRIMARY KEY, project TEXT, sset TEXT, path TEXT UNIQUE,
                                    mtime INTEGER, size INTEGER, package TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS imports(file INTEGER, line INTEGER, fqn TEXT, static INTEGER, wildcard INTEGER, via TEXT);
CREATE TABLE IF NOT EXISTS links(project TEXT, sset TEXT, fqn TEXT, static INTEGER, wildcard INTEGER,
                                 kind TEXT, ga TEXT, version TEXT, owner TEXT,
                                 PRIMARY KEY(project, sset, fqn, static, wildcard)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pcs(id INTEGER PRIMARY KEY, project TEXT, config TEXT, root INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS comps(id INTEGER PRIMARY KEY, cid TEXT UNIQUE, ga TEXT, version TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS nodes(pc INTEGER, comp INTEGER, direct INTEGER, reqs TEXT, reasons TEXT,
                                 PRIMARY KEY(pc, comp)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS edges(pc INTEGER, f INTEGER, t INTEGER, req TEXT, cons INTEGER);
CREATE TABLE IF NOT EXISTS unresolved(pc INTEGER, f INTEGER, req TEXT, err TEXT);
CREATE TABLE IF NOT EXISTS artifacts(ga TEXT, version TEXT, file TEXT, PRIMARY KEY(ga, version));
CREATE TABLE IF NOT EXISTS pkginfo(ga TEXT, version TEXT, description TEXT, url TEXT, licenses TEXT,
                                   jpms TEXT, classes INTEGER, PRIMARY KEY(ga, version));
CREATE TABLE IF NOT EXISTS packages(pkg TEXT, ga TEXT, version TEXT, classes INTEGER);
CREATE TABLE IF NOT EXISTS classes(simple TEXT, pkg TEXT, ga TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS catalog(accessor TEXT, kind TEXT, ga TEXT, version TEXT, ref TEXT, file TEXT);
CREATE TABLE IF NOT EXISTS latest(ga TEXT PRIMARY KEY, latest TEXT, fetched INTEGER);
CREATE TABLE IF NOT EXISTS vulns(ga TEXT, version TEXT, ids TEXT, fetched INTEGER, PRIMARY KEY(ga, version));
CREATE TABLE IF NOT EXISTS vuln_details(id TEXT PRIMARY KEY, summary TEXT, severity TEXT, fixed TEXT);
CREATE INDEX IF NOT EXISTS i_comps_ga ON comps(ga);
CREATE INDEX IF NOT EXISTS i_nodes_comp ON nodes(comp);
CREATE INDEX IF NOT EXISTS i_edges ON edges(pc, t);
CREATE INDEX IF NOT EXISTS i_pcs ON pcs(project, config);
CREATE INDEX IF NOT EXISTS i_pkg ON packages(pkg);
CREATE INDEX IF NOT EXISTS i_pkg_ga ON packages(ga, version);
CREATE INDEX IF NOT EXISTS i_cls ON classes(simple, pkg);
CREATE INDEX IF NOT EXISTS i_cls_ga ON classes(ga, version);
CREATE INDEX IF NOT EXISTS i_imp_file ON imports(file);
CREATE INDEX IF NOT EXISTS i_imp_fqn ON imports(fqn);
CREATE INDEX IF NOT EXISTS i_src_proj ON srcfiles(project, sset);
CREATE INDEX IF NOT EXISTS i_links_ga ON links(ga);
CREATE INDEX IF NOT EXISTS i_links_owner ON links(owner);
CREATE INDEX IF NOT EXISTS i_mrefs_file ON mrefs(file);
CREATE INDEX IF NOT EXISTS i_mrefs_owner ON mrefs(owner);
CREATE INDEX IF NOT EXISTS i_decl ON declared(project);
CREATE INDEX IF NOT EXISTS i_jarrefs ON jarrefs(ga, version, kind);
CREATE INDEX IF NOT EXISTS i_jvmrefs ON jvmrefs(src);
"""


# ---------------------------------------------------------------- utilities

def die(msg, code=1):
    print(f"depkb: {msg}", file=sys.stderr)
    sys.exit(code)


def emit(lines, limit):
    """Print lines, truncating to `limit` with a count of what was hidden."""
    lines = list(lines)
    if limit and len(lines) > limit:
        hidden = len(lines) - limit
        lines = lines[:limit] + [f"... {hidden} more (use --limit 0 for all)"]
    for ln in lines:
        print(ln)


def top_level(name, sep="/"):
    """Outer class of a (possibly nested) class name. '$' only separates nesting when it is not the
    first character of the simple name (Gson's '$Gson$Types' is a top-level class)."""
    pkg, dot, simple = name.rpartition(sep)
    i = simple.find("$", 1)
    return name if i < 1 else pkg + dot + simple[:i]


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}{'es' if word.endswith(('s', 'sh', 'ch')) else 's'}"


def ab(config):
    return ABBR.get(config, config)


def short(cid):
    """'org.acme:acme-core:2.0.0' -> 'acme-core:2.0.0'; 'project :x' -> ':x'."""
    if cid.startswith("project "):
        return cid[8:]
    p = cid.split(":")
    return ":".join(p[1:]) if len(p) >= 3 else cid


def split_cid(cid):
    if cid.startswith("project "):
        return cid, "", "p"
    p = cid.split(":", 2)
    if len(p) == 3:
        return f"{p[0]}:{p[1]}", p[2], "m"
    return cid, "", "m"


def req_version(q):
    if q.startswith("project "):
        return ""
    p = q.split(":", 2)
    return p[2] if len(p) == 3 else ""


_PRE = re.compile(r"(?i)(alpha|beta|rc|cr|milestone|preview|snapshot|ea|dev|incubating|[.-]m\d+|[.-]b\d+)")


def is_pre(v):
    return bool(_PRE.search(v))


def vkey(v):
    """Approximate Maven version ordering: numeric parts numerically, qualifiers lower."""
    out = []
    for tok in re.findall(r"\d+|[A-Za-z]+", v):
        out.append((1, int(tok), "") if tok.isdigit() else (0, 0, tok.lower()))
    # a bare release sorts above the same version with a qualifier (1.0 > 1.0-rc1)
    out.append((0.5, 0, ""))
    return out


def find_db(explicit):
    if explicit:
        return Path(explicit)
    if os.environ.get("DEPKB_DB"):
        return Path(os.environ["DEPKB_DB"])
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        if (d / ".depkb" / "depkb.db").exists():
            return d / ".depkb" / "depkb.db"
    return cur / ".depkb" / "depkb.db"


def connect(path, must_exist=True):
    if must_exist and not Path(path).exists():
        die(f"no knowledge base at {path}. Run `depkb build` in the Gradle root first.", 2)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if must_exist:
            conn.executescript(SCHEMA)
            die("knowledge base format changed; run `depkb build` once to rebuild it", 2)
    conn.executescript(SCHEMA)
    if must_exist and not meta(conn, "built_at"):
        die("knowledge base is empty; run `depkb build` in the Gradle root first", 2)
    return conn


def meta(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


# ---------------------------------------------------------------- build / ingest / auto-refresh

BUILD_EXT = (".gradle", ".gradle.kts", ".versions.toml", ".lockfile")
SRC_EXT = (".java", ".kt", ".groovy")
GLOBAL = "GLOBAL"  # build files that can affect every module (settings, root build, catalog, buildSrc...)


def discover_build_files(proj):
    out = []
    for dirpath, dirs, names in os.walk(proj):
        rel = Path(dirpath).relative_to(proj).parts
        in_bs = "buildSrc" in rel or "build-logic" in rel
        # build scripts never live inside source trees (except convention plugins)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (d != "src" or in_bs))
        for n in names:
            if n in HASH_NAMES or n.endswith(BUILD_EXT) or (in_bs and n.endswith(SRC_EXT)):
                out.append(Path(dirpath) / n)
    return out


def _sha(path):
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def track_build_files(conn, proj):
    """Remember every build input with its owner module so later edits can be mapped to modules."""
    dirmap = {d: p for p, d in conn.execute("SELECT path, dir FROM projects") if p != ":"}
    rows = []
    for f in discover_build_files(proj):
        owner = GLOBAL if f.name.startswith("settings.gradle") else dirmap.get(str(f.parent), GLOBAL)
        st = f.stat()
        rows.append((str(f), st.st_mtime_ns, st.st_size, _sha(f), owner))
    conn.execute("DELETE FROM buildfiles")
    conn.executemany("INSERT INTO buildfiles VALUES(?,?,?,?,?)", rows)
    conn.commit()


def changed_modules(conn):
    """Modules whose build inputs changed since the last export (stat first, hash only if needed)."""
    changed = set()
    tracked = set()
    for path, mt, sz, sha, owner in conn.execute("SELECT path, mtime, size, sha, owner FROM buildfiles").fetchall():
        tracked.add(path)
        try:
            st = os.stat(path)
        except OSError:
            changed.add(owner)
            continue
        if st.st_mtime_ns == mt and st.st_size == sz:
            continue
        if _sha(path) == sha:
            conn.execute("UPDATE buildfiles SET mtime=?, size=? WHERE path=?", (st.st_mtime_ns, st.st_size, path))
        else:
            changed.add(owner)
    # a build file or lockfile newly created in an existing module directory
    for p, d in conn.execute("SELECT path, dir FROM projects").fetchall():
        for n in ("build.gradle", "build.gradle.kts", "gradle.lockfile"):
            f = os.path.join(d or "", n)
            if f not in tracked and os.path.exists(f):
                changed.add(GLOBAL if p == ":" else p)
    conn.commit()
    return changed


def dependents(conn, projects):
    """Modules whose resolved graphs contain any of `projects` (they must be re-resolved too)."""
    out = set(projects)
    if not projects:
        return out
    marks = ",".join("?" * len(projects))
    for (p,) in conn.execute(f"""SELECT DISTINCT p.project FROM nodes n JOIN comps c ON c.id=n.comp
                                 JOIN pcs p ON p.id=n.pc WHERE c.cid IN ({marks})""",
                             [f"project {x}" for x in projects]):
        out.add(p)
    return out


def gradle_cmd(proj):
    for name in ("gradlew", "gradlew.bat"):
        w = proj / name
        if w.exists() and (os.name == "nt") == name.endswith(".bat"):
            return [str(w)]
    g = shutil.which("gradle")
    if not g:
        die("no ./gradlew in the project and no `gradle` on PATH")
    return [g]


def run_export(proj, opts, projects=None):
    """Run the Gradle exporter for all modules, or only `projects`. Returns the export dir."""
    export = proj / ".depkb" / ("export" if projects is None else "export-partial")
    shutil.rmtree(export, ignore_errors=True)
    tasks = ["depKbExport"] if projects is None else \
        [":depKbExport" if p == ":" else f"{p}:depKbExport" for p in sorted(projects)]
    cmd = gradle_cmd(proj) + ["-q", "-I", str(INIT_SCRIPT), *tasks, f"-Ddepkb.out={export}",
                              f"-Ddepkb.configs={opts['configs']}", "--continue", "--no-configuration-cache"]
    if not opts.get("no_parallel"):
        cmd.append("--parallel")
    if opts.get("offline"):
        cmd.append("--offline")
    cmd += shlex.split(opts.get("gradle_args") or "")
    t0 = time.time()
    scope = "all modules" if projects is None else f"{len(projects)} module(s): {', '.join(sorted(projects)[:5])}"
    print(f"depkb: Gradle export of {scope} ...", file=sys.stderr)
    r = subprocess.run(cmd, cwd=proj, capture_output=True, text=True)
    files = list((export / "projects").glob("*.json")) if (export / "projects").exists() else []
    if r.returncode != 0:
        tail = [ln for ln in (r.stderr or r.stdout).splitlines() if ln.strip() and "JAVA_TOOL_OPTIONS" not in ln][-15:]
        print("depkb: Gradle reported errors (partial export kept):\n  " + "\n  ".join(tail), file=sys.stderr)
    if not files:
        return None
    print(f"depkb: exported {len(files)} module(s) in {time.time() - t0:.0f}s", file=sys.stderr)
    return export


def build_opts(a):
    return {"configs": a.configs, "offline": a.offline, "no_parallel": a.no_parallel,
            "gradle_args": a.gradle_args, "no_jars": a.no_jars, "no_classes": a.no_classes}


def cmd_build(a):
    proj = Path(a.project_dir).resolve()
    if not any((proj / s).exists() for s in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts")):
        die(f"{proj} does not look like a Gradle root (no settings.gradle[.kts])")
    kb = proj / ".depkb"
    kb.mkdir(exist_ok=True)
    (kb / ".gitignore").write_text("*\n")
    db = Path(a.db) if a.db else kb / "depkb.db"
    opts = build_opts(a)
    conn = connect(db, must_exist=False)
    built = meta(conn, "built_at")
    if built and not a.force and json.loads(meta(conn, "opts", "{}")).get("configs") == a.configs:
        set_meta(conn, "opts", json.dumps(opts))
        if refresh(conn, sources=True, quiet=False) == "fresh":
            print(f"up to date (built {built}; build files and sources unchanged). Use --force to rebuild.")
        return
    export = run_export(proj, opts)
    if export is None:
        die("export produced no data", 3)
    ingest(conn, export, proj, opts)


def cmd_ingest(a):
    """Ingest an export directory produced elsewhere (e.g. a CI artifact)."""
    proj = Path(a.project_dir).resolve()
    db = Path(a.db) if a.db else proj / ".depkb" / "depkb.db"
    ingest(connect(db, must_exist=False), Path(a.export_dir).resolve(), proj, build_opts(a))


def refresh(conn, sources=False, quiet=True):
    """Bring the KB up to date before answering: re-export modules whose build files changed
    (plus modules that depend on them), then rescan changed source files. Returns a status word."""
    if os.environ.get("DEPKB_AUTO", "1") == "0":
        return "skipped"
    proj = Path(meta(conn, "project_dir", "."))
    if not proj.exists():
        return "skipped"
    opts = json.loads(meta(conn, "opts", "{}") or "{}")
    opts.setdefault("configs", DEFAULT_CONFIGS)
    status = "fresh"
    changed = changed_modules(conn)
    if changed:
        n_all = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
        affected = None if GLOBAL in changed else dependents(conn, changed)
        if affected is not None and len(affected) > max(30, n_all // 2):
            affected = None  # cheaper to export everything once
        why = "shared build files" if affected is None else ", ".join(sorted(changed)[:5])
        print(f"depkb: build files changed ({why}); refreshing", file=sys.stderr)
        export = run_export(proj, opts, affected)
        if export is None:
            print("depkb: refresh failed; answers below may be STALE", file=sys.stderr)
            return "stale"
        ingest(conn, export, proj, opts, replace=affected, quiet=quiet)
        status = "refreshed"
    if sources:
        n = scan_sources(conn)
        if n and status == "fresh":
            status = "refreshed"
    return status


def ingest(conn, export, proj, opts, replace=None, quiet=False):
    """Load exported JSON. replace=None rebuilds everything; otherwise only those modules."""
    t0 = time.time()
    files = sorted((export / "projects").glob("*.json"))
    docs = [json.loads(f.read_text()) for f in files]
    if replace is None:
        for t in ("projects", "pcs", "comps", "nodes", "edges", "unresolved", "sourcesets", "links", "declared"):
            conn.execute(f"DELETE FROM {t}")
    else:
        for d in docs:
            pj = d["project"]
            ids = [r[0] for r in conn.execute("SELECT id FROM pcs WHERE project=?", (pj,))]
            for t, col in (("nodes", "pc"), ("edges", "pc"), ("unresolved", "pc")):
                conn.executemany(f"DELETE FROM {t} WHERE {col}=?", [(i,) for i in ids])
            for t, col in (("pcs", "project"), ("projects", "path"), ("sourcesets", "project"), ("links", "project"),
                           ("declared", "project")):
                conn.execute(f"DELETE FROM {t} WHERE {col}=?", (pj,))
    comp_ids = dict(conn.execute("SELECT cid, id FROM comps"))

    def comp(cid):
        i = comp_ids.get(cid)
        if i is None:
            ga, v, k = split_cid(cid)
            i = conn.execute("INSERT INTO comps(cid, ga, version, kind) VALUES(?,?,?,?)", (cid, ga, v, k)).lastrowid
            comp_ids[cid] = i
        return i

    arts = {}
    errors = 0
    for d in docs:
        conn.execute("INSERT OR REPLACE INTO projects VALUES(?,?,?,?,?,?)",
                     (d["project"], d["name"], d.get("dir"), d.get("kind", "none"), 1 if d.get("javaLibrary") else 0,
                      json.dumps(d.get("java") or {})))
        for ss in d.get("sourceSets", []):
            conn.executemany("INSERT INTO sourcesets VALUES(?,?,?,?,?)",
                             [(d["project"], ss["name"], x, ss["cp"], "src") for x in ss["dirs"]]
                             + [(d["project"], ss["name"], x, ss["cp"], "classes") for x in ss.get("classes", [])])
        conn.executemany("INSERT INTO declared VALUES(?,?,?,?,?)",
                         [(d["project"], e["b"], e["id"], e.get("v", ""), e.get("platform", 0))
                          for e in d.get("declared", [])])
        for cfg, c in d.get("configs", {}).items():
            if "error" in c:
                errors += 1
                conn.execute("INSERT INTO pcs(project, config, error) VALUES(?,?,?)", (d["project"], cfg, c["error"]))
                continue
            root = comp(c["root"])
            pc = conn.execute("INSERT INTO pcs(project, config, root) VALUES(?,?,?)", (d["project"], cfg, root)).lastrowid
            reqs = defaultdict(set)
            direct = set()
            rows = []
            for e in c["edges"]:
                fi, ti = comp(e["f"]), comp(e["t"])
                cons = 1 if e.get("c") else 0
                rows.append((pc, fi, ti, e["q"], cons))
                rv = req_version(e["q"])
                if rv:
                    reqs[ti].add(rv)
                if fi == root and not cons:
                    direct.add(ti)
            conn.executemany("INSERT INTO edges VALUES(?,?,?,?,?)", rows)
            nodes = []
            for cid, info in c["components"].items():
                ci = comp(cid)
                nodes.append((pc, ci, 1 if ci in direct else 0, ",".join(sorted(reqs.get(ci, ()), key=vkey)),
                              "; ".join(info.get("r", []))))
            conn.executemany("INSERT INTO nodes VALUES(?,?,?,?,?)", nodes)
            conn.executemany("INSERT INTO unresolved VALUES(?,?,?,?)",
                             [(pc, comp(u["f"]), u["q"], u.get("e", "")) for u in c.get("unresolved", [])])
            for art in c.get("artifacts", []):
                ga, v, _ = split_cid(art["id"])
                arts.setdefault((ga, v), art["file"])
    conn.executemany("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)", [(g, v, f) for (g, v), f in arts.items()])
    # modules without a jar (BOMs, POM-only) still get POM metadata
    conn.execute("""INSERT OR IGNORE INTO artifacts SELECT DISTINCT ga, version, '' FROM comps
                    WHERE kind='m' AND version != ''""")
    if replace is not None:
        conn.execute("""DELETE FROM comps WHERE id NOT IN (SELECT comp FROM nodes)
                        AND id NOT IN (SELECT root FROM pcs WHERE root IS NOT NULL)""")
    conn.commit()

    if not opts.get("no_jars"):
        index_jars(conn, with_classes=not opts.get("no_classes"))
    conn.execute("DELETE FROM catalog")
    load_catalogs(conn, proj)
    set_meta(conn, "built_at", time.strftime("%Y-%m-%d %H:%M"))
    set_meta(conn, "project_dir", proj)
    set_meta(conn, "configs", opts["configs"])
    set_meta(conn, "opts", json.dumps(opts))
    conn.commit()
    track_build_files(conn, proj)
    scan_sources(conn)
    link_imports(conn)
    if not quiet or replace is None:
        n_proj = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
        n_mod = conn.execute("SELECT count(DISTINCT ga) FROM comps WHERE kind='m'").fetchone()[0]
        n_src = conn.execute("SELECT count(*) FROM srcfiles").fetchone()[0]
        print(f"depkb: indexed {n_proj} modules, {n_mod} external libraries, {n_src} source files"
              + (f", {errors} configs failed to resolve (see `depkb status`)" if errors else "")
              + f" in {time.time() - t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------- source imports -> dependencies

IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*(?:as\s+\w+)?\s*;?\s*$")
PKG_RE = re.compile(r"^\s*package\s+([\w.]+)")
JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "org.w3c.dom.", "org.xml.sax.", "org.ietf.jgss.",
                "kotlin.", "groovy.")


def parse_source(path):
    """Return (package, [(line, fqn, static, wildcard)]). Stops at the first declaration."""
    pkg, imps, in_block = None, [], False
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    with fh:
        for no, line in enumerate(fh, 1):
            s = line
            if in_block:
                if "*/" not in s:
                    continue
                in_block, s = False, s.split("*/", 1)[1]
            while "/*" in s:
                before, after = s.split("/*", 1)
                if "*/" in after:
                    s = before + after.split("*/", 1)[1]
                else:
                    s, in_block = before, True
            s = s.split("//", 1)[0]
            st = s.strip()
            if not st or st.startswith("@"):
                continue
            m = IMPORT_RE.match(s)
            if m:
                imps.append((no, m.group(2), 1 if m.group(1) else 0, 1 if m.group(3) else 0))
                continue
            m = PKG_RE.match(s)
            if m and pkg is None:
                pkg = m.group(1)
                continue
            break  # first real declaration: imports are over
    return pkg, imps


SKIP_REF_PREFIXES = ("java/", "jdk/", "sun/")  # always JDK; com/sun/ and javax/ may be libraries (JNA, javax.inject)
FQN_STRING = re.compile(r"^[a-z_][\w]*(\.[a-z_][\w]*)+\.[A-Z][\w$]*$")
SERVICE_FILES = ("spring.factories", "org.springframework.boot.autoconfigure.AutoConfiguration.imports")


def _is_resource_of_interest(path):
    p = path.replace(os.sep, "/")
    return "/META-INF/services/" in p or p.endswith(SERVICE_FILES) or (
        "/META-INF/spring/" in p and p.endswith(".imports"))


def parse_resource(path):
    """Class names referenced by ServiceLoader / Spring registration files -> (None, [(line, fqn, 0, 0, 'service')])."""
    out = []
    p = path.replace(os.sep, "/")
    if "/META-INF/services/" in p:
        out.append((0, p.rsplit("/", 1)[1], 0, 0, "service"))
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, out
    for no, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0]
        for tok in re.split(r"[=,\\\s]+", line):
            tok = tok.strip()
            if FQN_STRING.match(tok) or (tok.count(".") >= 2 and re.match(r"^[\w.$]+$", tok) and tok[0].islower()):
                out.append((no, tok.replace("$", "."), 0, 0, "service"))
    return None, out


def parse_class_file(path):
    """-> (package, rows, member_refs) where rows are (line=0, top-level fqn, 0, 0, via)."""
    try:
        info = classfile.parse(Path(path).read_bytes(), members=False)
    except (ValueError, IndexError, struct.error, OSError):
        return None, [], []
    name = info["name"] or ""
    pkg = name.rpartition("/")[0].replace("/", ".") or None
    own_prefix = name.rpartition("/")[0] + "/"
    rows = {}
    for ref, abi in info["refs"].items():
        if ref.startswith(SKIP_REF_PREFIXES) or (ref.startswith(own_prefix) and "/" not in ref[len(own_prefix):]):
            continue  # JDK and same-package classes (overwhelmingly the module itself)
        top = top_level(ref).replace("/", ".")
        rows[top] = "abi" if abi or rows.get(top) == "abi" else "class"
    for s in info["strings"]:
        if FQN_STRING.match(s):
            top = top_level(s, ".")
            rows.setdefault(top, "string")
    mrefs = [(o, n, d) for o, n, d in info["member_refs"]
             if not o.startswith(SKIP_REF_PREFIXES) and not (o.startswith(own_prefix) and "/" not in o[len(own_prefix):])]
    return pkg, [(0, f, 0, 0, v) for f, v in rows.items()], mrefs


def scan_sources(conn):
    """Incrementally index what every source set references: compiled classes (bytecode refs, ABI,
    member refs, reflective strings), source imports, and ServiceLoader/Spring registration files.
    Returns the number of files (re)parsed."""
    known = {p: (i, mt, sz) for i, p, mt, sz in conn.execute("SELECT id, path, mtime, size FROM srcfiles")}
    seen, todo = set(), []
    for project, sset, d, kind in conn.execute("SELECT project, name, dir, kind FROM sourcesets").fetchall():
        if not os.path.isdir(d):
            continue
        for dirpath, _dirs, names in os.walk(d):
            for n in names:
                p = os.path.join(dirpath, n)
                if kind == "classes":
                    if not n.endswith(".class") or n in ("module-info.class", "package-info.class"):
                        continue
                    fkind = "class"
                elif n.endswith(SRC_EXT):
                    fkind = "src"
                elif _is_resource_of_interest(p):
                    fkind = "res"
                else:
                    continue
                if p in seen:
                    continue
                seen.add(p)
                st = os.stat(p)
                k = known.get(p)
                if k and k[1] == st.st_mtime_ns and k[2] == st.st_size:
                    continue
                todo.append((p, project, sset, st.st_mtime_ns, st.st_size, fkind))
    gone = [known[p][0] for p in set(known) - seen]
    if not todo and not gone:
        return 0
    for fid in gone:
        for t, c in (("imports", "file"), ("mrefs", "file"), ("srcfiles", "id")):
            conn.execute(f"DELETE FROM {t} WHERE {c}=?", (fid,))

    def work(t):
        if t[5] == "class":
            return parse_class_file(t[0])
        if t[5] == "res":
            return parse_resource(t[0]) + ([],)
        pkg, imps = parse_source(t[0])
        return pkg, [i + ("import",) for i in imps], []

    with cf.ThreadPoolExecutor(8) as ex:
        parsed = list(ex.map(work, todo))
    pkgs_changed = bool(gone)
    for (p, project, sset, mt, sz, fkind), (pkg, rows, mrefs) in zip(todo, parsed):
        old = known.get(p)
        if old:
            fid = old[0]
            conn.execute("DELETE FROM imports WHERE file=?", (fid,))
            conn.execute("DELETE FROM mrefs WHERE file=?", (fid,))
            prev = conn.execute("SELECT package FROM srcfiles WHERE id=?", (fid,)).fetchone()
            pkgs_changed |= not prev or prev[0] != pkg
            conn.execute("UPDATE srcfiles SET project=?, sset=?, mtime=?, size=?, package=?, kind=? WHERE id=?",
                         (project, sset, mt, sz, pkg, fkind, fid))
        else:
            pkgs_changed = True
            fid = conn.execute("""INSERT INTO srcfiles(project, sset, path, mtime, size, package, kind)
                                  VALUES(?,?,?,?,?,?,?)""", (project, sset, p, mt, sz, pkg, fkind)).lastrowid
        conn.executemany("INSERT INTO imports VALUES(?,?,?,?,?,?)", [(fid, *r) for r in rows])
        conn.executemany("INSERT INTO mrefs VALUES(?,?,?,?)", [(fid, *m) for m in mrefs])
    if pkgs_changed:  # module packages moved: re-check links that depended on module ownership
        conn.execute("DELETE FROM links WHERE kind IN ('project', 'project-off-cp', 'self', 'unknown')")
    update_evidence(conn)
    conn.commit()
    link_imports(conn)
    return len(todo) + len(gone)


def update_evidence(conn):
    """Per source set: use bytecode when compiled classes exist and are not older than the sources."""
    conn.execute("DELETE FROM evidence")
    rows = conn.execute("""SELECT project, sset, kind, count(*), max(mtime) FROM srcfiles
                           WHERE kind IN ('src', 'class') GROUP BY project, sset, kind""").fetchall()
    st = defaultdict(dict)
    for pj, ss, kind, n, mx in rows:
        st[(pj, ss)][kind] = (n, mx)
    out = []
    for (pj, ss), k in st.items():
        src, cls = k.get("src"), k.get("class")
        if cls and (not src or src[1] <= cls[1] + 2_000_000_000):
            out.append((pj, ss, "class", "bytecode"))
        elif cls:
            out.append((pj, ss, "src", "imports (compiled classes are older than sources)"))
        else:
            out.append((pj, ss, "src", "imports (not compiled)"))
    conn.executemany("INSERT INTO evidence VALUES(?,?,?,?)", out)


def _split_fqn(fqn, static, wildcard):
    """-> (package, class or None)."""
    segs = fqn.split(".")
    for k, s in enumerate(segs):
        if s[:1].isupper():
            return ".".join(segs[:k]), s
    if wildcard and not static:
        return fqn, None
    return ".".join(segs[:-1]), segs[-1]


def link_imports(conn):
    """Resolve every not-yet-linked import against the classpath of its source set."""
    rows = conn.execute("""SELECT DISTINCT f.project, f.sset, i.fqn, i.static, i.wildcard
                           FROM imports i JOIN srcfiles f ON f.id=i.file
                           LEFT JOIN links l ON l.project=f.project AND l.sset=f.sset AND l.fqn=i.fqn
                                AND l.static=i.static AND l.wildcard=i.wildcard
                           WHERE l.project IS NULL""").fetchall()
    if not rows:
        return
    owners = defaultdict(set)  # package -> modules whose sources declare it
    for pkg, pj in conn.execute("SELECT DISTINCT package, project FROM srcfiles WHERE package IS NOT NULL"):
        owners[pkg].add(pj)
    groups = defaultdict(list)
    for r in rows:
        groups[(r[0], r[1])].append(r[2:])
    out = []
    memo = {}
    pk_jdk = jdk_packages()
    for (project, sset), items in groups.items():
        cp = conn.execute("SELECT cp FROM sourcesets WHERE project=? AND name=?", (project, sset)).fetchone()
        cp = cp[0] if cp else ("compileClasspath" if sset == "main" else "testCompileClasspath")
        pc = conn.execute("SELECT id FROM pcs WHERE project=? AND config=? AND error IS NULL", (project, cp)).fetchone()
        on_cp, cp_projects = set(), set()
        if pc:
            for ga, v, kind, cid in conn.execute("""SELECT c.ga, c.version, c.kind, c.cid FROM nodes n
                                                    JOIN comps c ON c.id=n.comp WHERE n.pc=?""", (pc[0],)):
                if kind == "m":
                    on_cp.add((ga, v))
                else:
                    cp_projects.add(cid[8:])
        for fqn, static, wildcard in items:
            pkg, cls = _split_fqn(fqn, static, wildcard)
            cands = memo.get((pkg, cls))
            if cands is None:
                if cls:
                    cands = conn.execute("SELECT ga, version FROM classes WHERE simple=? AND pkg=?", (cls, pkg)).fetchall()
                else:
                    cands = conn.execute("SELECT ga, version FROM packages WHERE pkg=?", (pkg,)).fetchall()
                memo[(pkg, cls)] = cands
            hit = sorted(c for c in cands if tuple(c) in on_cp)
            kind, ga, ver, owner = "unknown", None, None, None
            mods = owners.get(pkg, set())
            if project in mods:
                kind, owner = "self", project
            elif hit:
                kind, (ga, ver) = "lib", hit[0]
            elif mods:
                owner = sorted(mods)[0]
                inside = [m for m in sorted(mods) if m in cp_projects]
                kind, owner = ("project", inside[0]) if inside else ("project-off-cp", owner)
            elif cands:
                kind, (ga, ver) = "lib-off-cp", sorted(cands)[0]
            elif (pk_jdk is not None and pkg.replace(".", "/") in pk_jdk) or (pk_jdk is None and fqn.startswith(JDK_PREFIXES)):
                kind = "jdk"
            out.append((project, sset, fqn, static, wildcard, kind, ga, ver, owner))
    conn.executemany("INSERT OR REPLACE INTO links VALUES(?,?,?,?,?,?,?,?,?)", out)
    conn.commit()


def _pom_roots(conn):
    roots = []  # (path, layout) layout: 'cache' (files-2.1) or 'maven'
    guh = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle"))
    roots.append((guh / "caches" / "modules-2" / "files-2.1", "cache"))
    roots.append((Path.home() / ".m2" / "repository", "maven"))
    seen = {str(r[0]) for r in roots}
    for ga, _v, f in conn.execute("SELECT ga, version, file FROM artifacts"):
        p = Path(f)
        if "files-2.1" in p.parts:
            continue
        g = ga.split(":")[0].split(".")
        try:
            root = p.parents[2 + len(g)]
            if [x.name for x in p.parents[2:2 + len(g)]][::-1] == g and str(root) not in seen:
                seen.add(str(root))
                roots.append((root, "maven"))
        except IndexError:
            pass
    return [r for r in roots if r[0].exists()]


def find_pom(g, a, v, roots, hint=None):
    name = f"{a}-{v}.pom"
    if hint:
        hp = Path(hint)
        if (hp.parent / name).exists():
            return hp.parent / name
        for c in hp.parent.parent.glob(f"*/{name}"):
            return c
    for root, layout in roots:
        if layout == "cache":
            for c in (root / g / a / v).glob(f"*/{name}"):
                return c
        else:
            c = root.joinpath(*g.split("."), a, v, name)
            if c.exists():
                return c
    return None


def parse_pom(path):
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None
    for el in tree.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    r = tree.getroot()

    def txt(p):
        e = r.find(p)
        return " ".join(e.text.split()) if e is not None and e.text else None

    lic = [" ".join(e.text.split()) for e in r.findall("licenses/license/name") if e.text]
    par = None
    if r.find("parent") is not None:
        par = (txt("parent/groupId"), txt("parent/artifactId"), txt("parent/version"))
    props = {e.tag: (e.text or "").strip() for e in (r.find("properties") if r.find("properties") is not None else [])}
    props["project.version"] = props["version"] = txt("version") or (par[2] if par else "")
    props["project.groupId"] = txt("groupId") or (par[0] if par else "")

    def expand(v):
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), v or "")

    deps = []
    for d in r.findall("dependencies/dependency"):
        f = lambda k: (d.find(k).text or "").strip() if d.find(k) is not None and d.find(k).text else ""
        deps.append({"ga": f"{expand(f('groupId'))}:{expand(f('artifactId'))}", "version": expand(f("version")),
                     "scope": f("scope") or "compile", "optional": f("optional") == "true"})
    return {"description": txt("description"), "url": txt("url"), "licenses": lic, "parent": par, "deps": deps}


def pom_info(g, a, v, roots, hint):
    info = {"description": None, "url": None, "licenses": []}
    path = find_pom(g, a, v, roots, hint)
    depth = 0
    while path and depth < 6:
        p = parse_pom(path)
        if not p:
            break
        for k in ("description", "url"):
            info[k] = info[k] or p[k]
        if not info["licenses"]:
            info["licenses"] = p["licenses"]
        if info["licenses"] and info["description"] or not p["parent"] or None in p["parent"]:
            break
        path = find_pom(*p["parent"], roots)
        depth += 1
    return info


def scan_jar(path, with_classes):
    pk = defaultdict(int)
    cls = []
    jpms = None
    total = 0
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            services = [n[len("META-INF/services/"):] for n in names
                        if n.startswith("META-INF/services/") and not n.endswith("/")]
            for n in names:
                if not n.endswith(".class"):
                    continue
                if n.startswith("META-INF/versions/"):
                    parts = n.split("/", 3)
                    if len(parts) < 4:
                        continue
                    n = parts[3]
                elif n.startswith("META-INF/"):
                    continue
                if n.endswith(("module-info.class", "package-info.class")):
                    if n == "module-info.class":
                        jpms = jpms or "module-info"
                    continue
                d, _, base = n.rpartition("/")
                if not d:
                    continue
                pkg = d.replace("/", ".")
                if base.find("$", 1) > 0:
                    continue  # nested class
                pk[pkg] += 1
                total += 1
                if with_classes:
                    cls.append((base[:-6], pkg))
            if "META-INF/MANIFEST.MF" in names:
                mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
                m = re.search(r"^Automatic-Module-Name:\s*(\S+)", mf, re.M)
                if m:
                    jpms = m.group(1)
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return dict(pk), sorted(set(cls)), jpms, total, services


def index_jars(conn, with_classes=True):
    todo = conn.execute("""SELECT a.ga, a.version, a.file FROM artifacts a
                           LEFT JOIN pkginfo p ON p.ga=a.ga AND p.version=a.version
                           WHERE p.ga IS NULL""").fetchall()
    if not todo:
        return
    roots = _pom_roots(conn)

    def work(row):
        ga, v, f = row
        g, a = ga.split(":", 1)
        jar = scan_jar(f, with_classes) if f.endswith(".jar") else None
        return ga, v, jar, pom_info(g, a, v, roots, f)

    with cf.ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as ex:
        for ga, v, jar, pom in ex.map(work, todo):
            pk, cls, jpms, total, services = jar if jar else ({}, [], None, 0, [])
            conn.execute("INSERT OR REPLACE INTO pkginfo VALUES(?,?,?,?,?,?,?)",
                         (ga, v, pom["description"], pom["url"], "; ".join(pom["licenses"]), jpms, total))
            conn.execute("DELETE FROM packages WHERE ga=? AND version=?", (ga, v))
            conn.execute("DELETE FROM classes WHERE ga=? AND version=?", (ga, v))
            conn.execute("DELETE FROM services WHERE ga=? AND version=?", (ga, v))
            conn.executemany("INSERT INTO services VALUES(?,?,?)", [(ga, v, x) for x in services])
            conn.executemany("INSERT INTO packages VALUES(?,?,?,?)", [(p, ga, v, n) for p, n in pk.items()])
            conn.executemany("INSERT INTO classes VALUES(?,?,?,?)", [(s, p, ga, v) for s, p in cls])
    conn.commit()


def load_catalogs(conn, proj):
    if tomllib is None:
        return
    for toml in sorted((proj / "gradle").glob("*.versions.toml")):
        try:
            data = tomllib.loads(toml.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            continue
        name = toml.name.split(".")[0]
        versions = data.get("versions", {})

        def ver(spec):
            if spec is None:
                return None, None
            if isinstance(spec, str):
                return spec, None
            if "ref" in spec:
                v, _ = ver(versions.get(spec["ref"]))
                return v, spec["ref"]
            for k in ("strictly", "require", "prefer"):
                if k in spec:
                    return (spec[k] + ("!!" if k == "strictly" else "")), None
            return None, None

        def acc(alias, prefix=""):
            return f"{name}.{prefix}" + re.sub(r"[-_.]", ".", alias)

        rows = []
        for alias, spec in data.get("libraries", {}).items():
            if isinstance(spec, str):
                p = spec.split(":")
                ga, v, ref = f"{p[0]}:{p[1]}", (p[2] if len(p) > 2 else None), None
            else:
                ga = spec.get("module") or f"{spec.get('group')}:{spec.get('name')}"
                v, ref = ver(spec.get("version"))
            rows.append((acc(alias), "library", ga, v, ref, toml.name))
        for alias, spec in data.get("plugins", {}).items():
            if isinstance(spec, str):
                pid, _, v = spec.partition(":")
                ref = None
            else:
                pid = spec.get("id")
                v, ref = ver(spec.get("version"))
            rows.append((acc(alias, "plugins."), "plugin", pid, v or None, ref, toml.name))
        for alias, libs in data.get("bundles", {}).items():
            rows.append((acc(alias, "bundles."), "bundle", ",".join(libs), None, None, toml.name))
        conn.executemany("INSERT INTO catalog VALUES(?,?,?,?,?,?)", rows)


# ---------------------------------------------------------------- lookups

def find_gas(conn, q):
    """Resolve a user query to module group:artifact names, best matches first."""
    q = q.strip()
    p = q.split(":")
    if len(p) >= 2:
        ga = f"{p[0]}:{p[1]}"
        if conn.execute("SELECT 1 FROM comps WHERE ga=? AND kind='m'", (ga,)).fetchone():
            return [ga]
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT ga FROM comps WHERE kind='m' AND (ga=? OR ga LIKE ? OR ga LIKE ?) ORDER BY ga",
        (q, f"%:{q}", f"%{q}%"))]
    exact = [g for g in rows if g == q or g.endswith(":" + q)]
    # an exact artifact name wins outright: "lib1" must not also list lib10, lib101...
    return exact if exact else rows


def one_ga(conn, q, limit):
    gas = find_gas(conn, q)
    if not gas:
        die(f"no module matching '{q}' in the resolved graphs")
    exact = [g for g in gas if g == q or g.endswith(":" + q) or g == ":".join(q.split(":")[:2])]
    if len(exact) == 1:
        return exact[0]
    if len(gas) == 1:
        return gas[0]
    print(f"'{q}' is ambiguous; matches:")
    emit(("  " + g for g in gas), limit)
    sys.exit(1)


def norm_project(conn, q):
    if q is None:
        return None
    cands = [q, ":" + q.lstrip(":")]
    for c in cands:
        if conn.execute("SELECT 1 FROM projects WHERE path=?", (c,)).fetchone():
            return c
    rows = [r[0] for r in conn.execute("SELECT path FROM projects WHERE name=? OR path LIKE ?", (q, f"%{q}%"))]
    if len(rows) == 1:
        return rows[0]
    die(f"project '{q}' not found" + (f"; candidates: {', '.join(rows[:10])}" if rows else ""))


def pc_rows(conn, project=None, config=None):
    sql = "SELECT id, project, config, root FROM pcs WHERE error IS NULL"
    args = []
    if project:
        sql += " AND project=?"
        args.append(project)
    if config:
        sql += " AND config=?"
        args.append(config)
    return conn.execute(sql + " ORDER BY project, config", args).fetchall()


# ---------------------------------------------------------------- commands

def cmd_summary(a, conn):
    q = lambda s, *p: conn.execute(s, p).fetchone()[0]
    n_proj = q("SELECT count(*) FROM projects")
    n_err = q("SELECT count(*) FROM pcs WHERE error IS NOT NULL")
    n_ga = q("SELECT count(DISTINCT ga) FROM comps WHERE kind='m'")
    n_gav = q("SELECT count(*) FROM comps WHERE kind='m'")
    multi = q("SELECT count(*) FROM (SELECT ga FROM comps WHERE kind='m' GROUP BY ga HAVING count(*)>1)")
    confl = q("SELECT count(DISTINCT c.ga) FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.reasons LIKE '%conflict%'")
    unres = q("SELECT count(DISTINCT req) FROM unresolved")
    n_pkg = q("SELECT count(DISTINCT pkg) FROM packages")
    n_cat = q("SELECT count(*) FROM catalog")
    print(f"KB built {meta(conn, 'built_at')} | {n_proj} projects | configs: {meta(conn, 'configs')}")
    print(f"external modules: {n_ga} ({n_gav} module-versions), {multi} with >1 version in use")
    print(f"conflict-resolved modules: {confl} | unresolved requests: {unres} | failed configs: {n_err}")
    print(f"indexed packages: {n_pkg} | version catalog entries: {n_cat}")
    top = conn.execute("""SELECT c.ga, count(DISTINCT p.project) n FROM nodes n JOIN comps c ON c.id=n.comp
                          JOIN pcs p ON p.id=n.pc WHERE c.kind='m' GROUP BY c.ga ORDER BY n DESC LIMIT 5""").fetchall()
    if top:
        print("most used: " + ", ".join(f"{g} ({n})" for g, n in top))


def cmd_status(a, conn):
    proj = Path(meta(conn, "project_dir", "."))
    changed = changed_modules(conn) if proj.exists() else set()
    state = "fresh" if not changed else ("STALE: " + ", ".join(sorted(changed)[:8]) + " changed (any query refreshes it)")
    n_src = conn.execute("SELECT count(*) FROM srcfiles").fetchone()[0]
    print(f"built {meta(conn, 'built_at')} from {proj} — {state} | {n_src} source files indexed")
    errs = conn.execute("SELECT project, config, error FROM pcs WHERE error IS NOT NULL").fetchall()
    emit((f"  failed {p} [{ab(c)}]: {e.splitlines()[0][:160]}" for p, c, e in errs), a.limit)


# ---------------------------------------------------------------- usage: declared / transitive / used

MAIN_CFGS = ("compileClasspath", "runtimeClasspath")
TEST_CFGS = ("testCompileClasspath", "testRuntimeClasspath")
CAVEAT = ("note: bytecode/imports cannot see reflection by computed name, or classes only "
          "needed at runtime; verify UNUSED? before removing")
COMPILE_VIA = ("import", "class", "abi")
MAIN_BUCKETS = {"api": "api", "compileOnlyApi": "api", "implementation": "compile", "compileOnly": "compile",
                "runtimeOnly": "runtime"}
BUCKET_SUFFIX = {"Api": "api", "CompileOnlyApi": "api", "Implementation": "compile", "CompileOnly": "compile",
                 "RuntimeOnly": "runtime"}
EVID_JOIN = "JOIN evidence ev ON ev.project=f.project AND ev.sset=f.sset AND (f.kind=ev.kind OR f.kind='res')"


def _cid(conn, i):
    return conn.execute("SELECT cid FROM comps WHERE id=?", (i,)).fetchone()[0]


def override_note(conn, pc, comp_id, version, reqs, reasons):
    """'overridden 1.0.0->2.0.0 (conflict resolution; via acme-http:3.1.0)' or ''."""
    others = [r for r in (reqs or "").split(",") if r and r != version]
    kinds = sorted({x.split(":")[0] for x in (reasons or "").split("; ") if x})
    if not others and not kinds:
        return ""
    via = [short(_cid(conn, f)) for f, q, c in
           conn.execute("SELECT f, req, cons FROM edges WHERE pc=? AND t=?", (pc, comp_id))
           if c or req_version(q) == version]
    s = (f"overridden {','.join(others)}->{version}" if others else "selected") + f" ({', '.join(kinds) or 'requested'}"
    return s + (f"; via {', '.join(via[:2])}{' +' + str(len(via) - 2) if len(via) > 2 else ''}" if via else "") + ")"


def _node(conn, project, config, ga):
    return conn.execute("""SELECT p.id, c.id, c.version, n.direct, n.reqs, n.reasons FROM pcs p
                           JOIN nodes n ON n.pc=p.id JOIN comps c ON c.id=n.comp
                           WHERE p.project=? AND p.config=? AND c.ga=?""", (project, config, ga)).fetchone()


class Use:
    __slots__ = ("files", "compile", "abi", "offcp", "vias", "version", "detail")

    def __init__(self):
        self.files, self.vias, self.detail = set(), set(), []
        self.compile = self.abi = self.offcp = False
        self.version = None


def use_rows(conn, where, args):
    return conn.execute(f"""SELECT f.project, f.sset, f.path, f.kind, i.line, i.fqn, i.via, l.kind, l.ga, l.version, l.owner
                            FROM links l JOIN srcfiles f ON f.project=l.project AND f.sset=l.sset {EVID_JOIN}
                            JOIN imports i ON i.file=f.id AND i.fqn=l.fqn AND i.static=l.static AND i.wildcard=l.wildcard
                            WHERE {where} ORDER BY f.path, i.line""", args).fetchall()


def aggregate(rows):
    """-> ({(project, sset, key): Use}, {(project, sset): unknown file count})"""
    agg, unknown = defaultdict(Use), defaultdict(set)
    for pj, ss, path, _fk, line, fqn, via, kind, ga, ver, owner in rows:
        if kind in ("jdk", "self"):
            continue
        if kind == "unknown":
            if via in ("import", "class", "abi"):
                unknown[(pj, ss)].add((path, line, fqn))
            continue
        u = agg[(pj, ss, ga if kind.startswith("lib") else f"project {owner}")]
        u.files.add(path)
        u.vias.add(via)
        u.version = ver
        u.detail.append((path, line, fqn, via))
        if via in COMPILE_VIA:
            u.compile = True
            u.abi |= via == "abi"
            u.offcp |= kind.endswith("off-cp")
    return agg, unknown


def bucket_scope(bucket, ssets):
    """'testImplementation' -> ('test', 'compile'); 'api' -> ('main', 'api')."""
    if bucket in MAIN_BUCKETS:
        return "main", MAIN_BUCKETS[bucket]
    for ss in sorted(ssets, key=len, reverse=True):
        if ss != "main" and bucket.startswith(ss):
            k = BUCKET_SUFFIX.get(bucket[len(ss):])
            if k:
                return ss, k
    return None, None


class Analysis:
    """Everything known about how one module declares and uses its dependencies."""

    def __init__(self, conn, project, key=None):
        self.project = project
        self.ssets = [r[0] for r in conn.execute("SELECT DISTINCT name FROM sourcesets WHERE project=?", (project,))]
        self.evidence = {ss: (k, note) for ss, k, note in
                         conn.execute("SELECT sset, kind, note FROM evidence WHERE project=?", (project,))}
        self.javalib = bool((conn.execute("SELECT javalib FROM projects WHERE path=?", (project,)).fetchone() or [0])[0])
        self.decl = defaultdict(list)  # key -> [(bucket, sset, kind, version, platform)]
        q = "SELECT bucket, id, version, platform FROM declared WHERE project=?" + (" AND id=?" if key else "")
        for b, i, v, plat in conn.execute(q, (project, key) if key else (project,)):
            ss, bk = bucket_scope(b, self.ssets)
            self.decl[i].append((b, ss, bk, v, plat))
        if key:
            where = ("l.project=? AND l.owner=? AND l.kind LIKE 'project%'" if key.startswith("project ")
                     else "l.project=? AND l.ga=?")
            args = (project, key[8:] if key.startswith("project ") else key)
        else:
            where, args = "l.project=?", (project,)
        uses, self.unknown = aggregate(use_rows(conn, where, args))
        self.uses = defaultdict(dict)  # key -> {sset: Use}
        for (_pj, ss, k), u in uses.items():
            self.uses[k][ss] = u
        self.cp = {}  # sset -> keys on its compile classpath
        for ss in self.ssets:
            cpn = conn.execute("SELECT cp FROM sourcesets WHERE project=? AND name=?", (project, ss)).fetchone()[0]
            self.cp[ss] = {cid if k == "p" else ga for cid, ga, k in conn.execute(
                """SELECT c.cid, c.ga, c.kind FROM pcs p JOIN nodes n ON n.pc=p.id JOIN comps c ON c.id=n.comp
                   WHERE p.project=? AND p.config=?""", (project, cpn))}
        self._has_classes = {}
        self._conn = conn

    def bytecode(self, ss="main"):
        return self.evidence.get(ss, ("src",))[0] == "class"

    def has_classes(self, key):
        if key.startswith("project "):
            return True
        if key not in self._has_classes:
            self._has_classes[key] = self._conn.execute(
                "SELECT 1 FROM packages WHERE ga=? LIMIT 1", (key,)).fetchone() is not None
        return self._has_classes[key]

    def services(self, key):
        return [r[0] for r in self._conn.execute("SELECT DISTINCT iface FROM services WHERE ga=?", (key,))]

    def declared_for(self, key, ss):
        return any(bk in ("compile", "api") and (dss == ss or dss == "main") for _b, dss, bk, _v, _p in self.decl.get(key, ()))

    def findings(self, key):
        """-> [(type, sset, text)] for one dependency key."""
        out = []
        for ss, u in sorted(self.uses.get(key, {}).items()):
            if not u.compile:
                continue
            if u.offcp or key not in self.cp.get(ss, ()):
                out.append(("NOT_ON_CP", ss, "used but not on the compile classpath (won't compile)"))
            elif not self.declared_for(key, ss):
                out.append(("UNDECLARED", ss, "used but only transitive: declare it"))
        for b, dss, bk, _v, plat in self.decl.get(key, ()):
            if bk not in ("compile", "api") or plat or dss is None or not self.has_classes(key):
                continue
            if not any(u for s, u in self.uses.get(key, {}).items() if s == dss):
                svc = self.services(key)
                out.append(("UNUSED", dss, f"declared in {b}, never referenced"
                            + (f"; provides {', '.join(svc[:2])} -> runtimeOnly?" if svc else "")))
        if self.javalib and self.bytecode("main") and key in self.decl:
            kinds = {bk for _b, dss, bk, _v, _p in self.decl[key] if dss == "main"}
            u = self.uses.get(key, {}).get("main")
            if u and u.abi and "api" not in kinds and "compile" in kinds:
                out.append(("API", "main", "in public API (signatures/supertypes): should be api"))
            elif u and u.compile and not u.abi and "api" in kinds:
                out.append(("IMPL", "main", "not in public API: could be implementation"))
        return out

    def label(self, key):
        b = sorted({d[0] for d in self.decl.get(key, ())})
        if b:
            return ",".join(b)
        return None


def _rel(conn, project, path):
    d = conn.execute("SELECT dir FROM projects WHERE path=?", (project,)).fetchone()
    try:
        return str(Path(path).relative_to(d[0])) if d and d[0] else path
    except ValueError:
        return path


def _uses_summary(an, key):
    us = an.uses.get(key, {})
    if not us:
        return "not used"
    parts = []
    for ss, u in sorted(us.items()):
        tag = "" if u.compile else "(" + "/".join(sorted(u.vias)) + ")"
        parts.append(f"{ss}:{len(u.files)}f{tag}")
    return "used " + " ".join(parts)


def _print_detail(conn, project, by_sset, limit, indent="    "):
    rows = [(ss, p, ln, fqn, via) for ss, u in sorted(by_sset.items()) for p, ln, fqn, via in u.detail]
    emit((f"{indent}{_rel(conn, project, p)}{':' + str(ln) if ln else ''} {fqn}"
          + ("" if via in ("import", "class") else f" [{via}]") for ss, p, ln, fqn, via in rows), limit)


def _evidence_note(conn, projects):
    rows = conn.execute(f"""SELECT note, count(*) FROM evidence WHERE sset='main'
                            AND project IN ({','.join('?' * len(projects))}) GROUP BY note""", list(projects)).fetchall()
    return "; ".join(f"{n}: {plural(c, 'module')}" for n, c in rows)


def cmd_usage(a, conn):
    """Per module: how a library (or module) is declared, resolved, overridden and used by code."""
    if a.module.startswith(":"):
        key = "project " + norm_project(conn, a.module)
        ga = None
    else:
        ga = one_ga(conn, a.module, a.limit)
        key = ga
    projects = sorted({r[0] for r in conn.execute(
        """SELECT p.project FROM nodes n JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc
           WHERE c.ga=? OR c.cid=?""", (key, key))}
        | {r[0] for r in conn.execute("SELECT project FROM declared WHERE id=?", (key,))}
        | {r[0] for r in conn.execute("SELECT project FROM links WHERE ga=? OR 'project '||owner=?", (key, key))})
    kinds = dict(conn.execute("SELECT path, kind FROM projects"))
    lines, detail, flagged = [], [], False
    for pj in projects:
        if "project " + pj == key:
            continue
        an = Analysis(conn, pj, key)
        rt = None
        if ga:
            main = [_node(conn, pj, c, ga) for c in MAIN_CFGS]
            test = [_node(conn, pj, c, ga) for c in TEST_CFGS]
            rt = main[1] or main[0] or test[1] or test[0]
            on_compile, on_runtime, on_test = bool(main[0]), bool(main[1]), any(test)
        else:
            cfgs = {c for (c,) in conn.execute("""SELECT p.config FROM nodes n JOIN comps c ON c.id=n.comp
                                                 JOIN pcs p ON p.id=n.pc WHERE p.project=? AND c.cid=?""", (pj, key))}
            on_compile, on_runtime = "compileClasspath" in cfgs, "runtimeClasspath" in cfgs
            on_test = bool(cfgs & set(TEST_CFGS))
        decl = an.label(key) or ("transitive" if on_compile else "transitive(runtime only)" if on_runtime
                                 else "transitive(test)" if on_test else "not on classpath")
        s = f"{pj}{'(app)' if kinds.get(pj) == 'app' else ''}  {rt[2] + '  ' if rt else ''}{decl}  {_uses_summary(an, key)}"
        if rt:
            note = override_note(conn, rt[0], rt[1], rt[2], rt[4], rt[5])
            if note:
                s += f"  | {note}"
        f = an.findings(key)
        if f:
            flagged = True
            s += "  ! " + "; ".join(f"{t}{'' if ss == 'main' else '(' + ss + ')'}: {txt}" for t, ss, txt in f)
        lines.append(s)
        if a.files and an.uses.get(key):
            detail.append((pj, an.uses[key]))
    print(f"{key} — {plural(len(lines), 'module')}" + (f"  [{_evidence_note(conn, projects)}]" if projects else ""))
    emit(lines, a.limit)
    for pj, by in detail:
        print(f"  {pj}:")
        _print_detail(conn, pj, by, a.limit)
    if flagged:
        print(CAVEAT)


def cmd_runtime(a, conn):
    """What actually runs: the library as seen on each deployable (app) module's runtimeClasspath."""
    ga = one_ga(conn, a.module, a.limit)
    apps = [r[0] for r in conn.execute("SELECT path FROM projects WHERE kind='app' ORDER BY path")]
    if a.project:
        apps = [norm_project(conn, a.project)]
    note = ""
    if not apps:
        apps = [r[0] for r in conn.execute("SELECT path FROM projects ORDER BY path")]
        note = " (no application/war/boot modules detected; showing all modules)"
    print(f"{ga} at runtime{note}:")
    shown = 0
    for app in apps:
        n = _node(conn, app, "runtimeClasspath", ga)
        pc = conn.execute("SELECT id FROM pcs WHERE project=? AND config='runtimeClasspath'", (app,)).fetchone()
        if not n:
            if a.project:
                print(f"{app}: not on runtimeClasspath")
            continue
        shown += 1
        closure = {app} | {cid[8:] for (cid,) in conn.execute(
            "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.kind='p'", (pc[0],))}
        print(f"{app} runs {n[2]}" + (f" — {override_note(conn, n[0], n[1], n[2], n[4], n[5])}" if (n[4] or n[5]) else ""))
        others = [r for r in (n[4] or "").split(",") if r and r != n[2]]
        art = conn.execute("SELECT file FROM artifacts WHERE ga=? AND version=?", (ga, n[2])).fetchone()
        if others and art and art[0].endswith(".jar"):
            res = check_callers(conn, pc[0], ga, n[2], art[0])
            bad = [c for c, _v, p in res if p and not p[0].startswith("not compiled")]
            print("  binary check: " + (f"BREAKS {', '.join(bad)} (details: depkb compat {ga.split(':')[1]})" if bad
                                        else f"{plural(len(res), 'caller')} link fine"))
        decl = [f"{pj} ({b} {v})".replace(" )", ")") for pj, b, v in conn.execute(
            f"""SELECT project, bucket, version FROM declared WHERE id=? AND project IN ({','.join('?' * len(closure))})
                ORDER BY project""", [ga, *sorted(closure)])]
        agg, _ = aggregate(use_rows(conn, "l.ga=? AND f.sset='main'", (ga,)))
        users = {pj: u for (pj, _ss, _k), u in agg.items() if pj in closure}
        print("  declared by: " + (", ".join(decl) if decl else "nobody (purely transitive)"))
        print("  used by: " + (", ".join(f"{m} ({plural(len(u.files), 'file')})" for m, u in sorted(users.items()))
                               if users else "no main code references it"))
        if a.files:
            for m, u in sorted(users.items()):
                _print_detail(conn, m, {"main": u}, a.limit)
    if not shown:
        print("  not on the runtime classpath of any " + ("app " if not note else "") + "module")


def cmd_imports(a, conn):
    """What a module's code uses, grouped by dependency, with declared status and findings."""
    pj = norm_project(conn, a.project)
    an = Analysis(conn, pj)
    print(f"{pj} uses (" + ", ".join(f"{ss}: {an.evidence[ss][1]}" for ss in sorted(an.evidence)) + ")")
    by_sset = defaultdict(list)
    for key, us in an.uses.items():
        f = {(t, ss): txt for t, ss, txt in an.findings(key)}
        for ss, u in us.items():
            label = an.label(key) or ("transitive" if key in an.cp.get(ss, ()) else "not declared")
            flags = [t for (t, fss) in f if fss == ss]
            ver = f":{u.version}" if u.version else ""
            how = "" if u.compile else " (" + "/".join(sorted(u.vias)) + " only)"
            by_sset[ss].append(f"  {key}{ver}  {plural(len(u.files), 'file')}{how}  {label}"
                               + (f"  ! {', '.join(flags)}" if flags else ""))
    for ss in sorted(by_sset):
        print(f"{ss}:")
        emit(sorted(by_sset[ss]), a.limit)
    unused = [(key, ss, txt) for key in an.decl for t, ss, txt in an.findings(key) if t == "UNUSED"]
    if unused:
        print("declared but never referenced (UNUSED?):")
        emit((f"  {k} [{ss}] {txt}" for k, ss, txt in unused), a.limit)
    n_unknown = sum(len(v) for v in an.unknown.values())
    if n_unknown:
        print(f"unresolved references: {n_unknown}" + ("" if a.unknown else " (--unknown to list)"))
        if a.unknown:
            emit((f"  {_rel(conn, pj, p)}{':' + str(ln) if ln else ''} {fqn}"
                  for v in an.unknown.values() for p, ln, fqn in sorted(v)), a.limit)
    if unused:
        print(CAVEAT)


def cmd_users(a, conn):
    """Code that references a class or package (FQN, simple class name, or package prefix)."""
    q = a.name.strip().rstrip(".*")
    if "." in q:
        cond, args = "(i.fqn=? OR i.fqn GLOB ?)", [q, q + ".*"]
    else:  # simple class name; GLOB is case-sensitive, unlike LIKE
        cond, args = "(i.fqn GLOB ? OR i.fqn GLOB ?)", [f"*.{q}", f"*.{q}.*"]
    if a.project:
        cond += " AND f.project=?"
        args.append(norm_project(conn, a.project))
    rows = conn.execute(f"""SELECT f.project, f.sset, f.path, i.line, i.fqn, i.via, l.kind, l.ga, l.owner FROM imports i
                            JOIN srcfiles f ON f.id=i.file {EVID_JOIN}
                            LEFT JOIN links l ON l.project=f.project AND l.sset=f.sset
                            AND l.fqn=i.fqn AND l.static=i.static AND l.wildcard=i.wildcard
                            WHERE {cond} ORDER BY f.project, f.path""", args).fetchall()
    src = {}
    for *_, kind, ga, owner in rows:
        src[ga or (f"project {owner}" if owner else kind or "?")] = 1
    by_proj = defaultdict(list)
    for pj, ss, path, line, fqn, via, *_ in rows:
        by_proj[pj].append((ss, path, line, fqn, via))
    print(f"'{a.name}': referenced from {len({r[2] for r in rows})} files in {plural(len(by_proj), 'module')}"
          + (f"; provided by {', '.join(src)}" if src else ""))
    lines = []
    for pj, items in by_proj.items():
        if a.files:
            lines += [f"{pj} {_rel(conn, pj, p)}{':' + str(ln) if ln else ''} {fqn}"
                      + ("" if via in ("import", "class") else f" [{via}]") for ss, p, ln, fqn, via in items]
        else:
            per = defaultdict(set)
            for ss, p, *_ in items:
                per[ss].add(p)
            lines.append(f"{pj}: " + " ".join(f"{s}:{len(n)}f" for s, n in sorted(per.items())))
    emit(lines, a.limit)
    if not a.files and lines:
        print("(--files for file:line)")


FINDING_TITLES = (("UNDECLARED", "UNDECLARED (used, only transitive)"),
                  ("NOT_ON_CP", "NOT ON COMPILE CLASSPATH (will not compile)"),
                  ("UNUSED", "UNUSED? (declared, never referenced)"),
                  ("API", "SHOULD BE api (exposed in public API)"),
                  ("IMPL", "COULD BE implementation (not in public API)"))


def cmd_audit(a, conn):
    """Build hygiene across modules: undeclared, unused, broken, and api/implementation advice."""
    only = norm_project(conn, a.project) if a.project else None
    projects = [only] if only else [r[0] for r in conn.execute("SELECT path FROM projects ORDER BY path")]
    found = defaultdict(list)
    unknown = defaultdict(int)
    for pj in projects:
        an = Analysis(conn, pj)
        for key in set(an.uses) | set(an.decl):
            for t, ss, txt in an.findings(key):
                n = len(an.uses.get(key, {}).get(ss, Use()).files)
                found[t].append(f"  {pj}{'' if ss == 'main' else ' [' + ss + ']'} -> {key}"
                                + (f" ({plural(n, 'file')})" if n else "") + ("" if t in ("UNDECLARED", "NOT_ON_CP") else f": {txt}"))
        for v in an.unknown.values():
            unknown[pj] += len({p for p, _, _ in v})
    print(f"evidence: {_evidence_note(conn, projects)}")
    for t, title in FINDING_TITLES:
        items = sorted(found.get(t, []))
        print(f"{title}: {len(items)}")
        emit(items, a.limit)
    if unknown:
        print(f"unresolved references (generated code / missing deps): {sum(unknown.values())} files in "
              + ", ".join(f"{p}({n})" for p, n in sorted(unknown.items())[:5]) + " — `depkb imports <module> --unknown`")
    print(CAVEAT)


def cmd_modules(a, conn):
    """The module graph: kinds, module-to-module dependencies and dependents."""
    edges = defaultdict(set)    # module -> modules it depends on directly (main configs)
    for pj, cid in conn.execute("""SELECT DISTINCT p.project, c.cid FROM pcs p JOIN edges e ON e.pc=p.id AND e.f=p.root
                                   JOIN comps c ON c.id=e.t WHERE c.kind='p' AND p.config IN (?,?)""", MAIN_CFGS):
        edges[pj].add(cid[8:])
    rev = defaultdict(set)
    for m, ds in edges.items():
        for d in ds:
            rev[d].add(m)
    kinds = dict(conn.execute("SELECT path, kind FROM projects"))

    def closure(start, g):
        seen, dq = set(), deque([start])
        while dq:
            for n in g.get(dq.popleft(), ()):
                if n not in seen:
                    seen.add(n)
                    dq.append(n)
        return seen

    if a.project:
        m = norm_project(conn, a.project)
        print(f"{m} [{kinds.get(m)}]")
        print("depends on: " + (", ".join(sorted(edges[m])) or "-"))
        print("used by: " + (", ".join(sorted(rev[m])) or "-"))
        allrev = closure(m, rev)
        apps = sorted(x for x in allrev if kinds.get(x) == "app")
        print(f"affected by a change here: {len(allrev)} modules" + (f", apps: {', '.join(apps)}" if apps else ""))
        return
    lines = [f"{m} [{kinds[m]}] -> {', '.join(sorted(edges[m])) or '-'}  (used by {len(rev[m])})"
             for m in sorted(kinds) if not (m == ":" and kinds[m] == "none" and not edges[m])]
    print(f"{len(kinds)} modules, {sum(1 for k in kinds.values() if k == 'app')} apps")
    emit(lines, a.limit)


def cmd_version(a, conn):
    gas = find_gas(conn, a.module)
    if not gas:
        die(f"no module matching '{a.module}'")
    lines = []
    for ga in gas:
        vs = conn.execute("""SELECT c.version, count(DISTINCT p.project) FROM comps c JOIN nodes n ON n.comp=c.id
                             JOIN pcs p ON p.id=n.pc WHERE c.ga=? GROUP BY c.version""", (ga,)).fetchall()
        vs.sort(key=lambda r: vkey(r[0]), reverse=True)
        cat = conn.execute("SELECT accessor, version FROM catalog WHERE ga=? AND kind='library'", (ga,)).fetchall()
        s = f"{ga}: " + ", ".join(f"{v} ({n} proj)" for v, n in vs)
        if len(vs) > 1:
            s += "  <- multiple versions"
        if cat:
            s += "  | catalog " + ", ".join(f"{acc}={v}" for acc, v in cat)
        lines.append(s)
    emit(lines, a.limit)


def cmd_who_uses(a, conn):
    gas = find_gas(conn, a.module)
    if not gas:
        die(f"no module matching '{a.module}'")
    if len(gas) > 1 and not a.module.count(":"):
        gas = [g for g in gas if g.endswith(":" + a.module)] or gas
    used_abbr = False
    for ga in gas[:5]:
        rows = conn.execute("""SELECT p.project, p.config, c.version, n.direct FROM nodes n
                               JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc
                               WHERE c.ga=?""" + (" AND p.config=?" if a.config else ""),
                            (ga, a.config) if a.config else (ga,)).fetchall()
        agg = defaultdict(list)
        for proj, cfg, v, d in rows:
            agg[(proj, v, "direct" if d else "transitive")].append(ab(cfg))
        print(f"{ga} — {len({r[0] for r in rows})} proj")
        lines = [f"  {p}  {v}  {kind}  [{' '.join(sorted(c))}]" for (p, v, kind), c in sorted(agg.items())]
        emit(lines, a.limit)
        used_abbr = True
    if len(gas) > 5:
        print(f"(+{len(gas) - 5} more matching modules; be more specific)")
    if used_abbr:
        print(LEGEND)


def _paths(edges, root, target, max_paths):
    """Shortest paths root->target over edges {from: [to]} (dependency edges only)."""
    dist = {root: 0}
    preds = defaultdict(list)
    dq = deque([root])
    while dq:
        u = dq.popleft()
        if u == target:
            break
        for w in edges.get(u, ()):
            if w not in dist:
                dist[w] = dist[u] + 1
                preds[w].append(u)
                dq.append(w)
            elif dist[w] == dist[u] + 1:
                preds[w].append(u)
    if target not in dist:
        return []
    out = []

    def back(node, acc):
        if len(out) >= max_paths:
            return
        if node == root:
            out.append([root] + acc[::-1])
            return
        for p in preds[node]:
            back(p, acc + [node])

    back(target, [])
    return out


def cmd_why(a, conn):
    ga = one_ga(conn, a.module, a.limit)
    project = norm_project(conn, a.project)
    cands = conn.execute("""SELECT p.id, p.project, p.config, p.root, c.id, c.version, n.reasons, n.direct
                            FROM nodes n JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc
                            WHERE c.ga=?""" + (" AND p.project=?" if project else "")
                         + (" AND p.config=?" if a.config else ""),
                         tuple(x for x in (ga, project, a.config) if x)).fetchall()
    if not cands:
        die(f"{ga} is not in " + (f"{project}" if project else "any graph") + (f" [{a.config}]" if a.config else ""))
    pref = {"runtimeClasspath": 0, "compileClasspath": 1, "testRuntimeClasspath": 2}
    cands.sort(key=lambda r: (pref.get(r[2], 9), r[1]))
    pc, proj, cfg, root, tid, ver, reasons, direct = cands[0]
    cid = lambda i: conn.execute("SELECT cid FROM comps WHERE id=?", (i,)).fetchone()[0]
    print(f"{ga} in {proj} [{cfg}] = {ver}" + ("  (direct)" if direct else ""))
    if reasons:
        print(f"reason: {reasons}")
    inc = conn.execute("SELECT f, req, cons FROM edges WHERE pc=? AND t=?", (pc, tid)).fetchall()
    print("requested by:")
    emit((f"  {cid(f)} -> {req_version(q) or '(no version)'}{'  [constraint]' if c else ''}" for f, q, c in inc), a.limit)
    adj = defaultdict(list)
    for f, t in conn.execute("SELECT f, t FROM edges WHERE pc=? AND cons=0", (pc,)):
        adj[f].append(t)
    paths = _paths(adj, root, tid, a.paths)
    if paths:
        print("shortest paths:")
        for p in paths:
            print("  " + " > ".join(short(cid(x)) for x in p))
    others = defaultdict(set)
    for _, pj, cg, _, _, v, _, _ in cands[1:]:
        if pj != proj or v != ver:
            others[v].add(pj if pj != proj else f"{pj}[{ab(cg)}]")
    if others:
        print("elsewhere: " + "; ".join(f"{v} in {', '.join(sorted(ps)[:3])}{' ...' if len(ps) > 3 else ''}"
                                        for v, ps in sorted(others.items(), key=lambda kv: vkey(kv[0]), reverse=True))
              + " (use --project/--config)")


def cmd_deps(a, conn):
    project = norm_project(conn, a.project)
    cfg = a.config or "runtimeClasspath"
    rows = pc_rows(conn, project, cfg)
    if not rows:
        err = conn.execute("SELECT error FROM pcs WHERE project=? AND config=?", (project, cfg)).fetchone()
        die(f"{project} has no {cfg}" + (f": {err[0][:200]}" if err and err[0] else ""))
    pc = rows[0][0]
    nodes = conn.execute("""SELECT c.cid, c.version, n.direct, n.reqs, n.reasons FROM nodes n
                            JOIN comps c ON c.id=n.comp WHERE n.pc=? ORDER BY c.cid""", (pc,)).fetchall()
    unres = conn.execute("SELECT DISTINCT req FROM unresolved WHERE pc=?", (pc,)).fetchall()
    d = [n for n in nodes if n[2]]
    print(f"{project} [{cfg}]: {len(d)} direct, {len(nodes)} total" + (f", {len(unres)} unresolved" if unres else ""))

    def fmt(cid, v, reqs, reasons):
        s = cid
        rq = [r for r in (reqs or "").split(",") if r and r != v]
        if rq:
            s += f"  (requested {','.join(rq)})"
        return s

    print("direct:")
    emit(("  " + fmt(c, v, r, rs) for c, v, _, r, rs in d), a.limit)
    if a.all:
        print("transitive:")
        emit(("  " + fmt(c, v, r, rs) for c, v, dd, r, rs in nodes if not dd), a.limit)
    elif len(nodes) > len(d):
        print(f"(+{len(nodes) - len(d)} transitive; --all to list)")
    for (r,) in unres:
        print(f"UNRESOLVED: {r}")


def cmd_pkg(a, conn):
    q = a.name.strip().rstrip(".*")
    last = q.rsplit(".", 1)[-1]
    hits = []  # (label, ga, version, n)
    if last[:1].isupper():
        if "." in q:
            pkg = q.rsplit(".", 1)[0]
            hits = [(f"{pkg}.{last}", g, v, 1) for g, v in conn.execute(
                "SELECT ga, version FROM classes WHERE simple=? AND pkg=?", (last, pkg))]
        else:
            hits = [(f"{p}.{last}", g, v, 1) for p, g, v in conn.execute(
                "SELECT pkg, ga, version FROM classes WHERE simple=? ORDER BY pkg", (last,))]
        if not hits and "." in q:
            q = q.rsplit(".", 1)[0]
    if not hits:
        hits = conn.execute("SELECT pkg, ga, version, classes FROM packages WHERE pkg=? ORDER BY ga", (q,)).fetchall()
    if not hits:
        hits = conn.execute("SELECT pkg, ga, version, classes FROM packages WHERE pkg LIKE ? ORDER BY pkg, ga",
                            (q + ".%",)).fetchall()
    if not hits:
        print(f"nothing provides '{a.name}' in the indexed jars (only external modules are indexed)")
        return
    by_pkg = defaultdict(list)
    for p, g, v, n in hits:
        by_pkg[p].append((g, v, n))
    lines = []
    for p, provs in sorted(by_pkg.items()):
        gas = {g for g, _, _ in provs}
        tag = "  [SPLIT: in several modules]" if len(gas) > 1 else ""
        parts = []
        for g, v, n in provs:
            np = conn.execute("""SELECT count(DISTINCT p.project) FROM comps c JOIN nodes n ON n.comp=c.id
                                 JOIN pcs p ON p.id=n.pc WHERE c.ga=? AND c.version=?""", (g, v)).fetchone()[0]
            parts.append(f"{g}:{v} ({np} proj)")
        lines.append(f"{p} -> " + ", ".join(parts) + tag)
    emit(lines, a.limit)


def cmd_info(a, conn):
    ga = one_ga(conn, a.module, a.limit)
    vs = conn.execute("""SELECT c.version, count(DISTINCT p.project), max(n.direct) FROM comps c
                         JOIN nodes n ON n.comp=c.id JOIN pcs p ON p.id=n.pc WHERE c.ga=? GROUP BY c.version""",
                      (ga,)).fetchall()
    vs.sort(key=lambda r: vkey(r[0]), reverse=True)
    print(ga)
    print("in use: " + ", ".join(f"{v} ({n} proj{', direct' if d else ''})" for v, n, d in vs))
    v0 = vs[0][0] if vs else None
    pi = conn.execute("SELECT description, url, licenses, jpms, classes FROM pkginfo WHERE ga=? AND version=?",
                      (ga, v0)).fetchone()
    if pi:
        desc, url, lic, jpms, ncls = pi
        if desc:
            print(f"about: {desc[:200]}")
        print(f"license: {lic or 'unknown'}" + (f" | url: {url}" if url else ""))
        if jpms:
            print(f"JPMS module: {jpms}")
        pk = conn.execute("SELECT pkg, classes FROM packages WHERE ga=? AND version=? ORDER BY classes DESC",
                          (ga, v0)).fetchall()
        if pk:
            shown = ", ".join(p for p, _ in pk[:8])
            print(f"packages ({len(pk)}, {ncls} classes): {shown}" + (" ..." if len(pk) > 8 else ""))
    cat = conn.execute("SELECT accessor, version, ref FROM catalog WHERE ga=?", (ga,)).fetchall()
    for acc, v, ref in cat:
        print(f"catalog: {acc} = {v}" + (f" (version.ref {ref})" if ref else ""))
    lt = conn.execute("SELECT latest FROM latest WHERE ga=?", (ga,)).fetchone()
    if lt and lt[0]:
        print(f"latest: {lt[0]}")
    vu = [r for r in conn.execute("SELECT version, ids FROM vulns WHERE ga=? AND ids != ''", (ga,))]
    for v, ids in vu:
        print(f"vulns {v}: {ids}")


def cmd_conflicts(a, conn):
    project = norm_project(conn, a.project)
    pat = "" if a.all else "AND (n.reasons LIKE '%conflict%' OR n.reasons LIKE '%forced%' OR n.reasons LIKE '%rule%')"
    rows = conn.execute(f"""SELECT c.ga, c.version, n.reqs, n.reasons, p.project FROM nodes n
                            JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc
                            WHERE n.reasons != '' {pat}""" + (" AND p.project=?" if project else "")
                        + (" AND p.config=?" if a.config else ""),
                        tuple(x for x in (project, a.config) if x)).fetchall()
    agg = defaultdict(lambda: [set(), set(), set()])
    for ga, v, reqs, reasons, pj in rows:
        k = (ga, v)
        agg[k][0].update(r for r in reqs.split(",") if r)
        agg[k][1].add(pj)
        agg[k][2].update(x.split(":")[0] for x in reasons.split("; "))
    lines = []
    for (ga, v), (reqs, pjs, why) in sorted(agg.items(), key=lambda kv: -len(kv[1][1])):
        other = sorted(reqs - {v}, key=vkey)
        lines.append(f"{ga} -> {v}" + (f" (also requested {','.join(other)})" if other else "")
                     + f" [{', '.join(sorted(why))}] in {len(pjs)} proj")
    if not lines:
        print("no conflict-resolved modules")
    emit(lines, a.limit)


def cmd_drift(a, conn):
    cfg = a.config or "runtimeClasspath"
    rows = conn.execute("""SELECT c.ga, c.version, p.project FROM nodes n JOIN comps c ON c.id=n.comp
                           JOIN pcs p ON p.id=n.pc WHERE c.kind='m' AND p.config=?""", (cfg,)).fetchall()
    m = defaultdict(lambda: defaultdict(list))
    for ga, v, pj in rows:
        m[ga][v].append(pj)
    lines = []
    for ga, vs in sorted(m.items(), key=lambda kv: -len(kv[1])):
        if len(vs) < 2:
            continue
        parts = []
        for v in sorted(vs, key=vkey, reverse=True):
            ps = sorted(vs[v])
            parts.append(f"{v} ({', '.join(ps[:3])}{f' +{len(ps) - 3}' if len(ps) > 3 else ''})")
        lines.append(f"{ga}: " + " | ".join(parts))
    print(f"modules resolved to different versions across projects [{cfg}]: {len(lines)}")
    emit(lines, a.limit)


def cmd_split(a, conn):
    project = norm_project(conn, a.project)
    cfg = a.config or "runtimeClasspath"
    rows = conn.execute("""SELECT p.project, k.pkg, group_concat(DISTINCT c.ga) FROM nodes n
                           JOIN pcs p ON p.id=n.pc JOIN comps c ON c.id=n.comp
                           JOIN packages k ON k.ga=c.ga AND k.version=c.version
                           WHERE p.config=?""" + (" AND p.project=?" if project else "") + """
                           GROUP BY n.pc, k.pkg HAVING count(DISTINCT c.ga) > 1""",
                        (cfg, project) if project else (cfg,)).fetchall()
    agg = defaultdict(set)
    for pj, pkg, gas in rows:
        agg[(pkg, ",".join(sorted(gas.split(","))))].add(pj)
    lines = [f"{pkg}: {gas.replace(',', ' + ')} — {len(pjs)} proj" for (pkg, gas), pjs in
             sorted(agg.items(), key=lambda kv: -len(kv[1]))]
    print(f"split packages on {cfg}: {len(lines)}" + (" (risk: duplicate classes / JPMS errors)" if lines else ""))
    emit(lines, a.limit)


def cmd_unresolved(a, conn):
    rows = conn.execute("""SELECT u.req, p.project, p.config, u.err FROM unresolved u
                           JOIN pcs p ON p.id=u.pc ORDER BY u.req""").fetchall()
    agg = defaultdict(lambda: [set(), ""])
    for req, pj, cfg, err in rows:
        agg[req][0].add(f"{pj}[{ab(cfg)}]")
        agg[req][1] = agg[req][1] or (err or "").splitlines()[0][:120]
    lines = [f"{req}: {', '.join(sorted(w)[:4])}{' ...' if len(w) > 4 else ''} — {err}" for req, (w, err) in agg.items()]
    print(f"unresolved requests: {len(lines)}")
    emit(lines, a.limit)
    if lines:
        print(LEGEND)


def cmd_licenses(a, conn):
    rows = conn.execute("""SELECT DISTINCT c.ga, c.version, coalesce(nullif(i.licenses,''),'UNKNOWN') FROM comps c
                           JOIN nodes n ON n.comp=c.id LEFT JOIN pkginfo i ON i.ga=c.ga AND i.version=c.version
                           WHERE c.kind='m'""").fetchall()
    m = defaultdict(set)
    for ga, _v, lic in rows:
        if not a.filter or a.filter.lower() in lic.lower():
            m[lic].add(ga)
    lines = []
    for lic, gas in sorted(m.items(), key=lambda kv: -len(kv[1])):
        g = sorted(gas)
        lines.append(f"{lic} ({len(g)}): " + ", ".join(g[:a.show]) + (" ..." if len(g) > a.show else ""))
    emit(lines, a.limit)


def cmd_catalog(a, conn):
    if a.unused:
        rows = conn.execute("""SELECT accessor, ga, version FROM catalog WHERE kind='library'
                               AND ga NOT IN (SELECT ga FROM comps) ORDER BY accessor""").fetchall()
        print(f"catalog libraries not in any resolved graph: {len(rows)}")
        emit((f"  {acc} = {ga}:{v or ''}" for acc, ga, v in rows), a.limit)
        return
    if a.missing:
        rows = conn.execute("""SELECT DISTINCT c.ga, p.project FROM nodes n JOIN comps c ON c.id=n.comp
                               JOIN pcs p ON p.id=n.pc WHERE n.direct=1 AND c.kind='m'
                               AND c.ga NOT IN (SELECT ga FROM catalog) ORDER BY c.ga""").fetchall()
        agg = defaultdict(set)
        for ga, pj in rows:
            agg[ga].add(pj)
        print(f"direct dependencies not declared in the catalog: {len(agg)}")
        emit((f"  {ga} ({', '.join(sorted(p)[:3])}{' ...' if len(p) > 3 else ''})" for ga, p in agg.items()), a.limit)
        return
    q = f"%{a.query or ''}%"
    rows = conn.execute("""SELECT accessor, kind, ga, version, ref FROM catalog
                           WHERE accessor LIKE ? OR ga LIKE ? ORDER BY kind DESC, accessor""", (q, q)).fetchall()
    emit((f"{acc} = {ga}{':' + v if v else ''}" + (f" (ref {ref})" if ref else "")
          + ("" if kind == "library" else f" [{kind}]") for acc, kind, ga, v, ref in rows), a.limit)


def cmd_diff(a, conn):
    p1, p2 = norm_project(conn, a.a), norm_project(conn, a.b)
    cfg = a.config or "runtimeClasspath"

    def m(pj):
        return dict(conn.execute("""SELECT c.ga, c.version FROM nodes n JOIN comps c ON c.id=n.comp
                                    JOIN pcs p ON p.id=n.pc WHERE p.project=? AND p.config=? AND c.kind='m'""",
                                 (pj, cfg)).fetchall())

    m1, m2 = m(p1), m(p2)
    only1 = sorted(set(m1) - set(m2))
    only2 = sorted(set(m2) - set(m1))
    diff = sorted(g for g in set(m1) & set(m2) if m1[g] != m2[g])
    print(f"{p1} vs {p2} [{cfg}]: {len(only1)} only in {p1}, {len(only2)} only in {p2}, {len(diff)} version diffs")
    emit([f"  ~ {g}: {m1[g]} vs {m2[g]}" for g in diff] + [f"  < {g}:{m1[g]}" for g in only1]
         + [f"  > {g}:{m2[g]}" for g in only2], a.limit)


def _http(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "depkb"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    auth = os.environ.get("DEPKB_REPO_AUTH")  # "user:token" for private Maven repos
    if auth and data is None:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _latest_for(ga, repos, allow_pre):
    g, a = ga.split(":", 1)
    for repo in repos:
        url = repo.rstrip("/") + "/" + g.replace(".", "/") + f"/{a}/maven-metadata.xml"
        try:
            root = ET.fromstring(_http(url))
        except (urllib.error.URLError, ET.ParseError, OSError, ValueError):
            continue
        vs = [e.text for e in root.findall("versioning/versions/version") if e.text]
        if not allow_pre:
            vs = [v for v in vs if not is_pre(v)]
        if vs:
            return max(vs, key=vkey)
        rel = root.findtext("versioning/release")
        if rel:
            return rel
    return None


def cmd_outdated(a, conn):
    repos = a.repo or [r for r in os.environ.get("DEPKB_REPOS", "").split(",") if r] or [DEFAULT_REPO]
    project = norm_project(conn, a.project)
    rows = conn.execute("""SELECT DISTINCT c.ga, c.version FROM nodes n JOIN comps c ON c.id=n.comp
                           JOIN pcs p ON p.id=n.pc WHERE c.kind='m'""" + (" AND p.project=?" if project else "")
                        + (" AND n.direct=1" if a.direct else ""), (project,) if project else ()).fetchall()
    cur = defaultdict(list)
    for ga, v in rows:
        cur[ga].append(v)
    now = int(time.time())
    cached = {g: (l, f) for g, l, f in conn.execute("SELECT ga, latest, fetched FROM latest")}
    todo = [g for g in cur if a.refresh or g not in cached or now - cached[g][1] > CACHE_TTL]
    if todo:
        print(f"checking {len(todo)} modules against {', '.join(repos)} ...", file=sys.stderr)
        with cf.ThreadPoolExecutor(16) as ex:
            res = list(ex.map(lambda g: (g, _latest_for(g, repos, False)), todo))
        failed = sum(1 for _, l in res if l is None)
        # only cache answers; failed lookups are retried next time
        conn.executemany("INSERT OR REPLACE INTO latest VALUES(?,?,?)", [(g, l, now) for g, l in res if l])
        conn.commit()
        if failed == len(todo):
            print("no repository answered — check network/proxy or pass --repo URL", file=sys.stderr)
    lines = []
    for ga, vs in sorted(cur.items()):
        lt = conn.execute("SELECT latest FROM latest WHERE ga=?", (ga,)).fetchone()
        lt = lt[0] if lt else None
        mx = max(vs, key=vkey)
        if lt and vkey(lt) > vkey(mx):
            lines.append(f"{ga}: {', '.join(sorted(set(vs), key=vkey))} -> {lt}")
    print(f"outdated: {len(lines)} of {len(cur)} modules")
    emit(lines, a.limit)


def cmd_vulns(a, conn):
    project = norm_project(conn, a.project)
    rows = conn.execute("""SELECT DISTINCT c.ga, c.version FROM nodes n JOIN comps c ON c.id=n.comp
                           JOIN pcs p ON p.id=n.pc WHERE c.kind='m'""" + (" AND p.project=?" if project else ""),
                        (project,) if project else ()).fetchall()
    now = int(time.time())
    cached = {(g, v): f for g, v, f in conn.execute("SELECT ga, version, fetched FROM vulns")}
    todo = [r for r in rows if a.refresh or r not in cached or now - cached[r] > CACHE_TTL]
    try:
        for i in range(0, len(todo), 500):
            chunk = todo[i:i + 500]
            body = json.dumps({"queries": [{"package": {"name": g, "ecosystem": "Maven"}, "version": v}
                                           for g, v in chunk]}).encode()
            res = json.loads(_http(OSV_URL + "/v1/querybatch", body, timeout=60))["results"]
            conn.executemany("INSERT OR REPLACE INTO vulns VALUES(?,?,?,?)",
                             [(g, v, ",".join(x["id"] for x in (r.get("vulns") or [])), now)
                              for (g, v), r in zip(chunk, res)])
        conn.commit()
        need = {i for (ids,) in conn.execute("SELECT ids FROM vulns WHERE ids != ''") for i in ids.split(",")}
        have = {r[0] for r in conn.execute("SELECT id FROM vuln_details")}

        def detail(vid):
            d = json.loads(_http(f"{OSV_URL}/v1/vulns/{vid}"))
            sev = (d.get("database_specific") or {}).get("severity") or ""
            fixed = {}
            for af in d.get("affected", []):
                name = (af.get("package") or {}).get("name", "")
                for rg in af.get("ranges", []):
                    for ev in rg.get("events", []):
                        if "fixed" in ev:
                            fixed.setdefault(name, []).append(ev["fixed"])
            return vid, (d.get("summary") or "")[:160], sev, json.dumps(fixed)

        with cf.ThreadPoolExecutor(8) as ex:
            conn.executemany("INSERT OR REPLACE INTO vuln_details VALUES(?,?,?,?)", list(ex.map(detail, need - have)))
        conn.commit()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"OSV lookup failed ({e}); showing cached results only", file=sys.stderr)
    order = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "MEDIUM": 2, "LOW": 3}
    lines = []
    for g, v in rows:
        r = conn.execute("SELECT ids FROM vulns WHERE ga=? AND version=?", (g, v)).fetchone()
        if not r or not r[0]:
            continue
        for vid in r[0].split(","):
            d = conn.execute("SELECT summary, severity, fixed FROM vuln_details WHERE id=?", (vid,)).fetchone()
            summ, sev, fixed = d if d else ("", "", "{}")
            fx = [f for f in json.loads(fixed or "{}").get(g, []) if vkey(f) > vkey(v)]
            fx_s = f" fixed in {min(fx, key=vkey)}" if fx else ""
            lines.append((order.get((sev or "").upper(), 4), f"{g}:{v} {vid} {sev or '?'}{fx_s} — {summ}"))
    lines.sort()
    print(f"vulnerable module-versions: {len({l.split(' ')[0] for _, l in lines})}")
    emit((l for _, l in lines), a.limit)


# ---------------------------------------------------------------- bytecode: API listings and binary compatibility

JAVADOC_RE = re.compile(r"/\*\*(.*?)\*/", re.S)


def _first_sentence(doc):
    text = " ".join(ln.strip().lstrip("*").strip() for ln in doc.splitlines())
    text = re.split(r"\s@\w+", " " + text)[0].strip()
    text = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", text)
    m = re.match(r"(.+?\.)(\s|$)", text)
    s = (m.group(1) if m else text).strip()
    return s[:140] + ("..." if len(s) > 140 else "")


def javadocs(source, simple):
    """{'' : class doc, member name: doc} from Java source (first sentence, first overload wins)."""
    out = {}
    for m in JAVADOC_RE.finditer(source):
        after = source[m.end():m.end() + 400]
        decl = re.sub(r"@\w+(\([^)]*\))?", " ", after).split("{")[0].split(";")[0]
        doc = _first_sentence(m.group(1))
        if not doc:
            continue
        cm = re.search(r"\b(class|interface|enum|record)\s+(\w+)", decl)
        if cm and "(" not in decl.split(cm.group(0))[0]:
            if cm.group(2) == simple:
                out.setdefault("", doc)
            continue
        mm = re.search(r"(\w+)\s*\(", decl)
        if mm:
            out.setdefault("<init>" if mm.group(1) == simple else mm.group(1), doc)
        else:
            fm = re.search(r"(\w+)\s*(=|$)", decl.strip())
            if fm:
                out.setdefault(fm.group(1), doc)
    return out


def _find_sources(conn, ga, version, jar, internal):
    """Source text of a class: module sources, a -sources.jar, or $DEPKB_SOURCE_ROOTS."""
    top = top_level(internal) + ".java"
    if ga is None:  # a module's own class
        row = conn.execute("SELECT path FROM srcfiles WHERE kind='src' AND path LIKE ?", ("%/" + top,)).fetchone()
        return Path(row[0]).read_text(errors="replace") if row else None
    a = ga.split(":")[1]
    if jar:
        jp = Path(jar)
        name = f"{a}-{version}-sources.jar"
        for cand in [jp.parent / name, *jp.parent.parent.glob(f"*/{name}")]:
            if cand.exists():
                try:
                    with zipfile.ZipFile(cand) as z:
                        return z.read(top).decode("utf-8", "replace")
                except (KeyError, zipfile.BadZipFile, OSError):
                    pass
    for root in filter(None, os.environ.get("DEPKB_SOURCE_ROOTS", "").split(os.pathsep)):
        for cand in Path(root).glob(f"**/{top}"):
            return cand.read_text(errors="replace")
    return None


def _class_candidates(conn, q):
    """Resolve 'Client', 'Client.Builder', 'org.acme.http.Client$Builder' -> [(internal, ga, version, file)]."""
    q = q.strip().replace("$", ".")
    segs = q.split(".")
    k = next((i for i, s in enumerate(segs) if s[:1].isupper()), len(segs) - 1)
    pkg, top, nested = ".".join(segs[:k]), segs[k], segs[k + 1:]
    if pkg:
        rows = conn.execute("""SELECT c.pkg, c.ga, c.version, a.file FROM classes c JOIN artifacts a
                               ON a.ga=c.ga AND a.version=c.version WHERE c.simple=? AND c.pkg=?""", (top, pkg)).fetchall()
    else:
        rows = conn.execute("""SELECT c.pkg, c.ga, c.version, a.file FROM classes c JOIN artifacts a
                               ON a.ga=c.ga AND a.version=c.version WHERE c.simple=?""", (top,)).fetchall()
    suffix = "$".join([top] + nested)
    out = [(p.replace(".", "/") + "/" + suffix, g, v, f) for p, g, v, f in rows]
    # the build's own (compiled) classes
    like = ("%/" + pkg.replace(".", "/") + "/" if pkg else "%/") + suffix + ".class"
    for path, pj in conn.execute("SELECT path, project FROM srcfiles WHERE kind='class' AND path LIKE ?", (like,)):
        pk = conn.execute("SELECT package FROM srcfiles WHERE path=?", (path,)).fetchone()[0] or ""
        out.append((pk.replace(".", "/") + "/" + suffix, None, pj, path))
    return out


def _jdk_class(q):
    """(jmod, bytes) for a JDK class given as FQN or (java.base only) simple name."""
    q = q.strip().replace(".", "/")
    for m in jdk_jmods():
        try:
            with zipfile.ZipFile(m) as z:
                if "/" in q:
                    parts = q.split("/")
                    k = next((i for i, s in enumerate(parts) if s[:1].isupper()), len(parts) - 1)
                    name = "/".join(parts[:k + 1]) + "".join("$" + x for x in parts[k + 1:])
                    try:
                        return m, z.read("classes/" + name + ".class")
                    except KeyError:
                        continue
                if m.endswith("java.base.jmod"):
                    hits = sorted(n for n in z.namelist() if n.endswith("/" + q + ".class") and n.startswith("classes/java/"))
                    if hits:
                        return m, z.read(min(hits, key=len))
        except (OSError, zipfile.BadZipFile):
            continue
    return None


def _read_class(file, internal):
    if file.endswith(".class"):
        return Path(file).read_bytes()
    with zipfile.ZipFile(file) as z:
        return z.read(internal + ".class")


def cmd_api(a, conn):
    """Public API of a class, from the exact jar version on the module's classpath."""
    cands = _class_candidates(conn, a.name)
    if not cands:
        jdk = _jdk_class(a.name)
        if jdk:
            info = classfile.parse(jdk[1])
            print(f"// from the JDK ({Path(jdk[0]).name})")
            text = classfile.render_class(info, include_protected=not a.public_only).splitlines()
            if a.grep:
                text = [ln for ln in text if a.grep.lower() in ln.lower() or not ln.startswith("  ")]
            emit(text, a.limit)
            return
        die(f"class '{a.name}' not found in indexed jars, compiled modules or the JDK")
    project = norm_project(conn, a.project) if a.project else None
    chosen, why = None, ""
    if project:
        for cfg in ("compileClasspath", "runtimeClasspath"):
            on = {(g, v) for g, v in conn.execute("""SELECT c.ga, c.version FROM pcs p JOIN nodes n ON n.pc=p.id
                                                     JOIN comps c ON c.id=n.comp WHERE p.project=? AND p.config=?""",
                                                  (project, cfg))}
            chosen = next((c for c in cands if (c[1], c[2]) in on or (c[1] is None and c[2] == project)), None)
            if chosen:
                why = f"version on {project} {cfg}"
                break
    if not chosen:
        use = {}
        for c in cands:
            use[c] = conn.execute("""SELECT count(DISTINCT p.project) FROM comps c JOIN nodes n ON n.comp=c.id
                                     JOIN pcs p ON p.id=n.pc WHERE c.ga=? AND c.version=?""", (c[1], c[2])).fetchone()[0] if c[1] else 0
        chosen = max(cands, key=lambda c: (use[c], vkey(c[2]) if c[1] else []))
        tops = {c[0] for c in cands}
        why = "most used version" if len(cands) > 1 else ""
        if len(tops) > 1:
            print(f"'{a.name}' is ambiguous: " + ", ".join(sorted(t.replace('/', '.') for t in tops)[:6]))
    internal, ga, ver, file = chosen
    try:
        info = classfile.parse(_read_class(file, internal))
    except (KeyError, OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile):
        die(f"cannot read {internal} from {file}")
    src = _find_sources(conn, ga, ver, file if ga else None, internal)
    docs = javadocs(src, internal.rsplit("/", 1)[1].split("$")[-1]) if src else {}
    origin = f"{ga}:{ver}" if ga else f"module {ver} (compiled)"
    others = sorted({f"{c[2]}" for c in cands if c[1] == ga and c[2] != ver}, key=vkey)
    print(f"// from {origin}" + (f" ({why})" if why else "") + (f"; other versions in use: {', '.join(others)}" if others else "")
          + ("" if docs else "; no sources found for docs"))
    text = classfile.render_class(info, include_protected=not a.public_only, docs=docs).splitlines()
    if a.grep:
        text = [ln for ln in text if a.grep.lower() in ln.lower() or not ln.startswith("  ")]
    emit(text, a.limit)
    if file.endswith(".jar"):
        try:
            with zipfile.ZipFile(file) as z:
                nested = sorted({n[len(internal) + 1:-6].split("$")[0] for n in z.namelist()
                                 if n.startswith(internal + "$") and n.endswith(".class")})
        except (OSError, zipfile.BadZipFile):
            nested = []
        nested = [n for n in nested if not n.isdigit()]
        if nested:
            print("nested: " + ", ".join(nested[:20]))


# --- compatibility

def _jarrefs_one(t):
    """(ga, version, jar) -> rows for jarrefs. Top-level so it can run in a process pool."""
    ga, version, jar = t
    rows = []
    try:
        with zipfile.ZipFile(jar) as z:
            for n in z.namelist():
                if not n.endswith(".class") or n.startswith("META-INF/") or n.endswith(("module-info.class", "package-info.class")):
                    continue
                try:
                    info = classfile.parse(z.read(n), members=False)
                except (ValueError, IndexError, struct.error):
                    continue
                rows.append((ga, version, info["name"], "", "", "C"))
                rows.extend((ga, version, ref, "", "", "R") for ref in info["refs"] if not ref.startswith(SKIP_REF_PREFIXES))
                rows.extend((ga, version, o, nm, d, "M") for o, nm, d in info["member_refs"]
                            if not o.startswith(SKIP_REF_PREFIXES))
    except (OSError, zipfile.BadZipFile):
        pass
    return ga, version, rows


def _jar_index_many(conn, jars):
    done = {(g, v) for g, v in conn.execute("SELECT ga, version FROM jarscan")}
    todo = [(g, v, f) for g, v, f in jars if (g, v) not in done]
    if not todo:
        return
    if len(todo) > 20:
        print(f"depkb: indexing references of {len(todo)} jars (cached afterwards) ...", file=sys.stderr)
    try:
        with cf.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
            results = list(ex.map(_jarrefs_one, todo, chunksize=4))
    except (OSError, cf.process.BrokenProcessPool):
        results = [_jarrefs_one(t) for t in todo]
    for g, v, rows in results:
        conn.executemany("INSERT INTO jarrefs VALUES(?,?,?,?,?,?)", rows)
        conn.execute("INSERT OR IGNORE INTO jarscan VALUES(?,?)", (g, v))
    conn.commit()


def _jar_index(conn, ga, version, jar):
    """Cached per jar: classes it contains -> {internal: info} lazily, plus outgoing refs."""
    if not conn.execute("SELECT 1 FROM jarscan WHERE ga=? AND version=?", (ga, version)).fetchone():
        rows, crows = [], []
        try:
            with zipfile.ZipFile(jar) as z:
                for n in z.namelist():
                    if not n.endswith(".class") or n.startswith("META-INF/") or n.endswith(("module-info.class", "package-info.class")):
                        continue
                    try:
                        info = classfile.parse(z.read(n), members=False)
                    except (ValueError, IndexError, struct.error):
                        continue
                    crows.append((ga, version, info["name"], "", "", "C"))
                    for ref in info["refs"]:
                        if not ref.startswith(SKIP_REF_PREFIXES):
                            rows.append((ga, version, ref, "", "", "R"))
                    for o, nm, d in info["member_refs"]:
                        if not o.startswith(SKIP_REF_PREFIXES):
                            rows.append((ga, version, o, nm, d, "M"))
        except (OSError, zipfile.BadZipFile):
            pass
        conn.executemany("INSERT INTO jarrefs VALUES(?,?,?,?,?,?)", crows + rows)
        conn.execute("INSERT INTO jarscan VALUES(?,?)", (ga, version))
        conn.commit()


_JDK = None
OBJECT_MEMBERS = {("hashCode", "()I"), ("equals", "(Ljava/lang/Object;)Z"), ("toString", "()Ljava/lang/String;"),
                  ("getClass", "()Ljava/lang/Class;"), ("notify", "()V"), ("notifyAll", "()V"), ("wait", "()V"),
                  ("wait", "(J)V"), ("wait", "(JI)V"), ("clone", "()Ljava/lang/Object;"), ("finalize", "()V")}


def jdk_jmods():
    """jmod files of the local JDK ($JAVA_HOME or `java` on PATH); [] when the JDK ships none."""
    global _JDK
    if _JDK is None:
        homes = [os.environ.get("JAVA_HOME")]
        j = shutil.which("java")
        if j:
            homes.append(str(Path(os.path.realpath(j)).parent.parent))
        _JDK = []
        for h in filter(None, homes):
            mods = sorted(Path(h, "jmods").glob("*.jmod"))
            if mods:
                base = [m for m in mods if m.name == "java.base.jmod"]
                _JDK = [str(m) for m in base + [m for m in mods if m not in base]]
                break
    return _JDK


class JarView:
    """Class/member lookup across a set of jars, plus the JDK (jmods) for supertypes."""

    def __init__(self, jars, with_jdk=True):
        self.jars = [(j, "") for j in jars] + ([(m, "classes/") for m in jdk_jmods()] if with_jdk else [])
        self.has_jdk = with_jdk and bool(jdk_jmods())
        self._z, self._info = {}, {}

    def info(self, internal):
        if internal in self._info:
            return self._info[internal]
        res = None
        jdk_name = internal.startswith(("java/", "javax/", "jdk/", "sun/", "com/sun/"))
        for j, prefix in self.jars:
            if prefix and not jdk_name:
                continue
            z = self._z.get(j)
            if z is None:
                try:
                    z = self._z[j] = zipfile.ZipFile(j)
                except (OSError, zipfile.BadZipFile):
                    continue
            try:
                res = classfile.parse(z.read(prefix + internal + ".class"))
                break
            except KeyError:
                continue
        self._info[internal] = res
        return res

    def has_member(self, owner, name, desc, depth=0):
        """True / False / None (cannot tell: hierarchy leaves the known jars, e.g. JDK)."""
        if owner == "java/lang/Object" and not self.has_jdk:
            return (name, desc) in OBJECT_MEMBERS
        info = self.info(owner)
        if info is None:
            return None if depth else False
        is_method = desc.startswith("(")
        for acc, nm, ds, *_ in (info["methods"] if is_method else info["fields"]):
            if nm == name and ds == desc:
                return True
        if name == "<init>" or depth > 12:
            return False
        unknown = False
        for sup in [info["super"], *info["interfaces"]]:
            if not sup:
                continue
            r = self.has_member(sup, name, desc, depth + 1)
            if r:
                return True
            unknown |= r is None
        return None if unknown else False


def _member_text(owner, name, desc):
    cls = owner.rsplit("/", 1)[-1].replace("$", ".")
    if not desc.startswith("("):
        return f"{cls}.{name}"
    p = classfile._Sig(desc, lambda i: i.rsplit("/", 1)[-1].replace("$", "."))
    p.take()
    params = []
    while p.peek() != ")":
        params.append(p.type())
    return f"{cls}.{'<init>' if name == '<init>' else name}({', '.join(params)})"


_REFS_CACHE, _MOD_REFS = {}, {}


def check_callers(conn, pc, ga, target_version, target_jar):
    """Check every caller of `ga` on classpath `pc` against the given version's jar.
    Returns [(caller label, compiled-against version, [problems])]."""
    target_classes = {c for (c,) in conn.execute(
        "SELECT owner FROM jarrefs WHERE ga=? AND version=? AND kind='C'", (ga, target_version))}
    if not target_classes:
        _jar_index(conn, ga, target_version, target_jar)
        target_classes = {c for (c,) in conn.execute(
            "SELECT owner FROM jarrefs WHERE ga=? AND version=? AND kind='C'", (ga, target_version))}
    # classes that belong to this library: in any known version of it, or in one of its packages
    # and not provided by some other library (split packages, e.g. legacy jars reusing a package)
    known = set(target_classes) | {f"{p.replace('.', '/')}/{s}" for p, s in conn.execute(
        "SELECT pkg, simple FROM classes WHERE ga=?", (ga,))}
    pkgs = {c.rpartition("/")[0] for c in known}
    cache = {}

    def belongs(internal):
        top = top_level(internal)
        if top in known:
            return True
        if top.rpartition("/")[0] not in pkgs:
            return False
        if top not in cache:
            pk, _, sm = top.rpartition("/")
            cache[top] = not conn.execute("SELECT 1 FROM classes WHERE pkg=? AND simple=? AND ga!=? LIMIT 1",
                                          (pk.replace("/", "."), sm, ga)).fetchone()
        return cache[top]
    view = JarView([target_jar])
    results = []

    def problems_for(refs):
        probs = set()
        for o, nm, d, kind in refs:
            if not belongs(o):
                continue
            if kind == "R":
                if o not in target_classes and top_level(o) not in target_classes:
                    probs.add(f"class {o.replace('/', '.')} missing")
                continue
            if o not in target_classes:
                probs.add(f"class {o.replace('/', '.')} missing")
                continue
            if view.has_member(o, nm, d) is False:
                probs.add(f"{_member_text(o, nm, d)} missing")
        return sorted(probs)

    # jar callers: components on this classpath that depend on ga
    for cid, req in conn.execute("""SELECT fc.cid, e.req FROM edges e JOIN comps fc ON fc.id=e.f
                                    JOIN comps tc ON tc.id=e.t WHERE e.pc=? AND tc.ga=? AND e.cons=0
                                    AND fc.kind='m'""", (pc, ga)).fetchall():
        cga, cv, _ = split_cid(cid)
        art = conn.execute("SELECT file FROM artifacts WHERE ga=? AND version=?", (cga, cv)).fetchone()
        if not art or not art[0].endswith(".jar"):
            continue
        if req_version(req) == target_version:
            results.append((short(cid), target_version, []))  # built against this very version
            continue
        refs = _REFS_CACHE.get((cga, cv))
        if refs is None:
            _jar_index(conn, cga, cv, art[0])
            refs = _REFS_CACHE[(cga, cv)] = conn.execute(
                "SELECT owner, name, desc, kind FROM jarrefs WHERE ga=? AND version=? AND kind IN ('M','R')",
                (cga, cv)).fetchall()
        results.append((short(cid), req_version(req) or "?", problems_for(refs)))
    # module callers: compiled classes of modules on this classpath (and the root module)
    root, = conn.execute("SELECT c.cid FROM pcs p JOIN comps c ON c.id=p.root WHERE p.id=?", (pc,)).fetchone()
    mods = {root[8:]} | {cid[8:] for (cid,) in conn.execute(
        "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.kind='p'", (pc,))}
    for m in sorted(mods):
        compiled = _node(conn, m, "compileClasspath", ga)
        if compiled and compiled[2] == target_version:
            continue  # javac already linked this module against the same version
        if m not in _MOD_REFS:
            refs = conn.execute("""SELECT DISTINCT m.owner, m.name, m.desc, 'M' FROM mrefs m JOIN srcfiles f ON f.id=m.file
                                   WHERE f.project=? AND f.sset='main' AND f.kind='class'""", (m,)).fetchall()
            refs += [(fqn.replace(".", "/"), "", "", "R") for (fqn,) in conn.execute(
                """SELECT DISTINCT i.fqn FROM imports i JOIN srcfiles f ON f.id=i.file
                   WHERE f.project=? AND f.sset='main' AND f.kind='class' AND i.via IN ('class','abi')""", (m,))]
            _MOD_REFS[m] = (refs, {o.rpartition("/")[0] for o, *_ in refs})
        refs, ref_pkgs = _MOD_REFS[m]
        if not (ref_pkgs & pkgs) or not any(belongs(o) for o, *_ in refs if o.rpartition("/")[0] in pkgs):
            if "compiled" not in _MOD_REFS:
                _MOD_REFS["compiled"] = {p for (p,) in conn.execute("SELECT DISTINCT project FROM srcfiles WHERE kind='class'")}
            if m not in _MOD_REFS["compiled"] and conn.execute(
                    "SELECT 1 FROM links l WHERE l.project=? AND l.sset='main' AND l.ga=?", (m, ga)).fetchone():
                results.append((m, compiled[2] if compiled else "?", ["not compiled: cannot check (compile it first)"]))
            continue
        results.append((m, compiled[2] if compiled else "?", problems_for(refs)))
    return results


def _print_check(results, target_version, limit):
    bad = [(c, v, p) for c, v, p in results if p and not p[0].startswith("not compiled")]
    unk = [(c, v, p) for c, v, p in results if p and p[0].startswith("not compiled")]
    ok = [c for c, v, p in results if not p]
    for c, v, p in bad:
        print(f"  BREAKS {c} (built against {v}):")
        emit((f"    {x}" for x in p), limit)
    for c, v, p in unk:
        print(f"  ? {c}: {p[0]}")
    if ok:
        print(f"  ok: {', '.join(ok[:8])}{' +' + str(len(ok) - 8) if len(ok) > 8 else ''}")
    return bool(bad)


def _target_pcs(conn, ga, project, config):
    if project:
        rows = conn.execute("SELECT id, project, config FROM pcs WHERE project=? AND config=? AND error IS NULL",
                            (project, config)).fetchall()
    else:
        apps = [r[0] for r in conn.execute("SELECT path FROM projects WHERE kind='app'")] or \
               [r[0] for r in conn.execute("SELECT path FROM projects")]
        rows = [r for app in sorted(apps) for r in conn.execute(
            "SELECT id, project, config FROM pcs WHERE project=? AND config=? AND error IS NULL", (app, config))]
    return [(pc, pj, cfg, n) for pc, pj, cfg in rows
            for n in [conn.execute("""SELECT c.id, c.version, n.reqs FROM nodes n JOIN comps c ON c.id=n.comp
                                      WHERE n.pc=? AND c.ga=?""", (pc, ga)).fetchone()] if n]


def cmd_compat(a, conn):
    """Binary compatibility of version overrides: do callers built against another version still link?"""
    project = norm_project(conn, a.project) if a.project else None
    cfg = a.config or "runtimeClasspath"
    gas = [one_ga(conn, a.module, a.limit)] if a.module else None
    if project:
        pcs = conn.execute("SELECT id, project FROM pcs WHERE project=? AND config=? AND error IS NULL", (project, cfg)).fetchall()
    else:
        apps = [r[0] for r in conn.execute("SELECT path FROM projects WHERE kind='app'")] or \
               [r[0] for r in conn.execute("SELECT path FROM projects")]
        pcs = [r for app in sorted(apps) for r in conn.execute(
            "SELECT id, project FROM pcs WHERE project=? AND config=? AND error IS NULL", (app, cfg))]
    any_bad = checked = 0
    quiet_ok, uncompiled = [], defaultdict(set)
    compile_versions = defaultdict(set)
    for g, ver in conn.execute("""SELECT DISTINCT c.ga, c.version FROM pcs p JOIN nodes n ON n.pc=p.id
                                  JOIN comps c ON c.id=n.comp WHERE p.config='compileClasspath' AND c.kind='m'"""):
        compile_versions[g].add(ver)
    for pc, pj in pcs:
        rows = conn.execute("""SELECT c.ga, c.version, n.reqs FROM nodes n JOIN comps c ON c.id=n.comp
                               WHERE n.pc=? AND c.kind='m'""", (pc,)).fetchall()
        for ga, v, reqs in rows:
            if gas and ga not in gas:
                continue
            others = [r for r in (reqs or "").split(",") if r and r != v]
            compiled_other = compile_versions.get(ga, set()) - {v}
            if not others and not gas and not compiled_other:
                continue
            art = conn.execute("SELECT file FROM artifacts WHERE ga=? AND version=?", (ga, v)).fetchone()
            if not art or not art[0].endswith(".jar"):
                continue
            checked += 1
            res = check_callers(conn, pc, ga, v, art[0])
            head = f"{pj} [{ab(cfg)}] {ga} -> {v}" + (f" (requested {','.join(others)})" if others else "")
            breaks = any(p and not p[0].startswith("not compiled") for _c, _v, p in res)
            for c, _v, p in res:
                if p and p[0].startswith("not compiled"):
                    uncompiled[c].add(ga)
            if breaks or a.verbose or gas:
                print(head)
                any_bad |= _print_check([r for r in res if a.verbose or gas or not (r[2] and r[2][0].startswith("not compiled"))],
                                        v, a.limit)
            else:
                quiet_ok.append(ga)
    if not checked:
        print("no overridden libraries to check" + (f" for {a.module}" if a.module else ""))
    else:
        print(f"checked {plural(checked, 'override')}: " + ("BREAKS listed above" if any_bad else "no binary breaks found")
              + (f"; {len(set(quiet_ok))} without breaks not shown (--verbose)" if quiet_ok else ""))
    if uncompiled and not (a.verbose or gas):
        mods = sorted(uncompiled)
        print(f"not checked (compile these modules first): {', '.join(mods[:10])}{' ...' if len(mods) > 10 else ''}")
    print("note: checks classes/methods/fields callers link against; not behaviour or reflection")


def _fetch_jar(conn, ga, version, repos, ext="jar"):
    g, art = ga.split(":", 1)
    row = conn.execute("SELECT file FROM artifacts WHERE ga=? AND version=?", (ga, version)).fetchone()
    if ext == "jar" and row and row[0].endswith(".jar") and os.path.exists(row[0]):
        return row[0]
    if ext == "pom":
        hint = row[0] if row and row[0] else None
        p = find_pom(g, art, version, _pom_roots(conn), hint)
        if p:
            return str(p)
    name = f"{art}-{version}.{ext}"
    for root, layout in _pom_roots(conn):
        cands = list((root / g / art / version).glob(f"*/{name}")) if layout == "cache" else \
            [root.joinpath(*g.split("."), art, version, name)]
        for c in cands:
            if c.exists():
                return str(c)
    cache = Path(meta(conn, "project_dir", ".")) / ".depkb" / "jars"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / name
    if dest.exists():
        return str(dest)
    for repo in repos:
        url = repo.rstrip("/") + "/" + g.replace(".", "/") + f"/{art}/{version}/{name}"
        try:
            dest.write_bytes(_http(url, timeout=60))
            return str(dest)
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def cmd_impact(a, conn):
    """What would break if <lib> were moved to <version>: checks every caller on the app classpaths."""
    ga = one_ga(conn, a.module, a.limit)
    repos = a.repo or [r for r in os.environ.get("DEPKB_REPOS", "").split(",") if r] or [DEFAULT_REPO]
    jar = _fetch_jar(conn, ga, a.version, repos)
    if not jar:
        die(f"cannot find {ga}:{a.version} locally or in {', '.join(repos)} (use --repo)")
    project = norm_project(conn, a.project) if a.project else None
    targets = _target_pcs(conn, ga, project, a.config or "runtimeClasspath")
    if not targets:
        die(f"{ga} is not on any {'app ' if not project else ''}{a.config or 'runtimeClasspath'}")
    _jar_index(conn, ga, a.version, jar)
    print(f"impact of {ga} -> {a.version}:")
    bad = False
    for pc, pj, cfg, (_cid, cur, _reqs) in targets:
        print(f"{pj} [{ab(cfg)}] now {cur}")
        if cur == a.version:
            print("  already on this version")
            continue
        bad |= _print_check(check_callers(conn, pc, ga, a.version, jar), a.version, a.limit)
        bad |= _transitive_changes(conn, pc, ga, cur, a.version, repos, a.limit)
    print("verdict: " + ("BREAKING for the callers above" if bad else "no binary breaks or lost classes found")
          + ". After changing the build, depkb re-resolves: run `depkb compat` and `depkb missing` to confirm.")


def _runtime_deps(conn, ga, version, repos):
    p = _fetch_jar(conn, ga, version, repos, ext="pom")
    info = parse_pom(p) if p else None
    if not info:
        return None
    return {d["ga"]: d["version"] for d in info["deps"]
            if d["scope"] in ("compile", "runtime") and not d["optional"]}


def _transitive_changes(conn, pc, ga, cur, new, repos, limit):
    """Diff the dependencies the library brings along; flag dependencies that would leave the
    classpath while something still uses their classes (future ClassNotFoundException)."""
    old_d, new_d = _runtime_deps(conn, ga, cur, repos), _runtime_deps(conn, ga, new, repos)
    if old_d is None or new_d is None:
        print("  transitive changes: POM not available, not checked")
        return False
    added = sorted(set(new_d) - set(old_d))
    removed = sorted(set(old_d) - set(new_d))
    changed = sorted(g for g in set(old_d) & set(new_d) if old_d[g] != new_d[g])
    if not (added or removed or changed):
        print("  transitive changes: none")
        return False
    print("  transitive changes: " + ", ".join([f"+{g}:{new_d[g]}" for g in added] + [f"-{g}" for g in removed]
                                               + [f"~{g} {old_d[g]}->{new_d[g]}" for g in changed]))
    bad = False
    lib = conn.execute("SELECT id FROM comps WHERE ga=? AND version=?", (ga, cur)).fetchone()
    for g in removed:
        others = conn.execute("""SELECT DISTINCT fc.cid FROM edges e JOIN comps tc ON tc.id=e.t JOIN comps fc ON fc.id=e.f
                                 WHERE e.pc=? AND tc.ga=? AND e.cons=0 AND fc.id!=?""", (pc, g, lib[0] if lib else -1)).fetchall()
        if others:
            print(f"    -{g}: still on the classpath via {', '.join(short(c) for (c,) in others[:3])}")
            continue
        node = conn.execute("""SELECT c.version FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.ga=?""",
                            (pc, g)).fetchone()
        if not node:
            continue
        classes = {f"{p.replace('.', '/')}/{sm}" for p, sm in conn.execute(
            "SELECT pkg, simple FROM classes WHERE ga=? AND version=?", (g, node[0]))}
        users = defaultdict(int)
        jars = [j for j in _pc_jars(conn, pc) if j[0] not in (g, ga)]
        refs = _referrers(conn, pc, _closure(conn, pc), jars)
        for c in classes:
            for w in refs.get(c, ()):
                users[w] += 1
        if users:
            bad = True
            print(f"    LOST {g}:{node[0]} leaves the classpath, but its classes are used by "
                  + ", ".join(f"{w} ({plural(n, 'class')})" for w, n in sorted(users.items())[:limit])
                  + " -> ClassNotFoundException; declare it directly")
        else:
            print(f"    -{g}: leaves the classpath; nothing else references it")
    return bad


# ---------------------------------------------------------------- JVM upgrade impact

REL_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
JDKISH = ("java/", "javax/", "jdk/", "sun/", "com/sun/", "org/w3c/dom/", "org/xml/sax/", "org/ietf/jgss/", "org/omg/")
RULE_STRING = re.compile(r"^(?i:nashorn|javascript|js|ecmascript)$|[hHKk]{1,2}[:.]mm.*\sa\b|^TLSv1(\.1)?$"
                         r"|^(java|javax|sun|jdk)\.[a-z][\w.]*\.[A-Z]\w*$")
TIME_PATTERN = re.compile(r"[hHKk]{1,2}[:.]mm.*\sa\b")
# Methods that still link on the target but throw at run time
UOE_RULES = {
    ("java/lang/Thread", "stop", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/Thread", "suspend", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/Thread", "resume", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/ThreadGroup", "stop", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/ThreadGroup", "suspend", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/ThreadGroup", "resume", "()V"): (20, "throws UnsupportedOperationException since JDK 20"),
    ("java/lang/System", "setSecurityManager", "(Ljava/lang/SecurityManager;)V"):
        (18, "fails since JDK 18 unless run with -Djava.security.manager=allow (JEP 411)"),
}
# Calls that silently used the platform charset; JDK 18 makes the default UTF-8 everywhere (JEP 400)
DEFAULT_CHARSET = {
    ("java/lang/String", "getBytes", "()[B"), ("java/lang/String", "<init>", "([B)V"),
    ("java/lang/String", "<init>", "([BII)V"), ("java/io/InputStreamReader", "<init>", "(Ljava/io/InputStream;)V"),
    ("java/io/OutputStreamWriter", "<init>", "(Ljava/io/OutputStream;)V"),
    ("java/io/FileReader", "<init>", "(Ljava/lang/String;)V"), ("java/io/FileReader", "<init>", "(Ljava/io/File;)V"),
    ("java/io/FileWriter", "<init>", "(Ljava/lang/String;)V"), ("java/io/FileWriter", "<init>", "(Ljava/lang/String;Z)V"),
    ("java/io/FileWriter", "<init>", "(Ljava/io/File;)V"), ("java/io/FileWriter", "<init>", "(Ljava/io/File;Z)V"),
    ("java/io/PrintStream", "<init>", "(Ljava/io/OutputStream;)V"),
    ("java/io/PrintStream", "<init>", "(Ljava/io/OutputStream;Z)V"),
    ("java/io/PrintWriter", "<init>", "(Ljava/io/OutputStream;)V"),
    ("java/io/PrintWriter", "<init>", "(Ljava/lang/String;)V"), ("java/io/PrintWriter", "<init>", "(Ljava/io/File;)V"),
    ("java/util/Scanner", "<init>", "(Ljava/io/InputStream;)V"), ("java/util/Scanner", "<init>", "(Ljava/io/File;)V"),
    ("java/io/ByteArrayOutputStream", "toString", "()Ljava/lang/String;"),
    ("java/net/URLEncoder", "encode", "(Ljava/lang/String;)Ljava/lang/String;"),
    ("java/net/URLDecoder", "decode", "(Ljava/lang/String;)Ljava/lang/String;"),
    ("java/nio/charset/Charset", "defaultCharset", "()Ljava/nio/charset/Charset;"),
}
MIN_VERSIONS = {  # library -> (minimum version, JDK it is needed for, why)
    "org.projectlombok:lombok": ("1.18.30", 21, "first Lombok release supporting JDK 21"),
}
GRADLE_MIN = {21: ("8.5", "8.4"), 17: ("7.3", "7.3")}  # JDK -> (run Gradle on it, use as toolchain)
FLAG_FILES = ("Dockerfile", "Containerfile", "Procfile", "jvm.config", "gradle.properties")
FLAG_EXT = (".sh", ".bash", ".yml", ".yaml", ".properties", ".gradle", ".kts", ".conf", ".env", ".cfg", ".toml",
            ".service", ".bat", ".cmd", ".json", ".ini", ".options", ".vmoptions")
FLAG_RE = re.compile(r"(?<![\w-])(-XX:[+-]?[A-Za-z][A-Za-z0-9_]*(?:=[^\s\"',;\]}]*)?|--illegal-access=\w+|-Xloggc:[^\s\"']+"
                     r"|-Xverify:none|-Xincgc|-Xnoagent|-Xdebug|-XX:MaxPermSize=\S+|-Djava\.security\.manager(?:=\w+)?)")


def jdk_home_version(home):
    try:
        txt = (Path(home) / "release").read_text()
        v = re.search(r'JAVA_VERSION="([^"]+)"', txt).group(1)
    except (OSError, AttributeError):
        return None
    parts = v.split(".")
    return int(parts[1]) if parts[0] == "1" else int(re.match(r"\d+", parts[0]).group(0))


def find_jdk(explicit=None):
    cands = [explicit] if explicit else [os.environ.get("JAVA_HOME")]
    j = shutil.which("java")
    if j and not explicit:
        cands.append(str(Path(os.path.realpath(j)).parent.parent))
    for c in filter(None, cands):
        if jdk_home_version(c):
            return Path(c)
    return None


class JdkModel:
    """The public API (classes, members, deprecation, exports) of one Java release, read from a JDK's
    jmods (its own release) or its lib/ct.sym (older releases)."""

    def __init__(self, release, home):
        self.release, self.home = release, Path(home)
        hv = jdk_home_version(home)
        self.entries, self.exported, self._z, self._info = {}, set(), {}, {}
        if hv == release:
            mods = sorted((self.home / "jmods").glob("*.jmod"))
            if not mods:
                raise ValueError(f"JDK {hv} at {home} has no jmods; install a full JDK {release}")
            for m in mods:
                with zipfile.ZipFile(m) as z:
                    for n in z.namelist():
                        if n.startswith("classes/") and n.endswith(".class"):
                            internal = n[8:-6]
                            if internal == "module-info":
                                exp = classfile.parse(z.read(n)).get("exports") or []
                                self.exported.update(exp)
                            else:
                                self.entries.setdefault(internal, (str(m), n))
            self.source = f"JDK {hv} ({self.home})"
        elif hv and hv > release and release < len(REL_CHARS):
            ch = REL_CHARS[release]
            ct = self.home / "lib" / "ct.sym"
            with zipfile.ZipFile(ct) as z:
                for n in z.namelist():
                    dirs, _, rest = n.partition("/")
                    if ch in dirs and n.endswith(".sig") and "/" in rest:
                        internal = rest.split("/", 1)[1][:-4]
                        if internal != "module-info":
                            self.entries.setdefault(internal, (str(ct), n))
                            self.exported.add(internal.rpartition("/")[0])
            if not self.entries:
                raise ValueError(f"{ct} has no data for release {release}")
            self.source = f"release {release} API from JDK {hv} ct.sym"
        else:
            raise ValueError(f"JDK at {home} is {hv}; it cannot describe release {release}")

    def has_class(self, internal):
        return internal in self.entries

    def info(self, internal):
        if internal not in self._info:
            e = self.entries.get(internal)
            res = None
            if e:
                z = self._z.get(e[0]) or self._z.setdefault(e[0], zipfile.ZipFile(e[0]))
                try:
                    res = classfile.parse(z.read(e[1]))
                except (KeyError, ValueError, IndexError, struct.error):
                    res = None
            self._info[internal] = res
        return self._info[internal]

    def find_member(self, owner, name, desc, depth=0):
        """Declaring class (str), False (absent; hierarchy fully known), or None (cannot tell)."""
        info = self.info(owner)
        if info is None:
            return None
        pool = info["methods"] if desc.startswith("(") else info["fields"]
        if any(nm == name and ds == desc for _a, nm, ds, *_ in pool):
            return owner
        if name == "<init>" or depth > 15:
            return False
        unknown = False
        for sup in [info["super"], *info["interfaces"]]:
            if not sup:
                continue
            if not self.has_class(sup):
                unknown = True
                continue
            r = self.find_member(sup, name, desc, depth + 1)
            if r:
                return r
            unknown |= r is None
        if not desc.startswith("(") or not (info["access"] & classfile.ACC_INTERFACE):
            return None if unknown else False
        r = self.find_member("java/lang/Object", name, desc, depth + 1)  # interfaces inherit Object's methods
        return r if r else (None if unknown else False)

    def deprecation(self, owner, name=None, desc=None):
        info = self.info(owner)
        if not info:
            return None
        return info["deprecated"].get("" if name is None else (name, desc))


def _tool_max_java(info):
    """ASM Opcodes / Byte Buddy ClassFileVersion / Javassist ClassFile -> highest Java version it knows."""
    nm, fields = info["name"] or "", info.get("int_fields", {})
    vs = []
    if nm.endswith("/Opcodes") and "asm" in nm.lower():
        for f in fields:
            m = re.fullmatch(r"V(\d+)(?:_(\d+))?", f)
            if m:
                vs.append(int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1)))
    elif nm.endswith("bytebuddy/ClassFileVersion"):
        vs = [int(m.group(1)) for f in fields for m in [re.fullmatch(r"JAVA_V(\d+)", f)] if m]
    elif nm.endswith("javassist/bytecode/ClassFile"):
        vs = [int(m.group(1)) for f in fields for m in [re.fullmatch(r"JAVA_(\d+)", f)] if m]
    return max(vs) if vs else None


def _jvm_rows(info):
    rows = []
    name = info["name"] or ""
    for o, n, d in info["member_refs"]:
        if o.startswith(JDKISH):
            rows.append((o, n, d, "M"))
    for c in info["refs"]:
        if c.startswith(JDKISH):
            rows.append((c, "", "", "C"))
    for s in info["strings"]:
        if len(s) < 200 and RULE_STRING.search(s):
            rows.append((name, s, "", "S"))
    for _acc, nm, ds, *_ in info["methods"]:
        if nm == "finalize" and ds == "()V":
            rows.append((name, "finalize", "()V", "F"))
    t = _tool_max_java(info)
    if t:
        rows.append((name, str(t), "", "T"))
    return rows


def jvm_scan(conn):
    """Collect JDK-facing references of compiled modules and of every jar in the graphs (cached)."""
    stamps = dict(conn.execute("SELECT src, stamp FROM jvmscan"))
    todo = []
    for fid, path, mt in conn.execute("SELECT id, path, mtime FROM srcfiles WHERE kind='class'"):
        if stamps.get(f"f:{fid}") != str(mt):
            todo.append(("f", f"f:{fid}", path, str(mt)))
    for ga, v, f in conn.execute("""SELECT DISTINCT a.ga, a.version, a.file FROM artifacts a JOIN comps c
                                    ON c.ga=a.ga AND c.version=a.version WHERE a.file LIKE '%.jar'"""):
        try:
            st = str(os.stat(f).st_mtime_ns)
        except OSError:
            continue
        if stamps.get(f"j:{ga}:{v}") != st:
            todo.append(("j", f"j:{ga}:{v}", f, st))
    live = {f"f:{i}" for (i,) in conn.execute("SELECT id FROM srcfiles WHERE kind='class'")}
    for src in [s for s in stamps if s.startswith("f:") and s not in live]:
        conn.execute("DELETE FROM jvmrefs WHERE src=?", (src,))
        conn.execute("DELETE FROM jvmscan WHERE src=?", (src,))
    if not todo:
        return

    if len(todo) > 50:
        print(f"depkb: scanning {len(todo)} class files/jars for JDK references (cached afterwards) ...", file=sys.stderr)
    big = [t for t in todo if t[0] == "j"]
    small = [t for t in todo if t[0] == "f"]
    results = {}
    if big:
        try:
            with cf.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
                for src, rows in ex.map(_jvm_scan_one, big, chunksize=4):
                    results[src] = rows
        except (OSError, cf.process.BrokenProcessPool):
            for t in big:
                src, rows = _jvm_scan_one(t)
                results[src] = rows
    with cf.ThreadPoolExecutor(8) as ex:
        for src, rows in ex.map(_jvm_scan_one, small):
            results[src] = rows
    for (_k, src, _p, st) in todo:
        rows = results.get(src, set())
        conn.execute("DELETE FROM jvmrefs WHERE src=?", (src,))
        conn.executemany("INSERT INTO jvmrefs VALUES(?,?,?,?,?)", [(src, *r) for r in rows])
        conn.execute("INSERT OR REPLACE INTO jvmscan VALUES(?,?)", (src, st))
    conn.commit()


def _jvm_scan_one(t):
    """One class file ('f') or jar ('j') -> (src, JDK-facing rows). Top-level so processes can run it."""
    kind, src, path, _st = t
    rows, major = set(), 0
    infos = []
    try:
        if kind == "f":
            infos = [classfile.parse(Path(path).read_bytes())]
        else:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.endswith(".class") and not n.startswith("META-INF/") and not n.endswith("module-info.class"):
                        try:
                            infos.append(classfile.parse(z.read(n)))
                        except (ValueError, IndexError, struct.error):
                            pass
    except (OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile):
        infos = []
    for info in infos:
        rows.update(_jvm_rows(info))
        major = max(major, info.get("major") or 0)
    if major:
        rows.add(("", str(major), "", "V"))
    return src, rows


def _member_label(owner, name, desc):
    return _member_text(owner, name, desc).replace(owner.rsplit("/", 1)[-1].replace("$", "."),
                                                  owner.replace("/", ".").replace("$", "."), 1)


def scan_jvm_flags(proj):
    """-> {flag: [(relpath, line)]} for JVM options found in deployment/build files."""
    found = defaultdict(list)
    for dirpath, dirs, names in os.walk(proj):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in ("src", "out", "target")]
        for n in names:
            if not (n.startswith(FLAG_FILES) or n.endswith(FLAG_EXT)):
                continue
            p = Path(dirpath) / n
            try:
                if p.stat().st_size > 512_000:
                    continue
                for no, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    for m in FLAG_RE.finditer(line):
                        found[m.group(1).rstrip(".")].append((str(p.relative_to(proj)), no))
            except OSError:
                continue
    return found


def test_flag(java, flag):
    """Run the target JVM with the flag: -> ('ok'|'warn'|'fail', message)."""
    if "$" in flag or "{" in flag:
        return "skip", "contains a variable; not tested"
    try:
        r = subprocess.run([java, flag, "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "skip", str(e)
    lines = [ln for ln in r.stderr.splitlines() if ln.strip() and "JAVA_TOOL_OPTIONS" not in ln]
    if r.returncode != 0:
        return "fail", next((ln for ln in lines if "rror" in ln or "nrecognized" in ln), lines[0] if lines else "exit " + str(r.returncode))
    warn = [ln for ln in lines if "warning" in ln.lower() or "deprecated" in ln.lower()]
    return ("warn", warn[0].split("warning:", 1)[-1].strip()) if warn else ("ok", "")


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def cmd_jvm(a, conn):
    """Impact of moving the build and its apps from one Java version to another."""
    target = a.version
    home = find_jdk(a.jdk)
    if not home:
        die("no JDK found: pass --jdk <path to the target JDK> (or set JAVA_HOME)")
    hv = jdk_home_version(home)
    if hv != target:
        die(f"--jdk must point at a JDK {target} (found JDK {hv} at {home}); it is used for the exact {target} API "
            f"and to test JVM flags")
    pinfo = {pj: json.loads(js or "{}") for pj, js in conn.execute("SELECT path, java FROM projects")}
    source = a.source
    if not source:
        levels = [int(str(x)) for i in pinfo.values() for x in (i.get("release"), i.get("toolchain"), i.get("target"))
                  if x and str(x).isdigit()]
        majors = [int(n) - 44 for (n,) in conn.execute(
            "SELECT name FROM jvmrefs WHERE kind='V' AND src LIKE 'f:%'") if n.isdigit()]
        source = min(levels or majors or [0]) or None
        if not source:
            die("cannot tell the current Java version; pass --from 11")
    try:
        T = JdkModel(target, home)
        S = JdkModel(source, home)
    except ValueError as e:
        die(str(e))
    print(f"JVM upgrade {source} -> {target}  [target API: {T.source}; source API: {S.source}]", file=sys.stderr)
    jvm_scan(conn)

    # where each reference comes from
    files = {f"f:{i}": (pj, p) for i, pj, p in conn.execute("SELECT id, project, path FROM srcfiles WHERE kind='class'")}

    def where(src):
        if src.startswith("f:"):
            return files.get(src, ("?", ""))[0]
        return short(src[2:])

    lib_classes = {f"{p.replace('.', '/')}/{s}" for p, s in conn.execute("SELECT DISTINCT pkg, simple FROM classes")}
    jdk_pkgs = {k.rpartition("/")[0] for k in T.entries} | {k.rpartition("/")[0] for k in S.entries}
    findings = {}  # key -> [severity, title, why, {where: set(srcs)}]

    def add(sev, key, title, why, src):
        f = findings.setdefault(key, [sev, title, why, defaultdict(set)])
        f[3][where(src)].add(src)

    dep_api = defaultdict(set)
    by_src = defaultdict(list)
    for src, owner, name, desc, kind in conn.execute("SELECT src, owner, name, desc, kind FROM jvmrefs"):
        if src.startswith("f:") and src not in files:
            continue
        by_src[src].append((owner, name, desc, kind))
    for src, rows in by_src.items():
        calls = {(o, n, d) for o, n, d, k in rows if k == "M"}
        uses_engine_by_name = any(o == "javax/script/ScriptEngineManager" and n == "getEngineByName" for o, n, _d in calls)
        uses_patterns = any((o == "java/time/format/DateTimeFormatter" and n == "ofPattern")
                            or (o == "java/text/SimpleDateFormat" and n in ("<init>", "applyPattern")) for o, n, _d in calls)
        deep_reflection = any(o.startswith("java/lang/reflect/") and n in ("setAccessible", "trySetAccessible")
                              for o, n, _d in calls)
        for owner, name, desc, kind in rows:
            top = top_level(owner)
            if kind in ("M", "C") and top in lib_classes:
                continue  # provided by a library jar (e.g. javax.inject), not the JDK
            if kind == "C":
                pkg = owner.rpartition("/")[0]
                in_s, in_t = S.has_class(owner), T.has_class(owner)
                if in_s and not in_t:
                    add("BLOCKER", ("rm", top), f"removed: class {top.replace('/', '.')}" + (" (and nested)" if top != owner else ""),
                        f"exists in Java {source}, not in {target} -> NoClassDefFoundError", src)
                elif in_t and pkg not in T.exported and not in_s:
                    add("BLOCKER", ("enc", pkg), f"encapsulated JDK internal: {pkg.replace('/', '.')}",
                        "not exported -> IllegalAccessError (strong encapsulation, JDK 17); needs --add-exports/--add-opens or a replacement", src)
                elif not in_t and not in_s and owner.startswith(("sun/", "com/sun/", "jdk/internal/")) \
                        and (pkg in jdk_pkgs or pkg.startswith(("sun/", "jdk/internal/"))):
                    add("BLOCKER", ("gone", top), f"JDK internal not present in {target}: {top.replace('/', '.')}",
                        "internal API no longer present", src)
                elif in_t:
                    dep = T.deprecation(owner)
                    if dep and dep[0]:
                        dep_api[owner.replace("/", ".")].add(where(src))
                        if a.deprecations:
                            add("WARN", ("depc", owner), f"deprecated for removal: {owner.replace('/', '.')}",
                                f"forRemoval since {dep[1] or '?'}", src)
            elif kind == "M":
                if not T.has_class(owner):
                    continue
                d = T.find_member(owner, name, desc)
                if d is False:
                    if S.find_member(owner, name, desc):
                        add("BLOCKER", ("rmm", owner, name, desc), f"removed: {_member_label(owner, name, desc)}",
                            f"exists in Java {source}, not in {target} -> NoSuchMethodError/NoSuchFieldError", src)
                    continue
                decl = d or owner
                rule = UOE_RULES.get((decl, name, desc))
                if rule and target >= rule[0] > source:
                    add("BLOCKER", ("uoe", decl, name), f"{_member_label(decl, name, desc)} {rule[1]}", "", src)
                    continue
                if d:
                    dep = T.deprecation(d, name, desc)
                    if dep and dep[0]:
                        dep_api[_member_label(d, name, desc)].add(where(src))
                        if a.deprecations:
                            add("WARN", ("depm", d, name, desc), f"deprecated for removal: {_member_label(d, name, desc)}",
                                f"forRemoval since {dep[1] or '?'}", src)
                if (decl, name, desc) in DEFAULT_CHARSET and source < 18 <= target:
                    add("WARN", ("charset",), "default charset becomes UTF-8 (JEP 400, JDK 18)",
                        "these calls use the platform default; behaviour changes where production did not already "
                        "run with UTF-8 (e.g. containers without a locale used US-ASCII on 11). Pin a charset "
                        "or test with -Dfile.encoding=COMPAT", src)
            elif kind == "S":
                if uses_engine_by_name and re.fullmatch(r"(?i)nashorn|javascript|js|ecmascript", name) and source < 15 <= target:
                    add("BLOCKER", ("nashorn",), "Nashorn JavaScript engine removed (JEP 372, JDK 15)",
                        "getEngineByName(\"" + name + "\") returns null; add org.openjdk.nashorn:nashorn-core or migrate", src)
                elif uses_patterns and TIME_PATTERN.search(name) and source < 20 <= target:
                    add("WARN", ("cldr",), "time formats with AM/PM change (CLDR 42, JDK 20)",
                        f"pattern \"{name}\": formatted output uses a narrow no-break space before AM/PM; parsing "
                        "user-typed times can fail. Test, or run with -Djava.locale.providers=COMPAT", src)
                elif re.fullmatch(r"TLSv1(\.1)?", name):
                    add("WARN", ("tls",), "TLS 1.0/1.1 disabled by default",
                        "disabled in jdk.tls.disabledAlgorithms of current JDKs; connections using them fail", src)
                elif deep_reflection and re.match(r"(java|sun|jdk)\.", name) and source < 17 <= target:
                    add("WARN", ("deep",), "deep reflection into JDK classes",
                        f"setAccessible with JDK class names (e.g. {name}); under strong encapsulation (JDK 17) this "
                        "throws InaccessibleObjectException unless --add-opens is given", src)
            elif kind == "F" and source < 18 <= target:
                add("WARN", ("finalize",), "finalize() overrides (finalization deprecated for removal, JEP 421, JDK 18)",
                    "move to Cleaner or try-with-resources", src)
            elif kind == "T" and name.isdigit() and int(name) < target:
                add("BLOCKER", ("tool", owner), f"bytecode library too old: {owner.rsplit('/', 1)[0].replace('/', '.')} "
                    f"knows class files up to Java {name}",
                    f"fails on Java {target} class files ('Unsupported class file major version {target + 44}') when it "
                    f"reads JDK {target} classes or code compiled for {target}; upgrade the library that contains it", src)
            elif kind == "V" and name.isdigit() and int(name) - 44 > target:
                add("BLOCKER", ("major", src), f"compiled for Java {int(name) - 44}, newer than {target}",
                    "UnsupportedClassVersionError", src)

    if dep_api and not a.deprecations:
        users = set().union(*dep_api.values())
        top = sorted(dep_api, key=lambda k: -len(dep_api[k]))[:3]
        findings[("dep",)] = ["WARN", f"{plural(len(dep_api), 'API')} deprecated for removal in {target} are used",
                              "still work on " + str(target) + "; most used: " + ", ".join(top)
                              + " (--deprecations lists each one)", {u: {u} for u in users}]

    # library minimums and the build itself
    for ga, (minv, jdk, why) in MIN_VERSIONS.items():
        if target >= jdk:
            for (v,) in conn.execute("SELECT DISTINCT version FROM comps WHERE ga=?", (ga,)):
                if _vtuple(v) < _vtuple(minv):
                    f = findings.setdefault(("min", ga, v), ["BLOCKER", f"{ga}:{v} too old for JDK {target}",
                                                             f"needs >= {minv} ({why})", defaultdict(set)])
                    for (pj,) in conn.execute("""SELECT DISTINCT p.project FROM nodes n JOIN comps c ON c.id=n.comp
                                                 JOIN pcs p ON p.id=n.pc WHERE c.ga=? AND c.version=?""", (ga, v)):
                        f[3][pj].add(pj)
    gradle_v = next((i.get("gradle") for i in pinfo.values() if i.get("gradle")), None)
    gradle_jvm = next((i.get("gradleJvm") for i in pinfo.values() if i.get("gradleJvm")), None)
    if gradle_v and target in GRADLE_MIN:
        run_min, tc_min = GRADLE_MIN[target]
        if _vtuple(gradle_v) < _vtuple(run_min):
            sev = "BLOCKER" if _vtuple(gradle_v) < _vtuple(tc_min) else "WARN"
            findings[("gradle",)] = [sev, f"Gradle {gradle_v} cannot run on JDK {target}",
                                     f"needs >= {run_min} to run on {target}, >= {tc_min} to use {target} as a toolchain",
                                     {"build": {"gradle"}}]

    # JVM flags in deployment and build files, tested against the target JVM
    proj = Path(meta(conn, "project_dir", "."))
    java = str(home / "bin" / "java")
    flags = scan_jvm_flags(proj) if not a.no_flags else {}
    flag_results = {}
    for flag in sorted(flags):
        st, msg = test_flag(java, flag)
        flag_results[flag] = (st, msg)
        if st in ("fail", "warn"):
            locs = flags[flag]
            findings[("flag", flag)] = ["BLOCKER" if st == "fail" else "WARN",
                                        f"JVM option {flag}: " + ("JVM will not start" if st == "fail" else "ignored/deprecated"),
                                        msg, {f"{p}:{ln}": {p} for p, ln in locs}]

    # --- report
    def wh(f):
        items = sorted(f[3])
        return ", ".join(items[:a.show]) + (f" (+{len(items) - a.show})" if len(items) > a.show else "")

    toolchains = defaultdict(list)
    for pj, i in sorted(pinfo.items()):
        if i.get("release") or i.get("toolchain") or i.get("target"):
            lvl = (f"release {i['release']}" if i.get("release") else
                   f"toolchain {i['toolchain']}" if i.get("toolchain") else f"target {i['target']}")
            toolchains[lvl].append(pj)
    nmods = conn.execute("SELECT count(DISTINCT project) FROM srcfiles WHERE kind='class'").fetchone()[0]
    njars = sum(1 for s in by_src if s.startswith("j:"))
    print(f"JVM upgrade {source} -> {target}: checked {plural(nmods, 'compiled module')}, {plural(njars, 'jar')}, "
          f"{plural(len(flags), 'JVM option')}")
    print(f"build: Gradle {gradle_v or '?'} (running on Java {gradle_jvm or '?'}); "
          + ("; ".join(f"{k}: {len(v)} modules" for k, v in sorted(toolchains.items())) or "no explicit Java level set"))
    uncompiled = [pj for (pj,) in conn.execute("""SELECT DISTINCT project FROM sourcesets WHERE kind='src'
                                                  AND project NOT IN (SELECT project FROM srcfiles WHERE kind='class')
                                                  AND project IN (SELECT project FROM srcfiles WHERE kind='src')""")]
    if uncompiled:
        print(f"not compiled (their own code is not checked): {', '.join(sorted(uncompiled)[:8])}"
              + (" ..." if len(uncompiled) > 8 else ""))
    for sev in ("BLOCKER", "WARN"):
        items = sorted((f for f in findings.values() if f[0] == sev), key=lambda f: (-len(f[3]), f[1]))
        print(f"{sev}S: {len(items)}" + ("  (will fail to link, start or run)" if sev == "BLOCKER" else "  (behaviour changes / on the way out)"))
        lines = []
        for f in items:
            lines.append(f"  {f[1]}  — in {wh(f)}")
            if f[2] and a.why:
                lines.append(f"      {f[2]}")
        emit(lines, a.limit)
    if not a.why and findings:
        print("(--why for explanations and fixes; --show N for more locations)")
    apps = [r[0] for r in conn.execute("SELECT path FROM projects WHERE kind='app' ORDER BY path")]
    if apps:
        print("per app (runtime classpath + its modules):")
        for app in apps:
            pc = conn.execute("SELECT id FROM pcs WHERE project=? AND config='runtimeClasspath'", (app,)).fetchone()
            if not pc:
                continue
            closure = {app} | {cid[8:] for (cid,) in conn.execute(
                "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.kind='p'", (pc[0],))}
            jars = {short(cid) for (cid,) in conn.execute(
                "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.kind='m'", (pc[0],))}
            cnt = defaultdict(int)
            for f in findings.values():
                if any(w in closure or w in jars for w in f[3]) or f[1].startswith(("JVM option", "Gradle")):
                    cnt[f[0]] += 1
            print(f"  {app}: {cnt['BLOCKER']} blockers, {cnt['WARN']} warnings")
    print("not covered: GC/JIT tuning, performance, and behaviour changes without a code signal — run your tests on "
          f"JDK {target}. For code rewrites see OpenRewrite's Java {target} migration recipes.")


# ---------------------------------------------------------------- ClassNotFoundException / NoClassDefFoundError

# JDK modules removed in Java 11 (JEP 320) and later, with their usual replacements
JDK_REMOVED_HINTS = {
    "javax/xml/bind": "jakarta.xml.bind:jakarta.xml.bind-api plus an implementation (org.glassfish.jaxb:jaxb-runtime); "
                      "or javax.xml.bind:jaxb-api 2.3.x to keep the javax names",
    "javax/activation": "jakarta.activation:jakarta.activation-api (or com.sun.activation:javax.activation for javax names)",
    "javax/annotation": "jakarta.annotation:jakarta.annotation-api (or javax.annotation:javax.annotation-api 1.3.x)",
    "javax/xml/ws": "jakarta.xml.ws:jakarta.xml.ws-api plus com.sun.xml.ws:jaxws-rt",
    "javax/jws": "jakarta.jws:jakarta.jws-api",
    "javax/xml/soap": "jakarta.xml.soap:jakarta.xml.soap-api plus com.sun.xml.messaging.saaj:saaj-impl",
    "javax/transaction": "jakarta.transaction:jakarta.transaction-api",
    "org/omg": "org.glassfish.corba:glassfish-corba-orb",
    "javax/rmi/CORBA": "org.glassfish.corba:glassfish-corba-orb",
    "jdk/nashorn": "org.openjdk.nashorn:nashorn-core",
    "java/util/jar/Pack200": "org.apache.commons:commons-compress (Pack200 support)",
}
_JDK_PKGS = None


def jdk_packages():
    """Package names (a/b/c) of the running JDK, from its jmods; None when it ships none."""
    global _JDK_PKGS
    if _JDK_PKGS is None:
        pk = set()
        for m in jdk_jmods():
            try:
                with zipfile.ZipFile(m) as z:
                    pk.update(n[8:].rpartition("/")[0] for n in z.namelist()
                              if n.startswith("classes/") and n.endswith(".class"))
            except (OSError, zipfile.BadZipFile):
                pass
        _JDK_PKGS = pk
    return _JDK_PKGS or None


def jdk_class_history(internal):
    """Releases whose public API contains the class, from the running JDK's ct.sym + jmods."""
    home = find_jdk()
    out = set()
    if not home:
        return out
    hv = jdk_home_version(home)
    ct = home / "lib" / "ct.sym"
    if ct.exists():
        with zipfile.ZipFile(ct) as z:
            for n in z.namelist():
                if n.endswith("/" + internal + ".sig"):
                    out.update(REL_CHARS.index(c) for c in n.split("/", 1)[0] if c in REL_CHARS)
    for m in jdk_jmods():
        try:
            with zipfile.ZipFile(m) as z:
                z.getinfo("classes/" + internal + ".class")
                out.add(hv)
                break
        except (KeyError, OSError, zipfile.BadZipFile):
            continue
    return out


def _parse_class_name(text):
    """'java.lang.NoClassDefFoundError: com/foo/Bar$1' / 'Caused by: ... com.foo.Bar' -> 'com/foo/Bar$1'."""
    text = text.strip()
    if ":" in text and re.search(r"(Exception|Error)\s*:", text):
        text = re.split(r"(?:Exception|Error)\s*:\s*", text)[-1]
    m = re.search(r"([A-Za-z_$][\w$]*(?:[./][A-Za-z_$][\w$]*)+)", text)
    if not m:
        die(f"no class name in '{text}'")
    return m.group(1).replace(".", "/")


def _closure(conn, pc):
    root, = conn.execute("SELECT c.cid FROM pcs p JOIN comps c ON c.id=p.root WHERE p.id=?", (pc,)).fetchone()
    return {root[8:]} | {cid[8:] for (cid,) in conn.execute(
        "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.pc=? AND c.kind='p'", (pc,))}


def _pc_jars(conn, pc):
    return conn.execute("""SELECT c.ga, c.version, a.file FROM nodes n JOIN comps c ON c.id=n.comp
                           JOIN artifacts a ON a.ga=c.ga AND a.version=c.version
                           WHERE n.pc=? AND c.kind='m' AND a.file LIKE '%.jar'""", (pc,)).fetchall()


def _module_classes(conn, modules):
    """Top-level internal names of the build's own classes (compiled, else from source file names)."""
    out = {}
    for pj in modules:
        dirs = [d for (d,) in conn.execute("SELECT dir FROM sourcesets WHERE project=? AND kind='classes'", (pj,))]
        for path, kind, pkg in conn.execute("SELECT path, kind, package FROM srcfiles WHERE project=? AND kind IN ('class','src')",
                                            (pj,)):
            if kind == "class":
                d = next((d for d in dirs if path.startswith(d + os.sep)), None)
                if d:
                    out[top_level(path[len(d) + 1:-6].replace(os.sep, "/"))] = pj
            elif pkg:
                out[pkg.replace(".", "/") + "/" + Path(path).stem] = pj
    return out


def _referrers(conn, pc, modules, jars, only=None):
    """{top-level internal name: {referrer}} for what jars and modules on this classpath reference."""
    refs = defaultdict(set)
    _jar_index_many(conn, jars)
    for ga, v, f in jars:
        q = "SELECT owner FROM jarrefs WHERE ga=? AND version=? AND kind='R'" + (" AND owner GLOB ?" if only else "")
        for (o,) in conn.execute(q, (ga, v, only + "*") if only else (ga, v)):
            refs[top_level(o)].add(f"{ga.split(':')[1]}:{v}")
    for pj in modules:
        for fqn, via in conn.execute(f"""SELECT DISTINCT i.fqn, i.via FROM imports i JOIN srcfiles f ON f.id=i.file {EVID_JOIN}
                                         WHERE f.project=? AND f.sset='main' AND i.via IN ('import','class','abi')""", (pj,)):
            pkg, cls = _split_fqn(fqn, 0, 0)
            if cls is None:
                continue  # wildcard package import
            top = pkg.replace(".", "/") + "/" + cls
            if not only or top.startswith(only):
                refs[top].add(pj)
    return refs


def _target_classpaths(conn, project, config):
    if project:
        return conn.execute("SELECT id, project FROM pcs WHERE project=? AND config=? AND error IS NULL",
                            (project, config)).fetchall()
    apps = [r[0] for r in conn.execute("SELECT path FROM projects WHERE kind='app' ORDER BY path")] or \
           [r[0] for r in conn.execute("SELECT path FROM projects ORDER BY path")]
    return [r for app in apps for r in conn.execute(
        "SELECT id, project FROM pcs WHERE project=? AND config=? AND error IS NULL", (app, config))]


def cmd_missing(a, conn):
    """Predict ClassNotFoundException/NoClassDefFoundError: classes referenced on a runtime classpath
    that nothing on it provides."""
    project = norm_project(conn, a.project) if a.project else None
    cfg = a.config or "runtimeClasspath"
    jdk = jdk_packages()
    all_classes = defaultdict(set)  # top-level -> {(ga, version)}
    for p, sname, g, v in conn.execute("SELECT pkg, simple, ga, version FROM classes"):
        all_classes[p.replace(".", "/") + "/" + sname].add((g, v))
    for pc, pj in _target_classpaths(conn, project, cfg):
        mods = _closure(conn, pc)
        jars = _pc_jars(conn, pc)
        on_cp = {(g, v) for g, v, _f in jars}
        provided = set(_module_classes(conn, mods))
        for top, prov in all_classes.items():
            if prov & on_cp:
                provided.add(top)
        refs = _referrers(conn, pc, mods, jars)
        missing = {}
        for top, who in refs.items():
            pkg = top.rpartition("/")[0]
            if top in provided or (jdk is not None and pkg in jdk) or (jdk is None and top.startswith(JDKISH)):
                continue
            missing[top] = who
        groups = defaultdict(lambda: [set(), set(), set()])  # pkg -> classes, referrers, alternatives
        mismatch = []
        cp_ga = {g: v for g, v in on_cp}
        for top, who in missing.items():
            pkg = top.rpartition("/")[0]
            alts = all_classes.get(top, set())
            same_ga = [(g, v) for g, v in alts if g in cp_ga]
            if same_ga:
                mismatch.append((top, who, cp_ga[same_ga[0][0]], same_ga))
                continue
            groups[pkg][0].add(top)
            groups[pkg][1].update(who)
            groups[pkg][2].update(f"{g}:{v}" for g, v in alts)
        print(f"{pj} [{ab(cfg)}]: {plural(len(missing), 'referenced class')} not provided")
        lines = []
        for top, who, have, alts in sorted(mismatch):
            lines.append(f"  MISMATCH {top.replace('/', '.')}: {alts[0][0]} resolved {have} lacks it "
                         f"(in {', '.join(sorted(v for _g, v in alts))}) — used by {', '.join(sorted(who)[:3])}")
        by_jar = defaultdict(lambda: [set(), set()])  # library -> missing packages, alternatives in build
        for pkg, (cls, who, alts) in sorted(groups.items(), key=lambda kv: (not any(w.startswith(":") for w in kv[1][1]), kv[0])):
            mod_users = sorted(w for w in who if w.startswith(":"))
            if not mod_users and not a.all:
                for w in who:
                    by_jar[w][0].add(pkg.replace("/", "."))
                    by_jar[w][1].update(alts)
                continue
            sev = "ERROR" if mod_users else "optional?"
            hint = next((h for p, h in JDK_REMOVED_HINTS.items() if pkg.startswith(p) or (pkg + "/").startswith(p + "/")), None)
            fix = (f"; provided by {', '.join(sorted(alts)[:2])} elsewhere in the build" if alts else
                   f"; was in the JDK — add {hint}" if hint else
                   "" if not mod_users else "; no jar in the build provides it")
            lines.append(f"  {sev} {pkg.replace('/', '.')} ({plural(len(cls), 'class')}) — used by "
                         f"{', '.join((mod_users + sorted(w for w in who if not w.startswith(':')))[:a.show])}"
                         + (f" (+{len(who) - a.show})" if len(who) > a.show else "") + fix)
        for jar, (pkgs, alts) in sorted(by_jar.items(), key=lambda kv: (not kv[1][1], -len(kv[1][0]))):
            ps = sorted(pkgs, key=len)
            lines.append(f"  optional? {jar} references {plural(len(ps), 'missing package')}: {', '.join(ps[:3])}"
                         + (" ..." if len(ps) > 3 else "")
                         + (f" — provided elsewhere in the build by {', '.join(sorted(alts)[:2])}" if alts else ""))
        emit(lines, a.limit)
    print("ERROR = your code needs it: NoClassDefFoundError when that code runs. optional? = only libraries reference it; "
          "normal for optional integrations unless that feature is used. Details: depkb explain <Class>")


def cmd_explain(a, conn):
    """Diagnose a ClassNotFoundException / NoClassDefFoundError for one class."""
    internal = _parse_class_name(" ".join(a.name))
    top = top_level(internal)
    pkg = top.rpartition("/")[0]
    dotted = internal.replace("/", ".")
    print(dotted)
    project = norm_project(conn, a.project) if a.project else None
    cfg = a.config or "runtimeClasspath"

    # 1. JDK?
    jdk = jdk_packages() or set()
    hist = jdk_class_history(top) if (pkg in jdk or top.startswith(JDKISH)) else set()
    hv = jdk_home_version(find_jdk()) if find_jdk() else None
    hint = next((h for p, h in JDK_REMOVED_HINTS.items() if top.startswith(p)), None)
    internal_pkg = False
    if hist and hist == {hv}:
        try:
            internal_pkg = pkg not in JdkModel(hv, find_jdk()).exported
        except ValueError:
            pass
    if internal_pkg:
        print(f"JDK: JDK-internal class (package {pkg.replace('/', '.')} is not exported): on Java 17+ code outside the "
              "JDK cannot use it without --add-exports/--add-opens; prefer a public replacement")
    elif hist:
        if hv in hist:
            print(f"JDK: part of the JDK (present in Java {min(hist)}–{hv})")
        else:
            print(f"JDK: removed from the JDK after Java {max(hist)} (present {min(hist)}–{max(hist)})"
                  + (f"; replacement: {hint}" if hint else ""))
    elif pkg in jdk:
        print(f"JDK: JDK-internal package (not public API); not loadable by name on Java {hv} without --add-opens/--add-exports")

    # 2. who provides it
    provs = conn.execute("""SELECT DISTINCT c.ga, c.version FROM classes c WHERE c.pkg=? AND c.simple=?""",
                         (pkg.replace("/", "."), top.rpartition("/")[2])).fetchall()
    provs.sort(key=lambda r: (r[0], vkey(r[1])))
    own = _module_classes(conn, [r[0] for r in conn.execute("SELECT path FROM projects")]).get(top)
    by_ga = defaultdict(list)
    for g, v in provs:
        by_ga[g].append(v)
    if own:
        print(f"provided by: module {own}")
    if by_ga:
        print("provided by: " + "; ".join(f"{g} ({', '.join(vs)})" for g, vs in by_ga.items()))
    if not own and not by_ga and not hist:
        alt = None
        if top.startswith("javax/"):
            jk = "jakarta/" + top[len("javax/"):]
            if conn.execute("SELECT 1 FROM classes WHERE pkg=? AND simple=?",
                            (jk.rpartition("/")[0].replace("/", "."), jk.rpartition("/")[2])).fetchone():
                alt = jk.replace("/", ".")
        print("provided by: nothing in this build" + (f"; the build has {alt} (javax -> jakarta rename)" if alt else ""))

    # 3. per classpath diagnosis
    any_pc = False
    for pc, pj in _target_classpaths(conn, project, cfg):
        any_pc = True
        jars = _pc_jars(conn, pc)
        cp = {g: v for g, v, _f in jars}
        mods = _closure(conn, pc)
        print(f"{pj} [{ab(cfg)}]:")
        on = [f"{g}:{cp[g]}" for g in by_ga if g in cp and cp[g] in by_ga[g]]
        wrong = [(g, cp[g]) for g in by_ga if g in cp and cp[g] not in by_ga[g]]
        if own and own in mods:
            on.append(f"module {own}")
        if on:
            print(f"  on the classpath: {', '.join(on)}.")
            print("  so the build is not missing it. Likely causes: excluded from the packaged artifact (fat jar / "
                  "shading), 'provided' by an app server that lacks it, a different classloader (plugins, OSGi, "
                  "Spring Boot devtools), or a NoClassDefFoundError after an earlier failure in its static "
                  "initializer — look for the first error in the log.")
        elif wrong:
            for g, v in wrong:
                print(f"  VERSION MISMATCH: {g} resolved to {v}, which does not contain it (versions that do: "
                      f"{', '.join(by_ga[g])}).")
                n = _node(conn, pj, cfg, g)
                if n:
                    note = override_note(conn, n[0], n[1], n[2], n[4], n[5])
                    if note:
                        print(f"  {g}: {note}")
                print(f"  fix: align on a version with the class (e.g. constraint/force {g}:{by_ga[g][-1]}), or "
                      f"upgrade the code that still uses it (`depkb compat {g.split(':')[1]}`).")
        else:
            elsewhere = conn.execute(f"""SELECT DISTINCT p.config FROM pcs p JOIN nodes n ON n.pc=p.id
                                         JOIN comps c ON c.id=n.comp WHERE p.project=? AND c.ga IN
                                         ({','.join('?' * len(by_ga)) or "''"})""", (pj, *by_ga)).fetchall()
            if elsewhere:
                print(f"  NOT on {cfg}, but on {', '.join(sorted(c for (c,) in elsewhere))} of {pj}: probably declared "
                      "compileOnly (or test-only). fix: declare it implementation/runtimeOnly.")
            elif by_ga:
                g = next(iter(by_ga))
                alias = conn.execute("SELECT accessor FROM catalog WHERE ga=?", (g,)).fetchone()
                users = sorted({p for (p,) in conn.execute("""SELECT DISTINCT p.project FROM pcs p JOIN nodes n ON n.pc=p.id
                                                             JOIN comps c ON c.id=n.comp WHERE c.ga=?""", (g,))})
                print(f"  NOT on the classpath. {g} is used by {', '.join(users[:4])}. fix: add "
                      f"runtimeOnly/implementation {alias[0] if alias else repr(g + ':' + by_ga[g][-1])} where it is needed.")
            elif hist and hv not in hist:
                print(f"  NOT available: removed from the JDK. fix: add {hint or 'the standalone artifact for it'}.")
            elif not hist:
                print("  NOT on the classpath and no jar in the build provides it. fix: find the artifact that ships "
                      "it (check for renames, e.g. javax -> jakarta) and add it.")
        refs = _referrers(conn, pc, mods, jars, only=top)
        who = sorted(refs.get(top, set()))
        if who:
            print(f"  referenced by: {', '.join(who[:6])}" + (f" (+{len(who) - 6})" if len(who) > 6 else ""))
    if not any_pc:
        print(f"no {cfg} to check" + (f" for {project}" if project else ""))


def cmd_sql(a, conn):
    cur = conn.execute(a.query)
    conn.commit()
    cols = [d[0] for d in cur.description or []]
    if cols:
        print("\t".join(cols))
    emit(("\t".join("" if x is None else str(x) for x in r) for r in cur.fetchall()), a.limit)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="depkb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="SQLite file (default: nearest .depkb/depkb.db, or $DEPKB_DB)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="answer from the KB as is (default: re-export changed modules first; or DEPKB_AUTO=0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, *args):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        p.add_argument("--limit", type=int, default=40, help="max lines (0 = all)")
        for spec in args:
            spec(p)
        return p

    proj_opt = lambda p: p.add_argument("-p", "--project")
    cfg_opt = lambda p: p.add_argument("-c", "--config", help="e.g. runtimeClasspath")
    mod_arg = lambda p: p.add_argument("module", help="group:artifact, artifact, or substring")

    def build_opts(p):
        p.add_argument("-d", "--project-dir", default=".")
        p.add_argument("--configs", default=DEFAULT_CONFIGS, help="comma list or 'all'")
        p.add_argument("--force", action="store_true")
        p.add_argument("--offline", action="store_true")
        p.add_argument("--no-parallel", action="store_true")
        p.add_argument("--no-jars", action="store_true", help="skip jar/POM indexing")
        p.add_argument("--no-classes", action="store_true", help="index packages but not class names")
        p.add_argument("--gradle-args", help="extra Gradle arguments, quoted")

    add("build", None, "export from Gradle and (re)index", build_opts)
    ig = add("ingest", None, "index an existing export dir (e.g. from CI)", build_opts,
             lambda p: p.add_argument("export_dir"))
    ig.set_defaults(fn=None)
    add("summary", cmd_summary, "overview of the knowledge base")
    add("status", cmd_status, "freshness and failed configurations")
    add("version", cmd_version, "resolved versions of a module", mod_arg)
    add("who-uses", cmd_who_uses, "projects that use a module", mod_arg, cfg_opt)
    add("why", cmd_why, "why a module is present at its version", mod_arg, proj_opt, cfg_opt,
        lambda p: p.add_argument("--paths", type=int, default=3))
    add("deps", cmd_deps, "dependencies of a project", lambda p: p.add_argument("project"), cfg_opt,
        lambda p: p.add_argument("--all", action="store_true", help="include transitive"))
    add("pkg", cmd_pkg, "which jar provides a package or class", lambda p: p.add_argument("name"))
    add("info", cmd_info, "module facts: license, packages, JPMS, catalog alias", mod_arg)
    add("conflicts", cmd_conflicts, "modules whose version was chosen by conflict resolution", proj_opt, cfg_opt,
        lambda p: p.add_argument("--all", action="store_true", help="any non-default selection reason"))
    add("drift", cmd_drift, "modules resolved to different versions across projects", cfg_opt)
    add("split-packages", cmd_split, "packages provided by more than one jar", proj_opt, cfg_opt)
    add("unresolved", cmd_unresolved, "dependencies Gradle could not resolve")
    add("licenses", cmd_licenses, "modules grouped by license",
        lambda p: p.add_argument("filter", nargs="?"), lambda p: p.add_argument("--show", type=int, default=6))
    add("catalog", cmd_catalog, "version catalog entries", lambda p: p.add_argument("query", nargs="?"),
        lambda p: p.add_argument("--unused", action="store_true"),
        lambda p: p.add_argument("--missing", action="store_true", help="direct deps not in the catalog"))
    add("diff", cmd_diff, "compare two projects", lambda p: p.add_argument("a"), lambda p: p.add_argument("b"), cfg_opt)
    add("outdated", cmd_outdated, "newer releases in Maven repos (network, cached 24h)", proj_opt,
        lambda p: p.add_argument("--repo", action="append", help="repo base URL, repeatable"),
        lambda p: p.add_argument("--direct", action="store_true"),
        lambda p: p.add_argument("--refresh", action="store_true"))
    add("vulns", cmd_vulns, "known vulnerabilities from OSV (network, cached 24h)", proj_opt,
        lambda p: p.add_argument("--refresh", action="store_true"))
    add("sql", cmd_sql, "run raw SQL against the KB", lambda p: p.add_argument("query"))
    files_opt = lambda p: p.add_argument("--files", action="store_true", help="list file:line of each import")
    add("usage", cmd_usage, "per module: declared/transitive, effective version, override, imported?",
        lambda p: p.add_argument("module", help="library (group:artifact, artifact) or module path like :common"),
        files_opt)
    add("runtime", cmd_runtime, "a library as it runs in each app module (runtimeClasspath)", mod_arg, proj_opt, files_opt)
    add("imports", cmd_imports, "what a module's code imports, by dependency", lambda p: p.add_argument("project"),
        lambda p: p.add_argument("--unknown", action="store_true", help="list unresolved imports"))
    add("users", cmd_users, "source files importing a class/package", lambda p: p.add_argument("name"), proj_opt,
        files_opt)
    add("audit", cmd_audit, "undeclared/unused/broken dependencies from imports", proj_opt)
    add("api", cmd_api, "public API of a class from the exact version on the classpath (with docs if sources exist)",
        lambda p: p.add_argument("name", help="Class, Outer.Inner, or fully qualified"), proj_opt,
        lambda p: p.add_argument("--grep", help="only members containing this text"),
        lambda p: p.add_argument("--public-only", action="store_true"))
    add("compat", cmd_compat, "binary compatibility of overridden versions for their callers",
        lambda p: p.add_argument("module", nargs="?", help="only this library"), proj_opt, cfg_opt,
        lambda p: p.add_argument("--verbose", action="store_true", help="also list overrides without breaks"))
    add("impact", cmd_impact, "what breaks if a library moves to another version", mod_arg,
        lambda p: p.add_argument("version"), proj_opt, cfg_opt,
        lambda p: p.add_argument("--repo", action="append", help="repo base URL to fetch the jar, repeatable"))
    add("jvm", cmd_jvm, "impact of a Java upgrade: removed/encapsulated APIs, runtime breaks, tooling, JVM flags",
        lambda p: p.add_argument("version", type=int, help="target Java version, e.g. 21"),
        lambda p: p.add_argument("--from", dest="source", type=int, help="current Java version (default: detected)"),
        lambda p: p.add_argument("--jdk", help="path to the target JDK (default: JAVA_HOME / java on PATH)"),
        lambda p: p.add_argument("--why", action="store_true", help="explain each finding and how to fix it"),
        lambda p: p.add_argument("--show", type=int, default=4, help="locations listed per finding"),
        lambda p: p.add_argument("--no-flags", action="store_true", help="skip scanning/testing JVM options"),
        lambda p: p.add_argument("--deprecations", action="store_true", help="list each deprecated-for-removal API"))
    add("missing", cmd_missing, "predict ClassNotFound/NoClassDefFound: referenced classes nothing on the classpath provides",
        proj_opt, cfg_opt, lambda p: p.add_argument("--show", type=int, default=4),
        lambda p: p.add_argument("--all", action="store_true", help="list library-only gaps per package"))
    add("explain", cmd_explain, "diagnose a ClassNotFoundException/NoClassDefFoundError (paste the class or the error line)",
        lambda p: p.add_argument("name", nargs="+", help="class name, or the exception line from the log"), proj_opt, cfg_opt)
    add("modules", cmd_modules, "module graph: dependencies, dependents, impact", lambda p: p.add_argument("project", nargs="?"))

    a = ap.parse_args(argv)
    if a.cmd == "build":
        return cmd_build(a)
    if a.cmd == "ingest":
        return cmd_ingest(a)
    conn = connect(find_db(a.db))
    if a.cmd not in ("status", "sql") and not a.no_refresh:
        refresh(conn, sources=a.cmd in SOURCE_CMDS)
    try:
        a.fn(a, conn)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
