# dependency-helper: Specification

A token-efficient dependency knowledge base for Gradle Java projects, delivered as a single fat JAR.
Reference implementation: `../gradle-depkb/gradle-depkb/` (Python prototype — use as the ground truth for behaviour and edge cases).

---

## 1. Distribution

| Item | Value |
|---|---|
| Artifact | `depkb.jar` (fat JAR, all dependencies shaded) |
| Runtime | JVM 17+ (present in every Gradle Java project) |
| Install | Drop `depkb.jar` anywhere; no further setup |
| Invocation | `java -jar depkb.jar <command> [args]` |
| Alias | `alias depkb='java -jar /path/to/depkb.jar'` |

---

## 2. Knowledge base

- Stored in `<repo-root>/.depkb/depkb.db` (SQLite)
- `.depkb/` is gitignored on creation
- One DB per Gradle project root
- DB location overridable via `--db <path>` or `DEPKB_DB` env var
- Schema versioned; on schema change, `build` rebuilds automatically

---

## 3. Indexing workflow

### 3.1 First index

```
java -jar depkb.jar build
```

1. Runs `./gradlew -q -I depkb.init.gradle depKbExport -Ddepkb.out=<tmp>` to export the resolved dependency graph as JSON (one file per module)
2. Ingests the JSON: writes resolved graph, declared deps, artifacts to SQLite
3. Scans all jars on classpaths: packages, classes, JPMS name, service providers, POM metadata
4. Scans source files / compiled classes: imports, bytecode refs, member refs, reflective string hints, Spring/ServiceLoader registration files
5. Resolves imports to the dependency that provides them (links table)
6. Reports: `indexed N modules, M libraries, K source files in Xs`

### 3.2 Auto-refresh

Every query checks for staleness before answering:
- `stat` every tracked build file; hash only when `stat` changed
- Changed modules → re-export only those modules + their dependents
- Changed sources/classes → re-scan only changed files, re-link
- Nothing changed → answer immediately (~100–400 ms)

Disable with `--no-refresh` or `DEPKB_AUTO=0`.

### 3.3 Incremental targets

| Change | Max time |
|---|---|
| Nothing changed | < 500 ms |
| One module's build file | < 10 s |
| Root settings / version catalog | < 20 s (full re-export) |
| One source file edited | < 1 s |

### 3.4 Gradle init script

`depkb.init.gradle` (or `depkb.init.gradle.kts`) ships inside the JAR and is extracted to a temp dir at runtime. It uses Gradle's `ResolutionResult` API — it does not compile anything.

---

## 4. Commands

All commands accept `--limit N` (default 40; `0` = all) and `--db <path>`.
`<lib>` accepts `group:artifact`, artifact name, or substring. `<module>` is `:app`, `app`, or a substring.

### 4.1 Indexing

| Command | Description |
|---|---|
| `build [--offline] [--force] [--configs a,b] [--no-classes]` | Full export + index (no-op when nothing changed) |
| `status` | Show KB age, staleness, and failed configs |
| `summary` | One-screen overview: module count, conflict count, top libraries |
| `ingest <export-dir>` | Load a CI-produced export without running Gradle locally |

### 4.2 Dependency queries

| Command | Question answered |
|---|---|
| `usage <lib>` | Per module: bucket (`api`/`implementation`/transitive), effective version, override reason, code usage count. `--files` for file-level detail |
| `runtime <lib> [-p app]` | Version each deployable app actually runs; who declared it; does it link (binary check) |
| `why <lib> [-p module] [-c config]` | Why that version? Requesters, constraints, shortest paths |
| `who-uses <lib>` | All modules with it on any classpath |
| `version <lib>` | All versions in use across modules |
| `drift` | Libraries where different modules resolve different versions |
| `conflicts [--all]` | Libraries where Gradle applied conflict resolution |
| `unresolved` | Dependency requests that failed to resolve |

### 4.3 Code analysis

| Command | Question answered |
|---|---|
| `imports <module> [--unknown]` | What a module's code imports, grouped by the dependency that provides it |
| `users <Class\|pkg> [-p module] [--files]` | Source files that import a class or package |
| `pkg <Class\|pkg>` | Which jar provides a class or package |
| `audit [-p module]` | `UNDECLARED` / `UNUSED?` / `NOT_ON_CP` / `API` / `IMPL` findings |
| `api <Class> [-p module] [--grep text] [--public-only]` | Real API of a class in the version on that classpath |

### 4.4 Safety checks

| Command | Question answered |
|---|---|
| `compat [lib] [-p app] [--verbose]` | Callers that break with an overridden version (binary linkage check) |
| `impact <lib> <version> [-p app] [--repo URL]` | Would upgrading to that version break any caller or lose transitive dependencies? Fetches the jar if needed |
| `explain <Class\|log-line> [-p app]` | Diagnose a `ClassNotFoundException` / `NoClassDefFoundError` |
| `missing [-p app] [--all]` | Predict runtime class-not-found: classes referenced but nothing on the runtime classpath provides |
| `jvm <target> [--from N] [--jdk PATH] [--why] [--deprecations]` | Java upgrade blockers and warnings across modules, jars, and JVM config files |

### 4.5 Metadata

| Command | Question answered |
|---|---|
| `info <lib>` | POM description, URL, licenses, JPMS name, class count, catalog alias |
| `licenses [filter]` | All licenses across the dependency graph |
| `catalog [text] [--unused\|--missing]` | Version catalog entries; find the alias to use when adding a dependency |
| `modules [module]` | Module graph; blast radius of a change to a module |
| `deps <module> [--all]` | Dependencies of a module |
| `diff <snapshot-a> <snapshot-b>` | Compare two KB snapshots (before/after a change) |
| `outdated [--repo URL] [--direct]` | Libraries with newer versions available on Maven Central |
| `vulns` | CVE / vulnerability data from OSV for libraries in the graph |
| `split-packages` | Packages provided by more than one jar (common source of runtime surprises) |
| `sql "<query>"` | Raw SQLite query for ad-hoc investigation |

---

## 5. Output format

### 5.1 Token efficiency

Every command produces **compact output by default**: facts only, no headers, no repeating the question back. Agents get signal, not noise.

```
# usage jackson-databind
:app         2.15.3  implementation  used main:4f
:service     2.15.3  transitive      used main:1f  ! UNDECLARED: used but only transitive: declare it
:common      2.15.3  api             not used       ! UNUSED?: declared but never referenced
```

### 5.2 Output modes

Configurable via `--output <mode>` or `DEPKB_OUTPUT`:

| Mode | Description |
|---|---|
| `compact` (default) | One line per finding; numbers instead of file lists |
| `full` | Includes file paths, line numbers, dependency chain detail |
| `json` | Machine-readable; useful for CI checks or further processing |

### 5.3 Finding codes (machine-stable, safe to grep)

| Code | Meaning |
|---|---|
| `UNDECLARED` | Code uses it, but it is only transitive — declare it |
| `NOT_ON_CP` | Code uses it, but it is not on the compile classpath |
| `UNUSED?` | Declared but no code references it (verify before removing) |
| `API` | Library appears in public signatures — should be `api` not `implementation` |
| `IMPL` | Library does not appear in public signatures — could be `implementation` not `api` |
| `BREAKS` | Binary-incompatible: a caller references a class/method/field missing from the resolved version |
| `LOST` | A transitive dependency leaves the classpath and its classes are still referenced |
| `MISMATCH` | Wrong version resolved; class exists in a different version |
| `ERROR` | Your code references a class nothing on the runtime classpath provides |

---

## 6. Agent integration

### 6.1 Claude Code skill

Ship `skills/gradle-deps/SKILL.md` — a one-paragraph description that sits in context until a dependency question arises, then loads the full command reference. Copy to `<repo>/.claude/skills/gradle-deps/SKILL.md`.

### 6.2 Claude Code hooks (optional)

Ship `hooks/depkb_hook` (compiled into the JAR, extracted on demand). Three hooks:

| Hook | Behaviour |
|---|---|
| `SessionStart` | One line: KB exists, whether fresh, built timestamp |
| `PreToolUse(Bash)` | Block `gradle dependencies` / `dependencyInsight`; redirect to depkb. Override with `--depkb-allow` |
| `PostToolUse(Edit\|Write)` | After a build file edit: refresh KB, run `compat` + `missing`, feed findings back to Claude |

Hook JSON written to `<repo>/.claude/settings.json` (merged, not overwritten) by `java -jar depkb.jar install-hooks`.

### 6.3 Devin / other agents

`java -jar depkb.jar agents-snippet` prints the content to paste into `AGENTS.md`. Mirrors the Python `AGENTS.snippet.md`.

### 6.4 GitHub Copilot

`java -jar depkb.jar copilot-snippet` prints the content to paste into `.github/copilot-instructions.md`.

---

## 7. Snapshot and diff

```
java -jar depkb.jar snapshot save <name>     # save current KB state as a named snapshot
java -jar depkb.jar snapshot list
java -jar depkb.jar diff <name-a> <name-b>   # compare two snapshots
```

`diff` output:
- Libraries added / removed
- Version changes (with direction: upgrade / downgrade)
- New `BREAKS` or `LOST` findings introduced
- New `UNDECLARED` / `UNUSED?` findings

Use case: save a snapshot before a migration, make changes, diff to see the full impact.

---

## 8. Configuration

`<repo-root>/.depkb/config.properties` (created by `build` if absent):

```properties
# Gradle configurations to export (comma-separated)
depkb.configs=compileClasspath,runtimeClasspath,testCompileClasspath,testRuntimeClasspath,annotationProcessor

# Default output mode: compact | full | json
depkb.output=compact

# Default --limit
depkb.limit=40

# Maven repos for impact / outdated (space or comma-separated)
depkb.repos=https://repo1.maven.org/maven2/

# Authenticated repo: user:token
depkb.repo.auth=

# Auto-refresh on every query: true | false
depkb.auto.refresh=true

# Index jar contents (classes table); false shrinks DB, loses pkg/api/users detail
depkb.index.classes=true
```

All properties overridable via env vars: `DEPKB_CONFIGS`, `DEPKB_OUTPUT`, `DEPKB_LIMIT`, `DEPKB_REPOS`, `DEPKB_REPO_AUTH`, `DEPKB_AUTO`, `DEPKB_INDEX_CLASSES`.

---

## 9. Performance targets

Measured on a stress fixture of 100 modules, ~950 library versions, 2000 source files, 1400+ class files (matches Python prototype benchmark):

| Operation | Target |
|---|---|
| First `build` (Gradle export + index) | < 20 s |
| Refresh after one build file change | < 10 s |
| Refresh after one source edit | < 1 s |
| Queries (`usage`, `audit`, `why`, single-lib `compat`) | < 500 ms |
| `compat` over all overrides (1000+ checks, cached) | < 5 s |
| `missing` for one app (~250 jars, first run) | < 2 s |
| `jvm` on 135 jars (first run) | < 30 s |
| DB size (100 modules, 950 libs) | < 50 MB |

---

## 10. Technology choices

| Concern | Choice | Reason |
|---|---|---|
| Language | Java 17, Kotlin where it helps | JVM guaranteed; fat JAR distribution; ASM availability |
| Bytecode analysis | ASM 9.x | Industry standard; handles all JVM quirks; actively maintained |
| Database | SQLite via `sqlite-jdbc` | Zero-install; single file; fast for read-heavy workloads |
| CLI | picocli | Annotation-driven; generates `--help` automatically; GraalVM-friendly |
| HTTP (Maven/OSV) | Java `HttpClient` (JDK 11+) | Standard library; no extra dependency |
| Build | Gradle (shadow plugin for fat JAR) | Consistent with target ecosystem |
| POM parsing | Standard `javax.xml` | No extra dep; POMs are small |
| Concurrency | Virtual threads (JDK 21) or `CompletableFuture` | Jar scanning and POM fetching are I/O-bound |

---

## 11. What the Python prototype already validated

The `gradle-depkb` codebase is the reference for every command's exact behaviour. Do not re-design — re-implement faithfully:

- `depkb.py`: all commands, the SQLite schema, auto-refresh logic, jar scanning, bytecode analysis, import linking, POM parsing
- `classfile.py`: custom class file parser → replace with ASM
- `init/depkb.init.gradle`: the Gradle init script that exports resolution data → port to `.gradle.kts` or keep as `.gradle`
- `claude/skills/gradle-deps/SKILL.md`: the Claude Code skill text → update path from `python3 tools/depkb/depkb.py` to `java -jar tools/depkb.jar`
- `claude/hooks/depkb_hook.py`: the three Claude Code hooks → reimplement as a Java subprocess or keep as a thin Python shim that delegates to the JAR
- `AGENTS.snippet.md`: Devin instructions → update invocation
- `tests/test_fixture.py` + `make_fixture.py`: 32-check integration test → port to JUnit with the same fixture

---

## 12. Limits (carried from prototype, unchanged)

- Bytecode cannot see classes loaded by computed name; string constants that look like FQNs are recorded as reflection hints. Treat `UNUSED?` as a lead.
- `compat` checks linkage (classes, methods, fields, signatures), not behaviour.
- `missing` cannot know which optional code paths run. `optional?` means "fine unless that feature is used".
- Composite builds (`includeBuild`) appear as external dependencies; index each included build separately.
- File dependencies (`files(...)`) are not jar-indexed.
