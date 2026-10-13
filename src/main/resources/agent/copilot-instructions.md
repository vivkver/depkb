# Dependency questions: query depkb

This repository ships a dependency knowledge base (depkb). When you need to answer anything about
Gradle dependencies — versions, conflicts, which module uses a library, which jar provides a class,
or a library's real API — run `__DEPKB__ <command>` from the repo root instead of reading build
files, running Gradle reports, or recalling an API from memory.

Do not:
- run `./gradlew dependencies` or `dependencyInsight`
- infer versions from build files
- write library calls from memory

depkb answers from Gradle's own resolution and compiled bytecode, in a few lines. It refreshes itself
when build files, sources or compiled classes change. For the most accurate usage data, compile first
(`./gradlew classes testClasses`).

Common commands:

- `summary` — one-screen overview of the build
- `usage <lib>` — per module: declared bucket or transitive, effective version, overrides, whether code uses it (`--files`)
- `runtime <lib>` — the version each app runs with, who declared it, and whether callers still link
- `why <lib> -p <module>` — why a library resolves to its version; requesters and paths
- `api <Class> -p <module>` — methods/fields of the exact version on the classpath (use before writing calls)
- `compat [lib]` — callers that break with an overridden version
- `impact <lib> <version>` — check a version change first: binary breaks and lost transitive deps
- `imports <module>` — what the module's code uses, per dependency, declared or not
- `users <Class|package>` / `pkg <Class|package>` — who uses it / which jar provides it
- `audit` — undeclared, unused (verify), not-on-classpath, api-vs-implementation advice
- `explain "<class or log line>" -p <app>` — diagnose a ClassNotFoundException/NoClassDefFoundError
- `missing [-p app]` — classes referenced at runtime that nothing provides
- `jvm 21 --from 11` — Java upgrade blockers and warnings across modules, jars and JVM options
- `info <lib>`, `catalog <text>`, `modules [module]`, `version`, `drift`, `conflicts`, `unresolved`
- `snapshot save <name>` then `diff <a> <b>` — compare the dependency graph before and after a change

`<lib>` can be `group:artifact`, an artifact name, or a substring. Modules look like `:app` or `app`.
`--limit N` caps output (default 40, `0` = all).

When adding or changing a dependency: check `catalog` for an existing alias, run `impact <lib> <version>`,
edit the build file, then confirm with `why <lib> -p <module>` and `compat <lib>`.
