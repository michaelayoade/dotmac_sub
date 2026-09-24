import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/payment_proof.dart';
import '../../models/topup.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Open the receipt sheet for an already-created direct-transfer intent.
/// [accounts]/[instructions] show where to transfer and [initialAmount]
/// displays the server-owned amount.
enum TransferProofOutcome { submitted, canceled }

Future<TransferProofOutcome?> showSubmitProofSheet(
  BuildContext context, {
  required String intentId,
  String? initialAmount,
  List<BankAccount> accounts = const [],
  String? instructions,
}) {
  return showModalBottomSheet<TransferProofOutcome>(
    context: context,
    isScrollControlled: true,
    isDismissible: false,
    enableDrag: false,
    builder: (_) => SubmitProofSheet(
      intentId: intentId,
      initialAmount: initialAmount,
      accounts: accounts,
      instructions: instructions,
    ),
  );
}

/// Pay by bank transfer: upload the receipt, track verification. Verified
/// transfers are credited to the account (and applied to open invoices) by
/// our team.
class TransferProofsScreen extends ConsumerWidget {
  const TransferProofsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final proofs = ref.watch(paymentProofsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Bank transfer payments'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: 'Refresh',
            onPressed: () => ref.invalidate(paymentProofsProvider),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => context.push('/topup'),
        icon: const Icon(Icons.account_balance_outlined),
        label: const Text('Start transfer'),
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(paymentProofsProvider);
          await ref.read(paymentProofsProvider.future);
        },
        child: AsyncValueView<List<PaymentProofItem>>(
          value: proofs,
          onRetry: () => ref.invalidate(paymentProofsProvider),
          data: (items) => items.isEmpty
              ? ListView(
                  children: const [
                    Padding(
                      padding: EdgeInsets.all(24),
                      child: Text(
                        'Paid by bank transfer? Upload your receipt here and '
                        'we will verify it and credit your account — no card '
                        'or online payment needed.',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ],
                )
              : ListView(
                  padding: const EdgeInsets.all(12),
                  children: [
                    const Padding(
                      padding: EdgeInsets.fromLTRB(4, 4, 4, 12),
                      child: Text(
                        'Pull down or tap refresh to update status. Review '
                        'usually takes up to 1 business day.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ),
                    for (final p in items) _ProofTile(proof: p),
                  ],
                ),
        ),
      ),
    );
  }
}

class _ProofTile extends StatelessWidget {
  const _ProofTile({required this.proof});

  final PaymentProofItem proof;

  @override
  Widget build(BuildContext context) {
    final p = proof;
    final theme = Theme.of(context);
    final (icon, color) = switch (p.status) {
      'verified' => (Icons.check_circle, context.semantic.success),
      'rejected' => (Icons.cancel, theme.colorScheme.error),
      _ => (Icons.hourglass_top, theme.colorScheme.outline),
    };
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Icon(icon, color: color),
        title: Text(Fmt.money(p.amount, p.currency)),
        subtitle: Text(
          [
            if (p.invoiceNumber != null) 'Invoice ${p.invoiceNumber}',
            if (p.accountLabel != null) p.accountLabel!,
            if (p.bankName != null) p.bankName!,
            if (p.reference != null) p.reference!,
            if (p.createdAt != null) Fmt.date(p.createdAt!),
            if (p.status == 'rejected' && p.reviewNotes != null)
              'Reason: ${p.reviewNotes}',
          ].join(' · '),
        ),
        trailing: Text(
          p.status,
          style: theme.textTheme.labelMedium?.copyWith(
            color: color,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}

/// A copyable bank-account row (bank, account name, number) for transfers.
class _BankAccountCard extends StatelessWidget {
  const _BankAccountCard({
    required this.account,
    required this.selected,
    required this.onSelected,
  });

  final BankAccount account;
  final bool selected;
  final VoidCallback onSelected;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.only(bottom: 6),
      child: InkWell(
        onTap: onSelected,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 4, 10),
          child: Row(
            children: [
              Icon(
                selected ? Icons.radio_button_checked : Icons.radio_button_off,
                color: selected ? theme.colorScheme.primary : null,
              ),
              const SizedBox(width: 10),
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
                    Text(account.accountName,
                        style: theme.textTheme.bodyMedium),
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
      ),
    );
  }
}

class SubmitProofSheet extends ConsumerStatefulWidget {
  const SubmitProofSheet({
    super.key,
    required this.intentId,
    this.initialAmount,
    this.accounts = const [],
    this.instructions,
  });

  final String intentId;
  final String? initialAmount;
  final List<BankAccount> accounts;
  final String? instructions;

  @override
  ConsumerState<SubmitProofSheet> createState() => _SubmitProofSheetState();
}

class _SubmitProofSheetState extends ConsumerState<SubmitProofSheet> {
  late String? _selectedAccountId =
      widget.accounts.length == 1 ? widget.accounts.first.id : null;
  XFile? _file;
  bool _busy = false;
  String? _error;

  Future<void> _pick() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      imageQuality: 85,
    );
    if (picked != null) setState(() => _file = picked);
  }

  Future<void> _submit() async {
    if (_file == null) {
      setState(() => _error = 'Attach a receipt image.');
      return;
    }
    if (widget.accounts.length > 1 && _selectedAccountId == null) {
      setState(() => _error = 'Choose the Dotmac account you transferred to.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(billingRepositoryProvider).submitPaymentProof(
            filePath: _file!.path,
            fileName: _file!.name,
            intentId: widget.intentId,
            selectedAccountId: _selectedAccountId,
          );
      if (mounted) {
        Navigator.of(context).pop(TransferProofOutcome.submitted);
      }
    } catch (error) {
      setState(() {
        _busy = false;
        _error = error is ApiException
            ? error.message
            : 'Could not submit — check the details and try again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
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
            Text(
              'Upload transfer receipt',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            if (widget.accounts.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text(
                widget.accounts.length > 1
                    ? 'Choose the Dotmac account you transferred to'
                    : 'Transfer to',
                style: Theme.of(context).textTheme.titleSmall,
              ),
              const SizedBox(height: 6),
              if (widget.accounts.length > 1)
                RadioGroup<String>(
                  groupValue: _selectedAccountId,
                  onChanged: (value) {
                    if (_busy) return;
                    setState(() => _selectedAccountId = value);
                  },
                  child: Column(
                    children: [
                      for (final acct in widget.accounts)
                        RadioListTile<String>(
                          contentPadding: EdgeInsets.zero,
                          value: acct.id ?? acct.accountNumber,
                          title: Text('${acct.bankName} ${acct.accountNumber}'),
                          subtitle: Text(acct.accountName),
                        ),
                    ],
                  ),
                )
              else
                for (final acct in widget.accounts)
                  _BankAccountCard(
                    account: acct,
                    selected: true,
                    onSelected: () {},
                  ),
              if (widget.instructions != null &&
                  widget.instructions!.trim().isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4, bottom: 4),
                  child: Text(
                    widget.instructions!,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              const Divider(height: 24),
              Text(
                'Then upload your receipt below',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
            const SizedBox(height: 12),
            if (widget.initialAmount != null)
              Text('Amount: NGN ${widget.initialAmount}'),
            const SizedBox(height: 8),
            OutlinedButton.icon(
              onPressed: _pick,
              icon: const Icon(Icons.photo_library_outlined, size: 18),
              label: Text(
                _file == null ? 'Choose receipt image *' : _file!.name,
              ),
            ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(
                _error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => Navigator.of(context)
                          .pop(TransferProofOutcome.canceled),
                  child: const Text('Cancel transfer'),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: _busy ? null : _submit,
                  child: Text(_busy ? 'Uploading…' : 'Submit'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
