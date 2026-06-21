import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/payment_errors.dart';
import '../../models/vas.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../repositories/wallet_repository.dart';
import '../../widgets/async_value_view.dart';

/// Bill payments hub: catalog-driven category/service list, purchase sheet
/// with verify-echo + biometric confirmation above the server-set threshold,
/// and a receipt dialog with the token (re-retrievable from history).
class PayBillsScreen extends ConsumerStatefulWidget {
  const PayBillsScreen({super.key});

  @override
  ConsumerState<PayBillsScreen> createState() => _PayBillsScreenState();
}

class _PayBillsScreenState extends ConsumerState<PayBillsScreen> {
  @override
  Widget build(BuildContext context) {
    final catalog = ref.watch(vasCatalogProvider);
    final wallet = ref.watch(walletProvider).asData?.value;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Pay bills'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          tooltip: 'Back',
          onPressed: () =>
              context.canPop() ? context.pop() : context.go('/dashboard'),
        ),
      ),
      body: AsyncValueView<List<VasCategory>>(
        value: catalog,
        onRetry: () => ref.invalidate(vasCatalogProvider),
        data: (categories) {
          if (categories.isEmpty) {
            return const Center(child: Text('Bill payments are coming soon.'));
          }
          return RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(vasCatalogProvider);
              ref.invalidate(vasPurchasesProvider);
            },
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (wallet != null)
                  Card(
                    child: ListTile(
                      leading: const Icon(Icons.wallet_outlined),
                      title: Text(Fmt.money(wallet.balance, wallet.currency)),
                      subtitle: const Text('Wallet balance'),
                      trailing: TextButton(
                        onPressed: () => context.push('/wallet'),
                        child: const Text('Fund'),
                      ),
                    ),
                  ),
                for (final category in categories) ...[
                  const SizedBox(height: 16),
                  Text(category.label,
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  for (final service in category.services)
                    Card(
                      child: ListTile(
                        title: Text(service.name),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () => _openPurchaseSheet(service),
                      ),
                    ),
                ],
                const SizedBox(height: 24),
                _historySection(context),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _historySection(BuildContext context) {
    final purchases = ref.watch(vasPurchasesProvider).asData?.value ?? [];
    if (purchases.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Recent purchases',
            style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 4),
        for (final txn in purchases.take(10))
          ListTile(
            contentPadding: EdgeInsets.zero,
            dense: true,
            title: Text('${txn.serviceName ?? 'Purchase'} · ${txn.identifier}'),
            subtitle:
                txn.createdAt != null ? Text(Fmt.date(txn.createdAt!)) : null,
            trailing: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(Fmt.money(txn.amount, 'NGN'),
                    style: const TextStyle(fontWeight: FontWeight.w600)),
                Text(
                  txn.status.toUpperCase(),
                  style: TextStyle(
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                    color: txn.isDelivered
                        ? Colors.green
                        : txn.isRefunded
                            ? Colors.red
                            : Colors.amber.shade800,
                  ),
                ),
              ],
            ),
            onTap: () => _showReceipt(txn),
          ),
      ],
    );
  }

  Future<void> _openPurchaseSheet(VasService service) async {
    final txn = await showModalBottomSheet<VasTransaction>(
      context: context,
      isScrollControlled: true,
      builder: (context) => _PurchaseSheet(service: service),
    );
    if (txn == null || !mounted) return;
    ref.invalidate(walletProvider);
    ref.invalidate(vasPurchasesProvider);
    _showReceipt(txn);
  }

  void _showReceipt(VasTransaction txn) {
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        icon: Icon(
          txn.isDelivered
              ? Icons.check_circle
              : txn.isRefunded
                  ? Icons.cancel
                  : Icons.hourglass_top,
          color: txn.isDelivered
              ? Colors.green
              : txn.isRefunded
                  ? Colors.red
                  : Colors.amber,
          size: 40,
        ),
        title: Text(txn.isDelivered
            ? 'Delivered'
            : txn.isRefunded
                ? 'Not delivered'
                : 'Processing'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${txn.serviceName ?? 'Purchase'} · ${txn.identifier}'),
            const SizedBox(height: 4),
            Text(Fmt.money(txn.amount, 'NGN'),
                style: const TextStyle(fontWeight: FontWeight.bold)),
            if (txn.isRefunded) ...[
              const SizedBox(height: 8),
              const Text('The money is back in your wallet.',
                  style: TextStyle(color: Colors.green)),
            ],
            if (txn.isProcessing) ...[
              const SizedBox(height: 8),
              const Text('This can take a few minutes — we keep checking and '
                  'refund automatically if it fails.'),
            ],
            if (txn.token != null) ...[
              const SizedBox(height: 12),
              const Text('Token / PIN',
                  style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700)),
              SelectableText(
                txn.token!,
                style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 16,
                    fontWeight: FontWeight.bold),
              ),
            ],
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Done'),
          ),
        ],
      ),
    );
  }
}

class _PurchaseSheet extends ConsumerStatefulWidget {
  const _PurchaseSheet({required this.service});

  final VasService service;

  @override
  ConsumerState<_PurchaseSheet> createState() => _PurchaseSheetState();
}

class _PurchaseSheetState extends ConsumerState<_PurchaseSheet> {
  final _identifier = TextEditingController();
  final _amount = TextEditingController();
  VasVariation? _variation;
  bool _busy = false;
  bool _verifying = false;
  String? _verifiedName;
  String? _error;

  @override
  void dispose() {
    _identifier.dispose();
    _amount.dispose();
    super.dispose();
  }

  double? get _effectiveAmount {
    if (_variation?.amount != null) return _variation!.amount;
    return double.tryParse(_amount.text.trim().replaceAll(',', ''));
  }

  /// True when the typed amount has at most 2 decimal places (only checked for
  /// free-form entry; fixed-price variations are already exact).
  bool get _amountWellFormed {
    if (_variation?.amount != null) return true;
    final raw = _amount.text.trim().replaceAll(',', '');
    return RegExp(r'^\d+(\.\d{1,2})?$').hasMatch(raw);
  }

  Future<void> _verify() async {
    setState(() {
      _verifying = true;
      _error = null;
      _verifiedName = null;
    });
    try {
      final name = await ref.read(walletRepositoryProvider).verifyIdentifier(
            serviceId: widget.service.serviceId,
            identifier: _identifier.text.trim(),
          );
      if (mounted) setState(() => _verifiedName = name ?? 'Verified');
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _verifying = false);
    }
  }

  Future<void> _submit() async {
    final amount = _effectiveAmount;
    if (_identifier.text.trim().isEmpty) {
      setState(() => _error = '${widget.service.identifierLabel} is required.');
      return;
    }
    if (amount == null || amount <= 0) {
      setState(() => _error = 'Enter an amount greater than 0.');
      return;
    }
    if (!_amountWellFormed) {
      setState(() => _error = 'Amount can have at most 2 decimal places.');
      return;
    }
    final wallet = ref.read(walletProvider).asData?.value;
    if (wallet != null && amount > wallet.balance) {
      setState(() => _error =
          'Amount exceeds your wallet balance (${Fmt.money(wallet.balance, wallet.currency)}). Fund your wallet first.');
      return;
    }
    if (widget.service.requiresVerify && _verifiedName == null) {
      setState(() => _error = 'Check the customer name before paying.');
      return;
    }
    final threshold = wallet?.authThreshold ?? 5000;
    if (amount >= threshold) {
      final approved = await ref.read(biometricServiceProvider).authenticate(
          reason:
              'Confirm payment of ${Fmt.money(amount, 'NGN')} from your wallet');
      if (!approved) return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final txn = await ref.read(walletRepositoryProvider).purchase(
            serviceId: widget.service.serviceId,
            identifier: _identifier.text.trim(),
            variationCode: _variation?.code,
            amount: _variation?.amount == null ? amount : null,
          );
      if (mounted) Navigator.of(context).pop(txn);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = PaymentError.from(e).message;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final service = widget.service;
    return Padding(
      padding: EdgeInsets.only(
        left: 16,
        right: 16,
        top: 16,
        bottom: 16 + MediaQuery.of(context).viewInsets.bottom,
      ),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(service.name, style: Theme.of(context).textTheme.titleMedium),
            Builder(builder: (context) {
              final wallet = ref.watch(walletProvider).asData?.value;
              if (wallet == null) return const SizedBox.shrink();
              return Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  'Wallet balance: ${Fmt.money(wallet.balance, wallet.currency)}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              );
            }),
            const SizedBox(height: 12),
            TextField(
              controller: _identifier,
              keyboardType: TextInputType.phone,
              decoration: InputDecoration(
                labelText: '${service.identifierLabel} *',
                border: const OutlineInputBorder(),
                suffixIcon: service.requiresVerify
                    ? TextButton(
                        onPressed: _verifying ? null : _verify,
                        child: Text(_verifying ? '…' : 'Check'),
                      )
                    : null,
              ),
            ),
            if (_verifiedName != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(_verifiedName!,
                    style: const TextStyle(
                        color: Colors.green, fontWeight: FontWeight.w600)),
              ),
            if (service.variations.isNotEmpty) ...[
              const SizedBox(height: 12),
              DropdownButtonFormField<VasVariation>(
                initialValue: _variation,
                decoration: const InputDecoration(
                    labelText: 'Plan', border: OutlineInputBorder()),
                items: [
                  for (final variation in service.variations)
                    DropdownMenuItem(
                      value: variation,
                      child: Text(
                        variation.amount != null
                            ? '${variation.name} — ${Fmt.money(variation.amount!, 'NGN')}'
                            : variation.name,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
                onChanged: (value) => setState(() => _variation = value),
              ),
            ],
            if (_variation?.amount == null) ...[
              const SizedBox(height: 12),
              TextField(
                controller: _amount,
                keyboardType:
                    const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(
                    labelText: 'Amount (₦)', border: OutlineInputBorder()),
              ),
            ],
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(_error!,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
              ),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _busy ? null : _submit,
              child: _busy
                  ? const SizedBox(
                      height: 20,
                      width: 20,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Pay from wallet'),
            ),
            const SizedBox(height: 4),
            const Text(
              'If delivery fails, the money returns to your wallet instantly.',
              style: TextStyle(fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }
}
