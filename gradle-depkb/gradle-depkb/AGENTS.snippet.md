## Dependencies: use depkb, not Gradle output or memory

This build has a dependency knowledge base. For any question about dependencies, versions, conflicts,
which module uses a library, which jar provides a class, or a library's API, run
`python3 tools/depkb/depkb.py <command>` from the repo root.

Do not:
- run `./gradlew dependencies` or `dependencyInsight`
- infer versions from build files
- write library calls from memory

depkb reports what Gradle resolved and what the bytecode shows. It refreshes itself when build files,
sources or compiled classes change. Compile first (`./gradlew classes testClasses`) for the most
accurate usage data.

- `summary`: overview
- `usage <lib>`: per module, declared bucket or transitive, effective version, overrides, whether code uses it (`--files`)
- `runtime <lib>`: version each app runs with, who declares it, who uses it, and whether callers still link
- `why <lib> -p <module>`: why that version; requesters and paths
- `api <Class> -p <module>`: methods/fields of the exact version on that module's classpath (use before writing calls)
- `compat [lib]`: callers that break with an overridden version. `impact <lib> <version>`: check a version change first (binary breaks and lost transitive dependencies)
- `imports <module>`: what the module's code uses, per dependency, declared or not
- `users <Class|package>`: files using it. `pkg <Class|package>`: which jar provides it
- `audit`: undeclared, unused (verify), not-on-classpath, api-vs-implementation advice
- `explain "<class or log line>" -p <app>`: diagnose a ClassNotFoundException/NoClassDefFoundError (start here for those errors)
- `missing [-p app]`: classes referenced at runtime that nothing provides (ERROR / MISMATCH / optional?)
- `jvm 21 --from 11`: Java upgrade blockers and warnings across modules, jars and JVM options
- `info <lib>`, `catalog <text>`, `modules [module]`, `version`, `drift`, `conflicts`, `unresolved`

When changing a dependency:
1. Check `catalog` for an existing alias.
2. Run `impact <lib> <version>`.
3. Edit the build file.
4. Confirm with `why <lib> -p <module>` and `compat <lib>`.
