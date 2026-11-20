plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.itsonu.opentransfer.m0"
    compileSdk = 36
    defaultConfig {
        applicationId = "io.github.itsonu.opentransfer.m0"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "m0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}
