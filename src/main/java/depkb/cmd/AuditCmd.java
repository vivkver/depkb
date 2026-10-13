package depkb.cmd;

import depkb.Analysis;
import depkb.Db;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
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

@Command(name = "audit", description = "Undeclared/unused/broken dependencies from code analysis.")
public class AuditCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    private static final String[][] FINDING_TITLES = {
        {"UNDECLARED", "UNDECLARED (used, only transitive)"},
        {"NOT_ON_CP",  "NOT ON COMPILE CLASSPATH (will not compile)"},
        {"UNUSED",     "UNUSED? (declared, never referenced)"},
        {"API",        "SHOULD BE api (exposed in public API)"},
        {"IMPL",       "COULD BE implementation (not in public API)"}
    };

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);

            List<String> projects;
            if (project != null) {
                projects = List.of(Lookups.normProject(conn, project));
            } else {
                projects = new ArrayList<>();
                try (PreparedStatement ps = conn.prepareStatement("SELECT path FROM projects ORDER BY path");
                     ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) projects.add(rs.getString(1));
                }
            }

            Map<String, List<String>> found = new TreeMap<>();
            Map<String, Integer> unknown = new TreeMap<>();

            for (String pj : projects) {
                Analysis an = new Analysis(conn, pj);
                Set<String> keys = new HashSet<>(an.uses.keySet());
                keys.addAll(an.decl.keySet());

                for (String key : keys) {
                    for (Object[] f : an.findings(key)) {
                        String t = (String) f[0], ss = (String) f[1], txt = (String) f[2];
                        int n = 0;
                        Map<String, Analysis.Use> usesForKey = an.uses.get(key);
                        if (usesForKey != null) {
                            Analysis.Use u = usesForKey.get(ss);
                            if (u != null) n = u.files.size();
                        }
                        String line = "  " + pj + ("main".equals(ss) ? "" : " [" + ss + "]") +
                                      " -> " + key +
                                      (n > 0 ? " (" + Lookups.plural(n, "file") + ")" : "") +
                                      ("UNDECLARED".equals(t) || "NOT_ON_CP".equals(t) ? "" : ": " + txt);
                        found.computeIfAbsent(t, k -> new ArrayList<>()).add(line);
                    }
                }

                int unk = an.unknown.values().stream()
                    .mapToInt(v -> {
                        Set<String> paths = new HashSet<>();
                        for (Object[] r : v) paths.add((String) r[0]);
                        return paths.size();
                    }).sum();
                if (unk > 0) unknown.put(pj, unk);
            }

            System.out.println("evidence: " + Analysis.evidenceNote(conn, projects));

            for (String[] titleRow : FINDING_TITLES) {
                String t = titleRow[0], title = titleRow[1];
                List<String> items = found.getOrDefault(t, List.of());
                List<String> sorted = new ArrayList<>(items);
                java.util.Collections.sort(sorted);
                System.out.println(title + ": " + sorted.size());
                Lookups.emit(sorted, limit);
            }

            if (!unknown.isEmpty()) {
                int total = unknown.values().stream().mapToInt(Integer::intValue).sum();
                List<String> parts = new ArrayList<>();
                int shown = 0;
                for (Map.Entry<String, Integer> e : unknown.entrySet()) {
                    if (shown++ < 5) parts.add(e.getKey() + "(" + e.getValue() + ")");
                }
                System.out.println("unresolved references (generated code / missing deps): " + total +
                    " files in " + String.join(", ", parts) +
                    " — `depkb imports <module> --unknown`");
            }

            System.out.println(Analysis.CAVEAT);
        } catch (Exception e) {
            Db.die("audit: " + e.getMessage(), 1);
        }
    }
}
