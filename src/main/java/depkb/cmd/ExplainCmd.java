package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

import java.util.List;

@Command(name = "explain", description = "Diagnose a ClassNotFoundException or NoClassDefFoundError.")
public class ExplainCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0..*", description = "Class name, or the exception line from the log")
    public List<String> names;

    @Option(names = {"-p", "--project"}, description = "Project path (e.g. :app)")
    public String project;

    @Option(names = {"-c", "--config"}, description = "e.g. runtimeClasspath")
    public String config;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("explain: not yet implemented");
    }
}
