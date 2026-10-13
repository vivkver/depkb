package depkb;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

public final class Db {

    private static final String DB_FILE = "depkb.db";
    private static final String DB_DIR = ".depkb";

    private Db() {}

    /** Locate the DB file: explicit path > $DEPKB_DB > walk parents for .depkb/depkb.db > cwd/.depkb/depkb.db */
    public static Path findDb(String explicit) {
        if (explicit != null && !explicit.isBlank()) {
            return Paths.get(explicit).toAbsolutePath().normalize();
        }
        String env = System.getenv("DEPKB_DB");
        if (env != null && !env.isBlank()) {
            return Paths.get(env).toAbsolutePath().normalize();
        }
        Path dir = Paths.get("").toAbsolutePath();
        while (dir != null) {
            Path candidate = dir.resolve(DB_DIR).resolve(DB_FILE);
            if (Files.isRegularFile(candidate)) {
                return candidate;
            }
            dir = dir.getParent();
        }
        return Paths.get("").toAbsolutePath().resolve(DB_DIR).resolve(DB_FILE);
    }

    /**
     * Open (or create) the SQLite DB and apply the schema.
     * @param path    path to the .db file
     * @param mustExist  if true, exits with code 2 when no build has been run yet
     */
    public static Connection connect(Path path, boolean mustExist) {
        try {
            Files.createDirectories(path.getParent());
        } catch (IOException e) {
            die("cannot create directory " + path.getParent() + ": " + e.getMessage(), 1);
        }

        String url = "jdbc:sqlite:" + path.toString().replace('\\', '/');
        try {
            Connection conn = DriverManager.getConnection(url);
            try (Statement st = conn.createStatement()) {
                st.execute("PRAGMA journal_mode=WAL");
                st.execute("PRAGMA synchronous=NORMAL");
            }
            Schema.apply(conn);
            if (mustExist) {
                String builtAt = meta(conn, "built_at", null);
                if (builtAt == null) {
                    die("no knowledge base found at " + path + " — run 'depkb build' first", 2);
                }
            }
            return conn;
        } catch (SQLException e) {
            die("cannot open database " + path + ": " + e.getMessage(), 1);
            return null; // unreachable
        }
    }

    /**
     * Open an existing SQLite file read-only WITHOUT applying or altering the schema.
     * Used for snapshot copies, which must never be migrated or wiped.
     */
    public static Connection openReadOnly(Path path) {
        if (!Files.isRegularFile(path)) {
            die("no such database: " + path, 1);
        }
        String url = "jdbc:sqlite:" + path.toString().replace('\\', '/');
        try {
            org.sqlite.SQLiteConfig cfg = new org.sqlite.SQLiteConfig();
            cfg.setReadOnly(true);
            return DriverManager.getConnection(url, cfg.toProperties());
        } catch (SQLException e) {
            die("cannot open database " + path + ": " + e.getMessage(), 1);
            return null; // unreachable
        }
    }

    /** Read a meta key, returning defaultValue if absent. */
    public static String meta(Connection conn, String key, String defaultValue) {
        try (PreparedStatement ps = conn.prepareStatement("SELECT v FROM meta WHERE k=?")) {
            ps.setString(1, key);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? rs.getString(1) : defaultValue;
            }
        } catch (SQLException e) {
            return defaultValue;
        }
    }

    /** Write a meta key. */
    public static void setMeta(Connection conn, String key, String value) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)")) {
            ps.setString(1, key);
            ps.setString(2, value);
            ps.executeUpdate();
        }
    }

    /** Print an error to stderr and exit with the given code. */
    public static void die(String message, int code) {
        System.err.println("depkb: " + message);
        System.exit(code);
    }
}
