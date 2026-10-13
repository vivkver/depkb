package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

@Command(name = "vulns", description = "Known vulnerabilities from OSV (network, cached 24h).")
public class VulnsCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--refresh", description = "Ignore cached results")
    public boolean refresh;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("vulns: not yet implemented");
    }
}
