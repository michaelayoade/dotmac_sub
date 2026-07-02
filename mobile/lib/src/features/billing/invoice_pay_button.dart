import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/payment_errors.dart';
import '../../models/invoice.dart';
import '../../models/payment_method.dart';
import '../../models/topup.dart';
import '../../providers/data_providers.dart';
import 'payment_webview_screen.dart';
import 'transfer_proofs_screen.dart';

/// Runs the full pay flow for one invoice using the unified "Pay with" selector
/// (saved card one-tap, Paystack/Flutterwave, or bank transfer) — the same flow
/// as the Home top-up. Settles the specific invoice and refreshes affected
/// providers.
///
/// Shared by the invoice detail screen (full-width button) and the invoices
/// list (`compact: true`).
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
      // Reuse the account-level pay config (online gateways + bank transfer);
      // saved cards come from the payment-methods provider.
      final page = await repo.topupPage();
      final cards =
          ref.read(paymentMethodsProvider).asData?.value ?? const <SavedCard>[];
      if (!mounted) return;
      setState(() => _busy = false);

      final selection = await showModalBottomSheet<String>(
        context: context,
        isScrollControlled: true,
        builder: (_) => _PayMethodSheet(page: page, cards: cards),
      );
      if (selection == null || !mounted) return; // dismissed

      // Bank transfer: show the account + collect the receipt (staff verify
      // credits the account and auto-allocates to open invoices).
      if (selection == 'transfer') {
        final ok = await showSubmitProofSheet(
          context,
          initialAmount: inv.balanceDue.toStringAsFixed(2),
          accounts: page.bankTransfer.accounts,
          instructions: page.bankTransfer.instructions,
        );
        if (ok == true && mounted) {
          ref.invalidate(paymentProofsProvider);
          messenger.showSnackBar(
            const SnackBar(
              content: Text(
                'Receipt submitted — we will verify it and apply it to your invoice.',
              ),
            ),
          );
        }
        return;
      }

      setState(() => _busy = true);
      final cardId = selection.startsWith('card:')
          ? selection.substring(5)
          : null;
      final provider = selection.startsWith('gw:')
          ? selection.substring(3)
          : null;

      final initiation = await repo.initiatePayment(
        inv.id,
        provider: provider,
        paymentMethodId: cardId,
        idempotencyKey: cardId == null
            ? null
            : 'invpay-${DateTime.now().microsecondsSinceEpoch}-'
                  '${Random().nextInt(0x7fffffff)}',
      );
      if (!mounted) return;

      String reference;
      if (initiation.charged) {
        // Saved card charged server-side — skip the gateway webview.
        reference = initiation.paymentReference;
      } else {
        final ref0 = await router.push<String>(
          '/pay',
          extra: CheckoutArgs.invoice(initiation),
        );
        if (ref0 == null) return; // cancelled
        reference = ref0;
      }

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
      ref.invalidate(paymentMethodsProvider);

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
      if (mounted) showPaymentError(context, e, onRetry: _pay);
    } catch (e) {
      if (mounted) showPaymentError(context, e, onRetry: _pay);
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

/// Bottom-sheet method picker for paying an invoice. Pops a selection string:
/// `card:<id>`, `gw:<provider>`, or `transfer`.
class _PayMethodSheet extends StatelessWidget {
  const _PayMethodSheet({required this.page, required this.cards});

  final TopupPage page;
  final List<SavedCard> cards;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
              child: Text('Pay with', style: theme.textTheme.titleMedium),
            ),
            for (final c in cards)
              ListTile(
                leading: const Icon(Icons.credit_card),
                title: Text(
                  c.label ?? '${c.brand ?? 'Card'} •••• ${c.last4 ?? ''}',
                ),
                subtitle: c.expiry != null ? Text('Expires ${c.expiry}') : null,
                onTap: () => Navigator.of(context).pop('card:${c.id}'),
              ),
            for (final p in page.providers)
              ListTile(
                leading: const Icon(Icons.add_card_outlined),
                title: Text(p.label),
                onTap: () => Navigator.of(context).pop('gw:${p.providerType}'),
              ),
            if (page.bankTransfer.hasAccounts)
              ListTile(
                leading: const Icon(Icons.account_balance_outlined),
                title: const Text('Bank transfer'),
                subtitle: const Text(
                  'Show account details and upload your receipt',
                ),
                onTap: () => Navigator.of(context).pop('transfer'),
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }
}
