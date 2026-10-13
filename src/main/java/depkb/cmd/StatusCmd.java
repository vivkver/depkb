package depkb.cmd;

import depkb.BuildFileTracker;
import depkb.Db;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

@Command(name = "status", description = "KB freshness and failed configurations.")
public class StatusCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            String projDir = Db.meta(conn, "project_dir", ".");
            Path proj = Path.of(projDir);

            Set<String> changed = java.nio.file.Files.isDirectory(proj)
                    ? BuildFileTracker.changedModules(conn) : Set.of();
            String state = changed.isEmpty() ? "fresh"
                    : "STALE: " + String.join(", ",
                        changed.stream().sorted().limit(8).toList())
                        + " changed (any query refreshes it)";

            long nSrc = Lookups.scalar(conn, "SELECT count(*) FROM srcfiles");
            String builtAt = Db.meta(conn, "built_at", "?");
            System.out.println("built " + builtAt + " from " + proj + " — " + state + " | " + nSrc + " source files indexed");

            List<String> lines = new ArrayList<>();
            try (var ps = conn.prepareStatement(
                    "SELECT project, config, error FROM pcs WHERE error IS NOT NULL");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    String p = rs.getString(1), c = rs.getString(2), e = rs.getString(3);
                    String firstLine = e.split("\n")[0];
                    if (firstLine.length() > 160) firstLine = firstLine.substring(0, 160);
                    lines.add("  failed " + p + " [" + Lookups.ab(c) + "]: " + firstLine);
                }
            }
            Lookups.emit(lines, limit);
        } catch (Exception e) {
            Db.die("status: " + e.getMessage(), 1);
        }
    }
}
