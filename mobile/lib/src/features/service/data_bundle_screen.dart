import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/addon.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';

/// Buy a data top-up for a service. Data top-ups are add-ons that grant GB to
/// the current quota bucket (GET/POST /me/subscriptions/{id}/add-ons); the
/// charge comes from the wallet balance.
class DataBundleScreen extends ConsumerStatefulWidget {
  const DataBundleScreen({super.key, required this.service});
  final Subscription service;

  @override
  ConsumerState<DataBundleScreen> createState() => _DataBundleScreenState();
}

class _DataBundleScreenState extends ConsumerState<DataBundleScreen> {
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
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: Text('Buy ${option.grantGb} GB?'),
        content: Text(
          '${Fmt.money(option.amount, option.currency)} will be charged from '
          'your wallet and added to this cycle.',
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Buy')),
        ],
      ),
    );
    if (ok != true || !mounted) return;

    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final result = await ref
          .read(catalogRepositoryProvider)
          .purchaseAddon(_subId, option.addOnId, 1);
      if (result.success) {
        ref.invalidate(balanceProvider);
        ref.invalidate(ledgerProvider);
        ref.invalidate(quotaBucketsProvider);
        messenger.showSnackBar(
          SnackBar(content: Text('${option.grantGb} GB added')),
        );
        await _load();
      } else if (result.insufficient) {
        messenger.showSnackBar(SnackBar(
          content: Text(
              'Insufficient balance — top up ${Fmt.money(result.shortfall ?? 0, result.currency)}'),
        ));
      } else {
        messenger.showSnackBar(
          const SnackBar(content: Text('Could not buy this bundle')),
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
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Buy data')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : _error != null
                ? ListView(children: [
                    const SizedBox(height: 80),
                    const Center(child: Text('Could not load data bundles')),
                    const SizedBox(height: 8),
                    Center(
                      child: TextButton(
                          onPressed: _load, child: const Text('Retry')),
                    ),
                  ])
                : _content(theme),
      ),
    );
  }

  Widget _content(ThemeData theme) {
    final data = _data!;
    final bundles = data.available.where((o) => o.isDataTopup).toList()
      ..sort((a, b) => (a.grantGb ?? 0).compareTo(b.grantGb ?? 0));

    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        if (data.walletBalance != null)
          Card(
            child: ListTile(
              leading: const Icon(Icons.account_balance_wallet_outlined),
              title: const Text('Wallet balance'),
              trailing: Text(
                Fmt.money(data.walletBalance!, data.currency),
                style: theme.textTheme.titleMedium,
              ),
            ),
          ),
        const SizedBox(height: 8),
        if (bundles.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 40),
            child: Center(child: Text('No data bundles available')),
          )
        else
          for (final b in bundles)
            Card(
              margin: const EdgeInsets.only(bottom: 8),
              child: ListTile(
                leading: const Icon(Icons.data_usage),
                title: Text('${b.grantGb} GB'),
                subtitle: Text(b.name),
                trailing: FilledButton(
                  onPressed: _busy ? null : () => _buy(b),
                  child: Text(Fmt.money(b.amount, b.currency)),
                ),
              ),
            ),
      ],
    );
  }
}
