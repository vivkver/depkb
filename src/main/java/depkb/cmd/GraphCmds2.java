package depkb.cmd;

import depkb.Db;
import depkb.Ingest;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/** Public factory — exposes package-private command instances to Main. */
public final class GraphCmds2 {
    private GraphCmds2() {}
    public static List<Object> create() {
        return List.of(new ModulesCmd(), new UnresolvedCmd(), new SplitPackagesCmd(),
                       new CatalogCmd(), new DiffCmd(), new LicensesCmd(), new SqlCmd());
    }
}

// ---- modules ----

@Command(name = "modules", description = "Project module graph: kinds, direct deps, dependents.")
class ModulesCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = {"-p", "--project"}) String project;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            Map<String, Set<String>> edges = new HashMap<>(); // module -> direct project deps
            Map<String, Set<String>> rev   = new HashMap<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT DISTINCT p.project, c.cid FROM pcs p JOIN edges e ON e.pc=p.id AND e.f=p.root " +
                    "JOIN comps c ON c.id=e.t WHERE c.kind='p' AND p.config IN (?,?)")) {
                ps.setString(1, "compileClasspath"); ps.setString(2, "runtimeClasspath");
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        String pj = rs.getString(1);
                        String dep = rs.getString(2).replaceFirst("^project ", "");
                        edges.computeIfAbsent(pj, k -> new HashSet<>()).add(dep);
                        rev.computeIfAbsent(dep, k -> new HashSet<>()).add(pj);
                    }
                }
            }
            Map<String, String> kinds = new TreeMap<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT path, kind FROM projects");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) kinds.put(rs.getString(1), rs.getString(2));
            }

            if (project != null) {
                String m = Lookups.normProject(conn, project);
                System.out.println(m + " [" + kinds.getOrDefault(m, "?") + "]");
                Set<String> deps = edges.getOrDefault(m, Set.of());
                System.out.println("depends on: " + (deps.isEmpty() ? "-" : String.join(", ", deps.stream().sorted().toList())));
                Set<String> usedBy = rev.getOrDefault(m, Set.of());
                System.out.println("used by: " + (usedBy.isEmpty() ? "-" : String.join(", ", usedBy.stream().sorted().toList())));
                Set<String> allRev = bfsClosure(m, rev);
                List<String> apps = allRev.stream().filter(x -> "app".equals(kinds.get(x))).sorted().toList();
                System.out.print("affected by a change here: " + allRev.size() + " modules");
                if (!apps.isEmpty()) System.out.print(", apps: " + String.join(", ", apps));
                System.out.println();
                return;
            }

            long nApps = kinds.values().stream().filter("app"::equals).count();
            System.out.println(kinds.size() + " modules, " + nApps + " apps");
            List<String> lines = new ArrayList<>();
            kinds.forEach((m, k) -> {
                if (":".equals(m) && "none".equals(k) && !edges.containsKey(m)) return;
                Set<String> d = edges.getOrDefault(m, Set.of());
                int usedByCount = rev.getOrDefault(m, Set.of()).size();
                lines.add(m + " [" + k + "] -> " + (d.isEmpty() ? "-" : String.join(", ", d.stream().sorted().toList()))
                        + "  (used by " + usedByCount + ")");
            });
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }

    private static Set<String> bfsClosure(String start, Map<String, Set<String>> g) {
        Set<String> seen = new HashSet<>();
        Deque<String> q = new ArrayDeque<>();
        q.add(start);
        while (!q.isEmpty()) {
            String u = q.poll();
            for (String n : g.getOrDefault(u, Set.of())) {
                if (seen.add(n)) q.add(n);
            }
        }
        return seen;
    }
}

// ---- unresolved ----

@Command(name = "unresolved", description = "Dependency requests that could not be resolved.")
class UnresolvedCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = "--limit", defaultValue = "40") int limit;

    private static final String LEGEND = "configs: c=compileClasspath r=runtimeClasspath tc/tr=test compile/runtime ap=annotationProcessor";

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            Map<String, Object[]> agg = new LinkedHashMap<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT u.req, p.project, p.config, u.err FROM unresolved u " +
                    "JOIN pcs p ON p.id=u.pc ORDER BY u.req");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    String req = rs.getString(1), pj = rs.getString(2), cfg = rs.getString(3), err = rs.getString(4);
                    if (!agg.containsKey(req)) agg.put(req, new Object[]{new HashSet<String>(), ""});
                    @SuppressWarnings("unchecked") Set<String> where = (Set<String>) agg.get(req)[0];
                    where.add(pj + "[" + Lookups.ab(cfg) + "]");
                    if (err != null && !err.isEmpty() && ((String) agg.get(req)[1]).isEmpty())
                        agg.get(req)[1] = err.split("\n")[0].substring(0, Math.min(120, err.split("\n")[0].length()));
                }
            }
            System.out.println("unresolved requests: " + agg.size());
            List<String> lines = new ArrayList<>();
            agg.forEach((req, v) -> {
                @SuppressWarnings("unchecked") Set<String> w = (Set<String>) v[0];
                String err = (String) v[1];
                List<String> ws = w.stream().sorted().toList();
                lines.add(req + ": " + String.join(", ", ws.subList(0, Math.min(4, ws.size())))
                        + (ws.size() > 4 ? " ..." : "") + " — " + err);
            });
            Lookups.emit(lines, limit);
            if (!lines.isEmpty()) System.out.println(LEGEND);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- split-packages ----

@Command(name = "split-packages", description = "Packages provided by more than one module on the same classpath.")
class SplitPackagesCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = {"-p", "--project"}) String project;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String proj = Lookups.normProject(conn, project);
            String cfg = config != null ? config : "runtimeClasspath";
            String sql = "SELECT p.project, k.pkg, group_concat(DISTINCT c.ga) FROM nodes n " +
                "JOIN pcs p ON p.id=n.pc JOIN comps c ON c.id=n.comp " +
                "JOIN packages k ON k.ga=c.ga AND k.version=c.version " +
                "WHERE p.config=?" + (proj != null ? " AND p.project=?" : "") +
                " GROUP BY n.pc, k.pkg HAVING count(DISTINCT c.ga) > 1";
            Map<String, Set<String>> agg = new LinkedHashMap<>(); // "pkg|sortedGas" -> projects
            try (PreparedStatement ps = conn.prepareStatement(sql)) {
                ps.setString(1, cfg);
                if (proj != null) ps.setString(2, proj);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        String pj = rs.getString(1), pkg = rs.getString(2), gas = rs.getString(3);
                        List<String> sortedGas = List.of(gas.split(",")).stream().sorted().toList();
                        String key = pkg + "|" + String.join(",", sortedGas);
                        agg.computeIfAbsent(key, k -> new HashSet<>()).add(pj);
                    }
                }
            }
            List<String> lines = new ArrayList<>();
            agg.entrySet().stream()
                .sorted((a, b) -> Integer.compare(b.getValue().size(), a.getValue().size()))
                .forEach(e -> {
                    String[] kv = e.getKey().split("\\|", 2);
                    String pkg = kv[0], gas = kv.length > 1 ? kv[1].replace(",", " + ") : "";
                    lines.add(pkg + ": " + gas + " — " + e.getValue().size() + " proj");
                });
            System.out.println("split packages on " + cfg + ": " + lines.size()
                    + (lines.isEmpty() ? "" : " (risk: duplicate classes / JPMS errors)"));
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- catalog ----

@Command(name = "catalog", description = "Version catalog entries — search, unused, or missing.")
class CatalogCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0", arity = "0..1") String query;
    @Option(names = "--unused") boolean unused;
    @Option(names = "--missing") boolean missing;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            if (unused) {
                List<Object[]> rows = new ArrayList<>();
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT accessor, ga, version FROM catalog WHERE kind='library' " +
                        "AND ga NOT IN (SELECT ga FROM comps) ORDER BY accessor");
                     ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) rows.add(new Object[]{rs.getString(1), rs.getString(2), rs.getString(3)});
                }
                System.out.println("catalog libraries not in any resolved graph: " + rows.size());
                List<String> lines = new ArrayList<>();
                for (Object[] r : rows) {
                    String v = (String) r[2];
                    lines.add("  " + r[0] + " = " + r[1] + (v != null && !v.isEmpty() ? ":" + v : ""));
                }
                Lookups.emit(lines, limit);
                return;
            }
            if (missing) {
                Map<String, Set<String>> agg = new LinkedHashMap<>();
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT DISTINCT c.ga, p.project FROM nodes n JOIN comps c ON c.id=n.comp " +
                        "JOIN pcs p ON p.id=n.pc WHERE n.direct=1 AND c.kind='m' " +
                        "AND c.ga NOT IN (SELECT ga FROM catalog) ORDER BY c.ga");
                     ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) agg.computeIfAbsent(rs.getString(1), k -> new HashSet<>()).add(rs.getString(2));
                }
                System.out.println("direct dependencies not declared in the catalog: " + agg.size());
                List<String> lines = new ArrayList<>();
                agg.forEach((ga, pjs) -> {
                    List<String> ps = pjs.stream().sorted().toList();
                    lines.add("  " + ga + " (" + String.join(", ", ps.subList(0, Math.min(3, ps.size())))
                            + (ps.size() > 3 ? " ..." : "") + ")");
                });
                Lookups.emit(lines, limit);
                return;
            }
            String q = "%" + (query != null ? query : "") + "%";
            List<Object[]> rows = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT accessor, kind, ga, version, ref FROM catalog " +
                    "WHERE accessor LIKE ? OR ga LIKE ? ORDER BY kind DESC, accessor")) {
                ps.setString(1, q); ps.setString(2, q);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) rows.add(new Object[]{rs.getString(1), rs.getString(2), rs.getString(3), rs.getString(4), rs.getString(5)});
                }
            }
            List<String> lines = new ArrayList<>();
            for (Object[] r : rows) {
                String acc = (String) r[0], kind = (String) r[1], ga = (String) r[2], v = (String) r[3], ref = (String) r[4];
                String line = acc + " = " + ga + (v != null && !v.isEmpty() ? ":" + v : "")
                        + (ref != null ? " (ref " + ref + ")" : "")
                        + ("library".equals(kind) ? "" : " [" + kind + "]");
                lines.add(line);
            }
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- diff ----

@Command(name = "diff",
        description = "Compare two saved snapshots (before/after a change); or, if the names are projects, two project classpaths.")
class DiffCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0", description = "snapshot name, or project") String a;
    @Parameters(index = "1", description = "snapshot name, or project") String b;
    @Option(names = {"-c", "--config"}, description = "limit to one Gradle config (default: all for snapshots, runtimeClasspath for projects)") String config;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        Path db = Db.findDb(parent.db);
        Path snapA = AgentCmds.snapshotFile(db, a), snapB = AgentCmds.snapshotFile(db, b);
        if (Files.isRegularFile(snapA) && Files.isRegularFile(snapB)) {
            snapshotDiff(snapA, snapB);
        } else {
            projectDiff(db);
        }
    }

    // ---- snapshot vs snapshot (spec §7) ----

    private void snapshotDiff(Path snapA, Path snapB) {
        try (Connection ca = Db.openReadOnly(snapA); Connection cb = Db.openReadOnly(snapB)) {
            Map<String, TreeSet<String>> ma = resolvedModules(ca, config);
            Map<String, TreeSet<String>> mb = resolvedModules(cb, config);

            Set<String> all = new TreeSet<>();
            all.addAll(ma.keySet()); all.addAll(mb.keySet());

            List<String> added = new ArrayList<>(), removed = new ArrayList<>(), changed = new ArrayList<>();
            for (String ga : all) {
                TreeSet<String> sa = ma.get(ga), sb = mb.get(ga);
                if (sa == null) {
                    added.add("  + " + ga + ":" + joinVers(sb));
                } else if (sb == null) {
                    removed.add("  - " + ga + ":" + joinVers(sa));
                } else if (!sa.equals(sb)) {
                    changed.add("  ~ " + ga + ": " + joinVers(sa) + " -> " + joinVers(sb) + direction(sa, sb));
                }
            }
            String cfgNote = config != null ? " [" + config + "]" : "";
            System.out.println(a + " -> " + b + cfgNote + ": "
                    + changed.size() + " changed, " + added.size() + " added, " + removed.size() + " removed");
            List<String> lines = new ArrayList<>();
            lines.addAll(changed); lines.addAll(added); lines.addAll(removed);
            if (lines.isEmpty()) System.out.println("  (no dependency changes)");
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }

    /** ga -> set of resolved versions across all modules (optionally one config). */
    private static Map<String, TreeSet<String>> resolvedModules(Connection conn, String cfg) throws Exception {
        Map<String, TreeSet<String>> m = new TreeMap<>();
        String sql = "SELECT DISTINCT c.ga, c.version FROM nodes n JOIN comps c ON c.id=n.comp " +
                "JOIN pcs p ON p.id=n.pc WHERE c.kind='m'" + (cfg != null ? " AND p.config=?" : "");
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            if (cfg != null) ps.setString(1, cfg);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) m.computeIfAbsent(rs.getString(1),
                        k -> new TreeSet<>((x, y) -> Ingest.vkey(y).compareTo(Ingest.vkey(x)))).add(rs.getString(2));
            }
        }
        return m;
    }

    private static String joinVers(TreeSet<String> s) { return String.join(",", s); }

    private static String direction(TreeSet<String> sa, TreeSet<String> sb) {
        if (sa.size() != 1 || sb.size() != 1) return "  (set)";
        int cmp = Ingest.vkey(sb.first()).compareTo(Ingest.vkey(sa.first()));
        return cmp > 0 ? "  (upgrade)" : cmp < 0 ? "  (downgrade)" : "";
    }

    // ---- project vs project (fallback; original behaviour) ----

    private void projectDiff(Path db) {
        try {
            Connection conn = Db.connect(db, true);
            String p1 = Lookups.normProject(conn, a), p2 = Lookups.normProject(conn, b);
            String cfg = config != null ? config : "runtimeClasspath";
            Map<String, String> m1 = mods(conn, p1, cfg), m2 = mods(conn, p2, cfg);
            List<String> only1 = m1.keySet().stream().filter(g -> !m2.containsKey(g)).sorted().toList();
            List<String> only2 = m2.keySet().stream().filter(g -> !m1.containsKey(g)).sorted().toList();
            List<String> diff  = m1.keySet().stream().filter(g -> m2.containsKey(g) && !m1.get(g).equals(m2.get(g))).sorted().toList();
            System.out.println(p1 + " vs " + p2 + " [" + cfg + "]: " + only1.size() + " only in " + p1
                    + ", " + only2.size() + " only in " + p2 + ", " + diff.size() + " version diffs");
            List<String> lines = new ArrayList<>();
            for (String g : diff)  lines.add("  ~ " + g + ": " + m1.get(g) + " vs " + m2.get(g));
            for (String g : only1) lines.add("  < " + g + ":" + m1.get(g));
            for (String g : only2) lines.add("  > " + g + ":" + m2.get(g));
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }

    private static Map<String, String> mods(Connection conn, String proj, String cfg) throws Exception {
        Map<String, String> m = new LinkedHashMap<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT c.ga, c.version FROM nodes n JOIN comps c ON c.id=n.comp " +
                "JOIN pcs p ON p.id=n.pc WHERE p.project=? AND p.config=? AND c.kind='m'")) {
            ps.setString(1, proj); ps.setString(2, cfg);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) m.put(rs.getString(1), rs.getString(2));
            }
        }
        return m;
    }
}

// ---- licenses ----

@Command(name = "licenses", description = "License summary for all external modules.")
class LicensesCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = "--filter") String filter;
    @Option(names = "--show", defaultValue = "6") int show;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            Map<String, Set<String>> m = new TreeMap<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT DISTINCT c.ga, c.version, coalesce(nullif(i.licenses,''),'UNKNOWN') FROM comps c " +
                    "JOIN nodes n ON n.comp=c.id LEFT JOIN pkginfo i ON i.ga=c.ga AND i.version=c.version " +
                    "WHERE c.kind='m'");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    String ga = rs.getString(1), lic = rs.getString(3);
                    if (filter == null || filter.isEmpty() || lic.toLowerCase().contains(filter.toLowerCase()))
                        m.computeIfAbsent(lic, k -> new HashSet<>()).add(ga);
                }
            }
            List<String> lines = new ArrayList<>();
            m.entrySet().stream()
                .sorted((a, b) -> Integer.compare(b.getValue().size(), a.getValue().size()))
                .forEach(e -> {
                    List<String> gas = e.getValue().stream().sorted().toList();
                    lines.add(e.getKey() + " (" + gas.size() + "): "
                            + String.join(", ", gas.subList(0, Math.min(show, gas.size())))
                            + (gas.size() > show ? " ..." : ""));
                });
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- sql ----

@Command(name = "sql", description = "Execute a raw SQL query against the KB.")
class SqlCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String query;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            try (Statement st = conn.createStatement();
                 ResultSet rs = st.executeQuery(query)) {
                int nc = rs.getMetaData().getColumnCount();
                List<String> cols = new ArrayList<>();
                for (int i = 1; i <= nc; i++) cols.add(rs.getMetaData().getColumnName(i));
                System.out.println(String.join("\t", cols));
                List<String> lines = new ArrayList<>();
                while (rs.next()) {
                    List<String> row = new ArrayList<>();
                    for (int i = 1; i <= nc; i++) {
                        Object v = rs.getObject(i); row.add(v == null ? "" : v.toString());
                    }
                    lines.add(String.join("\t", row));
                }
                Lookups.emit(lines, limit);
            }
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}
