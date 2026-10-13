package depkb;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.List;
import java.util.Set;

public final class GradleRunner {

    private GradleRunner() {}

    /**
     * Run depKbExport for the given modules (or all if modules is null).
     * Returns the export directory path, or null if Gradle reported errors and produced no data.
     */
    public static Path run(Path projectDir, BuildOptions opts, Set<String> modules) {
        Path initScript = InitScript.extract();
        Path export = projectDir.resolve(".depkb").resolve(modules == null ? "export" : "export-partial");
        deleteDir(export);

        List<String> tasks = buildTasks(modules);
        List<String> cmd = gradleCmd(projectDir);
        cmd.add("-q");
        cmd.add("-I");
        cmd.add(initScript.toString());
        cmd.addAll(tasks);
        cmd.add("-Ddepkb.out=" + export.toAbsolutePath());
        cmd.add("-Ddepkb.configs=" + opts.configs);
        cmd.add("--continue");
        cmd.add("--no-configuration-cache");
        if (!opts.noParallel) cmd.add("--parallel");
        if (opts.offline) cmd.add("--offline");
        if (opts.gradleArgs != null && !opts.gradleArgs.isBlank()) {
            cmd.addAll(Arrays.asList(opts.gradleArgs.split("\\s+")));
        }

        String scope = modules == null ? "all modules"
                : modules.size() + " module(s): " + modules.stream().sorted().limit(5).toList();
        System.err.println("depkb: Gradle export of " + scope + " ...");
        long t0 = System.currentTimeMillis();

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(projectDir.toFile());
        pb.redirectErrorStream(true);

        List<String> output = new ArrayList<>();
        int exitCode;
        try {
            Process p = pb.start();
            try (var reader = new java.io.BufferedReader(
                    new java.io.InputStreamReader(p.getInputStream()))) {
                String line;
                while ((line = reader.readLine()) != null) output.add(line);
            }
            exitCode = p.waitFor();
        } catch (IOException | InterruptedException e) {
            System.err.println("depkb: failed to run Gradle: " + e.getMessage());
            return null;
        }

        // Check for exported project JSON files
        Path projectsDir = export.resolve("projects");
        List<Path> files = new ArrayList<>();
        if (Files.isDirectory(projectsDir)) {
            try (var stream = Files.list(projectsDir)) {
                stream.filter(f -> f.toString().endsWith(".json")).forEach(files::add);
            } catch (IOException ignored) {}
        }

        if (exitCode != 0) {
            List<String> tail = output.stream()
                .filter(l -> !l.isBlank() && !l.contains("JAVA_TOOL_OPTIONS"))
                .toList();
            int start = Math.max(0, tail.size() - 15);
            System.err.println("depkb: Gradle reported errors (partial export kept):");
            tail.subList(start, tail.size()).forEach(l -> System.err.println("  " + l));
        }

        if (files.isEmpty()) return null;

        System.err.printf("depkb: exported %d module(s) in %ds%n",
                files.size(), (System.currentTimeMillis() - t0) / 1000);
        return export;
    }

    private static List<String> buildTasks(Set<String> modules) {
        if (modules == null) return new ArrayList<>(List.of("depKbExport"));
        List<String> tasks = new ArrayList<>();
        for (String m : modules) {
            tasks.add(":".equals(m) ? ":depKbExport" : m + ":depKbExport");
        }
        return tasks.isEmpty() ? List.of("depKbExport") : tasks;
    }

    /** Returns the Gradle command prefix: ./gradlew.bat (Windows), ./gradlew (Unix), or gradle. */
    static List<String> gradleCmd(Path projectDir) {
        boolean windows = System.getProperty("os.name", "").toLowerCase().contains("win");
        Path bat = projectDir.resolve("gradlew.bat");
        Path sh  = projectDir.resolve("gradlew");
        if (windows && Files.isRegularFile(bat)) return new ArrayList<>(List.of(bat.toString()));
        if (!windows && Files.isRegularFile(sh)) return new ArrayList<>(List.of(sh.toString()));
        // Windows: try gradlew.bat, then gradlew (Git Bash), then gradle
        if (windows && Files.isRegularFile(sh)) return new ArrayList<>(List.of("cmd", "/c", sh.toString()));
        String gradle = findOnPath("gradle");
        if (gradle == null) Db.die("no ./gradlew in the project and no `gradle` on PATH", 1);
        return new ArrayList<>(List.of(gradle));
    }

    private static String findOnPath(String name) {
        String path = System.getenv("PATH");
        if (path == null) return null;
        String ext = System.getProperty("os.name", "").toLowerCase().contains("win") ? ".exe" : "";
        for (String dir : path.split(java.io.File.pathSeparator)) {
            Path p = Path.of(dir, name + ext);
            if (Files.isRegularFile(p)) return p.toString();
        }
        return null;
    }

    private static void deleteDir(Path dir) {
        if (!Files.exists(dir)) return;
        try (var walk = Files.walk(dir)) {
            walk.sorted(java.util.Comparator.reverseOrder())
                .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
        } catch (IOException ignored) {}
    }
}
