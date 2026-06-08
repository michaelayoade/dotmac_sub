import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/addon.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';
import '../billing/topup_screen.dart';

/// Browse and buy add-ons for a service. Purchases are charged from the wallet
/// balance (GET/POST /me/subscriptions/{id}/add-ons); insufficient balance
/// routes the customer to top up.
class AddOnsScreen extends ConsumerStatefulWidget {
  const AddOnsScreen({super.key, required this.service});
  final Subscription service;

  @override
  ConsumerState<AddOnsScreen> createState() => _AddOnsScreenState();
}

class _AddOnsScreenState extends ConsumerState<AddOnsScreen> {
  AddonsAvailable? _data;
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
      final d = await ref.read(catalogRepositoryProvider).addons(_subId);
      if (mounted) setState(() => _data = d);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _buy(AddonOption option) async {
    final qty = await showModalBottomSheet<int>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _BuySheet(
        option: option,
        subscriptionId: _subId,
      ),
    );
    if (qty == null) return;
    await _purchase(option, qty);
  }

  Future<void> _purchase(AddonOption option, int quantity) async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final result = await ref
          .read(catalogRepositoryProvider)
          .purchaseAddon(_subId, option.addOnId, quantity);
      if (result.success) {
        ref.invalidate(balanceProvider);
        ref.invalidate(ledgerProvider);
        messenger.showSnackBar(
          SnackBar(content: Text('${option.name} added')),
        );
        await _load();
      } else if (result.insufficient) {
        messenger.showSnackBar(SnackBar(
          content: Text(
              'Insufficient balance — top up ${Fmt.money(result.shortfall ?? 0, result.currency)}'),
        ));
      } else {
        messenger.showSnackBar(
          const SnackBar(content: Text('Could not add this add-on')),
        );
      }
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Add-ons')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _data == null
              ? _ErrorRetry(error: _error, onRetry: _load)
              : Stack(
                  children: [
                    RefreshIndicator(
                      onRefresh: _load,
                      child: _list(_data!),
                    ),
                    if (_busy)
                      const Positioned.fill(
                        child: ColoredBox(
                          color: Colors.black26,
                          child: Center(child: CircularProgressIndicator()),
                        ),
                      ),
                  ],
                ),
    );
  }

  Widget _list(AddonsAvailable d) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (d.walletBalance != null)
          Card(
            color: theme.colorScheme.surfaceContainerHighest,
            child: ListTile(
              leading: const Icon(Icons.account_balance_wallet_outlined),
              title: const Text('Wallet balance'),
              trailing: Text(
                Fmt.money(d.walletBalance!, d.currency),
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
            ),
          ),
        if (d.active.isNotEmpty) ...[
          const SizedBox(height: 16),
          Text('Your add-ons', style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          for (final a in d.active)
            Card(
              margin: const EdgeInsets.only(bottom: 8),
              child: ListTile(
                leading: const Icon(Icons.check_circle, color: Colors.green),
                title: Text(a.name),
                trailing: a.quantity > 1 ? Text('x${a.quantity}') : null,
              ),
            ),
        ],
        const SizedBox(height: 16),
        Text('Available add-ons', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        if (d.available.isEmpty)
          const Card(
            child: Padding(
              padding: EdgeInsets.all(16),
              child: Text('No add-ons are available for this plan.'),
            ),
          ),
        for (final o in d.available)
          Card(
            margin: const EdgeInsets.only(bottom: 8),
            child: ListTile(
              title: Text(o.name),
              subtitle: Text([
                if (o.description != null) o.description!,
                '${Fmt.money(o.amount, o.currency)} each',
              ].join('\n')),
              isThreeLine: o.description != null,
              trailing: FilledButton.tonal(
                onPressed: _busy ? null : () => _buy(o),
                child: const Text('Add'),
              ),
            ),
          ),
      ],
    );
  }
}

/// Quantity picker + live quote for one add-on. Pops the chosen quantity to
/// confirm, or null to cancel. Offers a top-up shortcut when unaffordable.
class _BuySheet extends ConsumerStatefulWidget {
  const _BuySheet({required this.option, required this.subscriptionId});
  final AddonOption option;
  final String subscriptionId;

  @override
  ConsumerState<_BuySheet> createState() => _BuySheetState();
}

class _BuySheetState extends ConsumerState<_BuySheet> {
  late int _qty = widget.option.minQuantity;
  AddonQuote? _quote;
  bool _loadingQuote = true;

  int get _max => widget.option.maxQuantity ?? 99;

  @override
  void initState() {
    super.initState();
    _fetchQuote();
  }

  Future<void> _fetchQuote() async {
    setState(() => _loadingQuote = true);
    try {
      final q = await ref
          .read(catalogRepositoryProvider)
          .addonQuote(widget.subscriptionId, widget.option.addOnId, _qty);
      if (mounted) setState(() => _quote = q);
    } catch (_) {
      // quote optional
    } finally {
      if (mounted) setState(() => _loadingQuote = false);
    }
  }

  void _setQty(int q) {
    if (q < widget.option.minQuantity || q > _max) return;
    setState(() => _qty = q);
    _fetchQuote();
  }

  @override
  Widget build(BuildContext context) {
    final o = widget.option;
    final q = _quote;
    final theme = Theme.of(context);
    final affordable = q?.canAfford ?? true;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(o.name, style: theme.textTheme.titleLarge),
            const SizedBox(height: 4),
            Text('${Fmt.money(o.amount, o.currency)} each'),
            if (o.maxQuantity == null || o.maxQuantity! > 1) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  const Text('Quantity'),
                  const Spacer(),
                  IconButton.filledTonal(
                    onPressed:
                        _qty > o.minQuantity ? () => _setQty(_qty - 1) : null,
                    icon: const Icon(Icons.remove),
                  ),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    child: Text('$_qty', style: theme.textTheme.titleMedium),
                  ),
                  IconButton.filledTonal(
                    onPressed: _qty < _max ? () => _setQty(_qty + 1) : null,
                    icon: const Icon(Icons.add),
                  ),
                ],
              ),
            ],
            const Divider(height: 24),
            if (_loadingQuote)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: LinearProgressIndicator(),
              )
            else if (q != null) ...[
              _row('Charge', Fmt.money(q.charge, q.currency), bold: true),
              _row('Wallet balance', Fmt.money(q.currentBalance, q.currency)),
              if (!affordable)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    'Top up ${Fmt.money(q.shortfall, q.currency)} to buy this.',
                    style: TextStyle(color: theme.colorScheme.error),
                  ),
                ),
            ],
            const SizedBox(height: 20),
            if (q != null && !affordable)
              FilledButton.tonalIcon(
                onPressed: () {
                  // Capture the navigator before popping the sheet — using the
                  // sheet's context after pop targets a defunct element.
                  final navigator = Navigator.of(context);
                  navigator.pop();
                  navigator.push(
                      MaterialPageRoute(builder: (_) => const TopUpScreen()));
                },
                icon: const Icon(Icons.add_card_outlined),
                label: const Text('Top up'),
              )
            else
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: () => Navigator.pop(context),
                      child: const Text('Cancel'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: FilledButton(
                      onPressed: (_loadingQuote || !affordable)
                          ? null
                          : () => Navigator.pop(context, _qty),
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

  Widget _row(String label, String value, {bool bold = false}) {
    final style = TextStyle(fontWeight: bold ? FontWeight.w700 : null);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [Text(label, style: style), Text(value, style: style)],
      ),
    );
  }
}

class _ErrorRetry extends StatelessWidget {
  const _ErrorRetry({required this.error, required this.onRetry});
  final Object? error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.cloud_off, size: 48),
          const SizedBox(height: 12),
          Text('Could not load add-ons.\n$error', textAlign: TextAlign.center),
          const SizedBox(height: 16),
          FilledButton.tonal(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
  }
}
