#!/usr/bin/env python3
"""Builds an offline test fixture: a local Maven repo whose jars are compiled with javac, and a
multi-project Java Gradle build. It contains a version conflict that breaks binary compatibility
(acme-core 2.0.0 removes Core.parse(String)), a BOM, a split package, an inherited license, a
service-provider jar, a test-only dependency and an unresolvable dependency.
Usage: make_fixture.py <dir>"""
import os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
repo = root / "repo"
proj = root / "project"
work = Path(tempfile.mkdtemp(prefix="fixture-src-"))
LIC = "<licenses><license><name>{0}</name></license></licenses>"


def pom(g, a, v, deps=(), packaging="jar", parent=None, extra="", license=None, desc=None):
    d = "".join(f"<dependency><groupId>{dg}</groupId><artifactId>{da}</artifactId><version>{dv}</version>"
                f"</dependency>" for dg, da, dv in deps)
    par = (f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId>"
           f"<version>{parent[2]}</version></parent>") if parent else ""
    return (f'<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
            f"<modelVersion>4.0.0</modelVersion>{par}<groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version><packaging>{packaging}</packaging>"
            f"{'<description>' + desc + '</description>' if desc else ''}<url>https://example.org/{a}</url>"
            f"{LIC.format(license) if license else ''}{extra}<dependencies>{d}</dependencies></project>")


def jar_path(g, a, v):
    return repo / g.replace(".", "/") / a / v / f"{a}-{v}.jar"


def publish(g, a, v, sources=None, deps=(), cp=(), packaging="jar", resources=None, javac=("--release", "11"), **kw):
    """sources: {relative .java path: code}; cp: (g, a, v) jars to compile against."""
    d = repo / g.replace(".", "/") / a / v
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{a}-{v}.pom").write_text(pom(g, a, v, deps, packaging, **kw))
    if packaging != "jar":
        return
    src, out = work / f"{a}-{v}-src", work / f"{a}-{v}-out"
    files = []
    for rel, code in (sources or {}).items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code)
        files.append(str(f))
    if files:
        cpath = os.pathsep.join(str(jar_path(*c)) for c in cp)
        cmd = ["javac", "-g", "-parameters", "-nowarn", *javac, "-d", str(out)] + (["-cp", cpath] if cpath else []) + files
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"javac failed for {a}-{v}:\n{r.stderr}")
    if sources:
        with zipfile.ZipFile(d / f"{a}-{v}-sources.jar", "w") as z:
            for rel, code in sources.items():
                z.writestr(rel, code)
    with zipfile.ZipFile(d / f"{a}-{v}.jar", "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        if out.exists():
            for f in sorted(out.rglob("*.class")):
                z.write(f, f.relative_to(out).as_posix())
        for rel, text in (resources or {}).items():
            z.writestr(rel, text)


def metadata(g, a, versions):
    d = repo / g.replace(".", "/") / a
    vs = "".join(f"<version>{v}</version>" for v in versions)
    (d / "maven-metadata.xml").write_text(
        f"<metadata><groupId>{g}</groupId><artifactId>{a}</artifactId><versioning><latest>{versions[-1]}</latest>"
        f"<release>{versions[-1]}</release><versions>{vs}</versions></versioning></metadata>")


CORE = "package org.acme.core;\n/** Core entry point of Acme. */\npublic class Core {\n{body}\n}\n"
STRINGS = {"org/acme/core/util/Strings.java": "package org.acme.core.util;\npublic final class Strings {\n"
           "  public static String trim(String s) { return s.trim(); }\n}\n"}
core_parse_string = "  /** Parses text into canonical form. */\n  public static String parse(String text) { return text; }\n"
core_parse_cs = "  public static String parse(CharSequence text) { return text.toString(); }\n"
core_more = "  public static Core create() { return new Core(); }\n  public int version() { return 2; }\n"

publish("org.acme", "acme-parent", "1", packaging="pom", license="Apache-2.0")
publish("org.acme", "acme-text", "1.0", {"org/acme/text/Text.java": "package org.acme.text;\npublic class Text {}\n"},
        license="Apache-2.0")
publish("org.acme", "acme-core", "1.0.0", {"org/acme/core/Core.java": CORE.replace("{body}", core_parse_string),
                                           "org/acme/core/Legacy.java": "package org.acme.core;\npublic class Legacy {}\n",
                                           **STRINGS}, parent=("org.acme", "acme-parent", "1"), desc="Acme core library")
publish("org.acme", "acme-core", "2.0.0", {"org/acme/core/Core.java": CORE.replace("{body}", core_parse_cs + core_more),
                                           **STRINGS}, deps=[("org.acme", "acme-text", "1.0")],
        parent=("org.acme", "acme-parent", "1"), desc="Acme core library")  # 2.1.0 drops acme-text again
publish("org.acme", "acme-core", "2.1.0", {"org/acme/core/Core.java": CORE.replace(
    "{body}", core_parse_string + core_parse_cs + core_more), **STRINGS},
    parent=("org.acme", "acme-parent", "1"), desc="Acme core library")
metadata("org.acme", "acme-core", ["1.0.0", "2.0.0", "2.1.0"])
publish("org.acme", "acme-json", "1.2.0", {
    "org/acme/json/Json.java": "package org.acme.json;\nimport org.acme.core.Core;\npublic class Json {\n"
                               "  public static String read(String text) { new org.acme.core.Legacy(); return Core.parse(text); }\n}\n",
    "org/acme/json/Parser.java": "package org.acme.json;\npublic class Parser {\n"
                                 "  public static Object parse(String text) { return text; }\n}\n"},
    deps=[("org.acme", "acme-core", "1.0.0")], cp=[("org.acme", "acme-core", "1.0.0")], license="MIT", desc="JSON for Acme")
metadata("org.acme", "acme-json", ["1.2.0", "1.3.0"])
publish("org.acme", "acme-http", "3.1.0", {
    "org/acme/http/Client.java": "package org.acme.http;\nimport org.acme.core.Core;\nimport org.acme.json.Json;\n"
                                 "public class Client {\n  public String get(String url) { Core.create(); new org.acme.text.Text(); return Json.read(url); }\n"
                                 "  public static class Builder { public Client build() { return new Client(); } }\n}\n",
    "org/acme/http/internal/Pool.java": "package org.acme.http.internal;\npublic class Pool {}\n"},
    deps=[("org.acme", "acme-core", "2.0.0"), ("org.acme", "acme-json", "1.2.0")],
    cp=[("org.acme", "acme-core", "2.0.0"), ("org.acme", "acme-json", "1.2.0"), ("org.acme", "acme-text", "1.0")],
    license="Apache-2.0", desc="HTTP client")
metadata("org.acme", "acme-http", ["3.1.0"])
publish("org.acme", "acme-bom", "2.0.0", packaging="pom",
        extra="<dependencyManagement><dependencies><dependency><groupId>org.acme</groupId>"
              "<artifactId>acme-json</artifactId><version>1.2.0</version></dependency></dependencies></dependencyManagement>")
publish("org.acme", "acme-driver", "1.0", {
    "org/acme/driver/AcmeDriver.java": "package org.acme.driver;\npublic class AcmeDriver {}\n"},
    resources={"META-INF/services/java.sql.Driver": "org.acme.driver.AcmeDriver\n"}, license="Apache-2.0")
# --- Java 11-era library for the JVM-upgrade checks (compiled against the Java 11 API)
publish("org.old", "old-utils", "1.0", {
    "org/old/OldUtils.java": "package org.old;\nimport java.util.jar.Pack200;\npublic class OldUtils {\n"
        "  public static Object packer() { return Pack200.newPacker(); }\n"
        "  public static void compiler() { Compiler.disable(); }\n"
        "  public static void stop(Thread t) { t.stop(); }\n"
        "  public static void sandbox() { System.setSecurityManager(new SecurityManager()); }\n"
        "  public static Object js() throws Exception { return new javax.script.ScriptEngineManager().getEngineByName(\"nashorn\"); }\n"
        "  public static Object peek(Object o) throws Exception {\n"
        "    java.lang.reflect.Field f = Class.forName(\"java.lang.String\").getDeclaredField(\"value\");\n"
        "    f.setAccessible(true); return f.get(o); }\n"
        "  public static String time(java.time.LocalTime t) { return java.time.format.DateTimeFormatter.ofPattern(\"h:mm a\").format(t); }\n"
        "  protected void finalize() {}\n}\n",
    "org/old/shaded/asm/Opcodes.java": "package org.old.shaded.asm;\npublic interface Opcodes {\n"
        "  int V1_8 = 52; int V9 = 53; int V11 = 55; int V16 = 60;\n}\n"}, license="Apache-2.0")
publish("org.old", "old-internal", "1.0", {
    "org/old/internal/Names.java": "package org.old.internal;\npublic class Names {\n"
        "  public static Object root() throws Exception { return new sun.security.x509.X500Name(\"CN=x\"); }\n}\n"},
    javac=("-source", "11", "-target", "11", "--add-exports", "java.base/sun.security.x509=ALL-UNNAMED"), license="MIT")
publish("org.projectlombok", "lombok", "1.18.20", {
    "lombok/Getter.java": "package lombok;\npublic @interface Getter {}\n"}, license="MIT")
publish("com.legacy", "legacy-utils", "0.9", {
    "org/acme/core/util/OldStrings.java": "package org.acme.core.util;\npublic class OldStrings {}\n",
    "com/legacy/Util.java": "package com.legacy;\npublic class Util {}\n"}, license="BSD-3-Clause")
metadata("com.legacy", "legacy-utils", ["0.9", "1.0"])
publish("test.tools", "testkit", "5.0", {
    "test/tools/Assert.java": "package test.tools;\npublic class Assert {\n"
                              "  public static void that(boolean ok) { if (!ok) throw new AssertionError(); }\n}\n"},
    license="EPL-2.0")
metadata("test.tools", "testkit", ["5.0", "5.1"])

# --- Gradle multi-project build ---
(proj / "gradle").mkdir(parents=True, exist_ok=True)
(proj / "settings.gradle").write_text(
    "rootProject.name = 'fixture'\n"
    "dependencyResolutionManagement {\n"
    f"  repositories {{ maven {{ url = uri('{repo.as_uri()}') }} }}\n"
    "}\ninclude 'common', 'service', 'app'\n")
(proj / "gradle" / "libs.versions.toml").write_text(
    '[versions]\nacme-core = "1.0.0"\n\n[libraries]\n'
    'acme-core = { module = "org.acme:acme-core", version.ref = "acme-core" }\n'
    'acme-json = { group = "org.acme", name = "acme-json", version = "1.2.0" }\n'
    'acme-http = "org.acme:acme-http:3.1.0"\n'
    'acme-bom = { module = "org.acme:acme-bom", version = "2.0.0" }\n'
    'acme-driver = "org.acme:acme-driver:1.0"\n'
    'legacy-utils = { module = "com.legacy:legacy-utils", version = "0.9" }\n'
    'testkit = { module = "test.tools:testkit", version = "5.0" }\n')
(proj / "build.gradle").write_text("subprojects { tasks.withType(JavaCompile).configureEach { options.release = 11 } }\n")
(proj / "deploy").mkdir(exist_ok=True)
(proj / "deploy" / "app.env").write_text('JAVA_OPTS="-Xmx512m -XX:+UseConcMarkSweepGC --illegal-access=permit -XX:+UseG1GC"\n')
(proj / "deploy" / "Dockerfile").write_text('FROM eclipse-temurin:11\nENTRYPOINT ["java", "-XX:+UseParallelOldGC", "-jar", "app.jar"]\n')
for name, body in {
    "common": "plugins { id 'java-library' }\ndependencies {\n"
              "  api libs.acme.core\n  implementation libs.legacy.utils\n"
              "  compileOnly 'org.projectlombok:lombok:1.18.20'\n}\n",
    "service": "plugins { id 'java-library' }\ndependencies {\n"
               "  implementation project(':common')\n  implementation libs.acme.json\n"
               "  implementation libs.acme.driver\n  testImplementation libs.testkit\n"
               "  implementation 'org.old:old-utils:1.0'\n  implementation 'org.old:old-internal:1.0'\n}\n",
    "app": "plugins { id 'application' }\ndependencies {\n"
           "  implementation project(':service')\n  implementation platform(libs.acme.bom)\n"
           "  implementation libs.acme.http\n  implementation libs.testkit\n"
           "  runtimeOnly libs.acme.driver\n  runtimeOnly 'org.ghost:ghost:1.0'\n}\n",
}.items():
    (proj / name).mkdir(exist_ok=True)
    (proj / name / "build.gradle").write_text(body)

# --- sources. :common and :service compile; :app deliberately does not (missing import,
#     :common not on its compile classpath) so it exercises the import-scan fallback.
SRC = {
    "common/src/main/java/org/fixture/common/CommonUtil.java":
        "package org.fixture.common;\n\nimport org.acme.core.Core;\nimport org.acme.core.util.OldStrings;\n"
        "import java.util.List;\n\npublic class CommonUtil {\n"
        "  public static String clean(String s) { new OldStrings(); return Core.parse(s); }\n"
        "  public static String decode(byte[] b) { return new String(b); }\n}\n",
    "common/src/main/java/org/fixture/common/Plugin.java":
        "package org.fixture.common;\npublic interface Plugin { String name(); }\n",
    "service/src/main/java/org/fixture/service/Service.java":
        "package org.fixture.service;\n\nimport org.acme.json.Json;\nimport org.acme.json.*;\n"
        "import org.acme.core.Core;\nimport org.fixture.common.CommonUtil;\n"
        "import static org.acme.json.Parser.parse;\n\n/** import org.fake.Nope; */\n"
        "public class Service {\n  // import org.fake.InBody;\n"
        "  public Json json() { return new Json(); }\n"
        "  public String run(String s) throws Exception {\n"
        "    Class.forName(\"org.acme.json.Parser\");\n"
        "    org.old.OldUtils.packer();\n"
        "    return Core.parse(CommonUtil.clean(s)) + parse(s);\n  }\n}\n",
    "service/src/main/java/org/fixture/service/MyPlugin.java":
        "package org.fixture.service;\npublic class MyPlugin implements org.fixture.common.Plugin {\n"
        "  public String name() { return \"my\"; }\n}\n",
    "service/src/main/resources/META-INF/services/org.fixture.common.Plugin": "org.fixture.service.MyPlugin\n",
    "service/src/test/java/org/fixture/service/ServiceTest.java":
        "package org.fixture.service;\nimport test.tools.Assert;\nimport org.fixture.service.Service;\n"
        "class ServiceTest { void t() { Assert.that(true); } }\n",
    "app/src/main/java/org/fixture/app/App.java":
        "package org.fixture.app;\n\nimport org.acme.http.Client;\nimport org.acme.http.internal.Pool;\n"
        "import org.fixture.service.Service;\nimport org.fixture.common.CommonUtil;\n"
        "import com.missing.Thing;\nimport java.util.Map;\nimport org.acme.http.Client.Builder;\n\n"
        "public class App {}\n",
}
for rel, body in SRC.items():
    f = proj / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
shutil.rmtree(work, ignore_errors=True)
print(proj)
