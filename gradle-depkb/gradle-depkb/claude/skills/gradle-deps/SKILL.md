---
name: gradle-deps
description: Answer any question about this Gradle build's dependencies, versions, conflicts, which module uses a library and where, which jar provides a class, the real API of a library class, or whether a version change breaks callers, by querying the depkb knowledge base instead of running gradle dependencies, reading build files, or guessing library APIs.
---

# Gradle dependency knowledge base (depkb)

Query with `python3 tools/depkb/depkb.py <command>` (adjust the path if depkb lives elsewhere). Answers
are a few lines and come from Gradle's own resolution and compiled bytecode, not from reading build
files.

**Do not**:
- run `./gradlew dependencies` or `dependencyInsight`
- grep build files for versions
- write calls to a library from memory

Ask depkb. `api <Class>` shows the methods of the version actually on the classpath.

The KB refreshes itself before answering. It picks up edited build files, the version catalog,
sources and newly compiled classes, and re-exports only the affected modules. For the most accurate
usage data, compile first (`./gradlew classes testClasses`); otherwise depkb falls back to imports
and says so.

## Pick the command

| Question | Command |
|---|---|
| Overview of the build | `summary` |
| Which modules use library X: bucket, version, override, does code reference it? | `usage X` (`--files` for where) |
| What version actually runs in the app(s), who declared it, who uses it, does it link? | `runtime X` |
| Why is X at this version / who pulls it in? | `why X -p <module>` |
| Methods/fields of class C in the version I compile against | `api C -p <module>` (`--grep name`) |
| Will an override break callers? | `compat [X]` |
| Is upgrading/downgrading X to version V safe? (e.g. a Renovate PR) | `impact X V` (binary breaks + lost transitive deps) |
| A ClassNotFoundException / NoClassDefFoundError happened | `explain "<class or the log line>" -p <app>` |
| Could any class be missing at runtime? | `missing [-p app]` |
| What breaks if we move Java 11 -> 21? | `jvm 21 --from 11` (`--why` for fixes) |
| What does module M use, and is each one declared correctly? | `imports M` |
| Where is class C / package P used? | `users C` (`--files`) |
| Which jar provides class C / package P? | `pkg C` |
| License, description, packages, JPMS name, catalog alias of X | `info X` |
| Which modules use module :M, and what does a change to it affect? | `usage :M`, `modules M` |
| Undeclared / unused / api-vs-implementation problems | `audit [-p M]` |
| Versions in use / drift between modules | `version X`, `drift` |
| Catalog alias to use when adding a dependency | `catalog <text>` |

`X` can be `group:artifact`, the artifact name, or a substring. Modules look like `:app` or `app`.
`--limit N` caps output (default 40, `0` = all).

## Reading the output

- Bucket names (`api`, `implementation`, `runtimeOnly`, `testImplementation`) mean the module declares
  it there.
- `transitive` means it is on the module's classpath only via something else.
- `used main:2f` means 2 files in `main` reference it. `[bytecode]` or `imports` in the header shows
  the evidence used.
- `overridden 1.0.0->2.0.0 (conflict resolution; via X)` means the module asked for 1.0.0, Gradle
  picked 2.0.0, and X is the requester that won.
- `UNDECLARED` means: declare it in the right bucket.
- `NOT_ON_CP` means the code will not compile against that classpath.
- `UNUSED?` means verify before removing (reflection/runtime use is invisible). "provides
  java.sql.Driver -> runtimeOnly?" means move it to `runtimeOnly`.
- `API: should be api` / `IMPL: could be implementation` is advice for `java-library` modules.
- `BREAKS <caller> (built against V): Class.method(Args) missing` means that caller will fail at
  runtime (NoSuchMethodError and the like) with the resolved version.
- `LOST g:a leaves the classpath, but its classes are used by ...` means the upgrade will cause a
  ClassNotFoundException: declare that dependency directly.
- In `missing`:
  - `ERROR` means your code needs a class nothing provides.
  - `MISMATCH` means the wrong version was resolved.
  - `optional?` means only libraries need it; usually fine.
- `jvm` lists BLOCKERS (will not link, start or run on the target) before WARNS (behaviour changes).
  JVM options are tested by actually starting the target `java`.

## When adding or changing a dependency

1. `catalog <name>` to reuse an existing alias and version.
2. `usage <lib>` and `runtime <lib>` to see what other modules already resolve. Avoid adding drift.
3. `impact <lib> <version>` before changing a version.
4. After editing the build file: `why <lib> -p <module>` and `compat <lib>`.
5. Before writing calls into a library: `api <Class> -p <module>`.
