package depkb;

/** Immutable options passed to GradleRunner and Ingest. */
public final class BuildOptions {

    public final String configs;
    public final boolean offline;
    public final boolean noParallel;
    public final boolean noJars;
    public final boolean noClasses;
    public final String gradleArgs;

    public BuildOptions(String configs, boolean offline, boolean noParallel,
                        boolean noJars, boolean noClasses, String gradleArgs) {
        this.configs = configs;
        this.offline = offline;
        this.noParallel = noParallel;
        this.noJars = noJars;
        this.noClasses = noClasses;
        this.gradleArgs = gradleArgs;
    }
}
