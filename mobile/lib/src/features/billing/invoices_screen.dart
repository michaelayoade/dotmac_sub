import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/ledger.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/status_chip.dart';

class InvoicesScreen extends ConsumerWidget {
  const InvoicesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final invoices = ref.watch(invoicesProvider);
    final payments = ref.watch(paymentsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Billing'),
        bottom: const TabBar(tabs: [
          Tab(text: 'Invoices'),
          Tab(text: 'Payments'),
          Tab(text: 'Activity'),
        ]),
      ),
      body: TabBarView(
        children: [
          RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(invoicesProvider);
              await ref.read(invoicesProvider.future);
            },
            child: AsyncValueView(
              value: invoices,
              onRetry: () => ref.invalidate(invoicesProvider),
              data: (page) {
                if (page.items.isEmpty) {
                  return const _ScrollableEmpty(
                    icon: Icons.receipt_long_outlined,
                    message: 'No invoices yet.',
                  );
                }
                return ListView.separated(
                  padding: const EdgeInsets.all(12),
                  itemCount: page.items.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 8),
                  itemBuilder: (_, i) {
                    final inv = page.items[i];
                    return Card(
                      margin: EdgeInsets.zero,
                      child: ListTile(
                        title: Text(inv.invoiceNumber ??
                            'Invoice ${inv.id.substring(0, 8)}'),
                        subtitle: Text('Due ${Fmt.date(inv.dueAt)}'),
                        trailing: Column(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Text(Fmt.money(inv.total, inv.currency),
                                style: const TextStyle(
                                    fontWeight: FontWeight.w600)),
                            const SizedBox(height: 4),
                            StatusChip.forInvoice(
                                inv.isOverdue ? 'overdue' : inv.status),
                          ],
                        ),
                        onTap: () => context.go('/billing/invoices/${inv.id}'),
                      ),
                    );
                  },
                );
              },
            ),
          ),
          RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(paymentsProvider);
              await ref.read(paymentsProvider.future);
            },
            child: AsyncValueView(
              value: payments,
              onRetry: () => ref.invalidate(paymentsProvider),
              data: (page) {
                if (page.items.isEmpty) {
                  return const _ScrollableEmpty(
                    icon: Icons.payments_outlined,
                    message: 'No payments recorded.',
                  );
                }
                return ListView.separated(
                  padding: const EdgeInsets.all(12),
                  itemCount: page.items.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 8),
                  itemBuilder: (_, i) {
                    final p = page.items[i];
                    return Card(
                      margin: EdgeInsets.zero,
                      child: ListTile(
                        leading: const Icon(Icons.check_circle_outline),
                        title: Text(Fmt.money(p.amount, p.currency)),
                        subtitle: Text(Fmt.dateTime(p.paidAt)),
                        trailing: StatusChip(p.status),
                      ),
                    );
                  },
                );
              },
            ),
          ),
          RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(ledgerProvider);
              await ref.read(ledgerProvider.future);
            },
            child: AsyncValueView(
              value: ref.watch(ledgerProvider),
              onRetry: () => ref.invalidate(ledgerProvider),
              data: (page) {
                if (page.items.isEmpty) {
                  return const _ScrollableEmpty(
                    icon: Icons.receipt_long_outlined,
                    message: 'No account activity yet.',
                  );
                }
                return ListView.separated(
                  padding: const EdgeInsets.all(12),
                  itemCount: page.items.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 8),
                  itemBuilder: (_, i) => _LedgerTile(txn: page.items[i]),
                );
              },
            ),
          ),
        ],
      ),
    ).withTabs();
  }
}

/// One account ledger row: credits (payments/refunds) are green +, debits
/// (charges) are red −.
class _LedgerTile extends StatelessWidget {
  const _LedgerTile({required this.txn});
  final LedgerTxn txn;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final credit = txn.isCredit;
    final color = credit ? Colors.green.shade700 : scheme.error;
    final sign = credit ? '+' : '−';
    return Card(
      margin: EdgeInsets.zero,
      child: ListTile(
        leading: Icon(
          credit ? Icons.south_west : Icons.north_east,
          color: color,
        ),
        title: Text(txn.title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(Fmt.dateTime(txn.createdAt)),
        trailing: Text(
          '$sign${Fmt.money(txn.amount, txn.currency)}',
          style: TextStyle(color: color, fontWeight: FontWeight.w600),
        ),
      ),
    );
  }
}

/// Empty state that still scrolls so pull-to-refresh works.
class _ScrollableEmpty extends StatelessWidget {
  const _ScrollableEmpty({required this.icon, required this.message});
  final IconData icon;
  final String message;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: SizedBox(
          height: constraints.maxHeight,
          child: EmptyState(icon: icon, message: message),
        ),
      ),
    );
  }
}

extension _Tabbed on Scaffold {
  /// Wrap the scaffold in a DefaultTabController matching the three tabs above.
  Widget withTabs() => DefaultTabController(length: 3, child: this);
}
