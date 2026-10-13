# depkb: Gradle dependency knowledge base for Java projects

A token-cheap dependency knowledge base for large Gradle builds, for developers and AI agents
(Claude Code, Devin, ...). Gradle resolves the real graph and depkb reads compiled bytecode where it
exists. It answers in a few lines from SQLite, instead of an agent reading build files, `gradle
dependencies` output, or library sources.

```
$ depkb usage acme-core
org.acme:acme-core — 3 modules  [bytecode: 2 modules; imports (not compiled): 1 module]
:app(app)  2.0.0  transitive  not used  | overridden 1.0.0->2.0.0 (conflict resolution; via acme-http:3.1.0)
:common  1.0.0  api  used main:1f  ! IMPL: not in public API: could be implementation
:service  1.0.0  transitive  used main:1f  ! UNDECLARED: used but only transitive: declare it

$ depkb runtime acme-core
:app runs 2.0.0 — overridden 1.0.0->2.0.0 (conflict resolution; via acme-http:3.1.0)
  binary check: BREAKS acme-json:1.2.0, :common, :service (details: depkb compat acme-core)
  declared by: :common (api 1.0.0)
  used by: :common (1 file), :service (1 file)

$ depkb compat
:app [r] org.acme:acme-core -> 2.0.0 (requested 1.0.0)
  BREAKS acme-json:1.2.0 (built against 1.0.0):
    Core.parse(String) missing
  ...
$ depkb impact acme-core 2.1.0
  BREAKS acme-json:1.2.0 (built against 1.0.0):
    class org.acme.core.Legacy missing
  ok: acme-http:3.1.0, :common, :service
  transitive changes: -org.acme:acme-text
    LOST org.acme:acme-text:1.0 leaves the classpath, but its classes are used by acme-http:3.1.0 (1 class) -> ClassNotFoundException

$ depkb api Core -p app
// from org.acme:acme-core:2.0.0 (version on :app compileClasspath); other versions in use: 1.0.0
public class Core {
  public static String parse(CharSequence text);
  public static Core create();
  ...

$ depkb explain "java.lang.NoClassDefFoundError: org/acme/core/Legacy"
:app [r]:
  VERSION MISMATCH: org.acme:acme-core resolved to 2.0.0, which does not contain it (versions that do: 1.0.0).
  org.acme:acme-core: overridden 1.0.0->2.0.0 (conflict resolution; via acme-http:3.1.0)
  referenced by: acme-json:1.2.0

$ depkb jvm 21 --from 11
BLOCKERS: 10  (will fail to link, start or run)
  JVM option -XX:+UseConcMarkSweepGC: JVM will not start  — in deploy/app.env:1
  removed: class java.util.jar.Pack200  — in old-utils:1.0
  java.lang.Thread.stop() throws UnsupportedOperationException since JDK 20  — in old-utils:1.0
  org.projectlombok:lombok:1.18.20 too old for JDK 21  — in :common
  ...
```

## Setup

Requirements: Python 3.11+ (standard library only), the project's own `./gradlew`, and a JDK. The JDK's
`jmods` are used for JDK classes in `api` and `compat`; without them, those two commands still work
with reduced JDK detail.

```bash
cp -r gradle-depkb <repo>/tools/depkb          # nothing is added to the build itself
cd <repo>
python3 tools/depkb/depkb.py build              # first run: full export + index
python3 tools/depkb/depkb.py summary
```

The KB lives in `<repo>/.depkb/` and ignores itself in git. Optional alias:
`alias depkb='python3 tools/depkb/depkb.py'`.

**Better data from compiling:** depkb never compiles anything itself. When classes exist
(`./gradlew classes testClasses`, which an IDE build, Devin, or CI already does), depkb reads them.
Without classes it falls back to scanning imports. Each source set uses whichever is fresher, and
outputs say which was used.

**Agents**
- **Claude Code skill (main integration):** copy `claude/skills/gradle-deps/` to `<repo>/.claude/skills/`.
  Only its one-line description sits in context until a dependency question comes up; then Claude
  loads it and runs depkb.
- **Claude Code hooks (optional automation):** merge `claude/settings.hooks.json` into
  `<repo>/.claude/settings.json`. Paths assume depkb lives in `tools/depkb`. It installs three hooks:
  - `SessionStart` adds one line telling Claude the KB exists and whether it is fresh.
  - `PreToolUse(Bash)` blocks `gradle dependencies` / `dependencyInsight` and points to depkb.
    Appending `--depkb-allow` to the command overrides it.
  - `PostToolUse(Edit|Write)`: after Claude edits a build file, it refreshes the KB and feeds any
    `compat` breaks and `missing` classes back to Claude.
- **Devin and other agents:** paste `AGENTS.snippet.md` into `AGENTS.md` or the agent's
  knowledge/instructions. They have no hooks; add a CI step running `depkb compat` and `depkb missing`
  if you want the same safety net.

## What it knows

| Layer | Source | Example question |
|---|---|---|
| Resolved graph per module and configuration | Gradle `ResolutionResult` (init script) | which version does `:app` really get, and why? |
| Requested vs effective version, selection reason | same | was it overridden, and by whom? |
| What build files declare, per bucket (`api`, `implementation`, `runtimeOnly`, ...) | Gradle configurations | is it `api` or `implementation`? |
| Module graph, app vs library modules | project dependencies, applied plugins | what is affected if I change `:common`? |
| **Code → dependency links** | compiled classes (refs, public-API refs, member refs, reflective strings); imports when not compiled; `META-INF/services` and Spring `*.imports` files | which modules really use it, and where? |
| Jar contents (packages, classes, JPMS name, service providers) | jars on the classpath | which jar provides `org.acme.json.Json`? |
| Class API (generics, parameter names, Javadoc first sentence) | class files; `-sources.jar` or `DEPKB_SOURCE_ROOTS` for docs | what methods does this version actually have? |
| Binary compatibility | callers' member refs vs. the resolved jar | will the override break at runtime? |
| POM description, URL, licenses (with parent inheritance) | Gradle cache / Maven repos | which licenses ship? |
| Version catalog aliases | `gradle/*.versions.toml` | which alias do I use? |

### Declared vs transitive vs used

For every module/library pair depkb keeps three facts:
- **declared**: which bucket in the module's build file
- **transitive**: on its classpath only through something else
- **used**: its code references it, with file (and line for imports) for `--files`

Combinations become findings in `usage`, `imports` and `audit`:
- `UNDECLARED`: used but only transitive
- `NOT_ON_CP`: used but not on the compile classpath
- `UNUSED?`: declared but never referenced. Jars that only provide services (JDBC drivers etc.) get
  a "use runtimeOnly?" hint. `runtimeOnly` declarations are never flagged.
- `API` / `IMPL`: with bytecode, depkb knows when a library appears in a module's public signatures or
  supertypes. It then advises "should be `api`" or "could be `implementation`" for `java-library`
  modules.

Compile and runtime are kept apart. `who-uses` shows both. `runtime` answers from the
`runtimeClasspath` of deployable modules (application, war, Spring Boot, Quarkus, Micronaut, shadow).

### Overrides and binary compatibility

When Gradle picks a version a caller was not built against, `compat` checks every caller. Callers are
the jars depending on it, and the build's own compiled modules. It reports classes, methods and fields
they link against that are missing from the version that will run, including inherited members and
JDK supertypes. `impact <lib> <version>` runs the same check for a hypothetical version, fetching the
jar if needed, so you can gate a Renovate PR before merging.

### ClassNotFoundException / NoClassDefFoundError

`explain <Class>` diagnoses an error from the build's own data. It accepts the class name or the log
line pasted as is. For each app it works out which of these applies:
- **Removed from the JDK**: gives the release it left in, from `ct.sym`, and the replacement artifact
  (JAXB, javax.annotation, Nashorn, CORBA, ...).
- **JDK-internal**: the class exists but its package isn't exported.
- **Version mismatch**: the library is on the classpath but the resolved version lacks the class.
  Shows which versions have it and which override caused the mismatch.
- **Not on the runtime classpath**: only on compile or test classpaths, i.e. `compileOnly` or
  test-only.
- **Provided elsewhere in the build**: gives the coordinates and catalog alias to add.
- **Present on the classpath**: points to packaging and class-loading causes instead.

It also lists who references the class.

`missing` predicts these errors for each app's runtime classpath. It lists every class that its jars
or modules reference but nothing on that classpath provides:
- `ERROR`: your code needs it.
- `MISMATCH`: wrong version.
- `optional?`: only libraries reference it, which is normal for optional integrations. Grouped per
  library.

`impact <lib> <version>` also diffs the transitive dependencies of the new version. It flags any
that would leave the classpath while other code still uses their classes (`LOST ... ->
ClassNotFoundException`).

### Java upgrades (`depkb jvm <target> [--from N]`)

Point it at the target JDK (`--jdk`, or `JAVA_HOME`); it needs the JDK's `jmods`, as in Temurin and
OpenJDK builds. The source release's API comes from the target JDK's `ct.sym`, so the old JDK
doesn't need to be installed. It checks compiled modules and every jar in the graphs (cached after
the first run), then lists blockers and warnings, with a count per app:

| Check | How | Severity |
|---|---|---|
| Classes/methods/fields removed between the releases | exact diff of the two APIs | blocker |
| JDK internals (non-exported packages) | target JDK module exports | blocker |
| Methods that now throw (`Thread.stop/suspend/resume` since 20, `System.setSecurityManager` since 18) | rules | blocker |
| Nashorn via `getEngineByName("nashorn"/"js")` | bytecode + string constants | blocker |
| Bytecode libraries too old for the target class format (ASM incl. shaded copies, Byte Buddy, Javassist) | reads their version constants | blocker |
| Lombok < 1.18.30 for JDK 21; Gradle that cannot run on or compile for the target | version rules | blocker |
| JVM options in Dockerfiles, scripts, YAML, `.env`, properties... | **runs the target `java` with each option** | blocker if the JVM won't start, else warning |
| Deprecated-for-removal APIs | target JDK annotations | warning (summarised; `--deprecations` lists them) |
| Default charset now UTF-8 (JEP 400), CLDR 42 AM/PM formats, deep reflection into JDK classes, `finalize()`, TLS 1.0/1.1 | bytecode signals | warning |

It does not cover GC/JIT tuning, performance, or behaviour changes with no code signal, so run the
tests on the new JDK. For automated code rewrites, OpenRewrite's Java migration recipes complement it.

## Staying current

Every query first checks whether anything changed: a `stat` of tracked files, plus a hash only when
the `stat` changed.

| Change | What happens | Typical cost |
|---|---|---|
| A module's `build.gradle(.kts)` / `gradle.lockfile` | re-export that module plus every module whose graph contains it | seconds |
| `settings.gradle`, root build, version catalog, `buildSrc`/`build-logic`, `gradle.properties` | full re-export | like `build` |
| Sources or freshly compiled classes | re-parse only changed files, relink; no Gradle | ~0.1–0.5 s |
| Nothing | answer immediately | ~0.1–0.4 s |

Options:
- Turn auto-refresh off with `--no-refresh` or `DEPKB_AUTO=0`.
- `depkb status` reports staleness without fixing it.

**CI option:** run the export in CI with
`./gradlew -I tools/depkb/init/depkb.init.gradle depKbExport -Ddepkb.out=$PWD/depkb-export`, publish the
folder, and run `depkb ingest depkb-export` locally.

## Commands

```
build [--offline] [--force] [--configs a,b|all] [--gradle-args "..."] [--no-classes]
summary | status
usage <lib | :module> [--files]      runtime <lib> [-p app] [--files]
why <lib> [-p module] [-c config]   imports <module> [--unknown]   audit [-p module]
users <Class|pkg> [-p module] [--files]   pkg <Class|pkg>   info <lib>
api <Class> [-p module] [--grep text] [--public-only]
compat [lib] [-p app] [--verbose]    impact <lib> <version> [-p app] [--repo URL]
explain <Class | log line> [-p app]   missing [-p app] [--all]
jvm <target> [--from N] [--jdk PATH] [--why] [--deprecations] [--no-flags]
modules [module]   deps <module> [--all]   diff <a> <b>
version <lib>   who-uses <lib>   conflicts [--all]   drift   split-packages
unresolved   licenses [filter]   catalog [text] [--unused|--missing]
outdated [--repo URL] [--direct]   vulns       sql "<query>"
```

Every command takes `--limit N` (default 40; 0 = all).
- `impact` and `outdated` read Maven repos from `--repo` or `DEPKB_REPOS` (default Maven Central),
  authenticated with `DEPKB_REPO_AUTH=user:token`.
- Javadoc for `api` comes from `-sources.jar` files next to the jars, or from `DEPKB_SOURCE_ROOTS`
  (directories holding internal libraries' sources).
- With Renovate in place, `outdated` is optional; `impact` is the per-PR check.

## Performance (measured)

Stress fixture: 100 modules, about 950 library versions, 2,000 source files with 28k imports, plus
1,430 real class files.

| Operation | Time |
|---|---|
| First `build` (Gradle export ~10 s, indexing ~6 s) | 17 s |
| Refresh after editing one module's build file (module + dependents) | ~7 s |
| Refresh after a source edit | ~0.2 s |
| Queries (`usage`, `audit`, `why`, `api`, one-library `compat`) | 150–400 ms |
| `compat` over every override of 4 apps (1,040 checks, cached) | ~2.4 s |
| `missing` for one app (255 jars; first run indexes the jars) | ~0.7 s cached |
| `jvm` on 135 real third-party jars (Gradle's own `lib`, incl. the Kotlin compiler) | 22 s first run on 2 cores, 2 s cached |
| Database size | 34 MB (`--no-classes` shrinks it) |

On a 7,500-class sample (all of `java.base`), the class-file parser runs at about 0.5 ms per class with
no failures.

## Limits

- Bytecode and imports cannot see classes loaded by computed name. String constants that look like
  class names are recorded as reflection hints. Treat `UNUSED?` as a lead.
- `compat` checks linkage (classes, methods, fields, signatures), not behaviour. `impact` diffs the new
  version's direct POM dependencies; after you edit the build, depkb re-resolves the full graph and
  `compat`/`missing` cover the rest.
- `missing` cannot know which optional code paths run. Treat `optional?` as "fine unless that feature
  is used".
- Default configurations: `compileClasspath`, `runtimeClasspath`, `testCompileClasspath`, `testRuntimeClasspath`,
  `annotationProcessor`, plus the compile classpath of every extra source set. Use `--configs all` for everything.
- Composite builds (`includeBuild`) appear as external dependencies; index an included build separately.
- File dependencies (`files(...)`) are not jar-indexed.

## Tests

`python3 tests/test_fixture.py` builds an offline fixture: jars compiled with javac into a local Maven
repo, including a release that removes a method and a class. It then compiles two of the three
modules and checks 32 answers:
- conflicts, BOM and split packages
- declared, transitive and used
- api/implementation advice
- ServiceLoader files and reflection strings
- `compat`, `impact` and `api` with Javadoc
- `missing` and `explain`: version mismatch, removed JDK class, test-only dependency, packaging
- `jvm 21 --from 11`, against a Java 11-era library and deployment files
- stale-class fallback
- partial refresh after build, source and catalog edits
