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
import '../features/billing/payment_methods_screen.dart';
import '../features/billing/payment_webview_screen.dart';
import '../features/home/dashboard_screen.dart';
import '../features/home/home_shell.dart';
import '../features/home/notifications_screen.dart';
import '../features/home/splash_screen.dart';
import '../features/reseller/reseller_account_screen.dart';
import '../features/reseller/reseller_home_screen.dart';
import '../features/service/add_ons_screen.dart';
import '../features/service/change_plan_screen.dart';
import '../features/service/data_bundle_screen.dart';
import '../features/service/service_detail_screen.dart';
import '../features/service/service_route.dart';
import '../features/settings/settings_screen.dart';
import '../features/support/create_ticket_screen.dart';
import '../features/support/ticket_detail_screen.dart';
import '../features/support/tickets_screen.dart';
import '../features/service/service_tab_screen.dart';
import '../models/subscription.dart';
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
        return (auth.me?.isReseller ?? false) ? '/reseller' : '/dashboard';
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
      // Modal money tasks — full-screen on the root navigator (above the
      // bottom-nav shell), entered with context.push so back returns to the
      // originating screen. Everything else (drill-downs) stays inside the
      // shell and keeps the bottom bar.
      GoRoute(
        path: '/topup',
        builder: (_, __) => const TopUpScreen(),
      ),
      GoRoute(
        path: '/pay',
        builder: (_, state) =>
            PaymentWebViewScreen(args: state.extra as CheckoutArgs),
      ),
      // Reseller portal — a standalone landing (resellers manage many customer
      // accounts), outside the customer bottom-nav shell.
      GoRoute(
        path: '/reseller',
        builder: (_, __) => const ResellerHomeScreen(),
        routes: [
          GoRoute(
            path: 'accounts/:id',
            builder: (_, state) => ResellerAccountScreen(
              accountId: state.pathParameters['id']!,
              title: state.extra as String?,
            ),
          ),
        ],
      ),
      // Authenticated shell with bottom navigation. An indexed-stack shell
      // keeps every tab's navigation stack and widget state alive across tab
      // switches (scroll positions, the Billing sub-tabs, filters).
      StatefulShellRoute.indexedStack(
        builder: (_, __, navigationShell) =>
            HomeShell(navigationShell: navigationShell),
        branches: [
          StatefulShellBranch(routes: [
            GoRoute(
              path: '/dashboard',
              builder: (_, __) => const DashboardScreen(),
              routes: [
                GoRoute(
                  path: 'notifications',
                  builder: (_, __) => const NotificationsScreen(),
                ),
              ],
            ),
            // Service drill-down + its sub-screens, in the Home branch (it is
            // entered from the dashboard). The originating screen passes the
            // Subscription via `extra`; deep links resolve the id from the
            // subscriptions cache (see ServiceRoute).
            GoRoute(
              path: '/service/:id',
              builder: (_, state) => ServiceRoute(
                id: state.pathParameters['id']!,
                initial: state.extra as Subscription?,
                builder: (s) => ServiceDetailScreen(service: s),
              ),
              routes: [
                GoRoute(
                  path: 'change-plan',
                  builder: (_, state) => ServiceRoute(
                    id: state.pathParameters['id']!,
                    initial: state.extra as Subscription?,
                    builder: (s) => ChangePlanScreen(service: s),
                  ),
                ),
                GoRoute(
                  path: 'addons',
                  builder: (_, state) => ServiceRoute(
                    id: state.pathParameters['id']!,
                    initial: state.extra as Subscription?,
                    builder: (s) => AddOnsScreen(service: s),
                  ),
                ),
                GoRoute(
                  path: 'buy-data',
                  builder: (_, state) => ServiceRoute(
                    id: state.pathParameters['id']!,
                    initial: state.extra as Subscription?,
                    builder: (s) => DataBundleScreen(service: s),
                  ),
                ),
              ],
            ),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(
              path: '/billing',
              builder: (_, __) => const InvoicesScreen(),
              routes: [
                GoRoute(
                  path: 'invoices/:id',
                  builder: (_, state) => InvoiceDetailScreen(
                      invoiceId: state.pathParameters['id']!),
                ),
              ],
            ),
          ]),
          StatefulShellBranch(routes: [
            // Path kept as /usage so old deep links and notifications keep working;
            // the tab itself is now the Service tab (plan + data + add-ons + usage).
            GoRoute(
                path: '/usage', builder: (_, __) => const ServiceTabScreen()),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(
              path: '/support',
              builder: (_, __) => const TicketsScreen(),
              routes: [
                GoRoute(
                  path: 'new',
                  builder: (_, __) => const CreateTicketScreen(),
                ),
                GoRoute(
                  path: ':id',
                  builder: (_, state) =>
                      TicketDetailScreen(ticketId: state.pathParameters['id']!),
                ),
              ],
            ),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(
              path: '/profile',
              builder: (_, __) => const ProfileScreen(),
              routes: [
                GoRoute(
                  path: 'sessions',
                  builder: (_, __) => const SessionsScreen(),
                ),
                GoRoute(
                  path: 'payment-methods',
                  builder: (_, __) => const PaymentMethodsScreen(),
                ),
                GoRoute(
                  path: 'settings',
                  builder: (_, __) => const SettingsScreen(),
                ),
              ],
            ),
          ]),
        ],
      ),
    ],
  );
});

/// Root navigator key. Drill-down routes live inside the shell (bottom bar
/// stays); only modal money tasks (/topup, /pay) sit above it as top-level
/// routes.
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
