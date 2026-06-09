import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/subscription.dart';
import '../../models/usage.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../providers/read_notifications.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import '../service/service_detail_screen.dart';

/// Home dashboard: an at-a-glance summary (account status, balance, data,
/// services) plus quick-action shortcuts into the rest of the app.
class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(currentUserProvider);
    final subs = ref.watch(subscriptionsProvider);
    final invoices = ref.watch(invoicesProvider);
    final sessions = ref.watch(accountingSessionsProvider);
    final notifications = ref.watch(notificationsProvider);
    final readIds = ref.watch(readNotificationsProvider);

    final unread = notifications.asData?.value.items
            .where((n) => !readIds.contains(n.id))
            .length ??
        0;

    // --- Summary values (null while loading) ---
    final subList = subs.asData?.value.items;
    final activeServices = subList?.where((s) => s.isActive).length;
    final hasSuspended = subList?.any((s) => !s.isActive) ?? false;

    final invItems = invoices.asData?.value.items;
    final outstanding = invItems
        ?.where((i) => !i.isPaid)
        .fold<double>(0, (sum, i) => sum + i.balanceDue);
    final currency = (invItems != null && invItems.isNotEmpty)
        ? invItems.first.currency
        : 'NGN';

    final sessItems = sessions.asData?.value.items;
    // Defined-window total (today) instead of summing the latest 50 sessions.
    final dataToday =
        ref.watch(usageSummaryProvider('today')).asData?.value.totalBytes;

    // Connection status: an open RADIUS accounting session (no end) means the
    // subscriber is currently online; its start gives the uptime.
    AccountingSession? activeSession;
    if (sessItems != null) {
      for (final s in sessItems) {
        if (s.isActive) {
          activeSession = s;
          break;
        }
      }
    }

    return Scaffold(
      appBar: AppBar(
        title: Text('Hi, ${me?.firstName ?? 'there'}'),
        actions: [
          IconButton(
            tooltip: 'Notifications',
            onPressed: () => context.go('/dashboard/notifications'),
            icon: Badge(
              isLabelVisible: unread > 0,
              label: Text('$unread'),
              child: const Icon(Icons.notifications_outlined),
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(subscriptionsProvider);
          ref.invalidate(invoicesProvider);
          ref.invalidate(accountingSessionsProvider);
          ref.invalidate(usageSummaryProvider('today'));
          await Future.wait([
            ref.read(subscriptionsProvider.future),
            ref.read(invoicesProvider.future),
          ]);
        },
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _ConnectionBanner(
              session: activeSession,
              known: sessions.hasValue,
            ),
            const SizedBox(height: 12),
            _StatusBanner(
              suspended: hasSuspended,
              known: subList != null,
              // A suspended service is resolved by paying — deep-link to billing.
              onTap: hasSuspended ? () => context.go('/billing') : null,
            ),
            const SizedBox(height: 16),

            // --- At-a-glance summary ---
            Row(
              children: [
                Expanded(
                  child: _StatCard(
                    icon: Icons.account_balance_wallet_outlined,
                    // Say "Amount due" in words when owing, so the state isn't
                    // conveyed by the red colour alone (accessibility).
                    label: (outstanding ?? 0) > 0 ? 'Amount due' : 'Balance',
                    value: outstanding == null
                        ? '—'
                        : Fmt.moneyCompact(outstanding, currency),
                    highlight: (outstanding ?? 0) > 0,
                    onTap: () => context.go('/billing'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: _StatCard(
                    icon: Icons.data_usage_outlined,
                    label: 'Data today',
                    value: dataToday == null ? '—' : Fmt.bytes(dataToday),
                    onTap: () => context.go('/usage'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: _StatCard(
                    icon: Icons.router_outlined,
                    label: 'Services',
                    value: activeServices?.toString() ?? '—',
                    onTap: () => context.go('/billing'),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 20),

            // --- Quick actions ---
            Text('Quick actions',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 10),
            const _QuickActions(),
            const SizedBox(height: 20),

            // --- Current service (with a switcher when there are several) ---
            const _SectionHeader('Current service'),
            Builder(
              builder: (context) {
                // Stale-while-revalidate: keep showing the last-known
                // service(s) — with a quiet "couldn't refresh" banner — instead
                // of replacing the card with an error when a refresh fails under
                // load. The on-disk cache means `subs` usually still has a value
                // even on a cold-start network blip.
                if (subs.hasValue) {
                  final services = subs.requireValue.items;
                  if (services.isEmpty) {
                    return const _MessageCard('No active service found.');
                  }
                  final selectedId = ref.watch(selectedServiceIdProvider);
                  final selected = services.firstWhere(
                    (s) => s.id == selectedId,
                    orElse: () => pickCurrentService(services),
                  );
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (subs.hasError && !subs.isLoading)
                        StaleBanner(
                          onRetry: () => ref.invalidate(subscriptionsProvider),
                        ),
                      if (services.length > 1) ...[
                        _ServiceSwitcher(
                          services: services,
                          selectedId: selected.id,
                          onSelect: (id) => ref
                              .read(selectedServiceIdProvider.notifier)
                              .state = id,
                        ),
                        const SizedBox(height: 10),
                      ],
                      _CurrentServiceCard(service: selected),
                    ],
                  );
                }
                return subs.isLoading
                    ? const CardSkeleton()
                    : const _MessageCard(
                        'Couldn’t load your service. Pull down to refresh.');
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// Network connection status — the headline reason customers open the app.
/// Derived from whether an open RADIUS accounting session exists.
class _ConnectionBanner extends StatelessWidget {
  const _ConnectionBanner({required this.session, required this.known});

  /// The active session, or null when offline. Only meaningful when [known].
  final AccountingSession? session;

  /// True once the sessions request has resolved with data.
  final bool known;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final Color bg;
    final Color fg;
    final IconData icon;
    final String text;

    if (!known) {
      bg = scheme.surfaceContainerHighest;
      fg = scheme.onSurfaceVariant;
      icon = Icons.wifi_find_outlined;
      text = 'Checking connection…';
    } else if (session != null) {
      final start = session!.sessionStart;
      bg = scheme.secondaryContainer;
      fg = scheme.onSecondaryContainer;
      icon = Icons.wifi;
      text = start == null
          ? 'Connected'
          : 'Connected · session up ${Fmt.uptime(start)}';
    } else {
      bg = scheme.errorContainer;
      fg = scheme.onErrorContainer;
      icon = Icons.wifi_off_outlined;
      text = 'Offline';
    }

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Icon(icon, color: fg),
          const SizedBox(width: 10),
          Expanded(
            child: Text(text,
                style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
          ),
        ],
      ),
    );
  }
}

class _StatusBanner extends StatelessWidget {
  const _StatusBanner({
    required this.suspended,
    required this.known,
    this.onTap,
  });
  final bool suspended;
  final bool known;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final (bg, fg, icon, text) = !known
        ? (
            scheme.surfaceContainerHighest,
            scheme.onSurface,
            Icons.hourglass_empty,
            'Loading your account…'
          )
        : suspended
            ? (
                scheme.errorContainer,
                scheme.onErrorContainer,
                Icons.warning_amber_rounded,
                'A service is suspended — tap to pay'
              )
            : (
                scheme.primaryContainer,
                scheme.onPrimaryContainer,
                Icons.check_circle_outline,
                'All services active'
              );
    final radius = BorderRadius.circular(14);
    return Material(
      color: bg,
      borderRadius: radius,
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Icon(icon, color: fg),
              const SizedBox(width: 10),
              Expanded(
                child: Text(text,
                    style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
              ),
              if (onTap != null) Icon(Icons.chevron_right, color: fg),
            ],
          ),
        ),
      ),
    );
  }
}

/// Horizontal chip selector for picking which subscription the "Current
/// service" card shows, when the customer has more than one.
class _ServiceSwitcher extends StatelessWidget {
  const _ServiceSwitcher({
    required this.services,
    required this.selectedId,
    required this.onSelect,
  });
  final List<Subscription> services;
  final String selectedId;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 40,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: services.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (_, i) {
          final s = services[i];
          return ChoiceChip(
            label: Text(s.displayName,
                maxLines: 1, overflow: TextOverflow.ellipsis),
            selected: s.id == selectedId,
            avatar: Icon(
              s.isActive ? Icons.circle : Icons.pause_circle_outline,
              size: 14,
              color: s.isActive ? Colors.green.shade600 : null,
            ),
            onSelected: (_) => onSelect(s.id),
          );
        },
      ),
    );
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.onTap,
    this.highlight = false,
  });
  final IconData icon;
  final String label;
  final String value;
  final VoidCallback onTap;
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, size: 20, color: theme.colorScheme.primary),
              const SizedBox(height: 10),
              // Scale the figure down to fit the narrow column rather than
              // truncating it (a cut-off "NGN 1,732,…" is unreadable).
              FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Text(value,
                    maxLines: 1,
                    softWrap: false,
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w700,
                      color: highlight ? theme.colorScheme.error : null,
                    )),
              ),
              const SizedBox(height: 2),
              Text(label,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.outline)),
            ],
          ),
        ),
      ),
    );
  }
}

class _QuickActions extends StatelessWidget {
  const _QuickActions();

  static const _actions = <(IconData, String, String)>[
    (Icons.payment, 'Pay bill', '/billing'),
    (Icons.add_card_outlined, 'Top up', '/topup'),
    (Icons.receipt_long_outlined, 'Invoices', '/billing'),
    (Icons.data_usage_outlined, 'Usage', '/usage'),
    (Icons.support_agent_outlined, 'Support', '/support'),
    (Icons.person_outline, 'Profile', '/profile'),
  ];

  @override
  Widget build(BuildContext context) {
    return GridView.count(
      crossAxisCount: 3,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      mainAxisSpacing: 10,
      crossAxisSpacing: 10,
      childAspectRatio: 1.4,
      children: [
        for (final (icon, label, path) in _actions)
          _ActionTile(
            icon: icon,
            label: label,
            onTap: () => context.go(path),
          ),
      ],
    );
  }
}

class _ActionTile extends StatelessWidget {
  const _ActionTile(
      {required this.icon, required this.label, required this.onTap});
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, color: theme.colorScheme.primary),
            const SizedBox(height: 6),
            Text(label,
                textAlign: TextAlign.center,
                style: theme.textTheme.labelMedium),
          ],
        ),
      ),
    );
  }
}

class _CurrentServiceCard extends StatelessWidget {
  const _CurrentServiceCard({required this.service});
  final Subscription service;

  @override
  Widget build(BuildContext context) {
    final s = service;
    final theme = Theme.of(context);
    final days = s.daysUntilExpiry;
    final (expiryColor, expiryText) = switch (days) {
      null => (theme.colorScheme.outline, null),
      < 0 => (theme.colorScheme.error, 'Expired'),
      0 => (theme.colorScheme.error, 'Expires today'),
      <= 3 => (Colors.orange.shade800, '$days day${days == 1 ? '' : 's'} left'),
      _ => (Colors.green.shade700, '$days days left'),
    };

    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => Navigator.of(context).push(
          MaterialPageRoute(builder: (_) => ServiceDetailScreen(service: s)),
        ),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Icon(Icons.router_outlined),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(s.displayName,
                        style: theme.textTheme.titleMedium,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                  ),
                  StatusChip.forSubscription(s.status),
                ],
              ),
              if (s.planType != null) ...[
                const SizedBox(height: 2),
                Text(s.planType!,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.outline)),
              ],
              const Divider(height: 20),
              Row(
                children: [
                  Expanded(
                    child: _MiniStat(
                      icon: Icons.lan_outlined,
                      label: 'IP address',
                      value: s.ipv4Address ?? '—',
                    ),
                  ),
                  if (expiryText != null)
                    Expanded(
                      child: _MiniStat(
                        icon: Icons.schedule,
                        label: 'Validity',
                        value: expiryText,
                        color: expiryColor,
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 8),
              Builder(builder: (context) {
                // Surface a pay CTA when the service needs attention: suspended,
                // or expiring within 3 days / already expired.
                final needsAttention =
                    !s.isActive || (days != null && days <= 3);
                return Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    if (needsAttention)
                      Expanded(
                        child: FilledButton.icon(
                          icon: const Icon(Icons.payment, size: 18),
                          onPressed: () => context.go('/billing'),
                          label: Text(s.isActive ? 'Renew' : 'Reactivate'),
                        ),
                      ),
                    if (needsAttention) const SizedBox(width: 8),
                    TextButton(
                      onPressed: () => Navigator.of(context).push(
                        MaterialPageRoute(
                            builder: (_) => ServiceDetailScreen(service: s)),
                      ),
                      child: const Text('Manage'),
                    ),
                  ],
                );
              }),
            ],
          ),
        ),
      ),
    );
  }
}

class _MiniStat extends StatelessWidget {
  const _MiniStat({
    required this.icon,
    required this.label,
    required this.value,
    this.color,
  });
  final IconData icon;
  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: [
        Icon(icon, size: 18, color: color ?? theme.colorScheme.outline),
        const SizedBox(width: 8),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label, style: theme.textTheme.labelSmall),
            Text(value,
                style: TextStyle(fontWeight: FontWeight.w600, color: color)),
          ],
        ),
      ],
    );
  }
}

class _SectionHeader extends StatelessWidget {
  const _SectionHeader(this.title);
  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(title, style: Theme.of(context).textTheme.titleMedium),
    );
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard(this.message);
  final String message;
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(message),
        ),
      );
}
