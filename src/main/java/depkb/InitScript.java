package depkb;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

public final class InitScript {

    private static final String RESOURCE = "/init/depkb.init.gradle";
    private static volatile Path cached;

    private InitScript() {}

    /** Extract the embedded Gradle init script to a temp dir and return its path. */
    public static Path extract() {
        if (cached != null && Files.isRegularFile(cached)) return cached;
        synchronized (InitScript.class) {
            if (cached != null && Files.isRegularFile(cached)) return cached;
            try {
                Path dir = Files.createTempDirectory("depkb-init");
                Path dest = dir.resolve("depkb.init.gradle");
                try (InputStream in = InitScript.class.getResourceAsStream(RESOURCE)) {
                    if (in == null) {
                        Db.die("embedded init script not found in JAR at " + RESOURCE, 1);
                    }
                    Files.copy(in, dest, StandardCopyOption.REPLACE_EXISTING);
                }
                // register shutdown hook to clean up on exit
                Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                    try { Files.deleteIfExists(dest); Files.deleteIfExists(dir); } catch (IOException ignored) {}
                }));
                cached = dest;
            } catch (IOException e) {
                Db.die("cannot extract init script: " + e.getMessage(), 1);
            }
            return cached;
        }
    }
}
