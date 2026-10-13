package depkb;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Shared query helpers used by every command. */
public final class Lookups {

    private static final Map<String, String> ABBR = Map.of(
        "compileClasspath", "c",
        "runtimeClasspath", "r",
        "testCompileClasspath", "tc",
        "testRuntimeClasspath", "tr",
        "annotationProcessor", "ap"
    );

    private Lookups() {}

    /** Resolve a user query to group:artifact names — best matches first. */
    public static List<String> findGas(Connection conn, String q) throws SQLException {
        q = q.strip();
        String[] parts = q.split(":");
        if (parts.length >= 2) {
            String ga = parts[0] + ":" + parts[1];
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT 1 FROM comps WHERE ga=? AND kind='m'")) {
                ps.setString(1, ga);
                try (ResultSet rs = ps.executeQuery()) {
                    if (rs.next()) return List.of(ga);
                }
            }
        }
        List<String> rows = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT DISTINCT ga FROM comps WHERE kind='m' AND (ga=? OR ga LIKE ? OR ga LIKE ?) ORDER BY ga")) {
            ps.setString(1, q);
            ps.setString(2, "%:" + q);
            ps.setString(3, "%" + q + "%");
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) rows.add(rs.getString(1));
            }
        }
        final String fq = q;
        List<String> exact = rows.stream()
            .filter(g -> g.equals(fq) || g.endsWith(":" + fq))
            .toList();
        return exact.isEmpty() ? rows : exact;
    }

    /** Like findGas but dies if ambiguous or not found. */
    public static String oneGa(Connection conn, String q, int limit) throws SQLException {
        List<String> gas = findGas(conn, q);
        if (gas.isEmpty()) Db.die("no module matching '" + q + "' in the resolved graphs", 1);
        List<String> exact = gas.stream()
            .filter(g -> g.equals(q) || g.endsWith(":" + q))
            .toList();
        if (exact.size() == 1) return exact.get(0);
        if (gas.size() == 1) return gas.get(0);
        System.out.println("'" + q + "' is ambiguous; matches:");
        emit(gas.stream().map(g -> "  " + g).toList(), limit);
        System.exit(1);
        return null;
    }

    /** Normalize a project query to its canonical path. */
    public static String normProject(Connection conn, String q) throws SQLException {
        if (q == null) return null;
        for (String cand : List.of(q, ":" + q.replaceFirst("^:", ""))) {
            try (PreparedStatement ps = conn.prepareStatement("SELECT 1 FROM projects WHERE path=?")) {
                ps.setString(1, cand);
                try (ResultSet rs = ps.executeQuery()) {
                    if (rs.next()) return cand;
                }
            }
        }
        List<String> rows = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT path FROM projects WHERE name=? OR path LIKE ?")) {
            ps.setString(1, q);
            ps.setString(2, "%" + q + "%");
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) rows.add(rs.getString(1));
            }
        }
        if (rows.size() == 1) return rows.get(0);
        String cands = rows.isEmpty() ? "" : ("; candidates: " + String.join(", ", rows.subList(0, Math.min(10, rows.size()))));
        Db.die("project '" + q + "' not found" + cands, 1);
        return null;
    }

    /** Return all (id, project, config, root) rows matching optional project/config filters. */
    public static List<long[]> pcRows(Connection conn, String project, String config) throws SQLException {
        StringBuilder sql = new StringBuilder("SELECT id, project, config, root FROM pcs WHERE error IS NULL");
        List<String> args = new ArrayList<>();
        if (project != null) { sql.append(" AND project=?"); args.add(project); }
        if (config  != null) { sql.append(" AND config=?");  args.add(config);  }
        sql.append(" ORDER BY project, config");
        List<long[]> rows = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(sql.toString())) {
            for (int i = 0; i < args.size(); i++) ps.setString(i + 1, args.get(i));
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) rows.add(new long[]{rs.getLong(1), rs.getLong(3)});
            }
        }
        return rows;
    }

    /** Print lines, truncating to limit with a count message. */
    public static void emit(List<String> lines, int limit) {
        if (limit > 0 && lines.size() > limit) {
            int hidden = lines.size() - limit;
            for (int i = 0; i < limit; i++) System.out.println(lines.get(i));
            System.out.println("... " + hidden + " more (use --limit 0 for all)");
        } else {
            lines.forEach(System.out::println);
        }
    }

    /** Configuration name abbreviation. */
    public static String ab(String config) {
        return ABBR.getOrDefault(config, config);
    }

    /** Grammatical plural: "1 module" or "5 modules". */
    public static String plural(int n, String word) {
        if (n == 1) return n + " " + word;
        return n + " " + (word.endsWith("s") || word.endsWith("sh") || word.endsWith("ch")
                ? word + "es" : word + "s");
    }

    /** Short form of a component id: "org.acme:core:1.0.0" -> "core:1.0.0". */
    public static String shortCid(String cid) {
        if (cid.startsWith("project ")) return cid.substring(8);
        String[] p = cid.split(":");
        return p.length >= 3 ? String.join(":", java.util.Arrays.copyOfRange(p, 1, p.length)) : cid;
    }

    /** Single scalar query helper. */
    public static long scalar(Connection conn, String sql, Object... params) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            for (int i = 0; i < params.length; i++) {
                if (params[i] instanceof String s) ps.setString(i + 1, s);
                else if (params[i] instanceof Long l) ps.setLong(i + 1, l);
                else ps.setObject(i + 1, params[i]);
            }
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? rs.getLong(1) : 0;
            }
        }
    }
}
