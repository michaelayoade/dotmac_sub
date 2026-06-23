import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/payment_errors.dart';
import '../../models/reseller.dart';
import '../../models/topup.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../billing/payment_webview_screen.dart';

/// Runs a reseller consolidated payment end-to-end: intent → gateway webview →
/// verify, with optional prefilled amount, a saved-card charge, and save-card.
/// Shared by the billing screen and the "Add card" entry on the payment-methods
/// screen so all reseller pay flows behave identically. Returns true on a
/// recorded payment.
Future<bool> runResellerPay(
  BuildContext context,
  WidgetRef ref, {
  String? prefillAmount,
  String? paymentMethodId,
  bool saveCard = false,
}) async {
  final messenger = ScaffoldMessenger.of(context);
  final controller = TextEditingController(text: prefillAmount ?? '');
  final amount = await showDialog<String>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Pay towards balance'),
      content: TextField(
        controller: controller,
        autofocus: true,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        decoration: const InputDecoration(
          labelText: 'Amount (NGN)',
          border: OutlineInputBorder(),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
          child: const Text('Continue'),
        ),
      ],
    ),
  );
  if (amount == null || amount.isEmpty) return false;
  // Block ≤0 and >2-decimal amounts before starting a checkout.
  final parsed = double.tryParse(amount.replaceAll(',', ''));
  if (parsed == null ||
      parsed <= 0 ||
      !RegExp(r'^\d+(\.\d{1,2})?$').hasMatch(amount.replaceAll(',', ''))) {
    messenger.showSnackBar(const SnackBar(
        content:
            Text('Enter an amount greater than 0 with at most 2 decimals.')));
    return false;
  }

  try {
    final repo = ref.read(resellerRepositoryProvider);
    final intent = await repo.payIntent(
      amount,
      paymentMethodId: paymentMethodId,
      saveCard: saveCard,
    );
    if (!context.mounted) return false;
    final reference = await context.push<String>(
      '/pay',
      extra: CheckoutArgs.resellerBilling(intent),
    );
    if (reference == null) return false; // cancelled in the webview
    await repo.payVerify(reference);
    ref.invalidate(resellerBillingProvider);
    // A save-card charge may have added a card; refresh that list too.
    ref.invalidate(resellerPaymentMethodsProvider);
    messenger.showSnackBar(
        const SnackBar(content: Text('Payment recorded — thank you.')));
    return true;
  } on ApiException catch (e) {
    messenger
        .showSnackBar(SnackBar(content: Text(PaymentError.from(e).message)));
    return false;
  } catch (_) {
    messenger.showSnackBar(const SnackBar(
        content: Text('Something went wrong — if you were charged, the payment '
            'will be reconciled automatically.')));
    return false;
  }
}

/// A copyable bank-account row (bank, account name, number) for transfers.
class _ResellerBankAccountCard extends StatelessWidget {
  const _ResellerBankAccountCard({required this.account});

  final BankAccount account;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.only(bottom: 6),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 4, 10),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(account.bankName,
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.outline)),
                  Text(account.accountNumber,
                      style: theme.textTheme.titleMedium
                          ?.copyWith(fontWeight: FontWeight.w700)),
                  Text(account.accountName, style: theme.textTheme.bodyMedium),
                ],
              ),
            ),
            IconButton(
              icon: const Icon(Icons.copy_outlined, size: 20),
              tooltip: 'Copy account number',
              onPressed: () {
                Clipboard.setData(ClipboardData(text: account.accountNumber));
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(content: Text('Account number copied')),
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// Bottom sheet to record a reseller bulk bank transfer, optionally net of
/// withholding tax. The account is credited the gross on staff verification and
/// the withheld tax is tracked as a receivable. Returns true on submit.
class _ResellerTransferSheet extends ConsumerStatefulWidget {
  const _ResellerTransferSheet({this.accounts = const [], this.instructions});

  final List<BankAccount> accounts;
  final String? instructions;

  @override
  ConsumerState<_ResellerTransferSheet> createState() =>
      _ResellerTransferSheetState();
}

class _ResellerTransferSheetState
    extends ConsumerState<_ResellerTransferSheet> {
  final _net = TextEditingController();
  final _rate = TextEditingController();
  final _bank = TextEditingController();
  final _reference = TextEditingController();
  XFile? _file;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _net.dispose();
    _rate.dispose();
    _bank.dispose();
    _reference.dispose();
    super.dispose();
  }

  double get _netValue => double.tryParse(_net.text.trim()) ?? 0;
  double get _rateValue => double.tryParse(_rate.text.trim()) ?? 0;
  double get _gross => (_rateValue > 0 && _rateValue < 100)
      ? _netValue / (1 - _rateValue / 100)
      : _netValue;
  double get _wht => _gross - _netValue;

  Future<void> _pick() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      imageQuality: 85,
    );
    if (picked != null) setState(() => _file = picked);
  }

  Future<void> _submit() async {
    if (_netValue <= 0 || _file == null) {
      setState(() =>
          _error = 'Enter the amount transferred and choose a receipt image.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(resellerRepositoryProvider).submitConsolidatedProof(
            amount: _net.text.trim(),
            whtRate: _rate.text.trim(),
            bankName: _bank.text.trim(),
            reference: _reference.text.trim(),
            filePath: _file!.path,
            fileName: _file!.name,
          );
      if (mounted) Navigator.of(context).pop(true);
    } catch (_) {
      setState(() {
        _busy = false;
        _error = 'Could not submit — check the details and try again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
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
            Text('Pay by bank transfer', style: theme.textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(
              'Transfer to our account, then upload the receipt. We verify it '
              'and credit your balance.',
              style: theme.textTheme.bodySmall,
            ),
            if (widget.accounts.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text('Transfer to', style: theme.textTheme.titleSmall),
              const SizedBox(height: 6),
              for (final acct in widget.accounts)
                _ResellerBankAccountCard(account: acct),
              if (widget.instructions != null &&
                  widget.instructions!.trim().isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(widget.instructions!,
                      style: theme.textTheme.bodySmall),
                ),
              const Divider(height: 24),
            ],
            const SizedBox(height: 12),
            TextField(
              controller: _net,
              keyboardType:
                  const TextInputType.numberWithOptions(decimal: true),
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(
                labelText: 'Amount transferred (NGN) *',
                helperText: 'Net cash you sent (after WHT)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _rate,
              keyboardType:
                  const TextInputType.numberWithOptions(decimal: true),
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(
                labelText: 'Withholding tax %',
                helperText: 'Leave blank if none',
                border: OutlineInputBorder(),
              ),
            ),
            if (_netValue > 0) ...[
              const SizedBox(height: 8),
              Text(
                'Credited (gross): ${Fmt.money(_gross, 'NGN')}'
                '${_wht > 0 ? '  ·  WHT receivable: ${Fmt.money(_wht, 'NGN')}' : ''}',
                style: theme.textTheme.bodyMedium
                    ?.copyWith(color: theme.colorScheme.primary),
              ),
            ],
            const SizedBox(height: 8),
            TextField(
              controller: _bank,
              decoration: const InputDecoration(
                labelText: 'From bank (optional)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _reference,
              decoration: const InputDecoration(
                labelText: 'Transfer reference (optional)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            OutlinedButton.icon(
              onPressed: _pick,
              icon: const Icon(Icons.photo_library_outlined, size: 18),
              label:
                  Text(_file == null ? 'Choose receipt image *' : _file!.name),
            ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: _busy ? null : () => Navigator.of(context).pop(),
                  child: const Text('Cancel'),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: _busy ? null : _submit,
                  child: Text(_busy ? 'Uploading…' : 'Submit receipt'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

/// Reseller consolidated billing: outstanding/unallocated totals, recent
/// payments, and a pay flow through the shared gateway webview
/// (GET /reseller/billing + pay intent/verify).
class ResellerBillingScreen extends ConsumerStatefulWidget {
  const ResellerBillingScreen({super.key});

  @override
  ConsumerState<ResellerBillingScreen> createState() =>
      _ResellerBillingScreenState();
}

class _ResellerBillingScreenState extends ConsumerState<ResellerBillingScreen> {
  bool _paying = false;

  Future<void> _pay({String? prefillAmount}) async {
    setState(() => _paying = true);
    try {
      await runResellerPay(context, ref, prefillAmount: prefillAmount);
    } finally {
      if (mounted) setState(() => _paying = false);
    }
  }

  Future<void> _payByTransfer(BankTransferConfig bankTransfer) async {
    final messenger = ScaffoldMessenger.of(context);
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _ResellerTransferSheet(
        accounts: bankTransfer.accounts,
        instructions: bankTransfer.instructions,
      ),
    );
    if (ok == true) {
      ref.invalidate(resellerBillingProvider);
      messenger.showSnackBar(const SnackBar(
        content: Text(
            'Receipt submitted — we will verify it and credit your account.'),
      ));
    }
  }

  @override
  Widget build(BuildContext context) {
    final billing = ref.watch(resellerBillingProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Billing'),
        actions: [
          IconButton(
            icon: const Icon(Icons.credit_card_outlined),
            tooltip: 'Payment methods',
            onPressed: () => context.push('/reseller/payment-methods'),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerBillingProvider);
          await ref.read(resellerBillingProvider.future);
        },
        child: AsyncValueView<ResellerBillingSummary>(
          value: billing,
          onRetry: () => ref.invalidate(resellerBillingProvider),
          data: (b) => ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Row(
                children: [
                  Expanded(
                    child: Card(
                      margin: EdgeInsets.zero,
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                Fmt.money(b.totalOutstanding, 'NGN'),
                                maxLines: 1,
                                style: Theme.of(context)
                                    .textTheme
                                    .titleMedium
                                    ?.copyWith(
                                      fontWeight: FontWeight.w700,
                                      color: b.totalOutstanding > 0
                                          ? Theme.of(context).colorScheme.error
                                          : null,
                                    ),
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text('Outstanding',
                                style: Theme.of(context).textTheme.bodySmall),
                          ],
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Card(
                      margin: EdgeInsets.zero,
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                Fmt.money(b.unallocatedBalance, 'NGN'),
                                maxLines: 1,
                                style: Theme.of(context)
                                    .textTheme
                                    .titleMedium
                                    ?.copyWith(fontWeight: FontWeight.w700),
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text('Unallocated credit',
                                style: Theme.of(context).textTheme.bodySmall),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              // One-tap "pay the full outstanding" prefills the amount; the
              // dialog still allows free-form entry of any other amount.
              if (b.totalOutstanding > 0)
                FilledButton.icon(
                  onPressed: _paying
                      ? null
                      : () => _pay(
                          prefillAmount: b.totalOutstanding.toStringAsFixed(2)),
                  icon: const Icon(Icons.payment, size: 18),
                  label: Text(_paying
                      ? 'Starting payment…'
                      : 'Pay outstanding ${Fmt.money(b.totalOutstanding, 'NGN')}'),
                ),
              if (b.totalOutstanding > 0) const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: _paying ? null : () => _pay(),
                icon: const Icon(Icons.payments_outlined, size: 18),
                label:
                    Text(_paying ? 'Starting payment…' : 'Pay another amount'),
              ),
              const SizedBox(height: 8),
              if (b.bankTransfer.hasAccounts)
                OutlinedButton.icon(
                  onPressed:
                      _paying ? null : () => _payByTransfer(b.bankTransfer),
                  icon: const Icon(Icons.account_balance_outlined, size: 18),
                  label: const Text('Pay by bank transfer'),
                ),
              const SizedBox(height: 16),
              Text('Activity', style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 8),
              if (b.recentPayments.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 16),
                  child: EmptyState(
                      icon: Icons.payments_outlined,
                      message: 'No payments yet'),
                )
              else
                for (final pmt in b.recentPayments)
                  Card(
                    margin: const EdgeInsets.only(bottom: 8),
                    child: ListTile(
                      dense: true,
                      leading: CircleAvatar(
                        radius: 18,
                        backgroundColor:
                            Theme.of(context).colorScheme.primaryContainer,
                        child: Icon(
                          Icons.south_west,
                          size: 18,
                          color:
                              Theme.of(context).colorScheme.onPrimaryContainer,
                        ),
                      ),
                      title: Text(
                        Fmt.money(pmt.amount, pmt.currency),
                        style: const TextStyle(fontWeight: FontWeight.w600),
                      ),
                      subtitle: Text([
                        'Payment',
                        if (pmt.method != null) pmt.method!,
                      ].join(' · ')),
                      trailing: pmt.receivedAt == null
                          ? null
                          : Text(
                              Fmt.date(pmt.receivedAt!),
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                    ),
                  ),
            ],
          ),
        ),
      ),
    );
  }
}
