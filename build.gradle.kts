plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}

group = "depkb"
version = "1.0.0"

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(17)
    }
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("info.picocli:picocli:4.7.6")
    implementation("org.xerial:sqlite-jdbc:3.46.1.0")
    implementation("org.ow2.asm:asm:9.7")
    implementation("org.ow2.asm:asm-util:9.7")
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.2")
    implementation("org.slf4j:slf4j-nop:2.0.9")
}


tasks.shadowJar {
    archiveBaseName = "depkb"
    archiveClassifier = ""
    archiveVersion = ""
    manifest {
        attributes["Main-Class"] = "depkb.Main"
    }
}

tasks.build {
    dependsOn(tasks.shadowJar)
}
