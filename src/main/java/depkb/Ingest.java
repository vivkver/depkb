package depkb;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

public final class Ingest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final DateTimeFormatter TIMESTAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final Pattern PRE = Pattern.compile(
        "(?i)(alpha|beta|rc|cr|milestone|preview|snapshot|ea|dev|incubating|[.-]m\\d+|[.-]b\\d+)");

    private Ingest() {}

    /**
     * Load an export directory into the DB.
     * @param replace  null = full rebuild; otherwise only the listed module paths
     */
    public static void ingest(Connection conn, Path exportDir, Path projectDir,
                              BuildOptions opts, Set<String> replace) throws Exception {
        long t0 = System.currentTimeMillis();

        // Collect all JSON files from export/projects/
        Path projectsDir = exportDir.resolve("projects");
        List<Path> files = new ArrayList<>();
        if (Files.isDirectory(projectsDir)) {
            try (var stream = Files.list(projectsDir)) {
                stream.filter(f -> f.getFileName().toString().endsWith(".json"))
                      .sorted().forEach(files::add);
            }
        }

        List<JsonNode> docs = new ArrayList<>();
        for (Path f : files) {
            docs.add(JSON.readTree(f.toFile()));
        }

        conn.setAutoCommit(false);

        if (replace == null) {
            // Full rebuild: delete graph data
            for (String t : List.of("projects","pcs","comps","nodes","edges","unresolved","sourcesets","links","declared")) {
                conn.createStatement().execute("DELETE FROM " + t);
            }
        } else {
            // Partial: delete only the modules being replaced
            for (JsonNode d : docs) {
                String pj = d.path("project").asText();
                List<Long> ids = new ArrayList<>();
                try (PreparedStatement ps = conn.prepareStatement("SELECT id FROM pcs WHERE project=?")) {
                    ps.setString(1, pj);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) ids.add(rs.getLong(1));
                    }
                }
                for (long id : ids) {
                    for (String[] tc : List.of(new String[]{"nodes","pc"}, new String[]{"edges","pc"}, new String[]{"unresolved","pc"})) {
                        try (PreparedStatement ps = conn.prepareStatement("DELETE FROM " + tc[0] + " WHERE " + tc[1] + "=?")) {
                            ps.setLong(1, id); ps.executeUpdate();
                        }
                    }
                }
                for (String[] tc : List.of(
                        new String[]{"pcs","project"}, new String[]{"projects","path"},
                        new String[]{"sourcesets","project"}, new String[]{"links","project"},
                        new String[]{"declared","project"})) {
                    try (PreparedStatement ps = conn.prepareStatement("DELETE FROM " + tc[0] + " WHERE " + tc[1] + "=?")) {
                        ps.setString(1, pj); ps.executeUpdate();
                    }
                }
            }
        }

        // Load existing comp_ids cache
        Map<String, Long> compIds = new HashMap<>();
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT cid, id FROM comps")) {
            while (rs.next()) compIds.put(rs.getString(1), rs.getLong(2));
        }

        Map<String, String> arts = new HashMap<>();
        int errors = 0;

        try (PreparedStatement insPrj = conn.prepareStatement(
                    "INSERT OR REPLACE INTO projects VALUES(?,?,?,?,?,?)");
             PreparedStatement insSS = conn.prepareStatement(
                    "INSERT INTO sourcesets VALUES(?,?,?,?,?)");
             PreparedStatement insDecl = conn.prepareStatement(
                    "INSERT INTO declared VALUES(?,?,?,?,?)");
             PreparedStatement insPcs = conn.prepareStatement(
                    "INSERT INTO pcs(project, config, error) VALUES(?,?,?)");
             PreparedStatement insPcRoot = conn.prepareStatement(
                    "INSERT INTO pcs(project, config, root) VALUES(?,?,?)");
             PreparedStatement insEdge = conn.prepareStatement(
                    "INSERT INTO edges VALUES(?,?,?,?,?)");
             PreparedStatement insNode = conn.prepareStatement(
                    "INSERT INTO nodes VALUES(?,?,?,?,?)");
             PreparedStatement insUnres = conn.prepareStatement(
                    "INSERT INTO unresolved VALUES(?,?,?,?)");
             PreparedStatement insComp = conn.prepareStatement(
                    "INSERT INTO comps(cid, ga, version, kind) VALUES(?,?,?,?)")) {

            for (JsonNode d : docs) {
                String project = d.path("project").asText();
                String name = d.path("name").asText();
                String dir  = d.path("dir").isMissingNode() ? null : d.path("dir").asText();
                String kind = d.path("kind").asText("none");
                int javalib = d.path("javaLibrary").asBoolean() ? 1 : 0;
                String java = d.path("java").isMissingNode() ? "{}" : d.path("java").toString();

                insPrj.setString(1, project); insPrj.setString(2, name); insPrj.setString(3, dir);
                insPrj.setString(4, kind); insPrj.setInt(5, javalib); insPrj.setString(6, java);
                insPrj.executeUpdate();

                // Source sets
                JsonNode ss = d.path("sourceSets");
                if (ss.isArray()) {
                    for (JsonNode s : ss) {
                        String ssName = s.path("name").asText();
                        String cp = s.path("cp").asText("");
                        for (JsonNode xd : s.path("dirs")) {
                            insSS.setString(1, project); insSS.setString(2, ssName);
                            insSS.setString(3, xd.asText()); insSS.setString(4, cp);
                            insSS.setString(5, "src"); insSS.addBatch();
                        }
                        for (JsonNode xd : s.path("classes")) {
                            insSS.setString(1, project); insSS.setString(2, ssName);
                            insSS.setString(3, xd.asText()); insSS.setString(4, cp);
                            insSS.setString(5, "classes"); insSS.addBatch();
                        }
                    }
                    insSS.executeBatch();
                }

                // Declared deps
                JsonNode decl = d.path("declared");
                if (decl.isArray()) {
                    for (JsonNode e : decl) {
                        insDecl.setString(1, project);
                        insDecl.setString(2, e.path("b").asText());
                        insDecl.setString(3, e.path("id").asText());
                        insDecl.setString(4, e.path("v").asText(""));
                        insDecl.setInt(5, e.path("platform").asInt(0));
                        insDecl.addBatch();
                    }
                    insDecl.executeBatch();
                }

                // Configs
                JsonNode configs = d.path("configs");
                if (configs.isObject()) {
                    Iterator<Map.Entry<String, JsonNode>> cfgIt = configs.fields();
                    while (cfgIt.hasNext()) {
                        Map.Entry<String, JsonNode> entry = cfgIt.next();
                        String cfg = entry.getKey();
                        JsonNode c = entry.getValue();

                        if (c.has("error")) {
                            errors++;
                            insPcs.setString(1, project); insPcs.setString(2, cfg);
                            insPcs.setString(3, c.path("error").asText());
                            insPcs.executeUpdate();
                            continue;
                        }

                        long root = compId(c.path("root").asText(), compIds, insComp, conn);
                        insPcRoot.setString(1, project); insPcRoot.setString(2, cfg);
                        insPcRoot.setLong(3, root);
                        long pc = executeInsertReturnId(conn, insPcRoot);

                        // Map: compId -> set of requested versions
                        Map<Long, List<String>> reqs = new HashMap<>();
                        // Set of direct children
                        java.util.Set<Long> direct = new java.util.HashSet<>();

                        // Edges
                        JsonNode edgesArr = c.path("edges");
                        if (edgesArr.isArray()) {
                            for (JsonNode e : edgesArr) {
                                long fi = compId(e.path("f").asText(), compIds, insComp, conn);
                                long ti = compId(e.path("t").asText(), compIds, insComp, conn);
                                int cons = e.path("c").asInt(0);
                                String q = e.path("q").asText("");
                                insEdge.setLong(1, pc); insEdge.setLong(2, fi);
                                insEdge.setLong(3, ti); insEdge.setString(4, q);
                                insEdge.setInt(5, cons); insEdge.addBatch();
                                String rv = reqVersion(q);
                                if (!rv.isEmpty()) reqs.computeIfAbsent(ti, k -> new ArrayList<>()).add(rv);
                                if (fi == root && cons == 0) direct.add(ti);
                            }
                            insEdge.executeBatch();
                        }

                        // Nodes
                        JsonNode comps = c.path("components");
                        if (comps.isObject()) {
                            Iterator<Map.Entry<String, JsonNode>> it = comps.fields();
                            while (it.hasNext()) {
                                Map.Entry<String, JsonNode> ce = it.next();
                                long ci = compId(ce.getKey(), compIds, insComp, conn);
                                List<String> rv = reqs.getOrDefault(ci, List.of());
                                rv.sort(Ingest::vkeyCompare);
                                StringBuilder reasons = new StringBuilder();
                                JsonNode r = ce.getValue().path("r");
                                if (r.isArray()) {
                                    for (JsonNode reason : r) {
                                        if (reasons.length() > 0) reasons.append("; ");
                                        reasons.append(reason.asText());
                                    }
                                }
                                insNode.setLong(1, pc);
                                insNode.setLong(2, ci);
                                insNode.setInt(3, direct.contains(ci) ? 1 : 0);
                                insNode.setString(4, String.join(",", rv));
                                insNode.setString(5, reasons.toString());
                                insNode.addBatch();
                            }
                            insNode.executeBatch();
                        }

                        // Unresolved
                        JsonNode unresArr = c.path("unresolved");
                        if (unresArr.isArray()) {
                            for (JsonNode u : unresArr) {
                                long fi = compId(u.path("f").asText(), compIds, insComp, conn);
                                insUnres.setLong(1, pc); insUnres.setLong(2, fi);
                                insUnres.setString(3, u.path("q").asText(""));
                                insUnres.setString(4, u.path("e").asText(""));
                                insUnres.addBatch();
                            }
                            insUnres.executeBatch();
                        }

                        // Artifacts
                        JsonNode artsArr = c.path("artifacts");
                        if (artsArr.isArray()) {
                            for (JsonNode art : artsArr) {
                                String[] gav = splitCid(art.path("id").asText());
                                arts.putIfAbsent(gav[0] + "|" + gav[1], art.path("file").asText(""));
                            }
                        }
                    }
                }
            }
        }

        // Insert artifacts
        try (PreparedStatement insArt = conn.prepareStatement(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?)")) {
            for (Map.Entry<String, String> e : arts.entrySet()) {
                String[] kv = e.getKey().split("\\|", 2);
                insArt.setString(1, kv[0]); insArt.setString(2, kv[1]);
                insArt.setString(3, e.getValue()); insArt.addBatch();
            }
            insArt.executeBatch();
        }
        // Ensure BOMs/POM-only modules get an empty artifact row for POM lookups
        conn.createStatement().execute(
            "INSERT OR IGNORE INTO artifacts SELECT DISTINCT ga, version, '' FROM comps " +
            "WHERE kind='m' AND version != ''");

        // Prune orphan comps for partial ingests
        if (replace != null) {
            conn.createStatement().execute(
                "DELETE FROM comps WHERE id NOT IN (SELECT comp FROM nodes) " +
                "AND id NOT IN (SELECT root FROM pcs WHERE root IS NOT NULL)");
        }

        conn.commit();

        // Post-ingest indexing — run with auto-commit still off; each sub-call commits its own work
        if (!opts.noJars) IndexJars.index(conn, !opts.noClasses);
        conn.createStatement().execute("DELETE FROM catalog");
        CatalogLoader.load(conn, projectDir);
        Db.setMeta(conn, "built_at", LocalDateTime.now().format(TIMESTAMP));
        Db.setMeta(conn, "project_dir", projectDir.toAbsolutePath().toString());
        Db.setMeta(conn, "configs", opts.configs);
        conn.commit();

        BuildFileTracker.track(conn, projectDir);
        conn.commit();

        conn.setAutoCommit(true);

        long n_proj, n_mod, n_src;
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT count(*) FROM projects")) {
            rs.next(); n_proj = rs.getLong(1);
        }
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT count(DISTINCT ga) FROM comps WHERE kind='m'")) {
            rs.next(); n_mod = rs.getLong(1);
        }
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT count(*) FROM srcfiles")) {
            rs.next(); n_src = rs.getLong(1);
        }
        long elapsed = (System.currentTimeMillis() - t0) / 1000;
        String msg = String.format("depkb: indexed %d modules, %d external libraries, %d source files",
                n_proj, n_mod, n_src);
        if (errors > 0) msg += String.format(", %d configs failed to resolve (see `depkb status`)", errors);
        msg += " in " + elapsed + "s";
        System.err.println(msg);
    }

    // ---- utilities ----

    private static long compId(String cid, Map<String, Long> cache,
                                PreparedStatement ins, Connection conn) throws SQLException {
        Long i = cache.get(cid);
        if (i != null) return i;
        String[] parts = splitCid(cid);
        ins.setString(1, cid); ins.setString(2, parts[0]);
        ins.setString(3, parts[1]); ins.setString(4, parts[2]);
        ins.executeUpdate();
        try (ResultSet rs = conn.createStatement().executeQuery("SELECT last_insert_rowid()")) {
            long id = rs.getLong(1);
            cache.put(cid, id);
            return id;
        }
    }

    private static long executeInsertReturnId(Connection conn, PreparedStatement ps) throws SQLException {
        ps.executeUpdate();
        try (ResultSet rs = conn.createStatement().executeQuery("SELECT last_insert_rowid()")) {
            return rs.getLong(1);
        }
    }

    /** "org.acme:core:1.0.0" -> ["org.acme:core", "1.0.0", "m"];
     *  "project :app"         -> ["project :app", "", "p"] */
    public static String[] splitCid(String cid) {
        if (cid.startsWith("project ")) return new String[]{cid, "", "p"};
        String[] p = cid.split(":", 3);
        if (p.length == 3) return new String[]{p[0] + ":" + p[1], p[2], "m"};
        return new String[]{cid, "", "m"};
    }

    /** Extract the requested version from an edge's q string ("group:artifact:1.0.0" -> "1.0.0"). */
    public static String reqVersion(String q) {
        if (q.startsWith("project ")) return "";
        String[] p = q.split(":", 3);
        return p.length == 3 ? p[2] : "";
    }

    /** Return the short form: "org.acme:core:1.0.0" -> "core:1.0.0"; "project :app" -> ":app". */
    public static String shortCid(String cid) {
        if (cid.startsWith("project ")) return cid.substring(8);
        String[] p = cid.split(":");
        return p.length >= 3 ? String.join(":", java.util.Arrays.copyOfRange(p, 1, p.length)) : cid;
    }

    /** Approximate Maven version ordering for sorting requested-version lists. */
    public static String vkey(String v) {
        StringBuilder sb = new StringBuilder();
        for (String tok : v.split("[^A-Za-z0-9]+")) {
            if (tok.matches("\\d+")) sb.append(String.format("%010d", Long.parseLong(tok)));
            else sb.append(tok.toLowerCase()).append('~');
        }
        // bare release sorts above same version with qualifier
        sb.append(isPre(v) ? "0" : "1");
        return sb.toString();
    }

    public static boolean isPre(String v) {
        return PRE.matcher(v).find();
    }

    public static int vkeyCompare(String a, String b) {
        return vkey(a).compareTo(vkey(b));
    }
}
