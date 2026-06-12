import java.util.Properties

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// FCM: only apply the Google Services plugin when the config file is present,
// so a checkout without google-services.json still builds (push disabled).
if (file("google-services.json").exists()) {
    apply(plugin = "com.google.gms.google-services")
}

// Release signing is configured via android/key.properties (gitignored). When
// the file is absent (e.g. local `flutter run --release`, or CI without the
// signing secrets), the release build falls back to the debug key so it still
// produces an installable — but unshippable — APK.
val keystoreProperties = Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
val hasReleaseKeystore = keystorePropertiesFile.exists()
if (hasReleaseKeystore) {
    keystoreProperties.load(keystorePropertiesFile.inputStream())
}

android {
    namespace = "com.example.dotmac_portal"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        // Required by flutter_local_notifications (FCM display) for Java 8+ API
        // support on older Android versions.
        isCoreLibraryDesugaringEnabled = true
    }

    defaultConfig {
        applicationId = "io.dotmac.selfcare"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
        // Payment-gateway return scheme registered in AndroidManifest.xml.
        // Override per white-label build with -PpaymentScheme=<scheme> (must
        // match BRAND_PAYMENT_SCHEME passed to the Dart build).
        manifestPlaceholders["paymentScheme"] =
            (project.findProperty("paymentScheme") as String?) ?: "dotmacpay"
    }

    signingConfigs {
        if (hasReleaseKeystore) {
            create("release") {
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["keyPassword"] as String
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["storePassword"] as String
            }
        }
    }

    buildTypes {
        release {
            // Use the real release keystore when key.properties is present;
            // otherwise fall back to debug so `flutter run --release` still works
            // locally (the resulting APK is NOT shippable to the Play Store).
            signingConfig = if (hasReleaseKeystore) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    // Core library desugaring runtime — paired with
    // isCoreLibraryDesugaringEnabled above (flutter_local_notifications).
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")
}
