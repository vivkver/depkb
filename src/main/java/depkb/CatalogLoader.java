package depkb;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Loads Gradle version catalog (*.versions.toml) files into the catalog table.
 * Handles [versions], [libraries], [plugins], [bundles] sections.
 */
public final class CatalogLoader {

    private CatalogLoader() {}

    public static void load(Connection conn, Path projectDir) throws SQLException {
        Path gradle = projectDir.resolve("gradle");
        if (!Files.isDirectory(gradle)) return;

        try (var stream = Files.list(gradle)) {
            List<Path> tomls = stream.filter(p -> p.getFileName().toString().endsWith(".versions.toml"))
                                     .sorted().toList();
            for (Path toml : tomls) {
                try {
                    Map<String, Map<String, Object>> data = parseToml(toml);
                    insertCatalog(conn, toml.getFileName().toString(), data);
                } catch (Exception e) {
                    System.err.println("depkb: warning: could not parse " + toml + ": " + e.getMessage());
                }
            }
        } catch (IOException e) {
            // gradle/ directory not listable
        }
    }

    private static void insertCatalog(Connection conn, String fileName,
                                       Map<String, Map<String, Object>> data) throws SQLException {
        String name = fileName.split("\\.")[0];
        Map<String, Object> versions = asStringMap(data.get("versions"));

        try (PreparedStatement ins = conn.prepareStatement(
                "INSERT INTO catalog VALUES(?,?,?,?,?,?)")) {

            // Libraries
            for (Map.Entry<String, Object> entry : entriesOf(data.get("libraries"))) {  // libraries section
                String alias = entry.getKey();
                Object spec = entry.getValue();
                String ga = null, v = null, ref = null;

                if (spec instanceof String s) {
                    String[] p = s.split(":");
                    if (p.length >= 2) { ga = p[0] + ":" + p[1]; v = p.length > 2 ? p[2] : null; }
                } else if (spec instanceof Map<?,?> m) {
                    if (m.containsKey("module")) ga = String.valueOf(m.get("module"));
                    else if (m.containsKey("group") && m.containsKey("name"))
                        ga = m.get("group") + ":" + m.get("name");
                    var ver = verSpec(m.get("version"), versions);
                    v = ver[0]; ref = ver[1];
                }
                if (ga == null) continue;
                ins.setString(1, acc(name, "", alias));
                ins.setString(2, "library");
                ins.setString(3, ga);
                ins.setString(4, v);
                ins.setString(5, ref);
                ins.setString(6, fileName);
                ins.addBatch();
            }

            // Plugins
            for (Map.Entry<String, Object> entry : entriesOf(data.get("plugins"))) {
                String alias = entry.getKey();
                Object spec = entry.getValue();
                String pid = null, v = null, ref = null;

                if (spec instanceof String s) {
                    String[] p = s.split(":", 2);
                    pid = p[0]; v = p.length > 1 ? p[1] : null;
                } else if (spec instanceof Map<?,?> m) {
                    pid = String.valueOf(m.get("id"));
                    var ver = verSpec(m.get("version"), versions);
                    v = ver[0]; ref = ver[1];
                }
                if (pid == null) continue;
                ins.setString(1, acc(name, "plugins.", alias));
                ins.setString(2, "plugin");
                ins.setString(3, pid);
                ins.setString(4, v);
                ins.setString(5, ref);
                ins.setString(6, fileName);
                ins.addBatch();
            }

            // Bundles
            for (Map.Entry<String, Object> entry : entriesOf(data.get("bundles"))) {
                String alias = entry.getKey();
                Object spec = entry.getValue();
                String libs = spec instanceof List<?> list
                        ? String.join(",", list.stream().map(Object::toString).toList())
                        : spec.toString();
                ins.setString(1, acc(name, "bundles.", alias));
                ins.setString(2, "bundle");
                ins.setString(3, libs);
                ins.setNull(4, java.sql.Types.VARCHAR);
                ins.setNull(5, java.sql.Types.VARCHAR);
                ins.setString(6, fileName);
                ins.addBatch();
            }

            ins.executeBatch();
        }
        conn.commit();
    }

    private static String acc(String name, String prefix, String alias) {
        return name + "." + prefix + alias.replaceAll("[-_.]", ".");
    }

    private static String[] verSpec(Object v, Map<String, Object> versions) {
        if (v == null) return new String[]{null, null};
        if (v instanceof String s) return new String[]{s, null};
        if (v instanceof Map<?,?> m) {
            if (m.containsKey("ref")) {
                String ref = String.valueOf(m.get("ref"));
                Object rv = versions.get(ref);
                return new String[]{rv != null ? String.valueOf(rv) : null, ref};
            }
            for (String k : List.of("strictly", "require", "prefer")) {
                if (m.containsKey(k)) {
                    String val = String.valueOf(m.get(k));
                    return new String[]{"strictly".equals(k) ? val + "!!" : val, null};
                }
            }
        }
        return new String[]{null, null};
    }

    private static Map<String, Object> asStringMap(Map<String, Object> section) {
        if (section == null) return Map.of();
        // Values in the versions section are plain strings from the parser
        return section;
    }

    private static List<Map.Entry<String, Object>> entriesOf(Map<String, Object> section) {
        if (section == null) return List.of();
        return new ArrayList<>(section.entrySet());
    }

    // ---- Minimal TOML parser ----

    /**
     * Parse the subset of TOML used by Gradle version catalogs.
     * Returns a map of section name -> (alias -> spec).
     */
    static Map<String, Map<String, Object>> parseToml(Path path) throws IOException {
        String text = Files.readString(path);
        Map<String, Map<String, Object>> result = new LinkedHashMap<>();
        String currentSection = null;

        for (String rawLine : text.split("\r?\n")) {
            String line = stripComment(rawLine).strip();
            if (line.isEmpty()) continue;

            // Section header
            if (line.startsWith("[") && !line.startsWith("[[")) {
                currentSection = line.substring(1, line.indexOf(']')).strip();
                result.computeIfAbsent(currentSection, k -> new LinkedHashMap<>());
                continue;
            }
            if (currentSection == null) continue;

            // Key = value
            int eq = line.indexOf('=');
            if (eq < 0) continue;
            String key = line.substring(0, eq).strip().replaceAll("^['\"]|['\"]$", "");
            String val = line.substring(eq + 1).strip();
            result.get(currentSection).put(key, parseValue(val));
        }
        return result;
    }

    private static Object parseValue(String val) {
        if (val.startsWith("{")) return parseInlineTable(val);
        if (val.startsWith("[")) return parseInlineArray(val);
        return parseScalar(val);
    }

    private static Map<String, Object> parseInlineTable(String val) {
        // { key = "v", key2 = "v2" }
        Map<String, Object> map = new LinkedHashMap<>();
        String inner = val.substring(1, val.lastIndexOf('}')).strip();
        for (String part : splitTopLevel(inner, ',')) {
            int eq = part.indexOf('=');
            if (eq < 0) continue;
            String k = part.substring(0, eq).strip().replaceAll("^['\"]|['\"]$", "");
            // Handle dotted keys like version.ref
            String v = part.substring(eq + 1).strip();
            if (k.contains(".")) {
                String[] parts = k.split("\\.", 2);
                @SuppressWarnings("unchecked")
                Map<String, Object> sub = (Map<String, Object>) map.computeIfAbsent(parts[0], x -> new LinkedHashMap<>());
                sub.put(parts[1], parseScalar(v));
            } else {
                map.put(k, parseValue(v));
            }
        }
        return map;
    }

    private static List<Object> parseInlineArray(String val) {
        String inner = val.substring(1, val.lastIndexOf(']')).strip();
        List<Object> list = new ArrayList<>();
        for (String part : splitTopLevel(inner, ',')) {
            String s = part.strip();
            if (!s.isEmpty()) list.add(parseScalar(s));
        }
        return list;
    }

    private static String parseScalar(String val) {
        val = val.strip();
        if ((val.startsWith("\"") && val.endsWith("\"")) ||
            (val.startsWith("'") && val.endsWith("'"))) {
            return val.substring(1, val.length() - 1);
        }
        return val;
    }

    private static String stripComment(String line) {
        boolean inStr = false;
        char strChar = 0;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (inStr) {
                if (c == strChar && (i == 0 || line.charAt(i - 1) != '\\')) inStr = false;
            } else if (c == '"' || c == '\'') {
                inStr = true; strChar = c;
            } else if (c == '#') {
                return line.substring(0, i);
            }
        }
        return line;
    }

    private static List<String> splitTopLevel(String s, char sep) {
        List<String> parts = new ArrayList<>();
        int depth = 0;
        boolean inStr = false;
        char strChar = 0;
        int start = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (inStr) {
                if (c == strChar && (i == 0 || s.charAt(i - 1) != '\\')) inStr = false;
            } else if (c == '"' || c == '\'') {
                inStr = true; strChar = c;
            } else if (c == '{' || c == '[') {
                depth++;
            } else if (c == '}' || c == ']') {
                depth--;
            } else if (c == sep && depth == 0) {
                parts.add(s.substring(start, i));
                start = i + 1;
            }
        }
        if (start < s.length()) parts.add(s.substring(start));
        return parts;
    }
}
