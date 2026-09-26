package com.example.dotmac_portal

import android.content.ActivityNotFoundException
import android.content.Intent
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel

// FlutterFragmentActivity (not FlutterActivity) is required by local_auth so the
// biometric prompt can attach to a FragmentActivity host.
class MainActivity : FlutterFragmentActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "io.dotmac.selfcare/payment_app",
        ).setMethodCallHandler { call, result ->
            if (call.method != "launchIntentUri") {
                result.notImplemented()
                return@setMethodCallHandler
            }

            val rawUrl = call.argument<String>("url")
            if (rawUrl == null) {
                result.error("INVALID_URL", "Missing payment app URL", null)
                return@setMethodCallHandler
            }
            result.success(launchPaymentIntent(rawUrl))
        }
    }

    private fun launchPaymentIntent(rawUrl: String): Map<String, Any?> {
        if (!rawUrl.startsWith("intent://")) {
            return mapOf("launched" to false)
        }

        return try {
            val intent = Intent.parseUri(rawUrl, Intent.URI_INTENT_SCHEME)
            val fallbackUrl = intent.getStringExtra("browser_fallback_url")
            val targetPackage = intent.`package`
            val targetScheme = intent.data?.scheme?.lowercase()
            val safeTargetScheme = targetScheme == OPAY_SCHEME ||
                targetScheme == "https"

            // This native bridge exists only because url_launcher cannot parse
            // Android intent:// URIs. Restrict it to OPay's verified package or
            // scheme; never execute an arbitrary intent supplied by web content.
            if (!safeTargetScheme ||
                (targetPackage != OPAY_PACKAGE && targetScheme != OPAY_SCHEME)
            ) {
                return mapOf(
                    "launched" to false,
                    "fallbackUrl" to fallbackUrl,
                )
            }

            intent.removeExtra("browser_fallback_url")
            intent.addCategory(Intent.CATEGORY_BROWSABLE)
            intent.component = null
            intent.selector = null
            intent.action = Intent.ACTION_VIEW
            intent.setPackage(OPAY_PACKAGE)
            startActivity(intent)
            mapOf("launched" to true)
        } catch (_: ActivityNotFoundException) {
            val fallbackUrl = runCatching {
                Intent.parseUri(rawUrl, Intent.URI_INTENT_SCHEME)
                    .getStringExtra("browser_fallback_url")
            }.getOrNull()
            mapOf("launched" to false, "fallbackUrl" to fallbackUrl)
        } catch (_: Exception) {
            mapOf("launched" to false)
        }
    }

    private companion object {
        const val OPAY_PACKAGE = "team.opay.pay"
        const val OPAY_SCHEME = "opay"
    }
}
