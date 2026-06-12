import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'config/env.dart';
import 'core/payment_link_handler.dart';
import 'providers/auth_controller.dart';
import 'providers/theme_controller.dart';
import 'router/app_router.dart';

class DotMacApp extends ConsumerStatefulWidget {
  const DotMacApp({super.key});

  @override
  ConsumerState<DotMacApp> createState() => _DotMacAppState();
}

class _DotMacAppState extends ConsumerState<DotMacApp>
    with WidgetsBindingObserver {
  bool _wasPaused = false;
  final GlobalKey<ScaffoldMessengerState> _messengerKey =
      GlobalKey<ScaffoldMessengerState>();
  late final PaymentLinkHandler _paymentLinks;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _paymentLinks = PaymentLinkHandler(ref, _messengerKey)..start();
  }

  @override
  void dispose() {
    _paymentLinks.dispose();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Re-arm the biometric lock only across a real background→foreground cycle.
    // On iOS the prompt reports `inactive`/`hidden` (not `paused`), so gating on
    // `paused` is enough there. Some Android OEMs host the prompt in a separate
    // activity, which emits a real `paused` — and its auth result can reach
    // Dart before the `resumed` event, by which point the prompt no longer
    // counts as active and lockOnResume would re-lock straight after a
    // successful unlock (prompt loop). Ignoring pauses raised while our own
    // prompt is up breaks that loop.
    final auth = ref.read(authControllerProvider.notifier);
    if (state == AppLifecycleState.paused) {
      if (!auth.promptActive) _wasPaused = true;
    } else if (state == AppLifecycleState.resumed && _wasPaused) {
      _wasPaused = false;
      auth.lockOnResume();
    }
  }

  @override
  Widget build(BuildContext context) {
    final router = ref.watch(routerProvider);

    ThemeData themeFor(Brightness brightness) => ThemeData(
      colorScheme: ColorScheme.fromSeed(
        seedColor: Brand.primaryColor,
        brightness: brightness,
      ),
      useMaterial3: true,
      appBarTheme: const AppBarTheme(centerTitle: false),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
    );

    return MaterialApp.router(
      title: Brand.name,
      scaffoldMessengerKey: _messengerKey,
      debugShowCheckedModeBanner: false,
      theme: themeFor(Brightness.light),
      darkTheme: themeFor(Brightness.dark),
      themeMode: ref.watch(themeModeProvider),
      routerConfig: router,
    );
  }
}
