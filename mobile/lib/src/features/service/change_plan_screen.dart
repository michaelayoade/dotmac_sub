import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
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
      if (mounted) setState(() => _options = opts);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  String get _today {
    final n = DateTime.now();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${n.year}-${two(n.month)}-${two(n.day)}';
  }

  Future<void> _confirm(PlanOffer offer) async {
    // Prorated quote for the selected plan (empty/no-op for postpaid).
    PlanChangeQuote? quote;
    bool quoteFailed = false;
    try {
      quote = await ref
          .read(catalogRepositoryProvider)
          .planChangeQuote(_subId, offer.id);
    } catch (_) {
      // The quote is optional, but a failure must not look like a confident
      // ₦0 cost — flag it so the sheet warns and the confirm is less assertive.
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
    if (ok != true) return;
    await _submit(offer);
  }

  Future<void> _submit(PlanOffer offer) async {
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() => _busy = true);
    try {
      await ref.read(catalogRepositoryProvider).submitPlanChange(
            _subId,
            offerId: offer.id,
            effectiveDate: _today,
          );
      // A prepaid change debits the wallet — refresh balance + ledger too.
      ref.invalidate(subscriptionsProvider);
      ref.invalidate(balanceProvider);
      ref.invalidate(ledgerProvider);
      messenger.showSnackBar(
        SnackBar(content: Text('Plan change to ${offer.name} requested')),
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
                  leading: const Icon(Icons.check_circle, color: Colors.green),
                  title: Text('Current: ${opts.currentOffer!.name}'),
                  subtitle: Text(
                      '${Fmt.money(opts.currentOffer!.amount, opts.currentOffer!.currency)} ${opts.currentOffer!.periodLabel}'),
                ),
              ),
            if (opts.walletBalance != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(4, 12, 4, 4),
                child: Text(
                  'Wallet balance: ${Fmt.money(opts.walletBalance!, opts.currentOffer?.currency ?? 'NGN')}',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            const SizedBox(height: 8),
            Text('Available plans', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            if (opts.availableOffers.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('No other plans are available right now.'),
                ),
              ),
            for (final o in opts.availableOffers)
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
            if (q != null && q.hasProration) ...[
              const Divider(height: 24),
              _row(context, 'Prorated charge (${q.daysRemaining} days left)',
                  Fmt.money(q.chargeAmount, cur)),
              _row(context, 'Wallet balance', Fmt.money(q.currentBalance, cur)),
              const Divider(height: 16),
              _row(context, 'Payable now', Fmt.money(q.netAmount, cur),
                  bold: true),
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
                          'Insufficient balance — top up ${Fmt.money(q.shortfall, cur)} to apply now.',
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
                        "Couldn't calculate the exact cost right now. You can "
                        'still switch — the final charge or proration will be '
                        'applied to your account.',
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
                    onPressed: () => Navigator.pop(context, true),
                    child: Text(quoteFailed ? 'Switch anyway' : 'Confirm'),
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
