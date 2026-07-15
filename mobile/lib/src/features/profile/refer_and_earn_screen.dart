import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../models/referral.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Refer & Earn — the customer's referral code, share link, earnings and
/// history (served from the sub's local mirror), plus refer-a-friend. Rewards
/// are issued as auditable account credits.
class ReferAndEarnScreen extends ConsumerWidget {
  const ReferAndEarnScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final referrals = ref.watch(referralsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Refer & Earn')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(referralsProvider);
          await ref.read(referralsProvider.future);
        },
        child: AsyncValueView<ReferralSummary>(
          value: referrals,
          onRetry: () => ref.invalidate(referralsProvider),
          data: (summary) => _Body(summary: summary),
        ),
      ),
    );
  }
}

class _Body extends ConsumerWidget {
  const _Body({required this.summary});

  final ReferralSummary summary;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Earned so far', style: theme.textTheme.labelMedium),
                const SizedBox(height: 4),
                Text(
                  '₦${summary.totals.totalEarned}',
                  style: theme.textTheme.headlineMedium?.copyWith(
                    fontWeight: FontWeight.bold,
                  ),
                ),
                if (summary.program.enabled &&
                    summary.program.rewardAmount != '0') ...[
                  const SizedBox(height: 8),
                  Text(
                    'Earn ₦${summary.program.rewardAmount} per friend who activates.',
                    style: theme.textTheme.bodyMedium,
                  ),
                ],
              ],
            ),
          ),
        ),
        const SizedBox(height: 12),
        if (summary.code.isNotEmpty)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Your referral link',
                    style: theme.textTheme.titleMedium,
                  ),
                  const SizedBox(height: 8),
                  SelectableText(
                    summary.shareUrl.isNotEmpty
                        ? summary.shareUrl
                        : summary.code,
                    style: theme.textTheme.bodyMedium?.copyWith(
                      fontFamily: 'monospace',
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: FilledButton.icon(
                          icon: const Icon(Icons.copy, size: 18),
                          label: const Text('Copy link'),
                          onPressed: () => _copy(
                            context,
                            summary.shareUrl.isNotEmpty
                                ? summary.shareUrl
                                : summary.code,
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: OutlinedButton.icon(
                          icon: const Icon(Icons.person_add_alt, size: 18),
                          label: const Text('Invite'),
                          onPressed: () => _showReferDialog(context, ref),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        const SizedBox(height: 12),
        Text('Your referrals', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        if (summary.referrals.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 32),
            child: Center(
              child: Text(
                'No referrals yet. Share your link to start earning.',
              ),
            ),
          )
        else
          ...summary.referrals.map((r) => _ReferralTile(item: r)),
      ],
    );
  }

  void _copy(BuildContext context, String text) {
    Clipboard.setData(ClipboardData(text: text));
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(const SnackBar(content: Text('Referral link copied')));
  }

  Future<void> _showReferDialog(BuildContext context, WidgetRef ref) async {
    final messenger = ScaffoldMessenger.of(context);
    final result = await showDialog<bool>(
      context: context,
      builder: (_) => const _ReferDialog(),
    );
    if (result == true) {
      ref.invalidate(referralsProvider);
      messenger.showSnackBar(
        const SnackBar(content: Text('Referral submitted')),
      );
    }
  }
}

class _ReferralTile extends StatelessWidget {
  const _ReferralTile({required this.item});

  final ReferralItem item;

  static const _statusColors = {
    'pending': Colors.grey,
    'qualified': Colors.blue,
    'rewarded': Colors.green,
    'rejected': Colors.red,
  };

  @override
  Widget build(BuildContext context) {
    final color = _statusColors[item.status] ?? Colors.grey;
    final date = item.createdAt;
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        title: Text(item.referredName ?? 'Invited friend'),
        subtitle: date == null
            ? null
            : Text(
                '${date.year}-${date.month.toString().padLeft(2, '0')}-'
                '${date.day.toString().padLeft(2, '0')}',
              ),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (item.status == 'rewarded' && item.rewardAmount != null) ...[
              Text(
                '₦${item.rewardAmount}',
                style: const TextStyle(fontWeight: FontWeight.bold),
              ),
              const SizedBox(width: 8),
            ],
            Chip(
              label: Text(item.status, style: const TextStyle(fontSize: 12)),
              backgroundColor: color.withValues(alpha: 0.15),
              side: BorderSide.none,
              materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              visualDensity: VisualDensity.compact,
            ),
          ],
        ),
      ),
    );
  }
}

class _ReferDialog extends ConsumerStatefulWidget {
  const _ReferDialog();

  @override
  ConsumerState<_ReferDialog> createState() => _ReferDialogState();
}

class _ReferDialogState extends ConsumerState<_ReferDialog> {
  final _name = TextEditingController();
  final _email = TextEditingController();
  final _phone = TextEditingController();
  bool _submitting = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    _email.dispose();
    _phone.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_email.text.trim().isEmpty && _phone.text.trim().isEmpty) {
      setState(() => _error = 'Enter an email or a phone number.');
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      await ref.read(referralRepositoryProvider).refer(
            name: _name.text.trim(),
            email: _email.text.trim(),
            phone: _phone.text.trim(),
          );
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      setState(() {
        _submitting = false;
        _error = e.message;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Refer a friend'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _name,
            decoration: const InputDecoration(labelText: 'Name (optional)'),
            textCapitalization: TextCapitalization.words,
          ),
          TextField(
            controller: _email,
            decoration: const InputDecoration(labelText: 'Email'),
            keyboardType: TextInputType.emailAddress,
          ),
          TextField(
            controller: _phone,
            decoration: const InputDecoration(labelText: 'Phone'),
            keyboardType: TextInputType.phone,
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Text(_error!, style: const TextStyle(color: Colors.red)),
          ],
        ],
      ),
      actions: [
        TextButton(
          onPressed:
              _submitting ? null : () => Navigator.of(context).pop(false),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _submitting ? null : _submit,
          child: _submitting
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : const Text('Send invite'),
        ),
      ],
    );
  }
}
