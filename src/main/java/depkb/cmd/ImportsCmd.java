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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

@Command(name = "imports", description = "What a module's code imports, grouped by dependency.")
public class ImportsCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--unknown", description = "List unresolved imports")
    public boolean unknown;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String pj = Lookups.normProject(conn, project);
            Analysis an = new Analysis(conn, pj);

            // Header: "proj uses (main: compiled 12 files; test: ...)"
            List<String> evParts = new ArrayList<>();
            for (Map.Entry<String, String[]> e : new TreeMap<>(an.evidence).entrySet()) {
                evParts.add(e.getKey() + ": " + e.getValue()[1]);
            }
            System.out.println(pj + " uses (" + String.join(", ", evParts) + ")");

            // Group by sourceset
            Map<String, List<String>> bySset = new TreeMap<>();
            for (Map.Entry<String, Map<String, Analysis.Use>> entry : new TreeMap<>(an.uses).entrySet()) {
                String key = entry.getKey();
                Map<String, Analysis.Use> ssUses = entry.getValue();
                List<Object[]> findings = an.findings(key);
                for (Map.Entry<String, Analysis.Use> se : new TreeMap<>(ssUses).entrySet()) {
                    String ss = se.getKey(); Analysis.Use u = se.getValue();
                    String lbl = an.label(key);
                    String label = lbl != null ? lbl :
                        (an.cp.getOrDefault(ss, java.util.Set.of()).contains(key) ? "transitive" : "not declared");

                    // flags for this (key, ss)
                    List<String> flags = new ArrayList<>();
                    for (Object[] f : findings) {
                        if (ss.equals(f[1])) flags.add((String) f[0]);
                    }

                    String ver = u.version != null ? ":" + u.version : "";
                    String how = "";
                    if (!u.compile) {
                        List<String> vias = new ArrayList<>(u.vias);
                        java.util.Collections.sort(vias);
                        how = " (" + String.join("/", vias) + " only)";
                    }
                    String line = "  " + key + ver + "  " + Lookups.plural(u.files.size(), "file") + how +
                                  "  " + label +
                                  (flags.isEmpty() ? "" : "  ! " + String.join(", ", flags));
                    bySset.computeIfAbsent(ss, k -> new ArrayList<>()).add(line);
                }
            }
            for (Map.Entry<String, List<String>> e : bySset.entrySet()) {
                System.out.println(e.getKey() + ":");
                List<String> sorted = new ArrayList<>(e.getValue());
                java.util.Collections.sort(sorted);
                Lookups.emit(sorted, limit);
            }

            // UNUSED? entries
            List<String> unusedLines = new ArrayList<>();
            for (Map.Entry<String, Map<String, Analysis.Use>> entry : an.uses.entrySet()) {
                // already shown above
            }
            for (String key : an.decl.keySet()) {
                for (Object[] f : an.findings(key)) {
                    if ("UNUSED".equals(f[0])) {
                        unusedLines.add("  " + key + " [" + f[1] + "] " + f[2]);
                    }
                }
            }
            if (!unusedLines.isEmpty()) {
                System.out.println("declared but never referenced (UNUSED?):");
                Lookups.emit(unusedLines, limit);
            }

            // Unknown references
            int nUnknown = an.unknown.values().stream().mapToInt(List::size).sum();
            if (nUnknown > 0) {
                System.out.println("unresolved references: " + nUnknown +
                    (unknown ? "" : " (--unknown to list)"));
                if (unknown) {
                    List<String> ukLines = new ArrayList<>();
                    for (Map.Entry<String, List<Object[]>> e : an.unknown.entrySet()) {
                        for (Object[] r : e.getValue()) {
                            String path = (String) r[0]; Object line = r[1]; String fqn = (String) r[2];
                            String relPath = Analysis.rel(conn, pj, path);
                            ukLines.add("  " + relPath + (line != null ? ":" + line : "") + " " + fqn);
                        }
                    }
                    java.util.Collections.sort(ukLines);
                    Lookups.emit(ukLines, limit);
                }
            }

            if (!unusedLines.isEmpty()) System.out.println(Analysis.CAVEAT);
        } catch (Exception e) {
            Db.die("imports: " + e.getMessage(), 1);
        }
    }
}
