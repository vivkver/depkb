package depkb.cmd;

import depkb.BuildFileTracker;
import depkb.BuildOptions;
import depkb.Db;
import depkb.GradleRunner;
import depkb.Ingest;
import depkb.Main;
import depkb.Refresh;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;

@Command(name = "build", description = "Export from Gradle and (re)index the knowledge base.")
public class BuildCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = {"-d", "--project-dir"}, defaultValue = ".", description = "Gradle project root")
    public String projectDir;

    @Option(names = "--configs", defaultValue = "compileClasspath,runtimeClasspath,testCompileClasspath,testRuntimeClasspath,annotationProcessor",
            description = "Comma-separated config names or 'all'")
    public String configs;

    @Option(names = "--force", description = "Rebuild even when nothing changed")
    public boolean force;

    @Option(names = "--offline", description = "Pass --offline to Gradle")
    public boolean offline;

    @Option(names = "--no-parallel", description = "Pass --no-parallel to Gradle")
    public boolean noParallel;

    @Option(names = "--no-jars", description = "Skip jar/POM indexing")
    public boolean noJars;

    @Option(names = "--no-classes", description = "Index packages but not class names")
    public boolean noClasses;

    @Option(names = "--gradle-args", description = "Extra Gradle arguments (quoted)")
    public String gradleArgs;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        Path proj = Path.of(projectDir).toAbsolutePath().normalize();
        boolean hasSettings = Files.isRegularFile(proj.resolve("settings.gradle"))
                || Files.isRegularFile(proj.resolve("settings.gradle.kts"));
        boolean hasBuild = Files.isRegularFile(proj.resolve("build.gradle"))
                || Files.isRegularFile(proj.resolve("build.gradle.kts"));
        if (!hasSettings && !hasBuild) {
            Db.die(proj + " does not look like a Gradle root (no settings.gradle[.kts])", 1);
        }

        Path kb = proj.resolve(".depkb");
        try {
            Files.createDirectories(kb);
            Path gitignore = kb.resolve(".gitignore");
            if (!Files.exists(gitignore)) Files.writeString(gitignore, "*\n");
        } catch (Exception e) {
            Db.die("cannot create .depkb directory: " + e.getMessage(), 1);
        }

        Path dbPath = parent.db != null ? Path.of(parent.db) : kb.resolve("depkb.db");
        BuildOptions opts = new BuildOptions(configs, offline, noParallel, noJars, noClasses, gradleArgs);

        try {
            Connection conn = Db.connect(dbPath, false);
            String builtAt = Db.meta(conn, "built_at", null);

            if (builtAt != null && !force) {
                // Check if configs match
                String prevConfigs = Db.meta(conn, "configs", "");
                if (configs.equals(prevConfigs)) {
                    Refresh.Status status = Refresh.refresh(conn, true, false, proj, opts);
                    if (status == Refresh.Status.FRESH) {
                        System.out.println("up to date (built " + builtAt +
                                "; build files and sources unchanged). Use --force to rebuild.");
                        return;
                    }
                    return;
                }
            }

            Path export = GradleRunner.run(proj, opts, null);
            if (export == null) Db.die("export produced no data", 3);

            Ingest.ingest(conn, export, proj, opts, null);
        } catch (Exception e) {
            Db.die("build failed: " + e.getMessage(), 1);
        }
    }
}
