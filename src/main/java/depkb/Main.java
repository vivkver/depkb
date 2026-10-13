package depkb;

import depkb.cmd.*;
import picocli.CommandLine;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;

@Command(
    name = "depkb",
    description = "Token-cheap dependency knowledge base for Gradle Java builds.",
    mixinStandardHelpOptions = true,
    subcommands = {
        BuildCmd.class,
        IngestCmd.class,
        SummaryCmd.class,
        StatusCmd.class,
        OutdatedCmd.class,
        VulnsCmd.class,
        UsageCmd.class,
        RuntimeCmd.class,
        ImportsCmd.class,
        UsersCmd.class,
        AuditCmd.class,
        ApiCmd.class,
        CompatCmd.class,
        ImpactCmd.class,
        JvmCmd.class,
        MissingCmd.class,
        ExplainCmd.class,
    }
)
public class Main implements Runnable {

    @Option(names = "--db", description = "SQLite file (default: nearest .depkb/depkb.db, or $DEPKB_DB)")
    public String db;

    @Option(names = "--no-refresh", description = "answer from the KB as-is without re-exporting changed modules")
    public boolean noRefresh;

    @Override
    public void run() {
        CommandLine.usage(this, System.out);
    }

    public static void main(String[] args) {
        CommandLine cmd = new CommandLine(new Main());
        GraphCmds1.create().forEach(c -> cmd.addSubcommand(c));
        GraphCmds2.create().forEach(c -> cmd.addSubcommand(c));
        AgentCmds.create().forEach(c -> cmd.addSubcommand(c));
        System.exit(cmd.execute(args));
    }
}
