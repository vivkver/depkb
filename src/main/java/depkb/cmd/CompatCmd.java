package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

@Command(name = "compat", description = "Binary compatibility of overridden versions for their callers.")
public class CompatCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", arity = "0..1", description = "Only check this library (group:artifact or artifact)")
    public String module;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = {"-c", "--config"}, description = "e.g. runtimeClasspath")
    public String config;

    @Option(names = "--verbose", description = "Also list overrides without breaks")
    public boolean verbose;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("compat: not yet implemented");
    }
}
