package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.ParentCommand;

@Command(name = "missing", description = "Predict ClassNotFoundException: referenced classes nothing on the classpath provides.")
public class MissingCmd implements Runnable {

    @ParentCommand Main parent;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = {"-c", "--config"}, description = "e.g. runtimeClasspath")
    public String config;

    @Option(names = "--show", defaultValue = "4", description = "Locations listed per missing class")
    public int show;

    @Option(names = "--all", description = "List library-only gaps per package")
    public boolean all;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("missing: not yet implemented");
    }
}
