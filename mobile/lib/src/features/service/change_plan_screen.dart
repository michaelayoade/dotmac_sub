import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/plan_change.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Lets the customer switch their service to another available plan. Quotes are
/// fetched lazily for the selected plan only (the backend no longer prices the
/// whole catalog upfront).
class ChangePlanScreen extends ConsumerStatefulWidget {
  const ChangePlanScreen({super.key, required this.service});
  final Subscription service;

  @override
  ConsumerState<ChangePlanScreen> createState() => _ChangePlanScreenState();
}

class _ChangePlanScreenState extends ConsumerState<ChangePlanScreen> {
  PlanChangeOptions? _options;
  Object? _error;
  bool _loading = true;
  bool _busy = false;
  String? _targetServiceAddressId;

  String get _subId => widget.service.id;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final opts =
          await ref.read(catalogRepositoryProvider).planChangeOptions(_subId);
      if (mounted) {
        setState(() {
          _options = opts;
          _targetServiceAddressId = opts.currentServiceAddressId;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _confirm(PlanOffer offer) async {
    // Owner preview for the selected plan (explicit no-ledger result for postpaid).
    PlanChangeQuote? quote;
    bool quoteFailed = false;
    try {
      quote = await ref.read(catalogRepositoryProvider).planChangeQuote(
            _subId,
            offer.id,
            targetServiceAddressId: _targetServiceAddressId,
          );
    } catch (_) {
      // Confirmation is disabled without the owner fingerprint; a transport
      // failure must never look like a confident zero-cost preview.
      quoteFailed = true;
    }
    if (!mounted) return;

    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _ConfirmSheet(
        offer: offer,
        quote: quote,
        quoteFailed: quoteFailed,
        billingMessage: _options?.billingMessage,
      ),
    );
    if (ok != true ||
        quote == null ||
        quote.previewFingerprint.isEmpty ||
        quote.previewEffectiveAt == null) {
      return;
    }
    await _submit(offer, quote);
  }

  Future<void> _submit(PlanOffer offer, PlanChangeQuote quote) async {
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() => _busy = true);
    try {
      final result = await ref.read(catalogRepositoryProvider).submitPlanChange(
            _subId,
            offerId: offer.id,
            previewFingerprint: quote.previewFingerprint,
            previewEffectiveAt: quote.previewEffectiveAt!,
            targetServiceAddressId: _targetServiceAddressId,
            fieldQuoteFingerprint: quote.fieldDeliveryQuote?.previewFingerprint,
          );
      // A prepaid change posts an exact debit — refresh funding and ledger too.
      ref.invalidate(subscriptionsProvider);
      ref.invalidate(balanceProvider);
      ref.invalidate(ledgerProvider);
      ref.invalidate(accountHealthProvider);
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            result.message ??
                (result.applied
                    ? 'Plan changed to ${offer.name}'
                    : 'Service change to ${offer.name} is awaiting delivery'),
          ),
        ),
      );
      navigator.pop();
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Change plan')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _options == null
              ? AsyncValueView(
                  value: AsyncValue<void>.error(
                      _error ?? 'error', StackTrace.empty),
                  data: (_) => const SizedBox.shrink(),
                  onRetry: _load,
                )
              : _list(_options!),
    );
  }

  Widget _list(PlanChangeOptions opts) {
    final theme = Theme.of(context);
    return Stack(
      children: [
        ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (opts.currentOffer != null)
              Card(
                color: theme.colorScheme.surfaceContainerHighest,
                child: ListTile(
                  leading:
                      Icon(Icons.check_circle, color: context.semantic.success),
                  title: Text('Current: ${opts.currentOffer!.name}'),
                  subtitle: Text(
                      '${Fmt.money(opts.currentOffer!.amount, opts.currentOffer!.currency)} ${opts.currentOffer!.periodLabel}'),
                ),
              ),
            if (opts.prepaidFunding != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(4, 12, 4, 4),
                child: Text(
                  'Prepaid funding: ${Fmt.money(opts.prepaidFunding!, opts.currentOffer?.currency ?? 'NGN')}',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            const SizedBox(height: 8),
            if (opts.serviceAddresses.isNotEmpty) ...[
              DropdownButtonFormField<String>(
                initialValue: _targetServiceAddressId,
                decoration: const InputDecoration(
                  labelText: 'Service address',
                  helperText:
                      'A new address requires field delivery; wireless/radio relocation carries a one-time charge.',
                  border: OutlineInputBorder(),
                ),
                items: [
                  for (final address in opts.serviceAddresses)
                    DropdownMenuItem(
                      value: address.id,
                      enabled: address.hasCoordinates || address.isCurrent,
                      child: Text(
                        '${address.label}${address.isCurrent ? ' — current' : ''}',
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
                onChanged: _busy
                    ? null
                    : (value) =>
                        setState(() => _targetServiceAddressId = value),
              ),
              const SizedBox(height: 16),
            ],
            Text('Available plans', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            if (opts.availableOffers.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('No other plans are available right now.'),
                ),
              ),
            for (final o in opts.availableOffers.where(
              (offer) =>
                  offer.id != opts.currentOffer?.id ||
                  _targetServiceAddressId != opts.currentServiceAddressId,
            ))
              Card(
                margin: const EdgeInsets.only(bottom: 8),
                child: ListTile(
                  title: Text(o.name),
                  subtitle: Text(
                      '${Fmt.money(o.amount, o.currency)} ${o.periodLabel}'),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: _busy ? null : () => _confirm(o),
                ),
              ),
          ],
        ),
        if (_busy)
          const Positioned.fill(
            child: ColoredBox(
              color: Colors.black26,
              child: Center(child: CircularProgressIndicator()),
            ),
          ),
      ],
    );
  }
}

/// Plan-change confirmation with a prorated cost breakdown (prepaid) or a
/// simple confirm (postpaid).
class _ConfirmSheet extends StatelessWidget {
  const _ConfirmSheet({
    required this.offer,
    required this.quote,
    required this.billingMessage,
    this.quoteFailed = false,
  });

  final PlanOffer offer;
  final PlanChangeQuote? quote;
  final String? billingMessage;

  /// True when the cost quote couldn't be fetched — show a warning and a less
  /// assertive confirm rather than implying a free / exact change.
  final bool quoteFailed;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final q = quote;
    final cur = offer.currency;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Switch to ${offer.name}',
                      style: theme.textTheme.titleLarge),
                ),
                if (q != null && (q.isUpgrade || q.isDowngrade))
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    decoration: BoxDecoration(
                      color: q.isUpgrade
                          ? theme.colorScheme.primaryContainer
                          : theme.colorScheme.secondaryContainer,
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: Text(q.isUpgrade ? 'Upgrade' : 'Downgrade',
                        style: theme.textTheme.labelMedium),
                  ),
              ],
            ),
            const SizedBox(height: 4),
            Text('${Fmt.money(offer.amount, cur)} ${offer.periodLabel}',
                style: theme.textTheme.bodyMedium
                    ?.copyWith(color: theme.colorScheme.outline)),
            if (q != null) ...[
              const SizedBox(height: 12),
              _row(context, 'Delivery', q.deliveryLabel, bold: true),
              if (q.deliveryMode == 'remote_reprovision')
                Text(
                  'We will verify the remote network change before switching your subscription. No ticket or site visit is required.',
                  style: theme.textTheme.bodySmall,
                ),
              if (q.requiresSiteVisit)
                Text(
                  'This access change needs field fulfillment. A work order is used only for the physical visit.',
                  style: theme.textTheme.bodySmall,
                ),
              if (q.fieldDeliveryQuote case final fieldQuote?) ...[
                const SizedBox(height: 8),
                _row(context, 'Target address', fieldQuote.targetAddressLabel),
                _row(context, 'Serviceability', fieldQuote.qualificationStatus),
                if (fieldQuote.feeAmount > 0)
                  _row(
                    context,
                    'One-time field charge',
                    Fmt.money(fieldQuote.feeAmount, fieldQuote.currency),
                    bold: true,
                  ),
                if (!fieldQuote.eligible)
                  Text(
                    fieldQuote.blockingReason ??
                        'This address is not eligible for relocation.',
                    style: TextStyle(color: theme.colorScheme.error),
                  ),
              ],
            ],
            if (q != null && q.hasProration) ...[
              const Divider(height: 24),
              _row(context, 'Prorated charge (${q.daysRemaining} days left)',
                  Fmt.money(q.chargeAmount, cur)),
              _row(context, 'Prepaid funding',
                  Fmt.money(q.prepaidFundingBefore, cur)),
              _row(context, 'Postpaid receivables',
                  Fmt.money(q.postpaidReceivables, cur)),
              _row(context, 'Collection-blocking balance',
                  Fmt.money(q.collectionBlockingBalance, cur)),
              _row(context, 'Funding after change',
                  Fmt.money(q.prepaidFundingAfter, cur)),
              const Divider(height: 16),
              _row(context, 'Payable now', Fmt.money(q.netAmount, cur),
                  bold: true),
              _row(
                context,
                'Exact ledger result',
                '${q.ledgerEntryType} / ${q.ledgerSource} / ${Fmt.money(q.ledgerAmount, cur)}',
              ),
              _row(context, 'Access consequence', q.accessConsequence),
              if (q.needsTopUp) ...[
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: theme.colorScheme.errorContainer,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    children: [
                      Icon(Icons.warning_amber_rounded,
                          size: 18, color: theme.colorScheme.onErrorContainer),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Insufficient prepaid funding — top up ${Fmt.money(q.shortfall, cur)} to apply now.',
                          style: TextStyle(
                              color: theme.colorScheme.onErrorContainer,
                              fontSize: 12),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ],
            if (q != null && !q.hasFinancialEffect) ...[
              const Divider(height: 24),
              _row(context, 'Exact ledger result', 'No ledger transaction'),
              _row(context, 'Postpaid receivables',
                  Fmt.money(q.postpaidReceivables, cur)),
              _row(context, 'Collection-blocking balance',
                  Fmt.money(q.collectionBlockingBalance, cur)),
              _row(context, 'Access consequence', q.accessConsequence),
            ],
            if (quoteFailed) ...[
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: theme.colorScheme.errorContainer,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Row(
                  children: [
                    Icon(Icons.info_outline,
                        size: 18, color: theme.colorScheme.onErrorContainer),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        "Couldn't calculate the exact result right now. Reload "
                        'the preview before confirming this plan change.',
                        style: TextStyle(
                            color: theme.colorScheme.onErrorContainer,
                            fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            if (billingMessage != null) ...[
              const SizedBox(height: 12),
              Text(billingMessage!, style: theme.textTheme.bodySmall),
            ],
            const SizedBox(height: 20),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton(
                    onPressed: quoteFailed ||
                            (q?.fieldDeliveryQuote != null &&
                                !q!.fieldDeliveryQuote!.eligible)
                        ? null
                        : () => Navigator.pop(context, true),
                    child: const Text('Confirm'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _row(BuildContext context, String label, String value,
      {bool bold = false}) {
    final style = TextStyle(fontWeight: bold ? FontWeight.w700 : null);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Flexible(child: Text(label, style: style)),
          Text(value, style: style),
        ],
      ),
    );
  }
}
