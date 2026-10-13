package depkb;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;

public final class Schema {

    public static final int SCHEMA_VERSION = 9;

    private static final String DDL =
        "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);" +
        "CREATE TABLE IF NOT EXISTS projects(path TEXT PRIMARY KEY, name TEXT, dir TEXT, kind TEXT, javalib INTEGER, java TEXT);" +
        "CREATE TABLE IF NOT EXISTS jvmscan(src TEXT PRIMARY KEY, stamp TEXT);" +
        "CREATE TABLE IF NOT EXISTS jvmrefs(src TEXT, owner TEXT, name TEXT, desc TEXT, kind TEXT);" +
        "CREATE TABLE IF NOT EXISTS sourcesets(project TEXT, name TEXT, dir TEXT, cp TEXT, kind TEXT);" +
        "CREATE TABLE IF NOT EXISTS declared(project TEXT, bucket TEXT, id TEXT, version TEXT, platform INTEGER);" +
        "CREATE TABLE IF NOT EXISTS evidence(project TEXT, sset TEXT, kind TEXT, note TEXT);" +
        "CREATE TABLE IF NOT EXISTS mrefs(file INTEGER, owner TEXT, name TEXT, desc TEXT);" +
        "CREATE TABLE IF NOT EXISTS services(ga TEXT, version TEXT, iface TEXT);" +
        "CREATE TABLE IF NOT EXISTS jarscan(ga TEXT, version TEXT, PRIMARY KEY(ga, version));" +
        "CREATE TABLE IF NOT EXISTS jarrefs(ga TEXT, version TEXT, owner TEXT, name TEXT, desc TEXT, kind TEXT);" +
        "CREATE TABLE IF NOT EXISTS buildfiles(path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER, sha TEXT, owner TEXT);" +
        "CREATE TABLE IF NOT EXISTS srcfiles(id INTEGER PRIMARY KEY, project TEXT, sset TEXT, path TEXT UNIQUE, " +
        "                                    mtime INTEGER, size INTEGER, package TEXT, kind TEXT);" +
        "CREATE TABLE IF NOT EXISTS imports(file INTEGER, line INTEGER, fqn TEXT, static INTEGER, wildcard INTEGER, via TEXT);" +
        "CREATE TABLE IF NOT EXISTS links(project TEXT, sset TEXT, fqn TEXT, static INTEGER, wildcard INTEGER, " +
        "                                 kind TEXT, ga TEXT, version TEXT, owner TEXT, " +
        "                                 PRIMARY KEY(project, sset, fqn, static, wildcard)) WITHOUT ROWID;" +
        "CREATE TABLE IF NOT EXISTS pcs(id INTEGER PRIMARY KEY, project TEXT, config TEXT, root INTEGER, error TEXT);" +
        "CREATE TABLE IF NOT EXISTS comps(id INTEGER PRIMARY KEY, cid TEXT UNIQUE, ga TEXT, version TEXT, kind TEXT);" +
        "CREATE TABLE IF NOT EXISTS nodes(pc INTEGER, comp INTEGER, direct INTEGER, reqs TEXT, reasons TEXT, " +
        "                                 PRIMARY KEY(pc, comp)) WITHOUT ROWID;" +
        "CREATE TABLE IF NOT EXISTS edges(pc INTEGER, f INTEGER, t INTEGER, req TEXT, cons INTEGER);" +
        "CREATE TABLE IF NOT EXISTS unresolved(pc INTEGER, f INTEGER, req TEXT, err TEXT);" +
        "CREATE TABLE IF NOT EXISTS artifacts(ga TEXT, version TEXT, file TEXT, PRIMARY KEY(ga, version));" +
        "CREATE TABLE IF NOT EXISTS pkginfo(ga TEXT, version TEXT, description TEXT, url TEXT, licenses TEXT, " +
        "                                   jpms TEXT, classes INTEGER, PRIMARY KEY(ga, version));" +
        "CREATE TABLE IF NOT EXISTS packages(pkg TEXT, ga TEXT, version TEXT, classes INTEGER);" +
        "CREATE TABLE IF NOT EXISTS classes(simple TEXT, pkg TEXT, ga TEXT, version TEXT);" +
        "CREATE TABLE IF NOT EXISTS catalog(accessor TEXT, kind TEXT, ga TEXT, version TEXT, ref TEXT, file TEXT);" +
        "CREATE TABLE IF NOT EXISTS latest(ga TEXT PRIMARY KEY, latest TEXT, fetched INTEGER);" +
        "CREATE TABLE IF NOT EXISTS vulns(ga TEXT, version TEXT, ids TEXT, fetched INTEGER, PRIMARY KEY(ga, version));" +
        "CREATE TABLE IF NOT EXISTS vuln_details(id TEXT PRIMARY KEY, summary TEXT, severity TEXT, fixed TEXT);" +
        "CREATE INDEX IF NOT EXISTS i_comps_ga ON comps(ga);" +
        "CREATE INDEX IF NOT EXISTS i_nodes_comp ON nodes(comp);" +
        "CREATE INDEX IF NOT EXISTS i_edges ON edges(pc, t);" +
        "CREATE INDEX IF NOT EXISTS i_pcs ON pcs(project, config);" +
        "CREATE INDEX IF NOT EXISTS i_pkg ON packages(pkg);" +
        "CREATE INDEX IF NOT EXISTS i_pkg_ga ON packages(ga, version);" +
        "CREATE INDEX IF NOT EXISTS i_cls ON classes(simple, pkg);" +
        "CREATE INDEX IF NOT EXISTS i_cls_ga ON classes(ga, version);" +
        "CREATE INDEX IF NOT EXISTS i_imp_file ON imports(file);" +
        "CREATE INDEX IF NOT EXISTS i_imp_fqn ON imports(fqn);" +
        "CREATE INDEX IF NOT EXISTS i_src_proj ON srcfiles(project, sset);" +
        "CREATE INDEX IF NOT EXISTS i_links_ga ON links(ga);" +
        "CREATE INDEX IF NOT EXISTS i_links_owner ON links(owner);" +
        "CREATE INDEX IF NOT EXISTS i_mrefs_file ON mrefs(file);" +
        "CREATE INDEX IF NOT EXISTS i_mrefs_owner ON mrefs(owner);" +
        "CREATE INDEX IF NOT EXISTS i_decl ON declared(project);" +
        "CREATE INDEX IF NOT EXISTS i_jarrefs ON jarrefs(ga, version, kind);" +
        "CREATE INDEX IF NOT EXISTS i_jvmrefs ON jvmrefs(src);";

    private Schema() {}

    public static void apply(Connection conn) throws SQLException {
        String stored = getVersion(conn);
        if (stored != null && !stored.equals(String.valueOf(SCHEMA_VERSION))) {
            dropAll(conn);
        }
        try (Statement st = conn.createStatement()) {
            for (String sql : DDL.split(";")) {
                sql = sql.strip();
                if (!sql.isEmpty()) {
                    st.execute(sql);
                }
            }
        }
        setVersion(conn);
    }

    private static String getVersion(Connection conn) {
        try (var st = conn.createStatement();
             var rs = st.executeQuery("SELECT v FROM meta WHERE k='schema_version'")) {
            return rs.next() ? rs.getString(1) : null;
        } catch (SQLException e) {
            return null; // meta table doesn't exist yet
        }
    }

    private static void setVersion(Connection conn) throws SQLException {
        try (var ps = conn.prepareStatement(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)")) {
            ps.setString(1, String.valueOf(SCHEMA_VERSION));
            ps.executeUpdate();
        }
    }

    private static void dropAll(Connection conn) throws SQLException {
        try (var st = conn.createStatement();
             var rs = st.executeQuery(
                     "SELECT name FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'")) {
            var names = new java.util.ArrayList<String>();
            while (rs.next()) names.add(rs.getString(1));
            for (String name : names) {
                st.execute("DROP TABLE IF EXISTS \"" + name + "\"");
                st.execute("DROP INDEX IF EXISTS \"" + name + "\"");
            }
        }
    }
}
