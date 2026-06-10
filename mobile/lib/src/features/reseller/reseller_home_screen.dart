import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
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
    final name = ref.watch(currentUserProvider)?.fullName ?? 'Reseller';

    return Scaffold(
      appBar: AppBar(
        title: const Text('Reseller Portal'),
        actions: [
          PopupMenuButton<String>(
            tooltip: 'Menu',
            icon: const Icon(Icons.menu),
            onSelected: (route) => context.push(route),
            itemBuilder: (_) => const [
              PopupMenuItem(
                value: '/reseller/billing',
                child: ListTile(
                  leading: Icon(Icons.receipt_long_outlined),
                  title: Text('Billing'),
                ),
              ),
              PopupMenuItem(
                value: '/reseller/revenue',
                child: ListTile(
                  leading: Icon(Icons.bar_chart_outlined),
                  title: Text('Revenue'),
                ),
              ),
              PopupMenuItem(
                value: '/reseller/service-requests',
                child: ListTile(
                  leading: Icon(Icons.add_business_outlined),
                  title: Text('Service requests'),
                ),
              ),
              PopupMenuItem(
                value: '/reseller/fiber-map',
                child: ListTile(
                  leading: Icon(Icons.map_outlined),
                  title: Text('Coverage map'),
                ),
              ),
              PopupMenuItem(
                value: '/reseller/profile',
                child: ListTile(
                  leading: Icon(Icons.manage_accounts_outlined),
                  title: Text('Profile & security'),
                ),
              ),
            ],
          ),
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
              Text('Accounts', style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 8),
              if (d.accounts.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: Text('No accounts yet.')),
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
            value: Fmt.money(totals.openBalance, 'NGN'),
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
            Text(value, style: Theme.of(context).textTheme.titleLarge),
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
      'danger' => (Icons.error_outline, Colors.red),
      'warning' => (Icons.warning_amber_outlined, Colors.orange),
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
