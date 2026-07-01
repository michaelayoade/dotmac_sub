import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/reseller.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Landing screen for a reseller: portfolio KPIs + their managed accounts.
class ResellerHomeScreen extends ConsumerWidget {
  const ResellerHomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final dashboard = ref.watch(resellerDashboardProvider);
    final name = ref.watch(currentUserProvider)?.greetingName ?? 'Reseller';

    return Scaffold(
      appBar: AppBar(
        title: const Text('Reseller Portal'),
        actions: [
          IconButton(
            tooltip: 'Sign out',
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(authControllerProvider.notifier).logout(),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerDashboardProvider);
          await ref.read(resellerDashboardProvider.future);
        },
        child: AsyncValueView<ResellerDashboard>(
          value: dashboard,
          onRetry: () => ref.invalidate(resellerDashboardProvider),
          data: (d) => ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Text('Welcome, $name',
                  style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 12),
              _Totals(totals: d.totals),
              const SizedBox(height: 12),
              const _SectionTiles(),
              if (d.openTickets > 0) ...[
                const SizedBox(height: 8),
                Card(
                  margin: EdgeInsets.zero,
                  child: ListTile(
                    dense: true,
                    leading: const Icon(Icons.confirmation_number_outlined),
                    title: Text(
                        '${d.openTickets} open support ticket${d.openTickets == 1 ? '' : 's'}'),
                  ),
                ),
              ],
              for (final a in d.alerts) _AlertTile(alert: a),
              const SizedBox(height: 16),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text('Accounts',
                      style: Theme.of(context).textTheme.titleSmall),
                  TextButton(
                    onPressed: () => context.push('/reseller/accounts'),
                    child: const Text('View all'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              if (d.accounts.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: EmptyState(
                      icon: Icons.people_outline, message: 'No accounts yet.'),
                )
              else
                for (final a in d.accounts) _AccountTile(account: a),
            ],
          ),
        ),
      ),
    );
  }
}

class _SectionTiles extends StatelessWidget {
  const _SectionTiles();

  static const _sections = [
    (Icons.add_location_alt_outlined, 'Quotes & installs', '/reseller/quotes'),
    (Icons.receipt_long_outlined, 'Billing', '/reseller/billing'),
    (Icons.forum_outlined, 'Live chat', '/reseller/chat'),
    (Icons.bar_chart_outlined, 'Revenue', '/reseller/revenue'),
    (
      Icons.add_business_outlined,
      'Service requests',
      '/reseller/service-requests'
    ),
    (Icons.bolt_outlined, 'Airtime & bills', '/reseller/vas'),
    (Icons.map_outlined, 'Coverage map', '/reseller/fiber-map'),
    (Icons.manage_accounts_outlined, 'Profile & security', '/reseller/profile'),
  ];

  @override
  Widget build(BuildContext context) {
    return GridView.count(
      crossAxisCount: 3,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      mainAxisSpacing: 8,
      crossAxisSpacing: 8,
      childAspectRatio: 1.15,
      children: [
        for (final (icon, label, route) in _sections)
          Card(
            margin: EdgeInsets.zero,
            child: InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: () => context.push(route),
              child: Padding(
                padding: const EdgeInsets.all(8),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(icon, size: 28),
                    const SizedBox(height: 6),
                    Text(
                      label,
                      textAlign: TextAlign.center,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _Totals extends StatelessWidget {
  const _Totals({required this.totals});

  final ResellerTotals totals;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(child: _Kpi(label: 'Accounts', value: '${totals.accounts}')),
        const SizedBox(width: 8),
        Expanded(
          child: _Kpi(
            label: 'Open balance',
            value: Fmt.moneyCompact(totals.openBalance, 'NGN'),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: _Kpi(label: 'Open invoices', value: '${totals.openInvoices}'),
        ),
      ],
    );
  }
}

class _Kpi extends StatelessWidget {
  const _Kpi({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(value, style: Theme.of(context).textTheme.titleLarge),
            ),
            const SizedBox(height: 4),
            Text(label, style: Theme.of(context).textTheme.bodySmall),
          ],
        ),
      ),
    );
  }
}

class _AlertTile extends StatelessWidget {
  const _AlertTile({required this.alert});

  final ResellerAlert alert;

  @override
  Widget build(BuildContext context) {
    final (IconData icon, Color color) = switch (alert.level) {
      'danger' => (Icons.error_outline, Theme.of(context).colorScheme.error),
      'warning' => (Icons.warning_amber_outlined, context.semantic.warning),
      _ => (Icons.info_outline, Colors.blue),
    };
    return Card(
      margin: const EdgeInsets.only(top: 8),
      child: ListTile(
        dense: true,
        leading: Icon(icon, color: color),
        title: Text(alert.message),
      ),
    );
  }
}

class _AccountTile extends StatelessWidget {
  const _AccountTile({required this.account});

  final ResellerAccount account;

  @override
  Widget build(BuildContext context) {
    final title = account.subscriberName.isEmpty
        ? (account.accountNumber ?? account.id)
        : account.subscriberName;
    final lastPaid = account.lastPaymentAt == null
        ? ''
        : ' · paid ${Fmt.date(account.lastPaymentAt)}';
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        onTap: () =>
            context.push('/reseller/accounts/${account.id}', extra: title),
        title: Text(title),
        subtitle: Text(
          '${account.status} · ${account.openInvoices} open$lastPaid',
        ),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(Fmt.money(account.openBalance, 'NGN')),
            const Icon(Icons.chevron_right),
          ],
        ),
      ),
    );
  }
}
