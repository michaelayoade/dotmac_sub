import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/payment_errors.dart';
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
    if (_busy) return;
    final messenger = ScaffoldMessenger.of(context);
    final router = GoRouter.of(context);
    // Guard the whole flow (including the amount dialog) so a double-tap can't
    // open two dialogs / fire two charges.
    setState(() => _busy = true);
    try {
      final amount = await _promptAmount(
        title: 'Fund wallet',
        hint:
            '${Fmt.money(wallet.minTopup, wallet.currency)} – ${Fmt.money(wallet.maxTopup, wallet.currency)}',
      );
      if (amount == null) return;
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
      if (mounted) showPaymentError(context, e, onRetry: () => _fund(wallet));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _payBill(WalletOverview wallet) async {
    if (_busy) return;
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final amount = await _promptAmount(
        title: 'Pay DotMac bill',
        hint: 'Up to ${Fmt.money(wallet.balance, wallet.currency)}',
      );
      if (amount == null) return;
      final balance = await ref.read(walletRepositoryProvider).payBill(amount);
      ref.invalidate(walletProvider);
      ref.invalidate(invoicesProvider);
      ref.invalidate(balanceProvider);
      messenger.showSnackBar(SnackBar(
          content: Text(
              'Bill paid — wallet balance ${Fmt.money(balance, wallet.currency)}')));
    } on ApiException catch (e) {
      if (mounted) showPaymentError(context, e);
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
          decoration:
              InputDecoration(labelText: 'Amount (₦)', helperText: hint),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final raw = controller.text.trim().replaceAll(',', '');
              final value = double.tryParse(raw);
              // Reject ≤0 and more than 2 decimal places (kobo precision).
              if (value == null || value <= 0 || !_validMoney(raw)) {
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                    content: Text(
                        'Enter an amount greater than 0 with at most 2 decimals.')));
                return;
              }
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

  /// True when [raw] has at most 2 decimal places.
  static bool _validMoney(String raw) =>
      RegExp(r'^\d+(\.\d{1,2})?$').hasMatch(raw);

  @override
  Widget build(BuildContext context) {
    final wallet = ref.watch(walletProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Wallet'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          tooltip: 'Back',
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
                  const SizedBox(height: 8),
                  TextButton.icon(
                    onPressed: () => context.push('/bills'),
                    icon: const Icon(Icons.bolt_outlined, size: 18),
                    label: const Text('Airtime, data & bills'),
                  ),
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
                  child: Text('No activity yet — fund your wallet to begin.')),
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
