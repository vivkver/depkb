package depkb;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Everything known about how one module declares and uses its dependencies.
 * Mirrors the Python Analysis class in depkb.py.
 */
public final class Analysis {

    private static final Set<String> COMPILE_VIA = Set.of("import", "class", "abi");
    private static final Map<String, String> MAIN_BUCKETS = Map.of(
        "api", "api", "compileOnlyApi", "api",
        "implementation", "compile", "compileOnly", "compile",
        "runtimeOnly", "runtime"
    );
    private static final Map<String, String> BUCKET_SUFFIX = Map.of(
        "Api", "api", "CompileOnlyApi", "api",
        "Implementation", "compile", "CompileOnly", "compile",
        "RuntimeOnly", "runtime"
    );

    public static final String CAVEAT =
        "note: bytecode/imports cannot see reflection by computed name, or classes only " +
        "needed at runtime; verify UNUSED? before removing";

    public static final class Use {
        public final Set<String> files = new HashSet<>();
        public final Set<String> vias = new HashSet<>();
        /** Each entry: {path, line(int), fqn, via} */
        public final List<Object[]> detail = new ArrayList<>();
        public boolean compile;
        public boolean abi;
        public boolean offcp;
        public String version;
    }

    /** key -> list of {bucket, sset, bkind, version, platform(bool)} */
    public final Map<String, List<Object[]>> decl = new HashMap<>();
    /** key -> {sset -> Use} */
    public final Map<String, Map<String, Use>> uses = new HashMap<>();
    /** sset -> list of {path, line, fqn} for unknown imports */
    public final Map<String, List<Object[]>> unknown = new HashMap<>();
    /** sset -> set of keys (ga or "project :xxx") on its compile classpath */
    public final Map<String, Set<String>> cp = new HashMap<>();

    public final String project;
    public final List<String> ssets;
    /** sset -> {kind, note} */
    public final Map<String, String[]> evidence = new HashMap<>();
    public final boolean javalib;

    private final Connection conn;

    public Analysis(Connection conn, String project) throws SQLException {
        this(conn, project, null);
    }

    public Analysis(Connection conn, String project, String key) throws SQLException {
        this.conn = conn;
        this.project = project;

        this.ssets = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT DISTINCT name FROM sourcesets WHERE project=?")) {
            ps.setString(1, project);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) ssets.add(rs.getString(1));
            }
        }

        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT sset, kind, note FROM evidence WHERE project=?")) {
            ps.setString(1, project);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) evidence.put(rs.getString(1), new String[]{rs.getString(2), rs.getString(3)});
            }
        }

        try (PreparedStatement ps = conn.prepareStatement("SELECT javalib FROM projects WHERE path=?")) {
            ps.setString(1, project);
            try (ResultSet rs = ps.executeQuery()) {
                this.javalib = rs.next() && rs.getInt(1) == 1;
            }
        }

        // declared deps
        String declSql = "SELECT bucket, id, version, platform FROM declared WHERE project=?" +
                         (key != null ? " AND id=?" : "");
        try (PreparedStatement ps = conn.prepareStatement(declSql)) {
            ps.setString(1, project);
            if (key != null) ps.setString(2, key);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    String b = rs.getString(1), id = rs.getString(2), v = rs.getString(3);
                    boolean plat = rs.getInt(4) == 1;
                    String[] scope = bucketScope(b, ssets);
                    decl.computeIfAbsent(id, k -> new ArrayList<>())
                        .add(new Object[]{b, scope[0], scope[1], v, plat});
                }
            }
        }

        // uses from links
        String where, linkArg2;
        if (key != null) {
            if (key.startsWith("project ")) {
                where = "l.project=? AND l.owner=? AND l.kind LIKE 'project%'";
                linkArg2 = key.substring(8);
            } else {
                where = "l.project=? AND l.ga=?";
                linkArg2 = key;
            }
        } else {
            where = "l.project=?";
            linkArg2 = null;
        }
        List<Object[]> rows = useRows(conn, where, project, linkArg2);
        aggregateInto(rows, uses, unknown);

        // compile classpath for each sset
        for (String ss : ssets) {
            String cpn = null;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT cp FROM sourcesets WHERE project=? AND name=?")) {
                ps.setString(1, project); ps.setString(2, ss);
                try (ResultSet rs = ps.executeQuery()) {
                    if (rs.next()) cpn = rs.getString(1);
                }
            }
            if (cpn == null) { cp.put(ss, Set.of()); continue; }
            Set<String> keys = new HashSet<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT c.cid, c.ga, c.kind FROM pcs p JOIN nodes n ON n.pc=p.id " +
                    "JOIN comps c ON c.id=n.comp WHERE p.project=? AND p.config=?")) {
                ps.setString(1, project); ps.setString(2, cpn);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        String cid = rs.getString(1), ga = rs.getString(2), k = rs.getString(3);
                        keys.add("p".equals(k) ? cid : ga);
                    }
                }
            }
            cp.put(ss, keys);
        }
    }

    // ---- static helpers ----

    private static final String EVID_JOIN =
        "JOIN evidence ev ON ev.project=f.project AND ev.sset=f.sset AND (f.kind=ev.kind OR f.kind='res')";

    /** Execute the use_rows query. arg2 is null when where clause only has one param. */
    public static List<Object[]> useRows(Connection conn, String where, String arg1, String arg2)
            throws SQLException {
        String sql = "SELECT f.project, f.sset, f.path, f.kind, i.line, i.fqn, i.via, " +
                     "l.kind, l.ga, l.version, l.owner " +
                     "FROM links l JOIN srcfiles f ON f.project=l.project AND f.sset=l.sset " +
                     EVID_JOIN + " " +
                     "JOIN imports i ON i.file=f.id AND i.fqn=l.fqn AND i.static=l.static " +
                     "AND i.wildcard=l.wildcard " +
                     "WHERE " + where + " ORDER BY f.path, i.line";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, arg1);
            if (arg2 != null) ps.setString(2, arg2);
            List<Object[]> out = new ArrayList<>();
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    out.add(new Object[]{
                        rs.getString(1),  // project
                        rs.getString(2),  // sset
                        rs.getString(3),  // path
                        rs.getString(4),  // file kind
                        rs.getObject(5),  // line (may be null)
                        rs.getString(6),  // fqn
                        rs.getString(7),  // via
                        rs.getString(8),  // link kind
                        rs.getString(9),  // ga
                        rs.getString(10), // version
                        rs.getString(11)  // owner
                    });
                }
            }
            return out;
        }
    }

    /** Aggregate use_rows into uses map and unknown map. */
    private static void aggregateInto(List<Object[]> rows,
                                      Map<String, Map<String, Use>> uses,
                                      Map<String, List<Object[]>> unknown) {
        for (Object[] r : rows) {
            String pj = (String) r[0], ss = (String) r[1], path = (String) r[2];
            Object lineObj = r[4];
            String fqn = (String) r[5], via = (String) r[6], kind = (String) r[7];
            String ga = (String) r[8], ver = (String) r[9], owner = (String) r[10];

            if ("jdk".equals(kind) || "self".equals(kind)) continue;
            if ("unknown".equals(kind)) {
                if (COMPILE_VIA.contains(via)) {
                    unknown.computeIfAbsent(ss, k -> new ArrayList<>())
                           .add(new Object[]{path, lineObj, fqn});
                }
                continue;
            }
            String depKey = (kind != null && kind.startsWith("lib")) ? ga : "project " + owner;
            Use u = uses.computeIfAbsent(depKey, k -> new HashMap<>())
                        .computeIfAbsent(ss, k -> new Use());
            u.files.add(path);
            u.vias.add(via);
            u.version = ver;
            u.detail.add(new Object[]{path, lineObj, fqn, via});
            if (COMPILE_VIA.contains(via)) {
                u.compile = true;
                if ("abi".equals(via)) u.abi = true;
                if (kind != null && kind.endsWith("off-cp")) u.offcp = true;
            }
        }
    }

    /** Map Gradle bucket name to {sset, bkind}. Returns {null, null} if not recognized. */
    public static String[] bucketScope(String bucket, List<String> ssets) {
        if (MAIN_BUCKETS.containsKey(bucket)) return new String[]{"main", MAIN_BUCKETS.get(bucket)};
        // sort by length descending so "androidTest" matches before "test"
        List<String> sorted = new ArrayList<>(ssets);
        sorted.sort((a, b) -> Integer.compare(b.length(), a.length()));
        for (String ss : sorted) {
            if (!"main".equals(ss) && bucket.startsWith(ss)) {
                String suffix = bucket.substring(ss.length());
                String bk = BUCKET_SUFFIX.get(suffix);
                if (bk != null) return new String[]{ss, bk};
            }
        }
        return new String[]{null, null};
    }

    // ---- instance methods ----

    public boolean bytecode(String ss) {
        String[] ev = evidence.get(ss);
        return ev != null && "class".equals(ev[0]);
    }

    public boolean hasClasses(String key) throws SQLException {
        if (key.startsWith("project ")) return true;
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT 1 FROM packages WHERE ga=? LIMIT 1")) {
            ps.setString(1, key);
            try (ResultSet rs = ps.executeQuery()) { return rs.next(); }
        }
    }

    public List<String> services(String key) throws SQLException {
        List<String> out = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT DISTINCT iface FROM services WHERE ga=?")) {
            ps.setString(1, key);
            try (ResultSet rs = ps.executeQuery()) { while (rs.next()) out.add(rs.getString(1)); }
        }
        return out;
    }

    public boolean declaredFor(String key, String ss) {
        for (Object[] d : decl.getOrDefault(key, List.of())) {
            String dss = (String) d[1], bk = (String) d[2];
            if (("compile".equals(bk) || "api".equals(bk)) &&
                (ss.equals(dss) || "main".equals(dss))) return true;
        }
        return false;
    }

    /**
     * Returns findings as list of {type, sset, text}.
     * Types: UNDECLARED, NOT_ON_CP, UNUSED, API, IMPL
     */
    public List<Object[]> findings(String key) throws SQLException {
        List<Object[]> out = new ArrayList<>();
        Map<String, Use> byss = uses.getOrDefault(key, Map.of());
        for (Map.Entry<String, Use> e : new TreeMap<>(byss).entrySet()) {
            String ss = e.getKey(); Use u = e.getValue();
            if (!u.compile) continue;
            Set<String> cpKeys = cp.getOrDefault(ss, Set.of());
            if (u.offcp || !cpKeys.contains(key)) {
                out.add(new Object[]{"NOT_ON_CP", ss, "used but not on the compile classpath (won't compile)"});
            } else if (!declaredFor(key, ss)) {
                out.add(new Object[]{"UNDECLARED", ss, "used but only transitive: declare it"});
            }
        }
        for (Object[] d : decl.getOrDefault(key, List.of())) {
            String b = (String) d[0], dss = (String) d[1], bk = (String) d[2];
            boolean plat = (boolean) d[4];
            if (!("compile".equals(bk) || "api".equals(bk)) || plat || dss == null) continue;
            if (!hasClasses(key)) continue;
            boolean used = byss.entrySet().stream().anyMatch(e -> e.getKey().equals(dss));
            if (!used) {
                List<String> svc = services(key);
                String svcNote = svc.isEmpty() ? "" :
                    "; provides " + String.join(", ", svc.subList(0, Math.min(2, svc.size()))) + " -> runtimeOnly?";
                out.add(new Object[]{"UNUSED", dss, "declared in " + b + ", never referenced" + svcNote});
            }
        }
        if (javalib && bytecode("main") && decl.containsKey(key)) {
            Set<String> bkinds = new HashSet<>();
            for (Object[] d : decl.get(key)) {
                if ("main".equals(d[1])) bkinds.add((String) d[2]);
            }
            Use u = byss.get("main");
            if (u != null && u.abi && !bkinds.contains("api") && bkinds.contains("compile")) {
                out.add(new Object[]{"API", "main", "in public API (signatures/supertypes): should be api"});
            } else if (u != null && u.compile && !u.abi && bkinds.contains("api")) {
                out.add(new Object[]{"IMPL", "main", "not in public API: could be implementation"});
            }
        }
        return out;
    }

    /** Declared bucket label for a key, or null if not declared. */
    public String label(String key) {
        List<Object[]> ds = decl.get(key);
        if (ds == null || ds.isEmpty()) return null;
        TreeMap<String, Boolean> buckets = new TreeMap<>();
        for (Object[] d : ds) buckets.put((String) d[0], true);
        return String.join(",", buckets.keySet());
    }

    /** "used main:3f  test:1f" or "not used". */
    public String usesSummary(String key) {
        Map<String, Use> us = uses.getOrDefault(key, Map.of());
        if (us.isEmpty()) return "not used";
        List<String> parts = new ArrayList<>();
        for (Map.Entry<String, Use> e : new TreeMap<>(us).entrySet()) {
            Use u = e.getValue();
            String tag;
            if (u.compile) {
                tag = "";
            } else {
                List<String> sortedVias = new ArrayList<>(u.vias);
                java.util.Collections.sort(sortedVias);
                tag = "(" + String.join("/", sortedVias) + ")";
            }
            parts.add(e.getKey() + ":" + u.files.size() + "f" + tag);
        }
        return "used " + String.join(" ", parts);
    }

    // ---- static DB helpers ----

    /** Get a node row for (project, config, ga): {pc, compId, version, direct, reqs, reasons}. */
    public static Object[] node(Connection conn, String project, String config, String ga)
            throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT p.id, c.id, c.version, n.direct, n.reqs, n.reasons FROM pcs p " +
                "JOIN nodes n ON n.pc=p.id JOIN comps c ON c.id=n.comp " +
                "WHERE p.project=? AND p.config=? AND c.ga=?")) {
            ps.setString(1, project); ps.setString(2, config); ps.setString(3, ga);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) return null;
                return new Object[]{rs.getLong(1), rs.getLong(2), rs.getString(3),
                                    rs.getInt(4), rs.getString(5), rs.getString(6)};
            }
        }
    }

    /** Format override/conflict note: "overridden 1.0->2.0 (conflict; via foo)" or "". */
    public static String overrideNote(Connection conn, long pc, long compId,
                                      String version, String reqs, String reasons)
            throws SQLException {
        List<String> others = new ArrayList<>();
        if (reqs != null) for (String r : reqs.split(",")) if (!r.isEmpty() && !r.equals(version)) others.add(r);
        List<String> kinds = new ArrayList<>();
        if (reasons != null) {
            Set<String> seen = new HashSet<>();
            for (String r : reasons.split("; ")) {
                if (!r.isEmpty()) { String k = r.split(":")[0]; if (seen.add(k)) kinds.add(k); }
            }
            java.util.Collections.sort(kinds);
        }
        if (others.isEmpty() && kinds.isEmpty()) return "";
        List<String> via = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT f, req, cons FROM edges WHERE pc=? AND t=?")) {
            ps.setLong(1, pc); ps.setLong(2, compId);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    long f = rs.getLong(1); String q = rs.getString(2); int cons = rs.getInt(3);
                    if (cons == 1 || version.equals(Ingest.reqVersion(q))) {
                        via.add(Lookups.shortCid(cidOf(conn, f)));
                    }
                }
            }
        }
        String s = (others.isEmpty() ? "selected" : "overridden " + String.join(",", others) + "->" + version)
                 + " (" + (kinds.isEmpty() ? "requested" : String.join(", ", kinds));
        if (!via.isEmpty()) {
            s += "; via " + String.join(", ", via.subList(0, Math.min(2, via.size())));
            if (via.size() > 2) s += " +" + (via.size() - 2);
        }
        return s + ")";
    }

    private static String cidOf(Connection conn, long id) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT cid FROM comps WHERE id=?")) {
            ps.setLong(1, id);
            try (ResultSet rs = ps.executeQuery()) { return rs.next() ? rs.getString(1) : "?"; }
        }
    }

    /** Relative path within the project dir, or the original path if not under it. */
    public static String rel(Connection conn, String project, String path) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT dir FROM projects WHERE path=?")) {
            ps.setString(1, project);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) return path;
                String dir = rs.getString(1);
                if (dir == null || dir.isEmpty()) return path;
                try {
                    return java.nio.file.Path.of(dir).relativize(java.nio.file.Path.of(path)).toString();
                } catch (IllegalArgumentException e2) {
                    // cross-root or not under dir
                    if (path.startsWith(dir)) return path.substring(dir.length()).replaceAll("^[\\\\/]+", "");
                    return path;
                }
            }
        }
    }

    /** "compiled: N modules; source: M modules" summary from evidence table. */
    public static String evidenceNote(Connection conn, List<String> projects) throws SQLException {
        if (projects.isEmpty()) return "";
        String placeholders = "?,".repeat(projects.size()).replaceAll(",$", "");
        String sql = "SELECT note, count(*) FROM evidence WHERE sset='main' AND project IN (" +
                     placeholders + ") GROUP BY note";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            for (int i = 0; i < projects.size(); i++) ps.setString(i + 1, projects.get(i));
            List<String> parts = new ArrayList<>();
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) parts.add(rs.getString(1) + ": " + Lookups.plural((int)rs.getLong(2), "module"));
            }
            return String.join("; ", parts);
        }
    }

    /** Print detail lines for a set of Use objects grouped by sset. */
    public static void printDetail(Connection conn, String project,
                                   Map<String, Use> bySset, int limit) throws SQLException {
        List<Object[]> rows = new ArrayList<>();
        for (Map.Entry<String, Use> e : new TreeMap<>(bySset).entrySet()) {
            rows.addAll(e.getValue().detail);
        }
        List<String> lines = new ArrayList<>();
        for (Object[] r : rows) {
            String path = (String) r[0], fqn = (String) r[2], via = (String) r[3];
            Object lineObj = r[1];
            String relPath = rel(conn, project, path);
            String loc = lineObj != null ? relPath + ":" + lineObj : relPath;
            lines.add("    " + loc + " " + fqn +
                      ("import".equals(via) || "class".equals(via) ? "" : " [" + via + "]"));
        }
        Lookups.emit(lines, limit);
    }
}
