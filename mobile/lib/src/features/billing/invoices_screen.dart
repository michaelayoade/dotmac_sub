import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
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
        bottom:
            const TabBar(tabs: [Tab(text: 'Invoices'), Tab(text: 'Payments')]),
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
        ],
      ),
    ).withTabs();
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
  /// Wrap the scaffold in a DefaultTabController matching the two tabs above.
  Widget withTabs() => DefaultTabController(length: 2, child: this);
}
