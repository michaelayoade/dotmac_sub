import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/invoice.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/status_chip.dart';
import 'payment_webview_screen.dart';

class InvoiceDetailScreen extends ConsumerWidget {
  const InvoiceDetailScreen({super.key, required this.invoiceId});

  final String invoiceId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final invoice = ref.watch(invoiceProvider(invoiceId));

    return Scaffold(
      appBar: AppBar(title: const Text('Invoice')),
      body: AsyncValueView(
        value: invoice,
        onRetry: () => ref.invalidate(invoiceProvider(invoiceId)),
        data: (inv) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Expanded(
                  child: Text(
                    inv.invoiceNumber ?? 'Invoice ${inv.id.substring(0, 8)}',
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                ),
                StatusChip.forInvoice(inv.isOverdue ? 'overdue' : inv.status),
              ],
            ),
            const SizedBox(height: 16),
            Card(
              child: Column(
                children: [
                  _Row('Issued', Fmt.date(inv.issuedAt)),
                  const Divider(height: 1),
                  _Row('Due', Fmt.date(inv.dueAt)),
                  if (inv.paidAt != null) ...[
                    const Divider(height: 1),
                    _Row('Paid', Fmt.date(inv.paidAt)),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 12),
            Card(
              child: Column(
                children: [
                  _Row('Subtotal', Fmt.money(inv.subtotal, inv.currency)),
                  const Divider(height: 1),
                  _Row('Tax', Fmt.money(inv.taxTotal, inv.currency)),
                  const Divider(height: 1),
                  _Row('Total', Fmt.money(inv.total, inv.currency), bold: true),
                  const Divider(height: 1),
                  _Row('Balance due', Fmt.money(inv.balanceDue, inv.currency),
                      bold: true,
                      color: inv.balanceDue > 0
                          ? Theme.of(context).colorScheme.error
                          : null),
                ],
              ),
            ),
            if (inv.memo != null && inv.memo!.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(inv.memo!),
                ),
              ),
            ],
            if (!inv.isPaid) ...[
              const SizedBox(height: 24),
              _PayButton(invoice: inv),
            ],
          ],
        ),
      ),
    );
  }
}

/// Runs the full pay flow: initiate hosted checkout → provider WebView →
/// verify → refresh the affected invoice/list providers.
class _PayButton extends ConsumerStatefulWidget {
  const _PayButton({required this.invoice});
  final Invoice invoice;

  @override
  ConsumerState<_PayButton> createState() => _PayButtonState();
}

class _PayButtonState extends ConsumerState<_PayButton> {
  bool _busy = false;

  Future<void> _pay() async {
    final inv = widget.invoice;
    final repo = ref.read(billingRepositoryProvider);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);

    setState(() => _busy = true);
    try {
      final initiation = await repo.initiatePayment(inv.id);

      if (!mounted) return;
      final reference = await navigator.push<String>(
        MaterialPageRoute(
          builder: (_) =>
              PaymentWebViewScreen(args: CheckoutArgs.invoice(initiation)),
        ),
      );
      if (reference == null) return; // cancelled

      final result = await repo.verifyPayment(
        reference,
        provider: initiation.providerType,
      );

      // Refresh anything that reflects the new payment.
      ref.invalidate(invoiceProvider(inv.id));
      ref.invalidate(invoicesProvider);
      ref.invalidate(paymentsProvider);

      messenger.showSnackBar(
        SnackBar(
          content: Text(result.succeeded
              ? 'Payment of ${Fmt.money(result.amount, result.currency)} received'
              : 'Payment recorded (${result.status})'),
        ),
      );
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Payment failed: $e')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final inv = widget.invoice;
    return FilledButton.icon(
      icon: _busy
          ? const SizedBox(
              height: 18,
              width: 18,
              child: CircularProgressIndicator(strokeWidth: 2))
          : const Icon(Icons.payment),
      label: Text('Pay ${Fmt.money(inv.balanceDue, inv.currency)}'),
      onPressed: _busy ? null : _pay,
    );
  }
}

class _Row extends StatelessWidget {
  const _Row(this.label, this.value, {this.bold = false, this.color});
  final String label;
  final String value;
  final bool bold;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final style = TextStyle(
      fontWeight: bold ? FontWeight.bold : FontWeight.normal,
      color: color,
    );
    return ListTile(
      title: Text(label),
      trailing: Text(value, style: style),
    );
  }
}
