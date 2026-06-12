import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../providers/impersonation.dart';

/// Authenticated shell with a bottom navigation bar. Each tab is a
/// [StatefulShellBranch] held in an indexed stack, so switching tabs preserves
/// every branch's navigation stack and widget state (scroll positions,
/// sub-tabs, filters). The selected index comes from the shell itself, so deep
/// links keep the bar in sync without path matching.
class HomeShell extends ConsumerWidget {
  const HomeShell({super.key, required this.navigationShell});

  final StatefulNavigationShell navigationShell;

  static const _tabs = [
    (icon: Icons.home_outlined, sel: Icons.home, label: 'Home'),
    (
      icon: Icons.receipt_long_outlined,
      sel: Icons.receipt_long,
      label: 'Billing'
    ),
    (icon: Icons.wifi_outlined, sel: Icons.wifi, label: 'Service'),
    (
      icon: Icons.support_agent_outlined,
      sel: Icons.support_agent,
      label: 'Support'
    ),
    (icon: Icons.person_outline, sel: Icons.person, label: 'Profile'),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final impersonation = ref.watch(impersonationProvider);
    return Scaffold(
      body: Column(
        children: [
          if (impersonation != null)
            Material(
              color: Theme.of(context).colorScheme.tertiaryContainer,
              child: SafeArea(
                bottom: false,
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                  child: Row(
                    children: [
                      Icon(Icons.supervisor_account,
                          size: 18,
                          color: Theme.of(context)
                              .colorScheme
                              .onTertiaryContainer),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Viewing as ${impersonation.customerName} '
                          '(read-only)',
                          style: TextStyle(
                            color: Theme.of(context)
                                .colorScheme
                                .onTertiaryContainer,
                            fontWeight: FontWeight.w600,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      TextButton(
                        onPressed: () {
                          ref.read(impersonationProvider.notifier).stop();
                          context.go('/reseller');
                        },
                        child: const Text('Exit'),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          Expanded(child: navigationShell),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: navigationShell.currentIndex,
        // Re-tapping the active tab pops its branch back to the root —
        // standard bottom-nav behaviour.
        onDestinationSelected: (i) => navigationShell.goBranch(
          i,
          initialLocation: i == navigationShell.currentIndex,
        ),
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
