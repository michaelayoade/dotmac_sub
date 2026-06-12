import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/wallet.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import 'payment_webview_screen.dart';

/// The Wallet: one fundable balance for DotMac bill payments (and, in
/// Phase 2, airtime/data/bill purchases). Server-flagged — the route is only
/// reachable from surfaces that saw a non-null walletProvider.
class WalletScreen extends ConsumerStatefulWidget {
  const WalletScreen({super.key});

  @override
  ConsumerState<WalletScreen> createState() => _WalletScreenState();
}

class _WalletScreenState extends ConsumerState<WalletScreen> {
  bool _busy = false;

  Future<void> _fund(WalletOverview wallet) async {
    final messenger = ScaffoldMessenger.of(context);
    final router = GoRouter.of(context);
    final amount = await _promptAmount(
      title: 'Fund wallet',
      hint:
          '${Fmt.money(wallet.minTopup, wallet.currency)} – ${Fmt.money(wallet.maxTopup, wallet.currency)}',
    );
    if (amount == null) return;
    setState(() => _busy = true);
    try {
      final initiation =
          await ref.read(walletRepositoryProvider).initiateTopup(amount);
      if (!mounted) return;
      final reference = await router.push<String>(
        '/pay',
        extra: CheckoutArgs(
          providerType: initiation.providerType,
          reference: initiation.reference,
          amount: initiation.amount,
          currency: initiation.currency,
          publicKey: initiation.publicKey,
          email: initiation.customerEmail,
          metadata: const {'payment_flow': 'vas_wallet_topup'},
        ),
      );
      if (reference == null) return; // cancelled
      final balance =
          await ref.read(walletRepositoryProvider).verifyTopup(reference);
      ref.invalidate(walletProvider);
      messenger.showSnackBar(SnackBar(
          content: Text(
              'Wallet funded — balance ${Fmt.money(balance, wallet.currency)}')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _payBill(WalletOverview wallet) async {
    final messenger = ScaffoldMessenger.of(context);
    final amount = await _promptAmount(
      title: 'Pay DotMac bill',
      hint: 'Up to ${Fmt.money(wallet.balance, wallet.currency)}',
    );
    if (amount == null) return;
    setState(() => _busy = true);
    try {
      final balance = await ref.read(walletRepositoryProvider).payBill(amount);
      ref.invalidate(walletProvider);
      ref.invalidate(invoicesProvider);
      ref.invalidate(balanceProvider);
      messenger.showSnackBar(SnackBar(
          content: Text(
              'Bill paid — wallet balance ${Fmt.money(balance, wallet.currency)}')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggleAutoPay(WalletOverview wallet) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref
          .read(walletRepositoryProvider)
          .setAutoDeduct(!wallet.autoPayBillEnabled);
      ref.invalidate(walletProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<double?> _promptAmount(
      {required String title, required String hint}) async {
    final controller = TextEditingController();
    final result = await showDialog<double>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: TextField(
          controller: controller,
          autofocus: true,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          decoration: InputDecoration(labelText: 'Amount (₦)', helperText: hint),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final value =
                  double.tryParse(controller.text.trim().replaceAll(',', ''));
              Navigator.of(context).pop(value);
            },
            child: const Text('Continue'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (result == null || result <= 0) return null;
    return result;
  }

  @override
  Widget build(BuildContext context) {
    final wallet = ref.watch(walletProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Wallet'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () =>
              context.canPop() ? context.pop() : context.go('/dashboard'),
        ),
      ),
      body: AsyncValueView<WalletOverview?>(
        value: wallet,
        onRetry: () => ref.invalidate(walletProvider),
        data: (data) {
          if (data == null) {
            return const Center(child: Text('Wallet is not available yet.'));
          }
          return _body(context, data);
        },
      ),
    );
  }

  Widget _body(BuildContext context, WalletOverview wallet) {
    final theme = Theme.of(context);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(walletProvider),
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Balance', style: theme.textTheme.labelLarge),
                  const SizedBox(height: 4),
                  Text(
                    Fmt.money(wallet.balance, wallet.currency),
                    style: theme.textTheme.headlineMedium
                        ?.copyWith(fontWeight: FontWeight.bold),
                  ),
                  const SizedBox(height: 16),
                  Row(children: [
                    Expanded(
                      child: FilledButton.icon(
                        onPressed: _busy ? null : () => _fund(wallet),
                        icon: const Icon(Icons.add),
                        label: const Text('Fund'),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: FilledButton.tonalIcon(
                        onPressed: _busy ? null : () => _payBill(wallet),
                        icon: const Icon(Icons.receipt_long_outlined),
                        label: const Text('Pay bill'),
                      ),
                    ),
                  ]),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          Card(
            child: SwitchListTile(
              title: const Text('Auto-pay my DotMac bill'),
              subtitle: const Text(
                  'Due invoices are paid from the wallet on their due date.'),
              value: wallet.autoPayBillEnabled,
              onChanged: _busy ? null : (_) => _toggleAutoPay(wallet),
            ),
          ),
          const SizedBox(height: 16),
          Text('Recent activity', style: theme.textTheme.titleMedium),
          const SizedBox(height: 4),
          if (wallet.entries.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 24),
              child: Center(
                  child:
                      Text('No activity yet — fund your wallet to begin.')),
            ),
          for (final entry in wallet.entries)
            ListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              leading: Icon(
                entry.isCredit
                    ? Icons.arrow_downward_rounded
                    : Icons.arrow_upward_rounded,
                color: entry.isCredit ? Colors.green : Colors.grey,
              ),
              title: Text(entry.memo ??
                  entry.category.replaceAll('_', ' ').toUpperCase()),
              subtitle: entry.createdAt != null
                  ? Text(Fmt.date(entry.createdAt!))
                  : null,
              trailing: Text(
                '${entry.isCredit ? '+' : '−'}${Fmt.money(entry.amount, entry.currency)}',
                style: TextStyle(
                  fontWeight: FontWeight.w600,
                  color: entry.isCredit ? Colors.green : null,
                ),
              ),
            ),
        ],
      ),
    );
  }
}
