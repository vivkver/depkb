package depkb.cmd;

import depkb.Analysis;
import depkb.Db;
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
import java.util.TreeMap;

@Command(name = "runtime", description = "A library as it runs in each app module (runtimeClasspath).")
public class RuntimeCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "group:artifact, artifact, or substring")
    public String module;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--files", description = "List file:line of each import")
    public boolean files;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String ga = Lookups.oneGa(conn, module, limit);

            // Find app projects (or all if project specified or no apps)
            List<String> apps = new ArrayList<>();
            String note = "";
            if (project != null) {
                apps.add(Lookups.normProject(conn, project));
            } else {
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT path FROM projects WHERE kind='app' ORDER BY path");
                     ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) apps.add(rs.getString(1));
                }
                if (apps.isEmpty()) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT path FROM projects ORDER BY path");
                         ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) apps.add(rs.getString(1));
                    }
                    note = " (no application/war/boot modules detected; showing all modules)";
                }
            }

            System.out.println(ga + " at runtime" + note + ":");
            int shown = 0;

            for (String app : apps) {
                Object[] n = Analysis.node(conn, app, "runtimeClasspath", ga);
                if (n == null) {
                    if (project != null) System.out.println(app + ": not on runtimeClasspath");
                    continue;
                }
                shown++;

                String ver = (String) n[2];
                String overNote = Analysis.overrideNote(conn, (long) n[0], (long) n[1],
                                                        ver, (String) n[4], (String) n[5]);
                System.out.println(app + " runs " + ver +
                    (overNote.isEmpty() ? "" : " — " + overNote));

                // Get the project closure (app + its project deps)
                long pcId = (long) n[0];
                Set<String> closure = new HashSet<>();
                closure.add(app);
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT c.cid FROM nodes n JOIN comps c ON c.id=n.comp " +
                        "WHERE n.pc=? AND c.kind='p'")) {
                    ps.setLong(1, pcId);
                    try (ResultSet rs = ps.executeQuery()) {
                        while (rs.next()) {
                            String cid = rs.getString(1);
                            if (cid.startsWith("project ")) closure.add(cid.substring(8));
                        }
                    }
                }

                // Who declared it?
                List<String> decl = new ArrayList<>();
                if (!closure.isEmpty()) {
                    String placeholders = "?,".repeat(closure.size()).replaceAll(",$", "");
                    String declSql = "SELECT project, bucket, version FROM declared WHERE id=? AND project IN (" +
                                     placeholders + ") ORDER BY project";
                    try (PreparedStatement ps = conn.prepareStatement(declSql)) {
                        ps.setString(1, ga);
                        List<String> closureList = new ArrayList<>(closure);
                        for (int i = 0; i < closureList.size(); i++) ps.setString(i + 2, closureList.get(i));
                        try (ResultSet rs = ps.executeQuery()) {
                            while (rs.next()) {
                                String pj = rs.getString(1), b = rs.getString(2), v = rs.getString(3);
                                decl.add(pj + " (" + b + (v != null && !v.isEmpty() ? " " + v : "") + ")");
                            }
                        }
                    }
                }

                // Who uses it in main code?
                List<Object[]> useRows = Analysis.useRows(conn, "l.ga=? AND f.sset='main'", ga, null);
                Map<String, Map<String, Analysis.Use>> agg = new TreeMap<>();
                for (Object[] r : useRows) {
                    String pj = (String) r[0], ss = (String) r[1], path = (String) r[2];
                    Object lineObj = r[4]; String fqn = (String) r[5], via = (String) r[6];
                    String lkind = (String) r[7]; String lga = (String) r[8]; String lver = (String) r[9];
                    String owner = (String) r[10];
                    if ("jdk".equals(lkind) || "self".equals(lkind) || "unknown".equals(lkind)) continue;
                    String depKey = (lkind != null && lkind.startsWith("lib")) ? lga : "project " + owner;
                    if (!ga.equals(depKey)) continue;
                    Analysis.Use u = agg.computeIfAbsent(pj, k -> new TreeMap<>())
                                       .computeIfAbsent(ss, k -> new Analysis.Use());
                    u.files.add(path);
                    u.vias.add(via);
                    u.version = lver;
                    u.detail.add(new Object[]{path, lineObj, fqn, via});
                }
                Map<String, Analysis.Use> users = new TreeMap<>();
                for (Map.Entry<String, Map<String, Analysis.Use>> e : agg.entrySet()) {
                    if (closure.contains(e.getKey())) {
                        Analysis.Use merged = new Analysis.Use();
                        for (Analysis.Use u : e.getValue().values()) {
                            merged.files.addAll(u.files);
                            merged.detail.addAll(u.detail);
                        }
                        users.put(e.getKey(), merged);
                    }
                }

                System.out.println("  declared by: " + (decl.isEmpty() ? "nobody (purely transitive)" :
                    String.join(", ", decl)));
                List<String> userParts = new ArrayList<>();
                for (Map.Entry<String, Analysis.Use> e : users.entrySet()) {
                    userParts.add(e.getKey() + " (" + Lookups.plural(e.getValue().files.size(), "file") + ")");
                }
                System.out.println("  used by: " + (userParts.isEmpty() ?
                    "no main code references it" : String.join(", ", userParts)));

                if (files) {
                    for (Map.Entry<String, Analysis.Use> e : users.entrySet()) {
                        Analysis.printDetail(conn, e.getKey(),
                            Map.of("main", e.getValue()), limit);
                    }
                }
            }

            if (shown == 0) {
                System.out.println("  not on the runtime classpath of any " +
                    (note.isEmpty() ? "app " : "") + "module");
            }
        } catch (Exception e) {
            Db.die("runtime: " + e.getMessage(), 1);
        }
    }
}
