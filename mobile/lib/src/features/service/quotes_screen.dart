import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../models/quote.dart';
import '../../models/service_request_option.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../billing/payment_webview_screen.dart';
import 'service_request_sheet.dart';

/// Customer installation and relocation quotes, coverage, and approved payment.
class QuotesScreen extends ConsumerStatefulWidget {
  const QuotesScreen({super.key});

  @override
  ConsumerState<QuotesScreen> createState() => _QuotesScreenState();
}

class _QuotesScreenState extends ConsumerState<QuotesScreen> {
  String? _payingId;

  Future<void> _requestService() async {
    final selection = await showModalBottomSheet<ServiceRequestSelection>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => const ServiceRequestSheet(),
    );
    if (selection != null && mounted) {
      context.push('/quotes/request', extra: selection);
    }
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  Future<void> _payDeposit(Quote quote) async {
    setState(() => _payingId = quote.id);
    try {
      final repo = ref.read(quotesRepositoryProvider);
      if (quote.isRelocation) {
        final prepared = await repo.prepareRelocation(quote.id);
        final billing = ref.read(billingRepositoryProvider);
        final initiation = await billing.initiatePayment(
          prepared.invoiceId,
          provider: 'paystack',
        );
        var reference = initiation.paymentReference;
        if (!initiation.charged) {
          if (!mounted) return;
          final returned = await context.push<String>(
            '/pay',
            extra: CheckoutArgs.invoice(initiation),
          );
          if (returned == null) return;
          reference = returned;
        }
        final result = await billing.verifyPayment(
          reference,
          provider: initiation.providerType,
        );
        ref.invalidate(quotesProvider);
        ref.invalidate(workOrdersProvider);
        ref.invalidate(invoicesProvider);
        _snack(result.succeeded
            ? 'Full charge received — your relocation is being scheduled.'
            : 'Payment is pending confirmation.');
        return;
      }
      final init = await repo.initiateDeposit(quote.id);

      var reference = init.paymentReference;
      if (!init.charged) {
        final args = CheckoutArgs(
          providerType: init.providerType,
          reference: init.paymentReference,
          amount: double.tryParse(init.amount) ?? 0,
          currency: init.currency,
          publicKey: init.providerPublicKey,
          email: init.customerEmail,
          checkoutUrl: CheckoutArgs.secureCheckoutUrl(init.checkoutUrl),
          metadata: {
            'payment_flow': 'quote_deposit',
            'invoice_id': init.invoiceId,
          },
        );
        if (!mounted) return;
        final result = await context.push<String>('/pay', extra: args);
        if (result == null) return; // cancelled
        reference = result;
      }

      final outcome = await repo.verifyDeposit(quote.id, reference: reference);
      ref.invalidate(quotesProvider);
      _snack(
        outcome.paid
            ? 'Deposit received — your service is being scheduled.'
            : 'Payment is pending confirmation.',
      );
    } on ApiException catch (e) {
      _snack(e.message);
    } finally {
      if (mounted) setState(() => _payingId = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final quotesPage = ref.watch(quotesProvider);
    final canRequest = quotesPage.asData?.value.actionsAvailable == true;
    return Scaffold(
      appBar: AppBar(title: const Text('Get a quote')),
      floatingActionButton: canRequest
          ? FloatingActionButton.extended(
              onPressed: _requestService,
              icon: const Icon(Icons.add_location_alt_outlined),
              label: const Text('Request service'),
            )
          : null,
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(quotesProvider),
        child: AsyncValueView(
          value: quotesPage,
          onRetry: () => ref.invalidate(quotesProvider),
          data: (page) {
            if (page.quotes.isEmpty) return _empty(context, page);
            return ListView(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 96),
              children: [
                if (!page.actionsAvailable) ...[
                  _availabilityNotice(context, page),
                  const SizedBox(height: 12),
                ],
                for (final quote in page.quotes) ...[
                  _QuoteCard(
                    quote: quote,
                    actionsAvailable: page.actionsAvailable,
                    paying: _payingId == quote.id,
                    onPay: () => _payDeposit(quote),
                  ),
                  const SizedBox(height: 12),
                ],
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _empty(BuildContext context, QuotesPage page) => ListView(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        children: [
          if (!page.actionsAvailable) ...[
            const SizedBox(height: 16),
            _availabilityNotice(context, page),
          ],
          const SizedBox(height: 120),
          Icon(
            Icons.map_outlined,
            size: 64,
            color: Theme.of(context).colorScheme.outline,
          ),
          const SizedBox(height: 16),
          Center(
            child: Text(
              'No quotes yet',
              style: Theme.of(context).textTheme.titleMedium,
            ),
          ),
          const SizedBox(height: 8),
          const Padding(
            padding: EdgeInsets.symmetric(horizontal: 32),
            child: Text(
              'Pin your service address to check coverage and request staff review.',
              textAlign: TextAlign.center,
            ),
          ),
        ],
      );

  Widget _availabilityNotice(BuildContext context, QuotesPage page) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: scheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.info_outline, color: scheme.onSurfaceVariant),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                page.actionsUnavailableMessage ??
                    'Online quote requests are currently unavailable. '
                        'Please contact support to continue.',
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _QuoteCard extends StatelessWidget {
  const _QuoteCard({
    required this.quote,
    required this.actionsAvailable,
    required this.paying,
    required this.onPay,
  });

  final Quote quote;
  final bool actionsAvailable;
  final bool paying;
  final VoidCallback onPay;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final text = Theme.of(context).textTheme;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    quote.address ?? 'Service request',
                    style: text.titleMedium,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                _StatusChip(quote: quote),
              ],
            ),
            const SizedBox(height: 8),
            if (quote.serviceOption != null) ...[
              Text(quote.serviceOption!.label, style: text.bodyMedium),
              const SizedBox(height: 8),
            ],
            Row(
              children: [
                Icon(
                  quote.feasibility.isCovered
                      ? Icons.check_circle
                      : Icons.info_outline,
                  size: 16,
                  color: quote.feasibility.isCovered
                      ? Colors.green
                      : scheme.tertiary,
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(quote.feasibility.label, style: text.bodySmall),
                ),
              ],
            ),
            const Divider(height: 24),
            if (quote.pricingVisible) ...[
              _row(context, 'Cost', naira(quote.total)),
              _row(
                context,
                quote.isRelocation ? 'Full relocation charge' : 'Deposit',
                naira(quote.depositAmount),
              ),
            ],
            const SizedBox(height: 10),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: scheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    quote.paymentReviewStatus == 'approved'
                        ? Icons.verified_outlined
                        : quote.paymentReviewStatus == 'rejected'
                            ? Icons.cancel_outlined
                            : Icons.schedule_outlined,
                    size: 18,
                    color: scheme.onSurfaceVariant,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      quote.paymentReviewMessage,
                      style: text.bodySmall,
                    ),
                  ),
                ],
              ),
            ),
            if (actionsAvailable && quote.canPayDeposit) ...[
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed: paying ? null : onPay,
                  icon: paying
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.payment),
                  label: Text(
                    paying
                        ? 'Processing…'
                        : quote.isRelocation
                            ? 'Pay relocation charge ${naira(quote.depositAmount)}'
                            : 'Pay deposit ${naira(quote.depositAmount)}',
                  ),
                ),
              ),
            ],
            if (quote.isAccepted &&
                (quote.projectId != null ||
                    quote.relocationWorkOrderId != null)) ...[
              const SizedBox(height: 8),
              Text(
                quote.isRelocation
                    ? 'Relocation booked — track it on Home.'
                    : 'Installation booked — track it on Home.',
                style: text.bodySmall,
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _row(BuildContext context, String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: Theme.of(context).textTheme.bodyMedium),
            Text(value, style: Theme.of(context).textTheme.titleSmall),
          ],
        ),
      );
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.quote});

  final Quote quote;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final accepted = quote.isAccepted;
    final color = accepted || quote.paymentReviewStatus == 'approved'
        ? Colors.green
        : quote.paymentReviewStatus == 'rejected'
            ? scheme.error
            : (quote.depositPaid ? scheme.primary : scheme.tertiary);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        quote.statusLabel,
        style: Theme.of(context).textTheme.labelSmall?.copyWith(color: color),
      ),
    );
  }
}
