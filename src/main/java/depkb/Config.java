package depkb;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

public final class Config {

    private static final String DEFAULT_CONFIGS =
        "compileClasspath,runtimeClasspath,testCompileClasspath,testRuntimeClasspath,annotationProcessor";
    private static final String DEFAULT_REPO = "https://repo1.maven.org/maven2/";

    public final String configs;
    public final String output;   // "compact" | "full" | "json"
    public final int limit;
    public final String repos;
    public final String repoAuth;
    public final boolean autoRefresh;
    public final boolean indexClasses;

    private Config(String configs, String output, int limit,
                   String repos, String repoAuth,
                   boolean autoRefresh, boolean indexClasses) {
        this.configs = configs;
        this.output = output;
        this.limit = limit;
        this.repos = repos;
        this.repoAuth = repoAuth;
        this.autoRefresh = autoRefresh;
        this.indexClasses = indexClasses;
    }

    public static Config load(Path projectDir) {
        Properties p = new Properties();
        Path configFile = projectDir.resolve(".depkb").resolve("config.properties");
        if (Files.isRegularFile(configFile)) {
            try (InputStream in = Files.newInputStream(configFile)) {
                p.load(in);
            } catch (IOException ignored) {
            }
        }

        String configs   = env("DEPKB_CONFIGS",  p.getProperty("configs",    DEFAULT_CONFIGS));
        String output    = env("DEPKB_OUTPUT",   p.getProperty("output",     "compact"));
        int    limit     = Integer.parseInt(env("DEPKB_LIMIT", p.getProperty("limit", "40")));
        String repos     = env("DEPKB_REPO",     p.getProperty("repo",       DEFAULT_REPO));
        String repoAuth  = env("DEPKB_REPO_AUTH",p.getProperty("repo_auth",  null));
        boolean auto     = !"0".equals(env("DEPKB_AUTO", p.getProperty("auto_refresh", "1")));
        boolean classes  = !"0".equals(env(null,          p.getProperty("index_classes", "1")));

        return new Config(configs, output, limit, repos, repoAuth, auto, classes);
    }

    private static String env(String envVar, String fallback) {
        if (envVar != null) {
            String v = System.getenv(envVar);
            if (v != null && !v.isBlank()) return v;
        }
        return fallback;
    }
}
