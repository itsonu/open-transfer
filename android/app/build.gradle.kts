import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// Python 3.12 (the default) only exists for 64-bit ABIs in Chaquopy. Building with
// `-PopenTransfer.python=3.11` adds 32-bit ARM (armeabi-v7a) for old phones.
val pythonVersion = (findProperty("openTransfer.python") as String?) ?: "3.12"

// Release signing comes from the environment (CI secrets). Without it, release builds
// are signed with the debug key so the APK can still be installed.
fun env(name: String): String? = System.getenv(name)?.takeIf { it.isNotBlank() }

val releaseKeystore: File? = env("ANDROID_KEYSTORE_FILE")?.let { file(it) }?.takeIf { it.isFile }
val hasReleaseKey = releaseKeystore != null &&
    env("ANDROID_KEYSTORE_PASSWORD") != null &&
    env("ANDROID_KEY_ALIAS") != null

android {
    namespace = "io.github.itsonu.opentransfer"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.itsonu.opentransfer"
        minSdk = 29
        targetSdk = 36
        versionCode = 300
        versionName = "3.0.0"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
            if (pythonVersion in listOf("3.10", "3.11")) {
                abiFilters += "armeabi-v7a"
            }
        }
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = releaseKeystore
                storePassword = env("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = env("ANDROID_KEY_ALIAS")
                keyPassword = env("ANDROID_KEY_PASSWORD") ?: env("ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("release") {
            // Chaquopy and the JavaScript bridge are reflection-heavy; keep everything.
            isMinifyEnabled = false
            isShrinkResources = false
            signingConfig = signingConfigs.getByName(if (hasReleaseKey) "release" else "debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        // Don't let lint-vital block release builds in CI; run `gradle lint` locally.
        checkReleaseBuilds = false
        abortOnError = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

chaquopy {
    defaultConfig {
        version = pythonVersion
        // Chaquopy finds `python3.12` on the PATH; override if needed.
        System.getenv("CHAQUOPY_BUILD_PYTHON")?.takeIf { it.isNotBlank() }?.let { buildPython(it) }
        pip {
            // Keep in sync with `dependencies` in the repository's pyproject.toml.
            install("flask>=3.0,<4")
            install("cheroot>=10.0")
            install("segno>=1.6")
        }
    }
    sourceSets {
        getByName("main") {
            // The very same `open_transfer` package the desktop app runs.
            srcDir(rootProject.file("../src"))
            exclude("**/__pycache__/**")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")

    testImplementation("junit:junit:4.13.2")
}
