package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

@Command(name = "api", description = "Public API of a class from the exact version on the classpath.")
public class ApiCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Class, Outer.Inner, or fully-qualified class name")
    public String name;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = "--grep", description = "Only show members containing this text")
    public String grep;

    @Option(names = "--public-only", description = "Suppress protected members")
    public boolean publicOnly;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("api: not yet implemented");
    }
}
