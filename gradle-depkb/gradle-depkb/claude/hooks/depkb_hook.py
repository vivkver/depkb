#!/usr/bin/env python3
"""Claude Code hooks for depkb. One script, three modes (see claude/settings.hooks.json):

  guard        PreToolUse(Bash): block `gradle dependencies` / `dependencyInsight` and point to depkb
  after-edit   PostToolUse(Edit|Write|MultiEdit): after a build file changes, refresh the KB and report
               binary breaks (compat) and missing classes (missing) back to Claude
  session      SessionStart: one line telling Claude the KB exists and whether it is fresh

Reads the hook JSON on stdin. Never fails the tool call because of its own errors.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEPKB = Path(os.environ.get("DEPKB", Path(__file__).resolve().parents[2] / "depkb.py"))
BUILD_FILE = re.compile(r"(build\.gradle(\.kts)?|settings\.gradle(\.kts)?|\.versions\.toml|gradle\.lockfile|gradle\.properties)$")
DEP_TASKS = re.compile(r"\b(gradlew?|gradle\.bat|gradlew\.bat)\b.*\b(dependencies|dependencyInsight|"
                       r"[\w:-]*:dependencies|htmlDependencyReport|buildEnvironment)\b")


def depkb(*args, timeout=600):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    r = subprocess.run([sys.executable, str(DEPKB), *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    tool_input = data.get("tool_input") or {}

    if mode == "guard":
        cmd = tool_input.get("command", "")
        if DEP_TASKS.search(cmd) and "--depkb-allow" not in cmd:
            out({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "Dependency reports are huge and miss resolution details. Use the depkb knowledge base instead: "
                    f"`python3 {DEPKB} why <lib> -p <module>`, `usage <lib>`, `deps <module>`, `runtime <lib>` "
                    "(see the gradle-deps skill). If you really need the raw Gradle report, append --depkb-allow."}})
        sys.exit(0)

    if mode == "after-edit":
        path = tool_input.get("file_path") or ""
        if not BUILD_FILE.search(path):
            sys.exit(0)
        try:
            _, compat, err1 = depkb("compat", "--limit", "15")
            _, missing, _ = depkb("missing", "--limit", "10")
        except (OSError, subprocess.TimeoutExpired) as e:
            out({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                        "additionalContext": f"depkb could not check the build change: {e}"}})
        problems = [ln for ln in (compat + "\n" + missing).splitlines()
                    if re.search(r"BREAKS|^\s+(ERROR|MISMATCH)|^\s{4}\S.* missing$", ln)]
        refresh_failed = "refresh failed" in err1
        if problems or refresh_failed:
            msg = (f"depkb checked the change to {Path(path).name}:\n"
                   + ("Gradle could not re-resolve the build:\n" + err1[-1500:] + "\n" if refresh_failed else "")
                   + "\n".join(problems[:25])
                   + "\nDetails: depkb compat / depkb missing / depkb explain <Class>.")
            out({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}})
        sys.exit(0)

    if mode == "session":
        try:
            _, status, _ = depkb("--no-refresh", "status", "--limit", "3", timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            sys.exit(0)
        if status and "no knowledge base" not in status:
            print(f"Gradle dependency knowledge base available (gradle-deps skill, `python3 {DEPKB} <command>`): "
                  + status.splitlines()[0])
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # a broken helper must never block the user's work
        sys.exit(0)
