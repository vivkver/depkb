# Changelog

All notable changes to depkb are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-13

First release of the Java port, distributed as a single fat JAR
(`java -jar depkb.jar <command>`). Builds a local SQLite knowledge base from
Gradle's resolved dependency graph and compiled bytecode (via ASM) and answers
dependency questions for AI agents in a few lines.

### Added
- **Indexing**: `build`, `ingest`, `status`, `summary` — export the resolved
  graph from Gradle and index jars, packages, classes and source imports, with
  incremental auto-refresh on changed build files and sources.
- **Dependency queries**: `usage`, `runtime`, `why`, `who-uses`, `version`,
  `drift`, `conflicts`, `unresolved`, `deps`, `modules`.
- **Code analysis**: `imports`, `users`, `pkg`, `audit`, `api` — resolve imports
  to the providing dependency and report the real API of the version on the
  classpath.
- **Safety checks**: `compat`, `impact`, `explain`, `missing`, `jvm` — binary
  linkage checks for version overrides and Java upgrades.
- **Metadata**: `info`, `licenses`, `catalog`, `outdated`, `vulns`,
  `split-packages`, `sql`.
- **Snapshots**: `snapshot save|list|delete` plus a snapshot-aware `diff` that
  reports added / removed / version-changed dependencies (with upgrade or
  downgrade direction) between two saved snapshots; `diff` falls back to a
  project-vs-project comparison when the arguments are projects.
- **Agent integration**: `install-hooks` merges Claude Code
  SessionStart/PreToolUse/PostToolUse hooks into `.claude/settings.json` and
  installs the `gradle-deps` skill; `agents-snippet` and `copilot-snippet` print
  ready-to-paste instructions for Devin and GitHub Copilot. Hooks invoke the JAR
  itself, so no Python runtime is required on client machines.

[1.0.0]: https://github.com/vivkver/depkb/releases/tag/v1.0.0
