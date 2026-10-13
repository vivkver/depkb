package depkb.cmd;

import depkb.Main;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;
import picocli.CommandLine.Parameters;
import picocli.CommandLine.ParentCommand;

@Command(name = "jvm", description = "Impact of a Java upgrade: removed/encapsulated APIs, JVM flags.")
public class JvmCmd implements Runnable {

    @ParentCommand Main parent;

    @Parameters(index = "0", description = "Target Java version (e.g. 21)")
    public int version;

    @Option(names = "--from", description = "Current Java version (default: detected)")
    public Integer source;

    @Option(names = "--jdk", description = "Path to the target JDK")
    public String jdk;

    @Option(names = "--why", description = "Explain each finding and how to fix it")
    public boolean why;

    @Option(names = "--show", defaultValue = "4", description = "Locations listed per finding")
    public int show;

    @Option(names = "--no-flags", description = "Skip scanning/testing JVM options")
    public boolean noFlags;

    @Option(names = "--deprecations", description = "List each deprecated-for-removal API")
    public boolean deprecations;

    @Option(names = "--limit", defaultValue = "40", description = "Max output lines (0 = all)")
    public int limit;

    @Override
    public void run() {
        throw new UnsupportedOperationException("jvm: not yet implemented");
    }
}
