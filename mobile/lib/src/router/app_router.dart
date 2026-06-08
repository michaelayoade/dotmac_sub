import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/observability.dart';
import '../features/auth/forgot_password_screen.dart';
import '../features/billing/topup_screen.dart';
import '../features/auth/lock_screen.dart';
import '../features/auth/login_screen.dart';
import '../features/auth/mfa_screen.dart';
import '../features/auth/profile_screen.dart';
import '../features/auth/reset_password_screen.dart';
import '../features/auth/sessions_screen.dart';
import '../features/billing/invoice_detail_screen.dart';
import '../features/billing/invoices_screen.dart';
import '../features/home/dashboard_screen.dart';
import '../features/home/home_shell.dart';
import '../features/home/notifications_screen.dart';
import '../features/home/splash_screen.dart';
import '../features/support/create_ticket_screen.dart';
import '../features/support/ticket_detail_screen.dart';
import '../features/support/tickets_screen.dart';
import '../features/usage/usage_screen.dart';
import '../providers/auth_controller.dart';

/// Bridges a Riverpod provider to a [Listenable] so GoRouter re-runs its
/// redirect whenever auth state changes.
class _AuthRefresh extends ChangeNotifier {
  _AuthRefresh(Ref ref) {
    ref.listen(authControllerProvider, (_, __) => notifyListeners());
  }
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = _AuthRefresh(ref);

  return GoRouter(
    navigatorKey: rootNavigatorKey,
    initialLocation: '/splash',
    refreshListenable: refresh,
    observers: [_BreadcrumbNavObserver()],
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      final loc = state.matchedLocation;

      // While bootstrap is resolving, hold on the splash so no authenticated
      // data screen mounts (and fires API calls) before we know the auth state.
      if (!auth.isKnown) return loc == '/splash' ? null : '/splash';

      const publicPaths = {
        '/login',
        '/mfa',
        '/forgot-password',
        '/reset-password',
      };

      if (!auth.isAuthenticated) {
        return publicPaths.contains(loc) ? null : '/login';
      }
      // Authenticated but held behind the biometric lock: stay on /lock.
      if (auth.locked) return loc == '/lock' ? null : '/lock';
      // Authenticated and unlocked: leave the splash/login/lock behind.
      if (loc == '/splash' || loc == '/login' || loc == '/lock') {
        return '/dashboard';
      }
      return null;
    },
    routes: [
      GoRoute(path: '/splash', builder: (_, __) => const SplashScreen()),
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      GoRoute(path: '/lock', builder: (_, __) => const LockScreen()),
      GoRoute(
        path: '/forgot-password',
        builder: (_, __) => const ForgotPasswordScreen(),
      ),
      GoRoute(
        path: '/reset-password',
        builder: (_, state) =>
            ResetPasswordScreen(token: state.uri.queryParameters['token']),
      ),
      GoRoute(
        path: '/mfa',
        builder: (_, state) => MfaScreen(mfaToken: state.extra as String),
      ),
      // Authenticated shell with bottom navigation.
      ShellRoute(
        builder: (_, __, child) => HomeShell(child: child),
        routes: [
          GoRoute(
            path: '/dashboard',
            builder: (_, __) => const DashboardScreen(),
            routes: [
              GoRoute(
                path: 'notifications',
                parentNavigatorKey: rootNavigatorKey,
                builder: (_, __) => const NotificationsScreen(),
              ),
            ],
          ),
          GoRoute(
            path: '/billing',
            builder: (_, __) => const InvoicesScreen(),
            routes: [
              GoRoute(
                path: 'invoices/:id',
                parentNavigatorKey: rootNavigatorKey,
                builder: (_, state) =>
                    InvoiceDetailScreen(invoiceId: state.pathParameters['id']!),
              ),
            ],
          ),
          GoRoute(path: '/usage', builder: (_, __) => const UsageScreen()),
          GoRoute(
            path: '/topup',
            parentNavigatorKey: rootNavigatorKey,
            builder: (_, __) => const TopUpScreen(),
          ),
          GoRoute(
            path: '/support',
            builder: (_, __) => const TicketsScreen(),
            routes: [
              GoRoute(
                path: 'new',
                parentNavigatorKey: rootNavigatorKey,
                builder: (_, __) => const CreateTicketScreen(),
              ),
              GoRoute(
                path: ':id',
                parentNavigatorKey: rootNavigatorKey,
                builder: (_, state) =>
                    TicketDetailScreen(ticketId: state.pathParameters['id']!),
              ),
            ],
          ),
          GoRoute(
            path: '/profile',
            builder: (_, __) => const ProfileScreen(),
            routes: [
              GoRoute(
                path: 'sessions',
                parentNavigatorKey: rootNavigatorKey,
                builder: (_, __) => const SessionsScreen(),
              ),
            ],
          ),
        ],
      ),
    ],
  );
});

/// Shared root navigator key so detail screens push above the shell's bottom
/// navigation bar.
final rootNavigatorKey = GlobalKey<NavigatorState>();

/// Leaves a breadcrumb on each navigation so crash reports show the screen
/// trail that led to the error.
class _BreadcrumbNavObserver extends NavigatorObserver {
  void _log(String action, Route<dynamic>? route) {
    final name = route?.settings.name;
    if (name != null) Log.breadcrumb('$action $name', category: 'navigation');
  }

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _log('push', route);

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _log('pop', route);

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) =>
      _log('replace', newRoute);
}
