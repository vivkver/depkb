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
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

@Command(name = "users", description = "Source files that import a class or package.")
public class UsersCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Class name or package (simple or fully-qualified)")
    public String name;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--files", description = "Show file:line of each import")
    public boolean files;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    private static final String EVID_JOIN =
        "JOIN evidence ev ON ev.project=f.project AND ev.sset=f.sset AND (f.kind=ev.kind OR f.kind='res')";

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String q = name.strip().replaceAll("\\.\\*$", "");
            String projFilter = project != null ? Lookups.normProject(conn, project) : null;

            // Build WHERE clause
            String cond;
            List<String> args = new ArrayList<>();
            if (q.contains(".")) {
                cond = "(i.fqn=? OR i.fqn LIKE ?)";
                args.add(q);
                args.add(q + ".%");
            } else {
                // simple class name: match *.Name or *.Name.*
                cond = "(i.fqn LIKE ? OR i.fqn LIKE ?)";
                args.add("%." + q);
                args.add("%." + q + ".%");
            }
            if (projFilter != null) {
                cond += " AND f.project=?";
                args.add(projFilter);
            }

            String sql = "SELECT f.project, f.sset, f.path, i.line, i.fqn, i.via, " +
                         "l.kind, l.ga, l.owner " +
                         "FROM imports i " +
                         "JOIN srcfiles f ON f.id=i.file " + EVID_JOIN + " " +
                         "LEFT JOIN links l ON l.project=f.project AND l.sset=f.sset " +
                         "AND l.fqn=i.fqn AND l.static=i.static AND l.wildcard=i.wildcard " +
                         "WHERE " + cond + " ORDER BY f.project, f.path";

            List<Object[]> rows = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(sql)) {
                for (int i = 0; i < args.size(); i++) ps.setString(i + 1, args.get(i));
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        rows.add(new Object[]{
                            rs.getString(1), rs.getString(2), rs.getString(3),
                            rs.getObject(4), rs.getString(5), rs.getString(6),
                            rs.getString(7), rs.getString(8), rs.getString(9)
                        });
                    }
                }
            }

            // Collect source libraries
            Map<String, Boolean> src = new HashMap<>();
            for (Object[] r : rows) {
                String kind = (String) r[6], ga = (String) r[7], owner = (String) r[8];
                String k = (ga != null && !ga.isEmpty()) ? ga :
                           (owner != null && !owner.isEmpty() ? "project " + owner :
                           (kind != null ? kind : "?"));
                src.put(k, true);
            }

            Set<String> uniqueFiles = new HashSet<>();
            Map<String, List<Object[]>> byProj = new TreeMap<>();
            for (Object[] r : rows) {
                String pj = (String) r[0], ss = (String) r[1], path = (String) r[2];
                Object line = r[3]; String fqn = (String) r[4], via = (String) r[5];
                uniqueFiles.add(path);
                byProj.computeIfAbsent(pj, k -> new ArrayList<>()).add(new Object[]{ss, path, line, fqn, via});
            }

            System.out.print("'" + name + "': referenced from " + uniqueFiles.size() + " files in " +
                             Lookups.plural(byProj.size(), "module"));
            if (!src.isEmpty()) System.out.print("; provided by " + String.join(", ", src.keySet()));
            System.out.println();

            List<String> lines = new ArrayList<>();
            for (Map.Entry<String, List<Object[]>> e : byProj.entrySet()) {
                String pj = e.getKey();
                List<Object[]> items = e.getValue();
                if (files) {
                    for (Object[] item : items) {
                        String path = (String) item[1]; Object line = item[2];
                        String fqn = (String) item[3], via = (String) item[4];
                        String relPath = Analysis.rel(conn, pj, path);
                        lines.add(pj + " " + relPath + (line != null ? ":" + line : "") + " " + fqn +
                                  ("import".equals(via) || "class".equals(via) ? "" : " [" + via + "]"));
                    }
                } else {
                    // Per sourceset: count files
                    Map<String, Set<String>> perSset = new TreeMap<>();
                    for (Object[] item : items) {
                        String ss = (String) item[0], path = (String) item[1];
                        perSset.computeIfAbsent(ss, k -> new HashSet<>()).add(path);
                    }
                    List<String> parts = new ArrayList<>();
                    for (Map.Entry<String, Set<String>> se : perSset.entrySet()) {
                        parts.add(se.getKey() + ":" + se.getValue().size() + "f");
                    }
                    lines.add(pj + ": " + String.join(" ", parts));
                }
            }
            Lookups.emit(lines, limit);
            if (!files && !lines.isEmpty()) System.out.println("(--files for file:line)");
        } catch (Exception e) {
            Db.die("users: " + e.getMessage(), 1);
        }
    }
}
