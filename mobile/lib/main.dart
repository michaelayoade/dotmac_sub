import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sentry/sentry.dart';

import 'src/app.dart';
import 'src/config/env.dart';

void main() {
  const app = ProviderScope(child: DotMacApp());

  // Crash reporting is opt-in and goes to your self-hosted GlitchTip
  // (Sentry-protocol). With no GLITCHTIP_DSN supplied at build time the app
  // runs exactly as before and nothing is initialised or sent.
  if (Env.glitchtipDsn.isEmpty) {
    runApp(app);
    return;
  }

  runZonedGuarded(
    () async {
      await Sentry.init((options) {
        options.dsn = Env.glitchtipDsn;
        options.environment = Env.glitchtipEnvironment;
        // Identifies app events in the shared GlitchTip project (filter by
        // release:dotmac-mobile@* or environment:mobile-*).
        options.release = 'dotmac-mobile@0.1.0';
        options.sendDefaultPii = false;
      });

      // Forward framework + platform errors into GlitchTip.
      FlutterError.onError = (details) {
        FlutterError.presentError(details);
        Sentry.captureException(details.exception, stackTrace: details.stack);
      };
      PlatformDispatcher.instance.onError = (error, stack) {
        Sentry.captureException(error, stackTrace: stack);
        return true;
      };

      runApp(app);
    },
    (error, stack) => Sentry.captureException(error, stackTrace: stack),
  );
}
