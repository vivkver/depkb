package depkb;

import java.nio.file.Path;
import java.sql.Connection;
import java.util.Set;

public final class Refresh {

    public enum Status { FRESH, REFRESHED, STALE, SKIPPED }

    private Refresh() {}

    /**
     * Bring the KB up to date before a query.
     * @param sources  true for SOURCE_CMDS — also rescan changed source files
     * @param quiet    suppress "refreshing" messages
     */
    public static Status refresh(Connection conn, boolean sources, boolean quiet,
                                 Path projectDir, BuildOptions opts) {
        if ("0".equals(System.getenv("DEPKB_AUTO"))) return Status.SKIPPED;

        try {
            Set<String> changed = BuildFileTracker.changedModules(conn);
            Status status = Status.FRESH;

            if (!changed.isEmpty()) {
                int nAll = countProjects(conn);
                boolean hasGlobal = changed.contains("GLOBAL");
                Set<String> affected = hasGlobal ? null : BuildFileTracker.dependents(conn, changed);
                if (affected != null && affected.size() > Math.max(30, nAll / 2)) {
                    affected = null; // cheaper to export everything
                }
                String why = hasGlobal ? "shared build files"
                        : String.join(", ", changed.stream().sorted().limit(5).toList());
                System.err.println("depkb: build files changed (" + why + "); refreshing");

                Path export = GradleRunner.run(projectDir, opts, affected);
                if (export == null) {
                    System.err.println("depkb: refresh failed; answers below may be STALE");
                    return Status.STALE;
                }
                Ingest.ingest(conn, export, projectDir, opts, affected);
                status = Status.REFRESHED;
            }

            if (sources) {
                // Source scanning is a no-op stub until Phase 4
                // int n = ScanSources.scan(conn);
                // if (n > 0 && status == Status.FRESH) status = Status.REFRESHED;
            }

            return status;
        } catch (Exception e) {
            System.err.println("depkb: refresh error: " + e.getMessage());
            return Status.STALE;
        }
    }

    private static int countProjects(Connection conn) {
        try (var st = conn.createStatement();
             var rs = st.executeQuery("SELECT count(*) FROM projects")) {
            return rs.next() ? rs.getInt(1) : 0;
        } catch (Exception e) {
            return 0;
        }
    }
}
