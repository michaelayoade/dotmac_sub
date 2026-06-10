import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../../core/formatters.dart';
import '../../models/payment_proof.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Pay by bank transfer: upload the receipt, track verification. Verified
/// transfers are credited to the account (and applied to open invoices) by
/// our team.
class TransferProofsScreen extends ConsumerWidget {
  const TransferProofsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final proofs = ref.watch(paymentProofsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Bank transfer payments')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () async {
          final ok = await showModalBottomSheet<bool>(
            context: context,
            isScrollControlled: true,
            builder: (_) => const _SubmitProofSheet(),
          );
          if (ok == true) {
            ref.invalidate(paymentProofsProvider);
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(
                  content: Text(
                    'Receipt submitted — we will verify it '
                    'and credit your account.',
                  ),
                ),
              );
            }
          }
        },
        icon: const Icon(Icons.upload_file),
        label: const Text('Upload receipt'),
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
                  children: [for (final p in items) _ProofTile(proof: p)],
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
      'verified' => (Icons.check_circle, Colors.green.shade700),
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

class _SubmitProofSheet extends ConsumerStatefulWidget {
  const _SubmitProofSheet();

  @override
  ConsumerState<_SubmitProofSheet> createState() => _SubmitProofSheetState();
}

class _SubmitProofSheetState extends ConsumerState<_SubmitProofSheet> {
  final _amount = TextEditingController();
  final _bank = TextEditingController();
  final _reference = TextEditingController();
  XFile? _file;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _amount.dispose();
    _bank.dispose();
    _reference.dispose();
    super.dispose();
  }

  Future<void> _pick() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      imageQuality: 85,
    );
    if (picked != null) setState(() => _file = picked);
  }

  Future<void> _submit() async {
    if (_amount.text.trim().isEmpty || _file == null) {
      setState(() => _error = 'Amount and a receipt image are both required.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(billingRepositoryProvider).submitPaymentProof(
            amount: _amount.text.trim(),
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
            const SizedBox(height: 12),
            TextField(
              controller: _amount,
              keyboardType: const TextInputType.numberWithOptions(
                decimal: true,
              ),
              decoration: const InputDecoration(
                labelText: 'Amount (NGN) *',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _bank,
              decoration: const InputDecoration(
                labelText: 'Bank',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _reference,
              decoration: const InputDecoration(
                labelText: 'Transfer reference',
                border: OutlineInputBorder(),
              ),
            ),
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
                  onPressed: _busy ? null : () => Navigator.of(context).pop(),
                  child: const Text('Cancel'),
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
