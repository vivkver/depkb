package depkb.cmd;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import depkb.Db;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * Public factory — exposes the agent-integration and snapshot commands to Main.
 * Grouped here (like GraphCmds*) so the command classes can stay package-private.
 */
public final class AgentCmds {
    private AgentCmds() {}

    public static List<Object> create() {
        return List.of(new SnapshotCmd(), new InstallHooksCmd(),
                       new AgentsSnippetCmd(), new CopilotSnippetCmd(), new HookCmd());
    }

    // ---- shared helpers ----

    private static final Pattern SAFE_NAME = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]*");

    static boolean isWindows() {
        return System.getProperty("os.name", "").toLowerCase().contains("win");
    }

    /** Absolute path of the running depkb.jar, or null when running from classes (dev/tests). */
    static String jarPath() {
        try {
            java.net.URI uri = Main.class.getProtectionDomain().getCodeSource().getLocation().toURI();
            Path p = Paths.get(uri);
            if (Files.isRegularFile(p) && p.toString().toLowerCase().endsWith(".jar")) {
                return p.toAbsolutePath().normalize().toString();
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /** Absolute path of the current `java` launcher. */
    static String javaBin() {
        String home = System.getProperty("java.home");
        if (home != null && !home.isBlank()) {
            Path b = Paths.get(home, "bin", "java" + (isWindows() ? ".exe" : ""));
            if (Files.isRegularFile(b)) return b.toString();
        }
        return "java";
    }

    /** Human/shell invocation string for docs and hook commands, e.g. {@code java -jar /abs/depkb.jar}. */
    static String invocation() {
        String jar = jarPath();
        return javaBinQuoted() + " -jar " + quoteIfNeeded(jar != null ? jar : "depkb.jar");
    }

    private static String javaBinQuoted() {
        return quoteIfNeeded(javaBin());
    }

    /** argv prefix for launching this same JAR as a subprocess, or null when not running from a JAR. */
    static List<String> selfArgv() {
        String jar = jarPath();
        if (jar == null) return null;
        List<String> argv = new ArrayList<>();
        argv.add(javaBin());
        argv.add("-jar");
        argv.add(jar);
        return argv;
    }

    static String quoteIfNeeded(String s) {
        return s != null && s.contains(" ") ? "\"" + s + "\"" : s;
    }

    /** Repo root = the directory that contains the {@code .depkb} folder. */
    static Path repoRoot(Path db) {
        Path dir = db.getParent();
        if (dir != null && dir.getFileName() != null && ".depkb".equals(dir.getFileName().toString())) {
            Path root = dir.getParent();
            if (root != null) return root;
        }
        return Paths.get("").toAbsolutePath();
    }

    static Path snapshotsDir(Path db) {
        return db.getParent().resolve("snapshots");
    }

    static Path snapshotFile(Path db, String name) {
        return snapshotsDir(db).resolve(name + ".db");
    }

    static void requireSafeName(String name) {
        if (name == null || name.isBlank() || !SAFE_NAME.matcher(name).matches()) {
            Db.die("invalid snapshot name '" + name + "' (use letters, digits, '.', '_', '-')", 1);
        }
    }

    /** Load a bundled agent doc from resources and substitute the depkb invocation. */
    static String agentDoc(String resource) {
        try (InputStream in = AgentCmds.class.getResourceAsStream(resource)) {
            if (in == null) {
                Db.die("bundled resource missing from JAR: " + resource, 1);
            }
            String text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            return text.replace("__DEPKB__", invocation());
        } catch (IOException e) {
            Db.die("cannot read " + resource + ": " + e.getMessage(), 1);
            return null; // unreachable
        }
    }
}

// ---- snapshot ----

@Command(name = "snapshot", description = "Save, list or delete named KB snapshots (for diffing before/after a change).")
class SnapshotCmd implements Runnable {
    @ParentCommand Main parent;

    @Parameters(index = "0", arity = "0..1", description = "save | list | delete  (default: list)")
    String action;

    @Parameters(index = "1", arity = "0..1", description = "snapshot name (for save/delete)")
    String name;

    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        Path db = Db.findDb(parent.db);
        String act = action == null ? "list" : action.toLowerCase();
        switch (act) {
            case "list" -> list(db);
            case "save" -> save(db);
            case "delete", "rm", "del" -> delete(db);
            default -> Db.die("unknown snapshot action '" + action + "' (use save | list | delete)", 1);
        }
    }

    private void save(Path db) {
        AgentCmds.requireSafeName(name);
        Connection conn = Db.connect(db, true); // must have been built
        try {
            Path dir = AgentCmds.snapshotsDir(db);
            Files.createDirectories(dir);
            Path target = AgentCmds.snapshotFile(db, name);
            boolean overwrite = Files.exists(target);
            Files.deleteIfExists(target);
            Files.deleteIfExists(Paths.get(target + "-wal"));
            Files.deleteIfExists(Paths.get(target + "-shm"));

            String lit = target.toString().replace('\\', '/').replace("'", "''");
            try (Statement st = conn.createStatement()) {
                st.execute("VACUUM INTO '" + lit + "'");
            }

            long libs = Lookups.scalar(conn, "SELECT count(DISTINCT ga) FROM comps WHERE kind='m'");
            long projs = Lookups.scalar(conn, "SELECT count(*) FROM projects");
            System.out.println((overwrite ? "overwrote" : "saved") + " snapshot '" + name + "'  "
                    + projs + " modules, " + libs + " libraries  -> " + target);
        } catch (Exception e) {
            Db.die("snapshot save failed: " + e.getMessage(), 1);
        }
    }

    private void list(Path db) {
        Path dir = AgentCmds.snapshotsDir(db);
        if (!Files.isDirectory(dir)) {
            System.out.println("no snapshots (save one with: snapshot save <name>)");
            return;
        }
        List<Path> files = new ArrayList<>();
        try (var stream = Files.list(dir)) {
            stream.filter(p -> p.getFileName().toString().endsWith(".db")).forEach(files::add);
        } catch (IOException e) {
            Db.die("cannot list snapshots: " + e.getMessage(), 1);
        }
        files.sort(Comparator.comparingLong((Path p) -> p.toFile().lastModified()).reversed());
        if (files.isEmpty()) {
            System.out.println("no snapshots (save one with: snapshot save <name>)");
            return;
        }
        List<String> lines = new ArrayList<>();
        for (Path f : files) {
            String nm = f.getFileName().toString().replaceFirst("\\.db$", "");
            String builtAt = "?";
            long libs = 0;
            try (Connection c = Db.openReadOnly(f)) {
                builtAt = Db.meta(c, "built_at", "?");
                libs = Lookups.scalar(c, "SELECT count(DISTINCT ga) FROM comps WHERE kind='m'");
            } catch (Exception ignored) {
            }
            java.time.Instant saved = java.time.Instant.ofEpochMilli(f.toFile().lastModified());
            lines.add(nm + "  " + libs + " libs  saved " + saved.toString().replace("T", " ").replaceFirst("\\..*", "")
                    + "  (built " + builtAt + ")");
        }
        System.out.println(files.size() + " snapshot" + (files.size() == 1 ? "" : "s") + ":");
        Lookups.emit(lines, limit);
    }

    private void delete(Path db) {
        AgentCmds.requireSafeName(name);
        Path target = AgentCmds.snapshotFile(db, name);
        if (!Files.exists(target)) Db.die("no snapshot named '" + name + "'", 1);
        try {
            Files.deleteIfExists(target);
            Files.deleteIfExists(Paths.get(target + "-wal"));
            Files.deleteIfExists(Paths.get(target + "-shm"));
            System.out.println("deleted snapshot '" + name + "'");
        } catch (IOException e) {
            Db.die("cannot delete snapshot: " + e.getMessage(), 1);
        }
    }
}

// ---- install-hooks ----

@Command(name = "install-hooks", description = "Install Claude Code hooks (and the gradle-deps skill) into the repo's .claude/ folder.")
class InstallHooksCmd implements Runnable {
    @ParentCommand Main parent;

    @Option(names = {"-d", "--project-dir"}, description = "Repo root (default: the folder containing .depkb)")
    String projectDir;

    @Option(names = "--no-skill", description = "Do not write the gradle-deps SKILL.md")
    boolean noSkill;

    @Option(names = "--print", description = "Show the merged settings.json without writing anything")
    boolean dryRun;

    @Override public void run() {
        Path root = projectDir != null
                ? Path.of(projectDir).toAbsolutePath().normalize()
                : AgentCmds.repoRoot(Db.findDb(parent.db));
        if (AgentCmds.jarPath() == null) {
            System.out.println("note: not running from a JAR; hook commands will assume 'depkb.jar' is on PATH");
        }
        String self = AgentCmds.invocation();
        try {
            mergeHooks(root.resolve(".claude").resolve("settings.json"), self);
            if (!noSkill) writeSkill(root);
        } catch (IOException e) {
            Db.die("install-hooks failed: " + e.getMessage(), 1);
        }
    }

    private void mergeHooks(Path settings, String self) throws IOException {
        ObjectMapper om = new ObjectMapper();
        ObjectNode rootNode;
        if (Files.isRegularFile(settings)) {
            JsonNode existing = om.readTree(settings.toFile());
            rootNode = existing instanceof ObjectNode on ? on : om.createObjectNode();
        } else {
            rootNode = om.createObjectNode();
        }
        ObjectNode hooks = rootNode.get("hooks") instanceof ObjectNode on ? on : om.createObjectNode();
        rootNode.set("hooks", hooks);

        addHook(om, hooks, "SessionStart", null, self, "session", 60);
        addHook(om, hooks, "PreToolUse", "Bash", self, "guard", 10);
        addHook(om, hooks, "PostToolUse", "Edit|Write|MultiEdit", self, "after-edit", 900);

        String json = om.writerWithDefaultPrettyPrinter().writeValueAsString(rootNode);
        if (dryRun) {
            System.out.println("would write " + settings + ":");
            System.out.println(json);
            return;
        }
        Files.createDirectories(settings.getParent());
        Files.writeString(settings, json + System.lineSeparator());
        System.out.println("installed hooks -> " + settings);
    }

    /** Replace any prior depkb hook group for this event, then append a fresh one (idempotent). */
    private static void addHook(ObjectMapper om, ObjectNode hooks, String event, String matcher,
                                String self, String mode, int timeout) {
        String command = self + " hook " + mode;
        ArrayNode arr = hooks.get(event) instanceof ArrayNode a ? a : om.createArrayNode();
        hooks.set(event, arr);

        // drop our previous group(s) for this mode
        for (int i = arr.size() - 1; i >= 0; i--) {
            JsonNode group = arr.get(i);
            JsonNode inner = group.path("hooks");
            boolean ours = false;
            if (inner.isArray()) {
                for (JsonNode h : inner) {
                    if (h.path("command").asText("").contains("hook " + mode)) { ours = true; break; }
                }
            }
            if (ours) arr.remove(i);
        }

        ObjectNode group = om.createObjectNode();
        if (matcher != null) group.put("matcher", matcher);
        ArrayNode inner = group.putArray("hooks");
        ObjectNode h = inner.addObject();
        h.put("type", "command");
        h.put("command", command);
        h.put("timeout", timeout);
        arr.add(group);
    }

    private void writeSkill(Path root) throws IOException {
        Path dest = root.resolve(".claude").resolve("skills").resolve("gradle-deps").resolve("SKILL.md");
        String content = AgentCmds.agentDoc("/agent/SKILL.md");
        if (dryRun) {
            System.out.println("would write " + dest);
            return;
        }
        Files.createDirectories(dest.getParent());
        Files.writeString(dest, content);
        System.out.println("installed skill -> " + dest);
    }
}

// ---- agents-snippet ----

@Command(name = "agents-snippet", description = "Print the AGENTS.md block for Devin and other agents.")
class AgentsSnippetCmd implements Runnable {
    @Override public void run() {
        System.out.print(AgentCmds.agentDoc("/agent/AGENTS.snippet.md"));
    }
}

// ---- copilot-snippet ----

@Command(name = "copilot-snippet", description = "Print the .github/copilot-instructions.md block for GitHub Copilot.")
class CopilotSnippetCmd implements Runnable {
    @Override public void run() {
        System.out.print(AgentCmds.agentDoc("/agent/copilot-instructions.md"));
    }
}

// ---- hook (internal) ----

@Command(name = "hook", hidden = true,
        description = "Internal: Claude Code hook entry point (session | guard | after-edit). Reads hook JSON on stdin.")
class HookCmd implements Runnable {
    @ParentCommand Main parent;

    @Parameters(index = "0", description = "session | guard | after-edit")
    String mode;

    private static final Pattern BUILD_FILE = Pattern.compile(
            "(build\\.gradle(\\.kts)?|settings\\.gradle(\\.kts)?|\\.versions\\.toml|gradle\\.lockfile|gradle\\.properties)$");
    private static final Pattern DEP_TASKS = Pattern.compile(
            "\\b(gradlew?|gradle\\.bat|gradlew\\.bat)\\b.*\\b(dependencies|dependencyInsight|"
            + "[\\w:-]*:dependencies|htmlDependencyReport|buildEnvironment)\\b");
    private static final Pattern PROBLEM = Pattern.compile("BREAKS|^\\s+(ERROR|MISMATCH)|^\\s{4}\\S.* missing$");

    @Override public void run() {
        // A broken hook must never block the user's work: swallow everything, exit 0.
        try {
            ObjectMapper om = new ObjectMapper();
            JsonNode data;
            try {
                byte[] stdin = System.in.readAllBytes();
                data = stdin.length == 0 ? om.createObjectNode() : om.readTree(stdin);
            } catch (Exception e) {
                data = om.createObjectNode();
            }
            switch (mode == null ? "" : mode) {
                case "guard" -> guard(om, data);
                case "after-edit" -> afterEdit(om, data);
                case "session" -> session();
                default -> { }
            }
        } catch (Throwable ignored) {
        }
    }

    private void guard(ObjectMapper om, JsonNode data) {
        String cmd = data.path("tool_input").path("command").asText("");
        if (!cmd.isEmpty() && DEP_TASKS.matcher(cmd).find() && !cmd.contains("--depkb-allow")) {
            String depkb = AgentCmds.invocation();
            ObjectNode out = om.createObjectNode();
            ObjectNode spec = out.putObject("hookSpecificOutput");
            spec.put("hookEventName", "PreToolUse");
            spec.put("permissionDecision", "deny");
            spec.put("permissionDecisionReason",
                    "Dependency reports are huge and miss resolution detail. Use the depkb knowledge base "
                    + "instead: `" + depkb + " why <lib> -p <module>`, `usage <lib>`, `deps <module>`, "
                    + "`runtime <lib>` (see the gradle-deps skill). If you really need the raw Gradle report, "
                    + "append --depkb-allow.");
            System.out.println(out);
        }
    }

    private void afterEdit(ObjectMapper om, JsonNode data) {
        String path = data.path("tool_input").path("file_path").asText("");
        if (path.isEmpty() || !BUILD_FILE.matcher(path).find()) return;

        String[] compat = runSelf(List.of("compat", "--limit", "15"), 900);
        String[] missing = runSelf(List.of("missing", "--limit", "10"), 120);
        if (compat == null && missing == null) return; // dev mode: can't shell to self

        String combined = (compat != null ? compat[0] : "") + "\n" + (missing != null ? missing[0] : "");
        List<String> problems = new ArrayList<>();
        for (String ln : combined.split("\n")) {
            if (PROBLEM.matcher(ln).find()) problems.add(ln);
        }
        String compatErr = compat != null ? compat[1] : "";
        boolean refreshFailed = compatErr.contains("refresh failed");
        if (problems.isEmpty() && !refreshFailed) return;

        String fileName = Path.of(path).getFileName().toString();
        StringBuilder msg = new StringBuilder("depkb checked the change to " + fileName + ":\n");
        if (refreshFailed) {
            String tail = compatErr.length() > 1500 ? compatErr.substring(compatErr.length() - 1500) : compatErr;
            msg.append("Gradle could not re-resolve the build:\n").append(tail).append("\n");
        }
        msg.append(String.join("\n", problems.subList(0, Math.min(25, problems.size()))));
        msg.append("\nDetails: depkb compat / depkb missing / depkb explain <Class>.");

        ObjectNode out = om.createObjectNode();
        ObjectNode spec = out.putObject("hookSpecificOutput");
        spec.put("hookEventName", "PostToolUse");
        spec.put("additionalContext", msg.toString());
        System.out.println(out);
    }

    private void session() {
        String[] status = runSelf(List.of("--no-refresh", "status", "--limit", "3"), 60);
        if (status == null) return;
        String out = status[0].strip();
        if (!out.isEmpty() && !out.contains("no knowledge base")) {
            String first = out.split("\n")[0];
            System.out.println("Gradle dependency knowledge base available (gradle-deps skill, `"
                    + AgentCmds.invocation() + " <command>`): " + first);
        }
    }

    /** Run this same JAR as a subprocess; returns {stdout, stderr} or null if not runnable. */
    private static String[] runSelf(List<String> extra, long timeoutSec) {
        List<String> argv = AgentCmds.selfArgv();
        if (argv == null) return null;
        argv.addAll(extra);
        try {
            ProcessBuilder pb = new ProcessBuilder(argv);
            String root = System.getenv("CLAUDE_PROJECT_DIR");
            pb.directory(new File(root != null && !root.isBlank() ? root : System.getProperty("user.dir")));
            Process p = pb.start();
            CompletableFuture<String> out = readAsync(p.getInputStream());
            CompletableFuture<String> err = readAsync(p.getErrorStream());
            if (!p.waitFor(timeoutSec, TimeUnit.SECONDS)) {
                p.destroyForcibly();
                return null;
            }
            return new String[]{out.get(2, TimeUnit.SECONDS), err.get(2, TimeUnit.SECONDS)};
        } catch (Exception e) {
            return null;
        }
    }

    private static CompletableFuture<String> readAsync(InputStream in) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                return new String(in.readAllBytes(), StandardCharsets.UTF_8);
            } catch (IOException e) {
                return "";
            }
        });
    }
}
