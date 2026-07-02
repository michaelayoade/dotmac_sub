import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/quote.dart';
import '../../models/reseller_crm.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Reseller view of the CRM operational modules across all managed customers —
/// self-serve quotes, installations, and field-service visits (Sales/Quotes B3).
class ResellerCrmScreen extends ConsumerWidget {
  const ResellerCrmScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return DefaultTabController(
      length: 3,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Quotes & installs'),
          bottom: const TabBar(
            tabs: [
              Tab(text: 'Quotes'),
              Tab(text: 'Installs'),
              Tab(text: 'Visits'),
            ],
          ),
        ),
        body: TabBarView(
          children: [_QuotesTab(), _ProjectsTab(), _WorkOrdersTab()],
        ),
      ),
    );
  }
}

class _QuotesTab extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(resellerQuotesProvider);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(resellerQuotesProvider),
      child: AsyncValueView(
        value: value,
        onRetry: () => ref.invalidate(resellerQuotesProvider),
        data: (list) => _list(
          context,
          list,
          'No quotes from your customers yet.',
          (q) => _QuoteTile(item: q),
        ),
      ),
    );
  }
}

class _ProjectsTab extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(resellerProjectsProvider);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(resellerProjectsProvider),
      child: AsyncValueView(
        value: value,
        onRetry: () => ref.invalidate(resellerProjectsProvider),
        data: (list) => _list(
          context,
          list,
          'No installations in progress.',
          (p) => _ProjectTile(item: p),
        ),
      ),
    );
  }
}

class _WorkOrdersTab extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(resellerWorkOrdersProvider);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(resellerWorkOrdersProvider),
      child: AsyncValueView(
        value: value,
        onRetry: () => ref.invalidate(resellerWorkOrdersProvider),
        data: (list) => _list(
          context,
          list,
          'No scheduled visits.',
          (w) => _WorkOrderTile(item: w),
        ),
      ),
    );
  }
}

Widget _list<T>(
  BuildContext context,
  List<T> items,
  String emptyMsg,
  Widget Function(T) tile,
) {
  if (items.isEmpty) {
    return ListView(
      children: [
        const SizedBox(height: 140),
        Center(
          child: Text(emptyMsg, style: Theme.of(context).textTheme.bodyMedium),
        ),
      ],
    );
  }
  return ListView.separated(
    padding: const EdgeInsets.all(12),
    itemCount: items.length,
    separatorBuilder: (_, __) => const SizedBox(height: 8),
    itemBuilder: (_, i) => tile(items[i]),
  );
}

Widget _accountHeader(BuildContext context, String? name) => Text(
  name ?? 'Customer',
  style: Theme.of(context).textTheme.labelMedium?.copyWith(
    color: Theme.of(context).colorScheme.primary,
  ),
);

class _QuoteTile extends StatelessWidget {
  const _QuoteTile({required this.item});
  final ResellerQuote item;

  @override
  Widget build(BuildContext context) {
    final q = item.quote;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _accountHeader(context, item.accountName),
            const SizedBox(height: 2),
            Text(
              q.address ?? 'Installation quote',
              style: Theme.of(context).textTheme.titleSmall,
            ),
            const SizedBox(height: 4),
            Text(
              '${q.feasibility.label} · ${q.statusLabel}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 6),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  'Estimate ${naira(q.total)}',
                  style: Theme.of(context).textTheme.bodyMedium,
                ),
                Text(
                  q.depositPaid
                      ? 'Deposit paid'
                      : 'Deposit ${naira(q.depositAmount)}',
                  style: Theme.of(context).textTheme.labelSmall,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ProjectTile extends StatelessWidget {
  const _ProjectTile({required this.item});
  final ResellerProject item;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _accountHeader(context, item.accountName),
            const SizedBox(height: 2),
            Text(item.name, style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 6),
            LinearProgressIndicator(
              value: (item.progressPct.clamp(0, 100)) / 100,
            ),
            const SizedBox(height: 4),
            Text(
              '${item.currentStage ?? item.status} · ${item.progressPct}%',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}

class _WorkOrderTile extends StatelessWidget {
  const _WorkOrderTile({required this.item});
  final ResellerWorkOrder item;

  @override
  Widget build(BuildContext context) {
    final eta = item.estimatedArrivalAt ?? item.scheduledStart;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _accountHeader(context, item.accountName),
            const SizedBox(height: 2),
            Text(item.title, style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 4),
            Text(
              [
                item.status,
                if (item.technicianName != null) 'Tech: ${item.technicianName}',
                if (eta != null) 'ETA ${eta.toLocal()}'.split('.').first,
              ].join(' · '),
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}
