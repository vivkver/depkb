package depkb.cmd;

import depkb.BuildOptions;
import depkb.Db;
import depkb.Ingest;
import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.nio.file.Path;
import java.sql.Connection;

@Command(name = "ingest", description = "Index an existing export directory (e.g. from CI).")
public class IngestCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Path to the export directory")
    public String exportDir;

    @Option(names = {"-d", "--project-dir"}, defaultValue = ".", description = "Gradle project root")
    public String projectDir;

    @Option(names = "--configs", defaultValue = "compileClasspath,runtimeClasspath,testCompileClasspath,testRuntimeClasspath,annotationProcessor",
            description = "Comma-separated config names or 'all'")
    public String configs;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        Path proj = Path.of(projectDir).toAbsolutePath().normalize();
        Path export = Path.of(exportDir).toAbsolutePath().normalize();
        Path dbPath = parent.db != null ? Path.of(parent.db)
                : proj.resolve(".depkb").resolve("depkb.db");
        BuildOptions opts = new BuildOptions(configs, false, false, false, false, null);
        try {
            Connection conn = Db.connect(dbPath, false);
            Ingest.ingest(conn, export, proj, opts, null);
        } catch (Exception e) {
            Db.die("ingest failed: " + e.getMessage(), 1);
        }
    }
}
