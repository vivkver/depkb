package depkb;

import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class BuildFileTracker {

    private static final String GLOBAL = "GLOBAL";
    private static final Set<String> HASH_NAMES = Set.of(
        "settings.gradle", "settings.gradle.kts", "gradle.properties",
        "gradle.lockfile", "buildscript-gradle.lockfile", "gradle-wrapper.properties"
    );
    private static final Set<String> BUILD_EXTS = Set.of(
        ".gradle", ".gradle.kts", ".versions.toml", ".lockfile"
    );
    private static final Set<String> SKIP_DIRS = Set.of(
        ".git", ".gradle", "build", ".depkb", "node_modules", ".idea"
    );

    private BuildFileTracker() {}

    /** Walk the project tree and collect all build input file paths. */
    public static List<Path> discover(Path projectDir) {
        List<Path> result = new ArrayList<>();
        try {
            Files.walkFileTree(projectDir, new SimpleFileVisitor<>() {
                boolean inBuildSrc = false;

                @Override
                public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                    String name = dir.getFileName() != null ? dir.getFileName().toString() : "";
                    if (SKIP_DIRS.contains(name) && !dir.equals(projectDir)) {
                        return FileVisitResult.SKIP_SUBTREE;
                    }
                    if ("buildSrc".equals(name) || "build-logic".equals(name)) {
                        inBuildSrc = true;
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult postVisitDirectory(Path dir, IOException exc) {
                    String name = dir.getFileName() != null ? dir.getFileName().toString() : "";
                    if ("buildSrc".equals(name) || "build-logic".equals(name)) {
                        inBuildSrc = false;
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                    String name = file.getFileName().toString();
                    boolean isHashName = HASH_NAMES.contains(name);
                    boolean hasBuildExt = BUILD_EXTS.stream().anyMatch(name::endsWith);
                    boolean isSrc = inBuildSrc && (name.endsWith(".java") || name.endsWith(".kt") || name.endsWith(".groovy"));
                    if (isHashName || hasBuildExt || isSrc) {
                        result.add(file);
                    }
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException e) {
            System.err.println("depkb: warning: error discovering build files: " + e.getMessage());
        }
        return result;
    }

    /** Store build file stats in the DB. Call after each ingest. */
    public static void track(Connection conn, Path projectDir) throws SQLException {
        // Build a dir -> module path map
        List<String[]> dirmap = new ArrayList<>();
        try (var ps = conn.prepareStatement("SELECT path, dir FROM projects")) {
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    String p = rs.getString(1), d = rs.getString(2);
                    if (!":".equals(p) && d != null) dirmap.add(new String[]{d, p});
                }
            }
        }

        conn.createStatement().execute("DELETE FROM buildfiles");

        try (PreparedStatement ins = conn.prepareStatement(
                "INSERT INTO buildfiles VALUES(?,?,?,?,?)")) {
            for (Path f : discover(projectDir)) {
                String fname = f.getFileName().toString();
                String owner = fname.startsWith("settings.gradle") ? GLOBAL
                        : ownerOf(f.getParent(), dirmap);
                try {
                    BasicFileAttributes attrs = Files.readAttributes(f, BasicFileAttributes.class);
                    long mtime = attrs.lastModifiedTime().toMillis();
                    long size = attrs.size();
                    String sha = sha1(f);
                    ins.setString(1, f.toAbsolutePath().toString());
                    ins.setLong(2, mtime);
                    ins.setLong(3, size);
                    ins.setString(4, sha);
                    ins.setString(5, owner);
                    ins.addBatch();
                } catch (IOException ignored) {}
            }
            ins.executeBatch();
        }
    }

    /** Return modules whose build inputs changed since the last track() call. */
    public static Set<String> changedModules(Connection conn) throws SQLException {
        Set<String> changed = new HashSet<>();
        Set<String> tracked = new HashSet<>();

        try (var ps = conn.prepareStatement("SELECT path, mtime, size, sha, owner FROM buildfiles");
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) {
                String path = rs.getString(1);
                long mt = rs.getLong(2), sz = rs.getLong(3);
                String sha = rs.getString(4), owner = rs.getString(5);
                tracked.add(path);
                try {
                    var attrs = Files.readAttributes(Path.of(path), BasicFileAttributes.class);
                    if (attrs.lastModifiedTime().toMillis() == mt && attrs.size() == sz) continue;
                    // mtime or size changed; confirm by hash
                    if (sha1(Path.of(path)).equals(sha)) {
                        // hash unchanged — update mtime/size to avoid re-hashing next time
                        try (var up = conn.prepareStatement(
                                "UPDATE buildfiles SET mtime=?, size=? WHERE path=?")) {
                            up.setLong(1, attrs.lastModifiedTime().toMillis());
                            up.setLong(2, attrs.size());
                            up.setString(3, path);
                            up.executeUpdate();
                        }
                    } else {
                        changed.add(owner);
                    }
                } catch (IOException e) {
                    changed.add(owner); // file deleted
                }
            }
        }

        // Check for newly created build files not yet tracked
        try (var ps = conn.prepareStatement("SELECT path, dir FROM projects");
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) {
                String p = rs.getString(1), d = rs.getString(2);
                if (d == null) continue;
                for (String n : List.of("build.gradle", "build.gradle.kts", "gradle.lockfile")) {
                    String f = d + java.io.File.separator + n;
                    if (!tracked.contains(f) && Files.isRegularFile(Path.of(f))) {
                        changed.add(":".equals(p) ? GLOBAL : p);
                    }
                }
            }
        }

        return changed;
    }

    /** Return the set of modules that include any of `projects` in their graphs (must re-export them). */
    public static Set<String> dependents(Connection conn, Set<String> projects) throws SQLException {
        Set<String> out = new HashSet<>(projects);
        if (projects.isEmpty()) return out;
        String marks = "?,".repeat(projects.size());
        marks = marks.substring(0, marks.length() - 1);
        String sql = "SELECT DISTINCT p.project FROM nodes n JOIN comps c ON c.id=n.comp " +
                     "JOIN pcs p ON p.id=n.pc WHERE c.cid IN (" + marks + ")";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            int i = 1;
            for (String proj : projects) ps.setString(i++, "project " + proj);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) out.add(rs.getString(1));
            }
        }
        return out;
    }

    private static String ownerOf(Path dir, List<String[]> dirmap) {
        if (dir == null) return GLOBAL;
        String abs = dir.toAbsolutePath().toString();
        for (String[] entry : dirmap) {
            if (abs.equals(entry[0])) return entry[1];
        }
        return GLOBAL;
    }

    private static String sha1(Path path) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] data = Files.readAllBytes(path);
            byte[] hash = md.digest(data);
            StringBuilder sb = new StringBuilder();
            for (byte b : hash) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }
}
