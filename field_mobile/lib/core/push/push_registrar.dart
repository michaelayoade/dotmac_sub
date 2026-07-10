import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/router.dart';
import '../../features/auth/auth_repository.dart' show appVersion;
import '../../features/auth/auth_state.dart';
import '../../features/jobs/jobs_providers.dart';
import 'push_source.dart';

final pushSourceProvider = Provider<PushSource>(
  (ref) => const NoopPushSource(),
);
final pushScaffoldMessengerKey = GlobalKey<ScaffoldMessengerState>();

String get _platformName {
  if (kIsWeb) return 'android'; // web builds register as android for now
  return Platform.isIOS ? 'ios' : 'android';
}

/// Registers the device token after login and on token rotation, and turns
/// notification taps into deep-link navigations.
class PushRegistrar {
  PushRegistrar(this.ref, {required this.onDeepLink, this.onForegroundMessage});

  final Ref ref;
  final void Function(String route) onDeepLink;
  final void Function(PushMessage message, String? route)? onForegroundMessage;

  StreamSubscription<String>? _tokenSub;
  StreamSubscription<PushMessage>? _messageSub;

  void start() {
    unawaited(registerToken());
    ref.listen<AuthState>(authControllerProvider, (previous, next) {
      if (next is Authenticated && previous is! Authenticated) {
        unawaited(registerToken());
      }
    });
    final source = ref.read(pushSourceProvider);
    _tokenSub = source.tokenRefresh.listen((_) => unawaited(registerToken()));
    _messageSub = source.messages.listen((message) {
      final route = routeForMessage(message.data);
      if (!message.fromTap) {
        onForegroundMessage?.call(message, route);
        return;
      }
      if (route != null) onDeepLink(route);
    });
  }

  Future<void> dispose() async {
    await _tokenSub?.cancel();
    await _messageSub?.cancel();
  }

  Future<bool> registerToken() async {
    if (ref.read(authControllerProvider) is! Authenticated) return false;
    final token = await ref.read(pushSourceProvider).token;
    if (token == null) return false;
    try {
      await ref
          .read(apiClientProvider)
          .dio
          .post(
            '/api/v1/field/devices',
            data: {
              'platform': _platformName,
              'fcm_token': token,
              'app_version': appVersion,
            },
          );
      return true;
    } catch (_) {
      // Registration is retried on next login/token rotation; push being
      // down must never break the app.
      return false;
    }
  }
}

final pushRegistrarProvider = Provider<PushRegistrar>(
  (ref) => PushRegistrar(
    ref,
    onDeepLink: (route) => ref.read(routerProvider).go(route),
    onForegroundMessage: (message, route) {
      _refreshRealtimeState(ref, message);
      _showForegroundPopup(message, route);
    },
  ),
);

void _refreshRealtimeState(Ref ref, PushMessage message) {
  final workOrderId = message.data['work_order_id'];
  if (workOrderId == null || workOrderId.trim().isEmpty) return;
  if (message.data['type'] == 'work_order_comment' ||
      message.data['type'] == 'work_order_assigned') {
    ref
      ..invalidate(jobDetailProvider(workOrderId))
      ..invalidate(todayJobsProvider)
      ..invalidate(allAssignedJobsProvider);
  }
}

void _showForegroundPopup(PushMessage message, String? route) {
  final messenger = pushScaffoldMessengerKey.currentState;
  if (messenger == null) return;
  final title = (message.title ?? message.data['title'] ?? '').trim();
  final body =
      (message.body ?? message.data['body'] ?? message.data['preview'] ?? '')
          .trim();
  final text = [
    if (title.isNotEmpty) title,
    if (body.isNotEmpty) body,
  ].join('\n');
  if (text.isEmpty) return;
  messenger
    ..hideCurrentSnackBar()
    ..showSnackBar(
      SnackBar(
        content: Text(text),
        action: route == null
            ? null
            : SnackBarAction(
                label: 'Open',
                onPressed: () => _openForegroundRoute(route),
              ),
      ),
    );
}

void _openForegroundRoute(String route) {
  final context = pushScaffoldMessengerKey.currentContext;
  if (context == null) return;
  ProviderScope.containerOf(
    context,
    listen: false,
  ).read(routerProvider).go(route);
}
