import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// App-wide [ScaffoldMessengerState] key, wired into [MaterialApp.router] so
/// that non-widget callers (interceptors, controllers reacting to an expired
/// session/grant) can surface a snackbar without a [BuildContext].
final scaffoldMessengerKeyProvider =
    Provider<GlobalKey<ScaffoldMessengerState>>(
  (ref) => GlobalKey<ScaffoldMessengerState>(),
);

/// True when the most recent API GET was served from the stale on-disk cache
/// because the network failed — i.e. the app is showing last-saved data.
/// Flipped back to false on the next fresh network response. Screens watch this
/// to show a subtle "offline" banner.
class OfflineController extends StateNotifier<bool> {
  OfflineController() : super(false);

  void set(bool fromCache) {
    if (state != fromCache) state = fromCache;
  }
}

final offlineProvider = StateNotifierProvider<OfflineController, bool>(
    (ref) => OfflineController());
