import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/api/token_store.dart' show LoginMode;
import '../features/auth/auth_state.dart';
import '../features/auth/login_screen.dart';
import '../features/auth/mfa_screen.dart';
import '../features/expenses/expenses_screen.dart';
import '../features/jobs/job_detail_screen.dart';
import '../features/location/location_tracking_controller.dart';
import '../features/materials/materials_screen.dart';
import '../features/profile/profile_screen.dart';
import '../features/schedule/schedule_screen.dart';
import '../features/today/map_screen.dart';
import '../features/today/today_screen.dart';
import '../features/vendor/vendor_map_screen.dart';
import '../features/vendor/vendor_screens.dart';

/// App shell: login gate + 4-tab bottom navigation per the visual plan.
GoRouter buildRouter(Ref ref) {
  final listenable = ValueNotifier(0);
  ref.listen(authControllerProvider, (_, _) => listenable.value++);

  return GoRouter(
    initialLocation: '/today',
    refreshListenable: listenable,
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      final atRestore = state.matchedLocation == '/restore';
      final atLogin = state.matchedLocation == '/login';
      final atMfa = state.matchedLocation == '/mfa';
      final atUpgrade = state.matchedLocation == '/upgrade';
      return switch (auth) {
        RestoringSession() => atRestore ? null : '/restore',
        Unauthenticated() => atLogin ? null : '/login',
        AwaitingMfa() => atMfa ? null : '/mfa',
        UpgradeRequired() => atUpgrade ? null : '/upgrade',
        Authenticated() =>
          (atRestore || atLogin || atMfa || atUpgrade) ? '/today' : null,
      };
    },
    routes: [
      GoRoute(path: '/restore', builder: (_, _) => const _RestoreScreen()),
      GoRoute(path: '/login', builder: (_, _) => const LoginScreen()),
      GoRoute(path: '/mfa', builder: (_, _) => const MfaScreen()),
      GoRoute(
        path: '/upgrade',
        builder: (_, _) => const UpgradeRequiredScreen(),
      ),
      GoRoute(
        path: '/jobs/:id',
        builder: (_, state) =>
            JobDetailScreen(jobId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/materials/new',
        builder: (_, state) => NewMaterialRequestScreen(
          initialWorkOrderId: state.uri.queryParameters['workOrderId'],
          initialWorkOrderLabel: state.uri.queryParameters['workOrderLabel'],
        ),
      ),
      GoRoute(
        path: '/materials/:id',
        builder: (_, state) =>
            MaterialRequestDetailScreen(id: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/expenses/new',
        builder: (_, state) => NewExpenseRequestScreen(
          initialWorkOrderId: state.uri.queryParameters['workOrderId'],
          initialWorkOrderLabel: state.uri.queryParameters['workOrderLabel'],
        ),
      ),
      GoRoute(
        path: '/expenses/:id',
        builder: (_, state) =>
            ExpenseRequestDetailScreen(id: state.pathParameters['id']!),
      ),
      StatefulShellRoute.indexedStack(
        builder: (context, state, shell) => _AppShell(shell: shell),
        branches: [
          StatefulShellBranch(
            routes: [
              GoRoute(path: '/today', builder: (_, _) => const _HomeSwitch()),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(path: '/map', builder: (_, _) => const _MapSwitch()),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/schedule',
                builder: (_, _) => const ScheduleScreen(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/materials',
                builder: (_, _) => const MaterialsScreen(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/expenses',
                builder: (_, _) => const ExpensesScreen(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/profile',
                builder: (_, _) => const ProfileScreen(),
              ),
            ],
          ),
        ],
      ),
    ],
  );
}

final routerProvider = Provider<GoRouter>(buildRouter);

class _RestoreScreen extends StatelessWidget {
  const _RestoreScreen();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(body: Center(child: CircularProgressIndicator()));
  }
}

/// Vendor crews get their Projects module where techs see Today.
class _HomeSwitch extends ConsumerWidget {
  const _HomeSwitch();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    if (auth is Authenticated && auth.mode == LoginMode.vendor) {
      return const VendorProjectsScreen();
    }
    return const TodayScreen();
  }
}

/// The Map tab shows vendors their vendor-scoped nearby plant; techs get the
/// full technician map (job pins + editable assets).
class _MapSwitch extends ConsumerWidget {
  const _MapSwitch();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    if (auth is Authenticated && auth.mode == LoginMode.vendor) {
      return const VendorMapScreen();
    }
    return const MapScreen();
  }
}

/// A bottom-nav destination bound to a shell branch index. The visible set
/// differs by login mode, but every entry maps to the same fixed branch so
/// `shell.goBranch` stays correct regardless of what's shown.
class _NavItem {
  const _NavItem(this.branchIndex, this.icon, this.label);
  final int branchIndex;
  final IconData icon;
  final String label;
}

// Branch order (see StatefulShellRoute above):
// 0 Today/Projects · 1 Map · 2 Schedule · 3 Materials · 4 Expenses ·
// 5 Profile
const _staffNav = [
  _NavItem(0, Icons.assignment_outlined, 'Today'),
  _NavItem(1, Icons.map_outlined, 'Map'),
  _NavItem(2, Icons.calendar_today_outlined, 'Schedule'),
  _NavItem(3, Icons.inventory_2_outlined, 'Materials'),
  _NavItem(4, Icons.receipt_long_outlined, 'Expenses'),
  _NavItem(5, Icons.person_outline, 'Profile'),
];

// Vendors get the tabs backed by vendor-aware endpoints: Projects, the
// vendor-scoped Map (nearby plant), and Profile. Schedule / Materials /
// Expenses are require_technician and would 403, so they stay hidden.
const _vendorNav = [
  _NavItem(0, Icons.assignment_outlined, 'Projects'),
  _NavItem(1, Icons.map_outlined, 'Map'),
  _NavItem(5, Icons.person_outline, 'Profile'),
];

class _AppShell extends ConsumerWidget {
  const _AppShell({required this.shell});

  final StatefulNavigationShell shell;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    final isVendor = auth is Authenticated && auth.mode == LoginMode.vendor;
    final items = isVendor ? _vendorNav : _staffNav;
    // Map the active branch to its position in the visible set (0 if the
    // current branch is hidden for this mode).
    final selected = items.indexWhere(
      (i) => i.branchIndex == shell.currentIndex,
    );
    return Scaffold(
      body: LocationTrackingHost(child: shell),
      bottomNavigationBar: NavigationBar(
        selectedIndex: selected < 0 ? 0 : selected,
        onDestinationSelected: (pos) => shell.goBranch(items[pos].branchIndex),
        destinations: [
          for (final item in items)
            NavigationDestination(icon: Icon(item.icon), label: item.label),
        ],
      ),
    );
  }
}
