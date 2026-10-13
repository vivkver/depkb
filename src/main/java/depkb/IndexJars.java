package depkb;

import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

public final class IndexJars {

    private IndexJars() {}

    public static void index(Connection conn, boolean withClasses) throws SQLException {
        // Find artifacts not yet indexed in pkginfo
        List<String[]> todo = new ArrayList<>();
        try (var ps = conn.prepareStatement(
                "SELECT a.ga, a.version, a.file FROM artifacts a " +
                "LEFT JOIN pkginfo p ON p.ga=a.ga AND p.version=a.version WHERE p.ga IS NULL");
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) todo.add(new String[]{rs.getString(1), rs.getString(2), rs.getString(3)});
        }
        if (todo.isEmpty()) return;

        List<PomRoot> roots = pomRoots(conn);
        int threads = Math.min(16, Runtime.getRuntime().availableProcessors() * 2);
        ExecutorService ex = Executors.newFixedThreadPool(threads);

        List<CompletableFuture<WorkResult>> futures = todo.stream()
            .map(row -> CompletableFuture.supplyAsync(() -> processOne(row, roots, withClasses), ex))
            .toList();

        ex.shutdown();

        try (PreparedStatement insPkg = conn.prepareStatement(
                    "INSERT OR REPLACE INTO pkginfo VALUES(?,?,?,?,?,?,?)");
             PreparedStatement delPkg = conn.prepareStatement(
                    "DELETE FROM packages WHERE ga=? AND version=?");
             PreparedStatement delCls = conn.prepareStatement(
                    "DELETE FROM classes WHERE ga=? AND version=?");
             PreparedStatement delSvc = conn.prepareStatement(
                    "DELETE FROM services WHERE ga=? AND version=?");
             PreparedStatement insPkgRow = conn.prepareStatement(
                    "INSERT INTO packages VALUES(?,?,?,?)");
             PreparedStatement insClsRow = conn.prepareStatement(
                    "INSERT INTO classes VALUES(?,?,?,?)");
             PreparedStatement insSvcRow = conn.prepareStatement(
                    "INSERT INTO services VALUES(?,?,?)")) {

            for (CompletableFuture<WorkResult> f : futures) {
                WorkResult r = f.join();
                insPkg.setString(1, r.ga); insPkg.setString(2, r.version);
                insPkg.setString(3, r.description); insPkg.setString(4, r.url);
                insPkg.setString(5, r.licenses); insPkg.setString(6, r.jpms);
                insPkg.setInt(7, r.total); insPkg.executeUpdate();

                delPkg.setString(1, r.ga); delPkg.setString(2, r.version); delPkg.executeUpdate();
                delCls.setString(1, r.ga); delCls.setString(2, r.version); delCls.executeUpdate();
                delSvc.setString(1, r.ga); delSvc.setString(2, r.version); delSvc.executeUpdate();

                for (String[] svc : r.services) {
                    insSvcRow.setString(1, r.ga); insSvcRow.setString(2, r.version);
                    insSvcRow.setString(3, svc[0]); insSvcRow.addBatch();
                }
                insSvcRow.executeBatch();
                for (Map.Entry<String, Integer> pkg : r.packages.entrySet()) {
                    insPkgRow.setString(1, pkg.getKey()); insPkgRow.setString(2, r.ga);
                    insPkgRow.setString(3, r.version); insPkgRow.setInt(4, pkg.getValue());
                    insPkgRow.addBatch();
                }
                insPkgRow.executeBatch();
                for (String[] cls : r.classes) {
                    insClsRow.setString(1, cls[0]); insClsRow.setString(2, cls[1]);
                    insClsRow.setString(3, r.ga); insClsRow.setString(4, r.version);
                    insClsRow.addBatch();
                }
                insClsRow.executeBatch();
            }
        }
        conn.commit();
    }

    // ---- per-jar work (runs in thread pool) ----

    private static WorkResult processOne(String[] row, List<PomRoot> roots, boolean withClasses) {
        String ga = row[0], version = row[1], file = row[2];
        String[] parts = ga.split(":", 2);
        String g = parts[0], a = parts.length > 1 ? parts[1] : parts[0];

        JarInfo jar = (file != null && file.endsWith(".jar")) ? scanJar(file, withClasses) : null;
        PomInfo pom = pomInfo(g, a, version, roots, file);

        WorkResult r = new WorkResult();
        r.ga = ga; r.version = version;
        if (jar != null) {
            r.packages = jar.packages;
            r.classes  = jar.classes;
            r.jpms     = jar.jpms;
            r.total    = jar.total;
            r.services = jar.services;
        }
        r.description = pom.description;
        r.url         = pom.url;
        r.licenses    = String.join("; ", pom.licenses);
        return r;
    }

    static JarInfo scanJar(String file, boolean withClasses) {
        Map<String, Integer> pk = new HashMap<>();
        List<String[]> cls = new ArrayList<>();
        List<String[]> services = new ArrayList<>();
        String jpms = null;
        int total = 0;
        try (ZipFile z = new ZipFile(file)) {
            java.util.Enumeration<? extends ZipEntry> entries = z.entries();
            List<String> names = new ArrayList<>();
            while (entries.hasMoreElements()) names.add(entries.nextElement().getName());
            // Collect services
            for (String n : names) {
                if (n.startsWith("META-INF/services/") && !n.endsWith("/")) {
                    services.add(new String[]{n.substring("META-INF/services/".length())});
                }
            }
            for (String n : names) {
                if (!n.endsWith(".class")) continue;
                // Multi-release jar: use base entry
                if (n.startsWith("META-INF/versions/")) {
                    String[] parts = n.split("/", 4);
                    if (parts.length < 4) continue;
                    n = parts[3];
                } else if (n.startsWith("META-INF/")) {
                    continue;
                }
                if (n.endsWith("module-info.class")) {
                    if (n.equals("module-info.class")) jpms = jpms == null ? "module-info" : jpms;
                    continue;
                }
                if (n.endsWith("package-info.class")) continue;
                int lastSlash = n.lastIndexOf('/');
                if (lastSlash < 0) continue; // default package — skip
                String base = n.substring(lastSlash + 1, n.length() - 6); // strip .class
                String pkg = n.substring(0, lastSlash).replace('/', '.');
                if (base.indexOf('$', 1) > 0) continue; // nested class
                pk.merge(pkg, 1, Integer::sum);
                total++;
                if (withClasses) cls.add(new String[]{base, pkg});
            }
            // Check MANIFEST for Automatic-Module-Name
            ZipEntry mf = z.getEntry("META-INF/MANIFEST.MF");
            if (mf != null && jpms == null) {
                String manifest = new String(z.getInputStream(mf).readAllBytes(), "UTF-8");
                java.util.regex.Matcher m = java.util.regex.Pattern
                    .compile("^Automatic-Module-Name:\\s*(\\S+)", java.util.regex.Pattern.MULTILINE)
                    .matcher(manifest);
                if (m.find()) jpms = m.group(1);
            }
        } catch (Exception e) {
            return null;
        }
        JarInfo info = new JarInfo();
        info.packages = pk; info.classes = cls; info.jpms = jpms;
        info.total = total; info.services = services;
        return info;
    }

    // ---- POM lookup ----

    record PomRoot(Path dir, String layout) {}

    static List<PomRoot> pomRoots(Connection conn) throws SQLException {
        List<PomRoot> roots = new ArrayList<>();
        String gradleHome = System.getenv("GRADLE_USER_HOME");
        Path guh = gradleHome != null ? Path.of(gradleHome)
                : Path.of(System.getProperty("user.home"), ".gradle");
        roots.add(new PomRoot(guh.resolve("caches/modules-2/files-2.1"), "cache"));
        roots.add(new PomRoot(Path.of(System.getProperty("user.home"), ".m2/repository"), "maven"));

        // Scan artifact paths for additional Maven local repos
        java.util.Set<String> seen = new java.util.HashSet<>();
        roots.forEach(r -> seen.add(r.dir().toString()));
        try (var ps = conn.prepareStatement("SELECT ga, version, file FROM artifacts");
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) {
                String ga = rs.getString(1), file = rs.getString(3);
                if (file == null || file.isEmpty() || file.contains("files-2.1")) continue;
                Path p = Path.of(file);
                String[] gParts = ga.split(":")[0].split("\\.");
                try {
                    Path root = p;
                    for (int i = 0; i < 2 + gParts.length; i++) root = root.getParent();
                    if (root != null && !seen.contains(root.toString()) && Files.isDirectory(root)) {
                        seen.add(root.toString());
                        roots.add(new PomRoot(root, "maven"));
                    }
                } catch (Exception ignored) {}
            }
        }
        return roots.stream().filter(r -> Files.isDirectory(r.dir())).toList();
    }

    static Path findPom(String g, String a, String v, List<PomRoot> roots, String hint) {
        String name = a + "-" + v + ".pom";
        if (hint != null && !hint.isEmpty()) {
            Path hp = Path.of(hint);
            Path sibling = hp.getParent().resolve(name);
            if (Files.isRegularFile(sibling)) return sibling;
            try (var stream = Files.list(hp.getParent().getParent())) {
                var found = stream.map(d -> d.resolve(name))
                        .filter(Files::isRegularFile).findFirst();
                if (found.isPresent()) return found.get();
            } catch (IOException ignored) {}
        }
        for (PomRoot root : roots) {
            if ("cache".equals(root.layout())) {
                Path dir = root.dir().resolve(g).resolve(a).resolve(v);
                if (Files.isDirectory(dir)) {
                    try (var stream = Files.list(dir)) {
                        var found = stream.flatMap(h -> {
                            try { return Files.list(h); } catch (IOException e) { return java.util.stream.Stream.empty(); }
                        }).filter(f -> f.getFileName().toString().equals(name)).findFirst();
                        if (found.isPresent()) return found.get();
                    } catch (IOException ignored) {}
                }
            } else {
                Path p = root.dir();
                for (String part : g.split("\\.")) p = p.resolve(part);
                p = p.resolve(a).resolve(v).resolve(name);
                if (Files.isRegularFile(p)) return p;
            }
        }
        return null;
    }

    static PomInfo parsePom(Path path) {
        try {
            DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
            dbf.setNamespaceAware(false);
            DocumentBuilder db = dbf.newDocumentBuilder();
            db.setErrorHandler(null);
            Document doc = db.parse(path.toFile());
            doc.getDocumentElement().normalize();
            Element r = doc.getDocumentElement();

            Map<String, String> props = new HashMap<>();
            NodeList propEls = r.getElementsByTagName("properties");
            if (propEls.getLength() > 0) {
                NodeList children = propEls.item(0).getChildNodes();
                for (int i = 0; i < children.getLength(); i++) {
                    if (children.item(i) instanceof Element e) {
                        props.put(e.getTagName(), e.getTextContent().strip());
                    }
                }
            }
            String version = text(r, "version");
            String groupId = text(r, "groupId");

            // Parent info
            String[] parent = null;
            NodeList pEl = r.getElementsByTagName("parent");
            if (pEl.getLength() > 0) {
                Element p = (Element) pEl.item(0);
                String pg = text(p, "groupId"), pa = text(p, "artifactId"), pv = text(p, "version");
                if (pg != null && pa != null) parent = new String[]{pg, pa, pv};
                if (groupId == null) groupId = pg;
                if (version == null) version = pv;
            }
            props.put("project.version", version != null ? version : "");
            props.put("version", version != null ? version : "");
            props.put("project.groupId", groupId != null ? groupId : "");
            final Map<String, String> fp = props;
            final String fVersion = version, fGroupId = groupId;

            String desc = expand(text(r, "description"), fp);
            String url  = expand(text(r, "url"), fp);
            List<String> lics = new ArrayList<>();
            NodeList licNames = r.getElementsByTagName("license");
            for (int i = 0; i < licNames.getLength(); i++) {
                String n = text((Element) licNames.item(i), "name");
                if (n != null) lics.add(n.replaceAll("\\s+", " ").strip());
            }

            PomInfo info = new PomInfo();
            info.description = desc; info.url = url; info.licenses = lics; info.parent = parent;
            return info;
        } catch (Exception e) {
            return new PomInfo();
        }
    }

    static PomInfo pomInfo(String g, String a, String v, List<PomRoot> roots, String hint) {
        PomInfo result = new PomInfo();
        Path path = findPom(g, a, v, roots, hint);
        int depth = 0;
        while (path != null && depth < 6) {
            PomInfo p = parsePom(path);
            if (result.description == null) result.description = p.description;
            if (result.url == null) result.url = p.url;
            if (result.licenses.isEmpty()) result.licenses = p.licenses;
            if ((!result.licenses.isEmpty() && result.description != null)
                    || p.parent == null || p.parent[0] == null) break;
            path = findPom(p.parent[0], p.parent[1], p.parent[2], roots, null);
            depth++;
        }
        return result;
    }

    private static String text(Element el, String tag) {
        NodeList nl = el.getElementsByTagName(tag);
        if (nl.getLength() == 0) return null;
        String t = nl.item(0).getTextContent();
        return t == null ? null : t.strip();
    }

    private static String expand(String s, Map<String, String> props) {
        if (s == null) return null;
        return java.util.regex.Pattern.compile("\\$\\{([^}]+)\\}")
            .matcher(s).replaceAll(m -> props.getOrDefault(m.group(1), m.group(0)));
    }

    // ---- result types ----

    static class JarInfo {
        Map<String, Integer> packages = new HashMap<>();
        List<String[]> classes = new ArrayList<>();
        List<String[]> services = new ArrayList<>();
        String jpms;
        int total;
    }

    static class PomInfo {
        String description;
        String url;
        List<String> licenses = new ArrayList<>();
        String[] parent; // {groupId, artifactId, version}
    }

    static class WorkResult {
        String ga, version, description, url, licenses, jpms;
        int total;
        Map<String, Integer> packages = new HashMap<>();
        List<String[]> classes = new ArrayList<>();
        List<String[]> services = new ArrayList<>();
    }
}
