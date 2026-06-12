import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/invoice.dart';
import '../../providers/data_providers.dart';
import 'payment_webview_screen.dart';

/// Runs the full pay flow for one invoice: initiate hosted checkout → provider
/// WebView → verify → refresh the affected invoice/list providers.
///
/// Shared by the invoice detail screen (full-width button) and the invoices
/// list (`compact: true` — a small tonal "Pay" action on each unpaid row) so
/// the pay logic lives in exactly one place.
class InvoicePayButton extends ConsumerStatefulWidget {
  const InvoicePayButton({
    super.key,
    required this.invoice,
    this.compact = false,
  });

  final Invoice invoice;
  final bool compact;

  @override
  ConsumerState<InvoicePayButton> createState() => _InvoicePayButtonState();
}

class _InvoicePayButtonState extends ConsumerState<InvoicePayButton> {
  bool _busy = false;

  Future<void> _pay() async {
    final inv = widget.invoice;
    final repo = ref.read(billingRepositoryProvider);
    final messenger = ScaffoldMessenger.of(context);
    final router = GoRouter.of(context);

    setState(() => _busy = true);
    try {
      final initiation = await repo.initiatePayment(inv.id);

      if (!mounted) return;
      final reference = await router.push<String>(
        '/pay',
        extra: CheckoutArgs.invoice(initiation),
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
      ref.invalidate(balanceProvider);
      ref.invalidate(ledgerProvider);

      messenger.showSnackBar(
        SnackBar(
          content: Text(
            result.succeeded
                ? 'Payment of ${Fmt.money(result.amount, result.currency)} received'
                : 'Payment recorded (${result.status})',
          ),
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

    if (widget.compact) {
      return FilledButton.tonal(
        onPressed: _busy ? null : _pay,
        style: FilledButton.styleFrom(
          visualDensity: VisualDensity.compact,
          padding: const EdgeInsets.symmetric(horizontal: 16),
        ),
        child: _busy
            ? const SizedBox(
                height: 16,
                width: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Text('Pay'),
      );
    }

    return FilledButton.icon(
      icon: _busy
          ? const SizedBox(
              height: 18,
              width: 18,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : const Icon(Icons.payment),
      label: Text('Pay ${Fmt.money(inv.balanceDue, inv.currency)}'),
      onPressed: _busy ? null : _pay,
    );
  }
}
