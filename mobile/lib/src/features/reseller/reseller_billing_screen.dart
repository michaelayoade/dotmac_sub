import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../billing/payment_webview_screen.dart';

/// Reseller consolidated billing: outstanding/unallocated totals, recent
/// payments, and a pay flow through the shared gateway webview
/// (GET /reseller/billing + pay intent/verify).
class ResellerBillingScreen extends ConsumerStatefulWidget {
  const ResellerBillingScreen({super.key});

  @override
  ConsumerState<ResellerBillingScreen> createState() =>
      _ResellerBillingScreenState();
}

class _ResellerBillingScreenState extends ConsumerState<ResellerBillingScreen> {
  bool _paying = false;

  Future<void> _pay() async {
    final messenger = ScaffoldMessenger.of(context);
    final controller = TextEditingController();
    final amount = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Pay towards balance'),
        content: TextField(
          controller: controller,
          autofocus: true,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          decoration: const InputDecoration(
            labelText: 'Amount (NGN)',
            border: OutlineInputBorder(),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('Continue'),
          ),
        ],
      ),
    );
    if (amount == null || amount.isEmpty) return;

    setState(() => _paying = true);
    try {
      final repo = ref.read(resellerRepositoryProvider);
      final intent = await repo.payIntent(amount);
      if (!mounted) return;
      final reference = await context.push<String>(
        '/pay',
        extra: CheckoutArgs.resellerBilling(intent),
      );
      if (reference == null) return; // cancelled in the webview
      await repo.payVerify(reference);
      ref.invalidate(resellerBillingProvider);
      messenger.showSnackBar(
        const SnackBar(content: Text('Payment recorded — thank you.')),
      );
    } catch (e) {
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            e.toString().contains('400')
                ? 'Payment could not be completed.'
                : 'Something went wrong — if you were charged, the payment '
                      'will be reconciled automatically.',
          ),
        ),
      );
    } finally {
      if (mounted) setState(() => _paying = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final billing = ref.watch(resellerBillingProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Billing')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerBillingProvider);
          await ref.read(resellerBillingProvider.future);
        },
        child: AsyncValueView<ResellerBillingSummary>(
          value: billing,
          onRetry: () => ref.invalidate(resellerBillingProvider),
          data: (b) => ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Row(
                children: [
                  Expanded(
                    child: Card(
                      margin: EdgeInsets.zero,
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                Fmt.money(b.totalOutstanding, 'NGN'),
                                maxLines: 1,
                                style: Theme.of(context).textTheme.titleMedium
                                    ?.copyWith(
                                      fontWeight: FontWeight.w700,
                                      color: b.totalOutstanding > 0
                                          ? Theme.of(context).colorScheme.error
                                          : null,
                                    ),
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'Outstanding',
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Card(
                      margin: EdgeInsets.zero,
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                Fmt.money(b.unallocatedBalance, 'NGN'),
                                maxLines: 1,
                                style: Theme.of(context).textTheme.titleMedium
                                    ?.copyWith(fontWeight: FontWeight.w700),
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'Unallocated credit',
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              FilledButton.icon(
                onPressed: _paying ? null : _pay,
                icon: const Icon(Icons.payment, size: 18),
                label: Text(_paying ? 'Starting payment…' : 'Pay'),
              ),
              const SizedBox(height: 16),
              Text(
                'Recent payments',
                style: Theme.of(context).textTheme.titleSmall,
              ),
              const SizedBox(height: 8),
              if (b.recentPayments.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 16),
                  child: EmptyState(
                    icon: Icons.payments_outlined,
                    message: 'No payments yet',
                  ),
                )
              else
                for (final pmt in b.recentPayments)
                  Card(
                    margin: const EdgeInsets.only(bottom: 8),
                    child: ListTile(
                      dense: true,
                      leading: const Icon(Icons.receipt_long_outlined),
                      title: Text(Fmt.money(pmt.amount, pmt.currency)),
                      subtitle: Text(
                        [
                          if (pmt.method != null) pmt.method!,
                          if (pmt.receivedAt != null) Fmt.date(pmt.receivedAt!),
                        ].join(' · '),
                      ),
                    ),
                  ),
            ],
          ),
        ),
      ),
    );
  }
}
