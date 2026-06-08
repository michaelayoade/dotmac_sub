import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

/// Authenticated shell with a bottom navigation bar. The visible tab is derived
/// from the current route so deep links keep the bar in sync.
class HomeShell extends StatelessWidget {
  const HomeShell({super.key, required this.child});

  final Widget child;

  static const _tabs = [
    (
      path: '/dashboard',
      icon: Icons.home_outlined,
      sel: Icons.home,
      label: 'Home'
    ),
    (
      path: '/billing',
      icon: Icons.receipt_long_outlined,
      sel: Icons.receipt_long,
      label: 'Billing'
    ),
    (
      path: '/usage',
      icon: Icons.data_usage_outlined,
      sel: Icons.data_usage,
      label: 'Usage'
    ),
    (
      path: '/support',
      icon: Icons.support_agent_outlined,
      sel: Icons.support_agent,
      label: 'Support'
    ),
    (
      path: '/profile',
      icon: Icons.person_outline,
      sel: Icons.person,
      label: 'Profile'
    ),
  ];

  int _indexForLocation(String location) {
    final i = _tabs.indexWhere((t) => location.startsWith(t.path));
    return i < 0 ? 0 : i;
  }

  @override
  Widget build(BuildContext context) {
    final location = GoRouterState.of(context).matchedLocation;
    final index = _indexForLocation(location);

    return Scaffold(
      body: child,
      bottomNavigationBar: NavigationBar(
        selectedIndex: index,
        onDestinationSelected: (i) => context.go(_tabs[i].path),
        destinations: [
          for (final t in _tabs)
            NavigationDestination(
              icon: Icon(t.icon),
              selectedIcon: Icon(t.sel),
              label: t.label,
            ),
        ],
      ),
    );
  }
}
