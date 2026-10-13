package depkb.cmd;

import depkb.Analysis;
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
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

@Command(name = "usage", description = "Per module: declared bucket, effective version, override, imported?")
public class UsageCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Library (group:artifact, artifact) or module path like :common")
    public String module;

    @Option(names = "--files", description = "List file:line of each import")
    public boolean files;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    private static final String[] MAIN_CFGS = {"compileClasspath", "runtimeClasspath"};
    private static final String[] TEST_CFGS  = {"testCompileClasspath", "testRuntimeClasspath"};

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);

            boolean isProject = module.startsWith(":");
            String key;
            String ga = null;
            if (isProject) {
                String norm = Lookups.normProject(conn, module);
                key = "project " + norm;
            } else {
                ga = Lookups.oneGa(conn, module, limit);
                key = ga;
            }

            // Collect all projects that have this key anywhere
            Set<String> projects = new TreeSet<>();
            final String fKey = key;
            final String fGa = ga;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT DISTINCT p.project FROM nodes n JOIN comps c ON c.id=n.comp " +
                    "JOIN pcs p ON p.id=n.pc WHERE c.ga=? OR c.cid=?")) {
                ps.setString(1, fKey); ps.setString(2, fKey);
                try (ResultSet rs = ps.executeQuery()) { while (rs.next()) projects.add(rs.getString(1)); }
            }
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT project FROM declared WHERE id=?")) {
                ps.setString(1, fKey);
                try (ResultSet rs = ps.executeQuery()) { while (rs.next()) projects.add(rs.getString(1)); }
            }
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT project FROM links WHERE ga=? OR 'project '||owner=?")) {
                ps.setString(1, fKey); ps.setString(2, fKey);
                try (ResultSet rs = ps.executeQuery()) { while (rs.next()) projects.add(rs.getString(1)); }
            }

            Map<String, String> kinds = new java.util.HashMap<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT path, kind FROM projects");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) kinds.put(rs.getString(1), rs.getString(2));
            }

            List<String> lines = new ArrayList<>();
            List<Object[]> detailRows = new ArrayList<>(); // {project, by_sset}
            boolean flagged = false;

            for (String pj : projects) {
                if (("project " + pj).equals(fKey)) continue;

                Analysis an = new Analysis(conn, pj, fKey);
                Object[] rt = null;

                boolean onCompile = false, onRuntime = false, onTest = false;
                if (!isProject) {
                    for (String cfg : MAIN_CFGS) {
                        Object[] n = Analysis.node(conn, pj, cfg, fGa);
                        if (n != null) {
                            if (rt == null) rt = n;
                            if ("compileClasspath".equals(cfg)) onCompile = true;
                            if ("runtimeClasspath".equals(cfg)) { onRuntime = true; if (rt == null || onCompile) rt = n; }
                        }
                    }
                    for (String cfg : TEST_CFGS) {
                        if (Analysis.node(conn, pj, cfg, fGa) != null) onTest = true;
                    }
                    // prefer runtimeClasspath node
                    Object[] rn = Analysis.node(conn, pj, "runtimeClasspath", fGa);
                    Object[] cn = Analysis.node(conn, pj, "compileClasspath", fGa);
                    rt = rn != null ? rn : cn;
                    if (rt == null) {
                        for (String cfg : TEST_CFGS) {
                            rt = Analysis.node(conn, pj, cfg, fGa);
                            if (rt != null) break;
                        }
                    }
                } else {
                    Set<String> cfgs = new HashSet<>();
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT p.config FROM nodes n JOIN comps c ON c.id=n.comp " +
                            "JOIN pcs p ON p.id=n.pc WHERE p.project=? AND c.cid=?")) {
                        ps.setString(1, pj); ps.setString(2, fKey);
                        try (ResultSet rs = ps.executeQuery()) { while (rs.next()) cfgs.add(rs.getString(1)); }
                    }
                    onCompile = cfgs.contains("compileClasspath");
                    onRuntime = cfgs.contains("runtimeClasspath");
                    onTest = cfgs.stream().anyMatch(c -> c.startsWith("test"));
                }

                String label = an.label(fKey);
                String decl = label != null ? label :
                    (onCompile ? "transitive" : onRuntime ? "transitive(runtime only)" :
                     onTest ? "transitive(test)" : "not on classpath");

                String ver = rt != null ? (String) rt[2] + "  " : "";
                String app = "app".equals(kinds.get(pj)) ? "(app)" : "";
                String s = pj + app + "  " + ver + decl + "  " + an.usesSummary(fKey);

                if (rt != null) {
                    String note = Analysis.overrideNote(conn, (long) rt[0], (long) rt[1],
                                                        (String) rt[2], (String) rt[4], (String) rt[5]);
                    if (!note.isEmpty()) s += "  | " + note;
                }

                List<Object[]> findings = an.findings(fKey);
                if (!findings.isEmpty()) {
                    flagged = true;
                    List<String> fparts = new ArrayList<>();
                    for (Object[] f : findings) {
                        String t = (String) f[0], ss = (String) f[1], txt = (String) f[2];
                        fparts.add(t + ("main".equals(ss) ? "" : "(" + ss + ")") + ": " + txt);
                    }
                    s += "  ! " + String.join("; ", fparts);
                }
                lines.add(s);

                if (files && an.uses.containsKey(fKey)) {
                    detailRows.add(new Object[]{pj, an.uses.get(fKey)});
                }
            }

            String projNote = projects.isEmpty() ? "" :
                "  [" + Analysis.evidenceNote(conn, new ArrayList<>(projects)) + "]";
            System.out.println(fKey + " — " + Lookups.plural(lines.size(), "module") + projNote);
            Lookups.emit(lines, limit);

            for (Object[] dr : detailRows) {
                String pj = (String) dr[0];
                @SuppressWarnings("unchecked") Map<String, Analysis.Use> bySset =
                    (Map<String, Analysis.Use>) dr[1];
                System.out.println("  " + pj + ":");
                Analysis.printDetail(conn, pj, bySset, limit);
            }

            if (flagged) System.out.println(Analysis.CAVEAT);
        } catch (Exception e) {
            Db.die("usage: " + e.getMessage(), 1);
        }
    }
}
