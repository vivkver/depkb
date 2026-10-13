# Intent: dependency-helper

Captured from /interview-me session — 2026-10-06.

- **Outcome:** A Java fat JAR (`java -jar depkb.jar <command>`) that runs locally inside a Gradle project, builds a local SQLite knowledge base from Gradle's resolved dependency graph and compiled bytecode, and lets AI agents (Claude Code, Devin, Copilot) query dependency intelligence via a token-efficient CLI
- **User:** Java developers and their clients; JVM is always present, no extra runtime needed
- **Why now:** The Python prototype (`gradle-depkb`) validated every core feature; rebuilding in Java gives clean distribution and proper bytecode parsing via ASM
- **Success:** `init` completes in seconds on a large project; incremental re-index after a build file edit takes under 10s; an AI agent answers "what breaks if I upgrade Spring Boot to 3.3?" in a few lines
- **Constraint:** Single JAR, no install beyond JVM; Gradle present for initial indexing; Java first, Kotlin where it helps; output configurable for token budget
- **Out of scope:** MCP server, cloud deployment, non-Gradle builds, Python runtime dependency on client machines, exposing the KB as a queryable network service
