#!/usr/bin/env python3
"""End-to-end check: builds the offline fixture (jars compiled with javac), compiles two of its
modules, runs depkb and asserts the answers; then edits build files and sources and checks the
KB refreshes itself. Needs a JDK and Gradle on PATH. Usage: python3 tests/test_fixture.py"""
import os, re, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPKB = [sys.executable, str(HERE.parent / "depkb.py")]
tmp = Path(tempfile.mkdtemp(prefix="depkb-test-"))
subprocess.run([sys.executable, str(HERE / "make_fixture.py"), str(tmp)], check=True, capture_output=True)
proj = tmp / "project"
GRADLE = os.environ.get("GRADLE", "gradle")
fails = 0


def gradle(*tasks):
    subprocess.run([GRADLE, "-q", "--offline", *tasks], cwd=proj, check=True, capture_output=True)


def run(*args):
    r = subprocess.run(DEPKB + list(args), cwd=proj, capture_output=True, text=True)
    return r.stdout + r.stderr


def check(name, out, *patterns, absent=()):
    global fails
    missing = [p for p in patterns if not re.search(p, out, re.M)]
    present = [p for p in absent if re.search(p, out, re.M)]
    print(("FAIL " if missing or present else "ok   ") + name)
    if missing or present:
        fails += 1
        print("  missing:", missing, " unexpected:", present, "\n  output:\n    " + out.replace("\n", "\n    "))


gradle(":common:classes", ":service:classes", ":service:testClasses")  # :app deliberately does not compile
check("build", run("build", "--offline"), r"indexed 4 modules")
check("conflict override", run("why", "acme-core", "-p", "app"),
      r"= 2\.0\.0", r"conflict resolution", r"acme-json:1\.2\.0 -> 1\.0\.0")
check("usage: declared/transitive/used + override", run("usage", "acme-core"),
      r":service  1\.0\.0  transitive  used main:1f  ! UNDECLARED",
      r":app\(app\)  2\.0\.0  transitive  not used  \| overridden 1\.0\.0->2\.0\.0",
      r"bytecode: 2 modules; imports \(not compiled\): 1 module")
check("runtime view + binary check", run("runtime", "acme-core"), r":app runs 2\.0\.0",
      r"binary check: BREAKS acme-json:1\.2\.0, :common, :service", r"declared by: :common \(api 1\.0\.0\)",
      r"used by: :common \(1 file\), :service \(1 file\)")
check("compat: removed method", run("compat"), r"BREAKS acme-json:1\.2\.0 \(built against 1\.0\.0\)",
      r"Core\.parse\(String\) missing", r"ok: acme-http:3\.1\.0", absent=[r"OldStrings"])
check("impact: upgrade fixes parse(String), still lacks Legacy, drops acme-text", run("impact", "acme-core", "2.1.0"),
      r"BREAKS acme-json:1\.2\.0", r"class org\.acme\.core\.Legacy missing", r"ok: acme-http:3\.1\.0, :common, :service",
      r"transitive changes: -org\.acme:acme-text",
      r"LOST org\.acme:acme-text:1\.0 leaves the classpath, but its classes are used by acme-http:3\.1\.0",
      absent=[r"Core\.parse\(String\) missing"])
check("impact: downgrade breaks http", run("impact", "acme-core", "1.0.0"),
      r"BREAKS acme-http:3\.1\.0 \(built against 2\.0\.0\)", r"Core\.create\(\) missing")
check("api: exact version + javadoc", run("api", "Core", "-p", "app"), r"from org\.acme:acme-core:2\.0\.0",
      r"public static String parse\(CharSequence text\);", r"/\*\* Core entry point of Acme\. \*/")
check("api: nested class", run("api", "Client.Builder"), r"public Client build\(\);")
check("api: own module class", run("api", "Service"), r"from module :service", r"org\.acme\.json\.Json json\(\);")
check("missing: version mismatch + own code", run("missing"),
      r"MISMATCH org\.acme\.core\.Legacy: org\.acme:acme-core resolved 2\.0\.0 lacks it \(in 1\.0\.0\) — used by acme-json:1\.2\.0",
      r"ERROR com\.missing \(1 class\) — used by :app")
check("explain: version mismatch", run("explain", "java.lang.NoClassDefFoundError:", "org/acme/core/Legacy"),
      r"VERSION MISMATCH: org\.acme:acme-core resolved to 2\.0\.0", r"overridden 1\.0\.0->2\.0\.0",
      r"referenced by: acme-json:1\.2\.0")
check("explain: removed from JDK", run("explain", "Caused by: java.lang.ClassNotFoundException: javax.xml.bind.JAXBContext"),
      r"removed from the JDK after Java 10", r"jakarta\.xml\.bind")
check("explain: test-only dependency", run("explain", "test.tools.Assert", "-p", "service"),
      r"NOT on runtimeClasspath, but on testCompileClasspath")
check("explain: present -> packaging/classloader", run("explain", "org.acme.driver.AcmeDriver", "-p", "service"),
      r"on the classpath: org\.acme:acme-driver:1\.0", r"classloader")
if os.path.exists("/usr/lib/jvm") or os.environ.get("JAVA_HOME"):
    out = run("jvm", "21", "--from", "11")
    if "no JDK found" in out or "must point at a JDK 21" in out or "no jmods" in out:
        print("skip jvm 21 checks (no full JDK 21 here)")
    else:
        check("jvm 11 -> 21", out, r"JVM option -XX:\+UseConcMarkSweepGC: JVM will not start",
              r"removed: class java\.util\.jar\.Pack200", r"removed: class java\.lang\.Compiler",
              r"Thread\.stop\(\) throws UnsupportedOperationException", r"setSecurityManager",
              r"encapsulated JDK internal: sun\.security\.x509", r"Nashorn", r"knows class files up to Java 16",
              r"lombok:1\.18\.20 too old", r"default charset becomes UTF-8 .* — in :common", r"CLDR 42",
              r"--illegal-access=permit: ignored")
check("package -> jar", run("pkg", "org.acme.core.util"), r"SPLIT")
check("class -> jar", run("pkg", "Json"), r"org\.acme\.json\.Json -> org\.acme:acme-json:1\.2\.0")
check("license inherited from parent POM", run("info", "acme-core"), r"license: Apache-2\.0")
check("audit", run("audit"), r":service -> org\.acme:acme-core \(1 file\)", r":app -> project :common",
      r":app -> test\.tools:testkit", r"acme-driver.*provides java\.sql\.Driver -> runtimeOnly\?",
      r"SHOULD BE api.*: 2", r":service -> org\.acme:acme-json", r"COULD BE implementation.*: 1",
      r":common -> org\.acme:acme-core", absent=[r":app -> org\.acme:acme-driver"])
check("service-loader file counts as use", run("usage", ":common", "--files"),
      r"META-INF/services/org\.fixture\.common\.Plugin.*\[service\]")
check("reflection string seen", run("imports", "service"), r"org\.acme:acme-json")
check("comment imports ignored", run("users", "org.fake"), r"from 0 files")
check("unresolved", run("unresolved"), r"org\.ghost:ghost:1\.0")

# --- source edit after compiling: classes are now older -> falls back to imports for that source set
time.sleep(1.1)
svc = proj / "service/src/main/java/org/fixture/service/Service.java"
svc.write_text(svc.read_text() + "\n")
check("stale classes -> imports evidence", run("imports", "service"), r"main: imports \(compiled classes are older")
gradle(":service:classes")
check("recompiled -> bytecode again", run("imports", "service"), r"main: bytecode")

# --- auto refresh: build file edit re-exports only the module and its dependents
bf = proj / "service" / "build.gradle"
bf.write_text(bf.read_text().replace("implementation libs.acme.json", "implementation libs.acme.json\n  implementation libs.acme.core"))
check("partial refresh after build.gradle edit", run("usage", "acme-core"),
      r"Gradle export of 2 module\(s\): :app, :service", r":service  1\.0\.0  implementation  used main:1f",
      absent=[r":service .*UNDECLARED"])
# --- source edit in an uncompiled module: no Gradle, import index updates
src = proj / "app/src/main/java/org/fixture/app/App.java"
src.write_text(src.read_text().replace("import java.util.Map;", "import java.util.Map;\nimport org.acme.core.Core;"))
out = run("usage", "acme-core")
check("source edit picked up without Gradle", out, r":app\(app\)  2\.0\.0  transitive  used main:1f", absent=[r"Gradle export"])
# --- catalog edit is global
cat = proj / "gradle/libs.versions.toml"
cat.write_text(cat.read_text().replace('acme-core = "1.0.0"', 'acme-core = "2.1.0"'))
check("catalog edit -> full refresh", run("version", "acme-core"), r"Gradle export of all modules", r"2\.1\.0")
# :common uses `implementation` for nothing here, but :service hides :common from :app's compile classpath
check("compile vs runtime differ", run("who-uses", "acme-core"),
      r":app  2\.0\.0  transitive  \[c tc\]", r":app  2\.1\.0  transitive  \[r tr\]")
check("no change -> no Gradle", run("version", "acme-core"), r"^org\.acme:acme-core", absent=[r"Gradle export"])
check("after the bump acme-text is gone and acme-http misses it", run("missing"),
      r"optional\? acme-http:3\.1\.0 references 1 missing package: org\.acme\.text")

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'} ({tmp})")
sys.exit(1 if fails else 0)
