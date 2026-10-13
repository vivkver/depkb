package depkb.cmd;

import depkb.Db;
import depkb.Lookups;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;

@Command(name = "summary", description = "One-screen overview of the knowledge base.")
public class SummaryCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        try {
            Connection conn = Db.connect(Db.findDb(parent.db), true);
            long nProj  = Lookups.scalar(conn, "SELECT count(*) FROM projects");
            long nErr   = Lookups.scalar(conn, "SELECT count(*) FROM pcs WHERE error IS NOT NULL");
            long nGa    = Lookups.scalar(conn, "SELECT count(DISTINCT ga) FROM comps WHERE kind='m'");
            long nGav   = Lookups.scalar(conn, "SELECT count(*) FROM comps WHERE kind='m'");
            long multi  = Lookups.scalar(conn, "SELECT count(*) FROM (SELECT ga FROM comps WHERE kind='m' GROUP BY ga HAVING count(*)>1)");
            long confl  = Lookups.scalar(conn, "SELECT count(DISTINCT c.ga) FROM nodes n JOIN comps c ON c.id=n.comp WHERE n.reasons LIKE '%conflict%'");
            long unres  = Lookups.scalar(conn, "SELECT count(DISTINCT req) FROM unresolved");
            long nPkg   = Lookups.scalar(conn, "SELECT count(DISTINCT pkg) FROM packages");
            long nCat   = Lookups.scalar(conn, "SELECT count(*) FROM catalog");
            String builtAt = Db.meta(conn, "built_at", "?");
            String configs = Db.meta(conn, "configs", "?");

            System.out.println("KB built " + builtAt + " | " + nProj + " projects | configs: " + configs);
            System.out.println("external modules: " + nGa + " (" + nGav + " module-versions), " + multi + " with >1 version in use");
            System.out.println("conflict-resolved modules: " + confl + " | unresolved requests: " + unres + " | failed configs: " + nErr);
            System.out.println("indexed packages: " + nPkg + " | version catalog entries: " + nCat);

            // Top 5 most-used
            List<String> top = new ArrayList<>();
            try (var ps = conn.prepareStatement(
                    "SELECT c.ga, count(DISTINCT p.project) n FROM nodes n JOIN comps c ON c.id=n.comp " +
                    "JOIN pcs p ON p.id=n.pc WHERE c.kind='m' GROUP BY c.ga ORDER BY n DESC LIMIT 5");
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) top.add(rs.getString(1) + " (" + rs.getLong(2) + ")");
            }
            if (!top.isEmpty()) System.out.println("most used: " + String.join(", ", top));
        } catch (Exception e) {
            Db.die("summary: " + e.getMessage(), 1);
        }
    }
}
