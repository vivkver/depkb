package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.util.List;

@Command(name = "impact", description = "What breaks if a library moves to a different version.")
public class ImpactCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "group:artifact, artifact, or substring")
    public String module;

    @Parameters(index = "1", description = "Target version to evaluate")
    public String version;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = {"-c", "--config"}, description = "e.g. runtimeClasspath")
    public String config;

    @Option(names = "--repo", description = "Repo base URL to fetch the jar (repeatable)")
    public List<String> repos;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("impact: not yet implemented");
    }
}
