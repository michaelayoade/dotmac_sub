import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../models/payment_method.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Autopay opt-in. Hidden when the status endpoint isn't available (e.g. before
/// the autopay migration is applied) so the card list still works.
class _AutopayTile extends ConsumerStatefulWidget {
  const _AutopayTile();

  @override
  ConsumerState<_AutopayTile> createState() => _AutopayTileState();
}

class _AutopayTileState extends ConsumerState<_AutopayTile> {
  bool _busy = false;

  Future<void> _toggle(bool on) async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final repo = ref.read(billingRepositoryProvider);
      on ? await repo.enableAutopay() : await repo.disableAutopay();
      ref.invalidate(autopayStatusProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final status = ref.watch(autopayStatusProvider);
    return status.maybeWhen(
      data: (s) => Card(
        margin: EdgeInsets.zero,
        child: SwitchListTile(
          secondary: const Icon(Icons.autorenew),
          title: const Text('Autopay'),
          subtitle: const Text(
            'Automatically pay invoices from your default card',
          ),
          value: s.enabled,
          onChanged: _busy ? null : _toggle,
        ),
      ),
      orElse: () => const SizedBox.shrink(),
    );
  }
}

/// Manage saved cards (GET/PATCH/DELETE /me/payment-methods). Cards appear here
/// after a card payment where the customer chose to save it.
class PaymentMethodsScreen extends ConsumerWidget {
  const PaymentMethodsScreen({super.key});

  Future<void> _setDefault(
    BuildContext context,
    WidgetRef ref,
    String id,
  ) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(billingRepositoryProvider).setDefaultCard(id);
      ref.invalidate(paymentMethodsProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<void> _remove(
    BuildContext context,
    WidgetRef ref,
    SavedCard card,
  ) async {
    final messenger = ScaffoldMessenger.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Remove card'),
        content: Text('Remove ${card.title}?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await ref.read(billingRepositoryProvider).removeCard(card.id);
      ref.invalidate(paymentMethodsProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cards = ref.watch(paymentMethodsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Payment methods')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(paymentMethodsProvider);
          await ref.read(paymentMethodsProvider.future);
        },
        child: AsyncValueView(
          value: cards,
          onRetry: () => ref.invalidate(paymentMethodsProvider),
          data: (list) {
            if (list.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 100),
                  EmptyState(
                    icon: Icons.credit_card_outlined,
                    message:
                        'No saved cards yet.\nWhen you pay by card you can choose to save it here.',
                  ),
                ],
              );
            }
            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                const _AutopayTile(),
                const SizedBox(height: 8),
                for (final c in list) ...[
                  Card(
                    margin: EdgeInsets.zero,
                    child: ListTile(
                      leading: const Icon(Icons.credit_card),
                      title: Text(c.title),
                      subtitle: Text(
                        [
                          if (c.expiry != null) 'Expires ${c.expiry}',
                          if (c.isDefault) 'Default',
                        ].join(' · '),
                      ),
                      trailing: PopupMenuButton<String>(
                        onSelected: (v) => v == 'default'
                            ? _setDefault(context, ref, c.id)
                            : _remove(context, ref, c),
                        itemBuilder: (_) => [
                          if (!c.isDefault)
                            const PopupMenuItem(
                              value: 'default',
                              child: Text('Set as default'),
                            ),
                          const PopupMenuItem(
                            value: 'remove',
                            child: Text('Remove'),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                ],
              ],
            );
          },
        ),
      ),
    );
  }
}
