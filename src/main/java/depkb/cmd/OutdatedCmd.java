package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

import java.util.List;

@Command(name = "outdated", description = "Newer releases in Maven repos (network, cached 24h).")
public class OutdatedCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--repo", description = "Repo base URL (repeatable)")
    public List<String> repos;

    @Option(names = "--direct", description = "Limit to direct dependencies")
    public boolean direct;

    @Option(names = "--refresh", description = "Ignore cached results")
    public boolean refresh;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("outdated: not yet implemented");
    }
}
