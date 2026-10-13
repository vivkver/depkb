package depkb.cmd;

import depkb.Db;
import depkb.Ingest;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
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

/** Public factory — exposes package-private command instances to Main. */
public final class GraphCmds1 {
    private GraphCmds1() {}
    public static List<Object> create() {
        return List.of(new VersionCmd(), new WhoUsesCmd(), new WhyCmd(), new DepsCmd(),
                       new PkgCmd(), new InfoCmd(), new ConflictsCmd(), new DriftCmd());
    }
}

// ---- version ----

@Command(name = "version", description = "Resolved versions of a module across all projects.")
class VersionCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String module;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            List<String> gas = Lookups.findGas(conn, module);
            if (gas.isEmpty()) Db.die("no module matching '" + module + "'", 1);
            List<String> lines = new ArrayList<>();
            for (String ga : gas) {
                List<Object[]> vs = new ArrayList<>();
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT c.version, count(DISTINCT p.project) FROM comps c JOIN nodes n ON n.comp=c.id " +
                        "JOIN pcs p ON p.id=n.pc WHERE c.ga=? GROUP BY c.version")) {
                    ps.setString(1, ga);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) vs.add(new Object[]{rs.getString(1), rs.getLong(2)});
                    }
                }
                vs.sort((a, b) -> Ingest.vkey((String) b[0]).compareTo(Ingest.vkey((String) a[0])));
                List<String> cat = new ArrayList<>();
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT accessor, version FROM catalog WHERE ga=? AND kind='library'")) {
                    ps.setString(1, ga);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) cat.add(rs.getString(1) + "=" + rs.getString(2));
                    }
                }
                StringBuilder s = new StringBuilder(ga).append(": ");
                for (int i = 0; i < vs.size(); i++) {
                    if (i > 0) s.append(", ");
                    s.append(vs.get(i)[0]).append(" (").append(vs.get(i)[1]).append(" proj)");
                }
                if (vs.size() > 1) s.append("  <- multiple versions");
                if (!cat.isEmpty()) s.append("  | catalog ").append(String.join(", ", cat));
                lines.add(s.toString());
            }
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- who-uses ----

@Command(name = "who-uses", description = "Projects that have a module on any classpath.")
class WhoUsesCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String module;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            List<String> gas = Lookups.findGas(conn, module);
            if (gas.isEmpty()) Db.die("no module matching '" + module + "'", 1);
            if (gas.size() > 1 && !module.contains(":")) {
                List<String> exact = gas.stream().filter(g -> g.endsWith(":" + module)).toList();
                if (!exact.isEmpty()) gas = exact;
            }
            boolean usedAbbr = false;
            for (String ga : gas.subList(0, Math.min(5, gas.size()))) {
                String sql = "SELECT p.project, p.config, c.version, n.direct FROM nodes n " +
                    "JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc WHERE c.ga=?" +
                    (config != null ? " AND p.config=?" : "");
                Map<String, List<String>> agg = new LinkedHashMap<>();
                Set<String> projects = new HashSet<>();
                try (PreparedStatement ps = conn.prepareStatement(sql)) {
                    ps.setString(1, ga);
                    if (config != null) ps.setString(2, config);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) {
                            String p = rs.getString(1), cfg = rs.getString(2),
                                   v = rs.getString(3);
                            boolean direct = rs.getInt(4) == 1;
                            String key = p + "|" + v + "|" + (direct ? "direct" : "transitive");
                            agg.computeIfAbsent(key, k -> new ArrayList<>()).add(Lookups.ab(cfg));
                            projects.add(p);
                        }
                    }
                }
                System.out.println(ga + " — " + projects.size() + " proj");
                List<String> lines = new ArrayList<>();
                for (Map.Entry<String, List<String>> e : agg.entrySet()) {
                    String[] parts = e.getKey().split("\\|", 3);
                    String cfgs = String.join(" ", e.getValue().stream().sorted().toList());
                    lines.add("  " + parts[0] + "  " + parts[1] + "  " + parts[2] + "  [" + cfgs + "]");
                }
                lines.sort(null);
                Lookups.emit(lines, limit);
                usedAbbr = true;
            }
            if (gas.size() > 5) System.out.println("(+" + (gas.size() - 5) + " more matching modules; be more specific)");
            if (usedAbbr) System.out.println("configs: c=compileClasspath r=runtimeClasspath tc/tr=test compile/runtime ap=annotationProcessor");
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- why ----

@Command(name = "why", description = "Why a module is present at its resolved version.")
class WhyCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String module;
    @Option(names = {"-p", "--project"}) String project;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--paths", defaultValue = "3") int paths;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String ga = Lookups.oneGa(conn, module, limit);
            String proj = Lookups.normProject(conn, project);

            String sql = "SELECT p.id, p.project, p.config, p.root, c.id, c.version, n.reasons, n.direct " +
                "FROM nodes n JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc WHERE c.ga=?" +
                (proj != null ? " AND p.project=?" : "") +
                (config != null ? " AND p.config=?" : "");
            List<Object[]> cands = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(sql)) {
                int i = 1; ps.setString(i++, ga);
                if (proj != null) ps.setString(i++, proj);
                if (config != null) ps.setString(i++, config);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) cands.add(new Object[]{
                        rs.getLong(1), rs.getString(2), rs.getString(3),
                        rs.getLong(4), rs.getLong(5), rs.getString(6),
                        rs.getString(7), rs.getInt(8)
                    });
                }
            }
            if (cands.isEmpty()) Db.die(ga + " is not in " + (proj != null ? proj : "any graph") +
                (config != null ? " [" + config + "]" : ""), 1);

            Map<String, Integer> pref = Map.of("runtimeClasspath", 0, "compileClasspath", 1, "testRuntimeClasspath", 2);
            cands.sort((a, b) -> {
                int pa = pref.getOrDefault((String) a[2], 9);
                int pb = pref.getOrDefault((String) b[2], 9);
                if (pa != pb) return Integer.compare(pa, pb);
                return ((String) a[1]).compareTo((String) b[1]);
            });

            Object[] first = cands.get(0);
            long pc = (long) first[0]; String pj = (String) first[1]; String cfg = (String) first[2];
            long root = (long) first[3]; long tid = (long) first[4]; String ver = (String) first[5];
            String reasons = (String) first[6]; boolean direct = (int) first[7] == 1;

            System.out.println(ga + " in " + pj + " [" + cfg + "] = " + ver + (direct ? "  (direct)" : ""));
            if (reasons != null && !reasons.isEmpty()) System.out.println("reason: " + reasons);

            // Incoming edges
            System.out.println("requested by:");
            List<String> reqLines = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT f, req, cons FROM edges WHERE pc=? AND t=?")) {
                ps.setLong(1, pc); ps.setLong(2, tid);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        String cid = cidOf(conn, rs.getLong(1));
                        String rv = Ingest.reqVersion(rs.getString(2));
                        boolean cons = rs.getInt(3) == 1;
                        reqLines.add("  " + cid + " -> " + (rv.isEmpty() ? "(no version)" : rv) + (cons ? "  [constraint]" : ""));
                    }
                }
            }
            Lookups.emit(reqLines, limit);

            // Shortest paths
            Map<Long, List<Long>> adj = new HashMap<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT f, t FROM edges WHERE pc=? AND cons=0")) {
                ps.setLong(1, pc);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) adj.computeIfAbsent(rs.getLong(1), k -> new ArrayList<>()).add(rs.getLong(2));
                }
            }
            List<List<Long>> shortPaths = bfsPaths(adj, root, tid, paths);
            if (!shortPaths.isEmpty()) {
                System.out.println("shortest paths:");
                for (List<Long> path : shortPaths) {
                    List<String> names = new ArrayList<>();
                    for (long id : path) names.add(Lookups.shortCid(cidOf(conn, id)));
                    System.out.println("  " + String.join(" > ", names));
                }
            }

            // Elsewhere
            Map<String, Set<String>> others = new TreeMap<>((a, b) -> Ingest.vkey(b).compareTo(Ingest.vkey(a)));
            for (int i = 1; i < cands.size(); i++) {
                Object[] c = cands.get(i);
                String opj = (String) c[1]; String ocfg = (String) c[2]; String ov = (String) c[5];
                if (!opj.equals(pj) || !ov.equals(ver)) {
                    others.computeIfAbsent(ov, k -> new HashSet<>())
                          .add(opj.equals(pj) ? pj + "[" + Lookups.ab(ocfg) + "]" : opj);
                }
            }
            if (!others.isEmpty()) {
                List<String> parts = new ArrayList<>();
                for (Map.Entry<String, Set<String>> e : others.entrySet()) {
                    List<String> ps = e.getValue().stream().sorted().toList();
                    parts.add(e.getKey() + " in " + String.join(", ", ps.subList(0, Math.min(3, ps.size())))
                            + (ps.size() > 3 ? " ..." : ""));
                }
                System.out.println("elsewhere: " + String.join("; ", parts) + " (use --project/--config)");
            }
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }

    private static String cidOf(Connection conn, long id) throws Exception {
        try (PreparedStatement ps = conn.prepareStatement("SELECT cid FROM comps WHERE id=?")) {
            ps.setLong(1, id);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? rs.getString(1) : "?";
            }
        }
    }

    private static List<List<Long>> bfsPaths(Map<Long, List<Long>> adj, long root, long target, int maxPaths) {
        Map<Long, Integer> dist = new HashMap<>();
        Map<Long, List<Long>> preds = new HashMap<>();
        Deque<Long> queue = new ArrayDeque<>();
        dist.put(root, 0); queue.add(root);
        outer:
        while (!queue.isEmpty()) {
            long u = queue.poll();
            if (u == target) break;
            for (long w : adj.getOrDefault(u, List.of())) {
                if (!dist.containsKey(w)) {
                    dist.put(w, dist.get(u) + 1);
                    preds.computeIfAbsent(w, k -> new ArrayList<>()).add(u);
                    queue.add(w);
                } else if (dist.get(w) == dist.get(u) + 1) {
                    preds.computeIfAbsent(w, k -> new ArrayList<>()).add(u);
                }
            }
        }
        if (!dist.containsKey(target)) return List.of();
        List<List<Long>> out = new ArrayList<>();
        backtrack(preds, root, target, new ArrayList<>(), out, maxPaths);
        return out;
    }

    private static void backtrack(Map<Long, List<Long>> preds, long root, long node,
                                   List<Long> acc, List<List<Long>> out, int maxPaths) {
        if (out.size() >= maxPaths) return;
        if (node == root) {
            List<Long> path = new ArrayList<>();
            path.add(root);
            for (int i = acc.size() - 1; i >= 0; i--) path.add(acc.get(i));
            out.add(path);
            return;
        }
        for (long p : preds.getOrDefault(node, List.of())) {
            acc.add(node);
            backtrack(preds, root, p, acc, out, maxPaths);
            acc.remove(acc.size() - 1);
        }
    }
}

// ---- deps ----

@Command(name = "deps", description = "Direct (or all transitive) dependencies of a project.")
class DepsCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String project;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--all") boolean all;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String proj = Lookups.normProject(conn, project);
            String cfg = config != null ? config : "runtimeClasspath";

            List<long[]> pcs = Lookups.pcRows(conn, proj, cfg);
            if (pcs.isEmpty()) {
                String errMsg = "";
                try (PreparedStatement ps = conn.prepareStatement("SELECT error FROM pcs WHERE project=? AND config=?")) {
                    ps.setString(1, proj); ps.setString(2, cfg);
                    try (ResultSet rs = ps.executeQuery()) {
                        if (rs.next() && rs.getString(1) != null) errMsg = ": " + rs.getString(1).substring(0, Math.min(200, rs.getString(1).length()));
                    }
                }
                Db.die(proj + " has no " + cfg + errMsg, 1);
            }
            long pc = pcs.get(0)[0];
            List<Object[]> nodes = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT c.cid, c.version, n.direct, n.reqs, n.reasons FROM nodes n " +
                    "JOIN comps c ON c.id=n.comp WHERE n.pc=? ORDER BY c.cid")) {
                ps.setLong(1, pc);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) nodes.add(new Object[]{
                        rs.getString(1), rs.getString(2), rs.getInt(3), rs.getString(4), rs.getString(5)
                    });
                }
            }
            long unresCount = Lookups.scalar(conn, "SELECT count(DISTINCT req) FROM unresolved WHERE pc=?", pc);
            List<Object[]> direct = nodes.stream().filter(n -> (int) n[2] == 1).toList();

            System.out.print(proj + " [" + cfg + "]: " + direct.size() + " direct, " + nodes.size() + " total");
            if (unresCount > 0) System.out.print(", " + unresCount + " unresolved");
            System.out.println();

            System.out.println("direct:");
            Lookups.emit(direct.stream().map(n -> "  " + fmt(n)).toList(), limit);
            if (all) {
                System.out.println("transitive:");
                Lookups.emit(nodes.stream().filter(n -> (int) n[2] == 0).map(n -> "  " + fmt(n)).toList(), limit);
            } else if (nodes.size() > direct.size()) {
                System.out.println("(+" + (nodes.size() - direct.size()) + " transitive; --all to list)");
            }
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }

    private static String fmt(Object[] n) {
        String cid = (String) n[0], v = (String) n[1], reqs = (String) n[3];
        StringBuilder s = new StringBuilder(cid);
        if (reqs != null && !reqs.isEmpty()) {
            List<String> rq = new ArrayList<>();
            for (String r : reqs.split(",")) if (!r.isEmpty() && !r.equals(v)) rq.add(r);
            if (!rq.isEmpty()) s.append("  (requested ").append(String.join(",", rq)).append(")");
        }
        return s.toString();
    }
}

// ---- pkg ----

@Command(name = "pkg", description = "Which jar (and version) provides a package or class.")
class PkgCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String name;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String q = name.strip().replaceAll("\\.\\*$", "");
            String last = q.contains(".") ? q.substring(q.lastIndexOf('.') + 1) : q;
            List<Object[]> hits = new ArrayList<>();

            if (last.length() > 0 && Character.isUpperCase(last.charAt(0))) {
                // Class lookup
                if (q.contains(".")) {
                    String pkg = q.substring(0, q.lastIndexOf('.'));
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT ga, version FROM classes WHERE simple=? AND pkg=?")) {
                        ps.setString(1, last); ps.setString(2, pkg);
                        try (ResultSet rs = ps.executeQuery()) {
                            while (rs.next()) hits.add(new Object[]{pkg + "." + last, rs.getString(1), rs.getString(2), 1});
                        }
                    }
                } else {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT pkg, ga, version FROM classes WHERE simple=? ORDER BY pkg")) {
                        ps.setString(1, last);
                        try (ResultSet rs = ps.executeQuery()) {
                            while (rs.next()) hits.add(new Object[]{rs.getString(1) + "." + last, rs.getString(2), rs.getString(3), 1});
                        }
                    }
                }
                // Fallback: treat as package
                if (hits.isEmpty() && q.contains(".")) q = q.substring(0, q.lastIndexOf('.'));
            }
            if (hits.isEmpty()) {
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT pkg, ga, version, classes FROM packages WHERE pkg=? ORDER BY ga")) {
                    ps.setString(1, q);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) hits.add(new Object[]{rs.getString(1), rs.getString(2), rs.getString(3), rs.getLong(4)});
                    }
                }
            }
            if (hits.isEmpty()) {
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT pkg, ga, version, classes FROM packages WHERE pkg LIKE ? ORDER BY pkg, ga")) {
                    ps.setString(1, q + ".%");
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) hits.add(new Object[]{rs.getString(1), rs.getString(2), rs.getString(3), rs.getLong(4)});
                    }
                }
            }
            if (hits.isEmpty()) {
                System.out.println("nothing provides '" + name + "' in the indexed jars (only external modules are indexed)");
                return;
            }
            // Group by package
            Map<String, List<Object[]>> byPkg = new LinkedHashMap<>();
            for (Object[] h : hits) byPkg.computeIfAbsent((String) h[0], k -> new ArrayList<>()).add(h);
            List<String> lines = new ArrayList<>();
            for (Map.Entry<String, List<Object[]>> e : byPkg.entrySet()) {
                Set<String> gas = new HashSet<>();
                for (Object[] h : e.getValue()) gas.add((String) h[1]);
                String tag = gas.size() > 1 ? "  [SPLIT: in several modules]" : "";
                List<String> parts = new ArrayList<>();
                for (Object[] h : e.getValue()) {
                    long np = Lookups.scalar(conn,
                        "SELECT count(DISTINCT p.project) FROM comps c JOIN nodes n ON n.comp=c.id " +
                        "JOIN pcs p ON p.id=n.pc WHERE c.ga=? AND c.version=?", h[1], h[2]);
                    parts.add(h[1] + ":" + h[2] + " (" + np + " proj)");
                }
                lines.add(e.getKey() + " -> " + String.join(", ", parts) + tag);
            }
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- info ----

@Command(name = "info", description = "Module facts: license, packages, JPMS name, version catalog alias.")
class InfoCmd implements Runnable {
    @ParentCommand Main parent;
    @Parameters(index = "0") String module;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String ga = Lookups.oneGa(conn, module, limit);
            List<Object[]> vs = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT c.version, count(DISTINCT p.project), max(n.direct) FROM comps c " +
                    "JOIN nodes n ON n.comp=c.id JOIN pcs p ON p.id=n.pc WHERE c.ga=? GROUP BY c.version")) {
                ps.setString(1, ga);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) vs.add(new Object[]{rs.getString(1), rs.getLong(2), rs.getInt(3)});
                }
            }
            vs.sort((a, b) -> Ingest.vkey((String) b[0]).compareTo(Ingest.vkey((String) a[0])));
            System.out.println(ga);
            if (!vs.isEmpty()) {
                List<String> vParts = new ArrayList<>();
                for (Object[] v : vs) vParts.add(v[0] + " (" + v[1] + " proj" + ((int) v[2] == 1 ? ", direct" : "") + ")");
                System.out.println("in use: " + String.join(", ", vParts));
            }
            String v0 = vs.isEmpty() ? null : (String) vs.get(0)[0];
            if (v0 != null) {
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT description, url, licenses, jpms, classes FROM pkginfo WHERE ga=? AND version=?")) {
                    ps.setString(1, ga); ps.setString(2, v0);
                    try (ResultSet rs = ps.executeQuery()) {
                        if (rs.next()) {
                            String desc = rs.getString(1), url = rs.getString(2),
                                   lic = rs.getString(3), jpms = rs.getString(4);
                            long ncls = rs.getLong(5);
                            if (desc != null && !desc.isEmpty())
                                System.out.println("about: " + desc.substring(0, Math.min(200, desc.length())));
                            System.out.println("license: " + (lic != null && !lic.isEmpty() ? lic : "unknown")
                                    + (url != null && !url.isEmpty() ? " | url: " + url : ""));
                            if (jpms != null && !jpms.isEmpty()) System.out.println("JPMS module: " + jpms);
                            List<Object[]> pk = new ArrayList<>();
                            try (PreparedStatement ps2 = conn.prepareStatement(
                                    "SELECT pkg, classes FROM packages WHERE ga=? AND version=? ORDER BY classes DESC")) {
                                ps2.setString(1, ga); ps2.setString(2, v0);
                                try (ResultSet rs2 = ps2.executeQuery()) {
                                    while (rs2.next()) pk.add(new Object[]{rs2.getString(1), rs2.getLong(2)});
                                }
                            }
                            if (!pk.isEmpty()) {
                                List<String> pkNames = pk.subList(0, Math.min(8, pk.size()))
                                        .stream().map(p -> (String) p[0]).toList();
                                System.out.println("packages (" + pk.size() + ", " + ncls + " classes): "
                                        + String.join(", ", pkNames) + (pk.size() > 8 ? " ..." : ""));
                            }
                        }
                    }
                }
            }
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT accessor, version, ref FROM catalog WHERE ga=?")) {
                ps.setString(1, ga);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) System.out.println("catalog: " + rs.getString(1) + " = " + rs.getString(2)
                            + (rs.getString(3) != null ? " (version.ref " + rs.getString(3) + ")" : ""));
                }
            }
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- conflicts ----

@Command(name = "conflicts", description = "Modules whose version was chosen by conflict resolution.")
class ConflictsCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = {"-p", "--project"}) String project;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--all") boolean all;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String proj = Lookups.normProject(conn, project);
            String pat = all ? "" : "AND (n.reasons LIKE '%conflict%' OR n.reasons LIKE '%forced%' OR n.reasons LIKE '%rule%')";
            String sql = "SELECT c.ga, c.version, n.reqs, n.reasons, p.project FROM nodes n " +
                "JOIN comps c ON c.id=n.comp JOIN pcs p ON p.id=n.pc WHERE n.reasons != '' " + pat
                + (proj != null ? " AND p.project=?" : "") + (config != null ? " AND p.config=?" : "");
            Map<String, Object[]> agg = new LinkedHashMap<>();
            try (PreparedStatement ps = conn.prepareStatement(sql)) {
                int i = 1;
                if (proj != null) ps.setString(i++, proj);
                if (config != null) ps.setString(i++, config);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        String ga = rs.getString(1), v = rs.getString(2), reqs = rs.getString(3),
                               reasons = rs.getString(4), pj = rs.getString(5);
                        String key = ga + "|" + v;
                        if (!agg.containsKey(key)) agg.put(key, new Object[]{ga, v, new HashSet<String>(), new HashSet<String>(), new HashSet<String>()});
                        Object[] a = agg.get(key);
                        if (reqs != null) for (String r : reqs.split(",")) if (!r.isEmpty()) ((Set<String>) a[2]).add(r);
                        ((Set<String>) a[3]).add(pj);
                        if (reasons != null) for (String r : reasons.split("; ")) ((Set<String>) a[4]).add(r.split(":")[0]);
                    }
                }
            }
            List<String> lines = new ArrayList<>();
            agg.values().stream()
                .sorted((a, b) -> Integer.compare(((Set<?>) b[3]).size(), ((Set<?>) a[3]).size()))
                .forEach(entry -> {
                    String ga = (String) entry[0], v = (String) entry[1];
                    @SuppressWarnings("unchecked") Set<String> reqs = (Set<String>) entry[2];
                    @SuppressWarnings("unchecked") Set<String> pjs  = (Set<String>) entry[3];
                    @SuppressWarnings("unchecked") Set<String> why  = (Set<String>) entry[4];
                    List<String> other = reqs.stream().filter(r -> !r.equals(v)).sorted(Ingest::vkeyCompare).toList();
                    lines.add(ga + " -> " + v + (other.isEmpty() ? "" : " (also requested " + String.join(",", other) + ")")
                        + " [" + String.join(", ", why.stream().sorted().toList()) + "] in " + pjs.size() + " proj");
                });
            if (lines.isEmpty()) System.out.println("no conflict-resolved modules");
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// ---- drift ----

@Command(name = "drift", description = "Libraries resolved to different versions across projects.")
class DriftCmd implements Runnable {
    @ParentCommand Main parent;
    @Option(names = {"-c", "--config"}) String config;
    @Option(names = "--limit", defaultValue = "40") int limit;

    @Override public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String cfg = config != null ? config : "runtimeClasspath";
            Map<String, Map<String, List<String>>> m = new TreeMap<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT c.ga, c.version, p.project FROM nodes n JOIN comps c ON c.id=n.comp " +
                    "JOIN pcs p ON p.id=n.pc WHERE c.kind='m' AND p.config=?")) {
                ps.setString(1, cfg);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) m.computeIfAbsent(rs.getString(1), k -> new TreeMap<>())
                            .computeIfAbsent(rs.getString(2), k -> new ArrayList<>()).add(rs.getString(3));
                }
            }
            List<String> lines = new ArrayList<>();
            m.entrySet().stream().filter(e -> e.getValue().size() >= 2).forEach(e -> {
                List<String> parts = new ArrayList<>();
                e.getValue().entrySet().stream()
                    .sorted((a, b) -> Ingest.vkey(b.getKey()).compareTo(Ingest.vkey(a.getKey())))
                    .forEach(ve -> {
                        List<String> ps = ve.getValue().stream().sorted().toList();
                        parts.add(ve.getKey() + " (" + String.join(", ", ps.subList(0, Math.min(3, ps.size())))
                            + (ps.size() > 3 ? " +" + (ps.size() - 3) : "") + ")");
                    });
                lines.add(e.getKey() + ": " + String.join(" | ", parts));
            });
            System.out.println("modules resolved to different versions across projects [" + cfg + "]: " + lines.size());
            Lookups.emit(lines, limit);
        } catch (Exception e) { Db.die(e.getMessage(), 1); }
    }
}

// keep package-private stubs so they compile as package reference from Main
