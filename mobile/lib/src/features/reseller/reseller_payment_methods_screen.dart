import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../models/payment_method.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import 'reseller_billing_screen.dart';

/// Manage the reseller's saved cards (GET/POST default/DELETE
/// /reseller/payment-methods). Cards appear here after a consolidated payment
/// where the reseller chose to save the card. Mirrors the customer
/// PaymentMethodsScreen.
class ResellerPaymentMethodsScreen extends ConsumerWidget {
  const ResellerPaymentMethodsScreen({super.key});

  Future<void> _setDefault(
      BuildContext context, WidgetRef ref, String id) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(resellerRepositoryProvider).setDefaultCard(id);
      ref.invalidate(resellerPaymentMethodsProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<void> _remove(
      BuildContext context, WidgetRef ref, SavedCard card) async {
    final messenger = ScaffoldMessenger.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Remove card'),
        content: Text('Remove ${card.title}?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Remove')),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await ref.read(resellerRepositoryProvider).removeCard(card.id);
      ref.invalidate(resellerPaymentMethodsProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  /// "Add card": run a consolidated payment with save-card enabled so the
  /// charge captures a reusable authorization.
  Future<void> _addCard(BuildContext context, WidgetRef ref) async {
    await runResellerPay(context, ref, saveCard: true);
  }

  /// Charge a saved card directly (passes payment_method_id to the intent).
  Future<void> _payWith(
      BuildContext context, WidgetRef ref, SavedCard card) async {
    await runResellerPay(context, ref, paymentMethodId: card.id);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cards = ref.watch(resellerPaymentMethodsProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Payment methods'),
        actions: [
          IconButton(
            icon: const Icon(Icons.add_card_outlined),
            tooltip: 'Add card',
            onPressed: () => _addCard(context, ref),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerPaymentMethodsProvider);
          await ref.read(resellerPaymentMethodsProvider.future);
        },
        child: AsyncValueView(
          value: cards,
          onRetry: () => ref.invalidate(resellerPaymentMethodsProvider),
          data: (list) {
            if (list.isEmpty) {
              return ListView(
                children: [
                  const SizedBox(height: 100),
                  const EmptyState(
                    icon: Icons.credit_card_outlined,
                    message:
                        'No saved cards yet.\nAdd a card by making a payment and choosing to save it.',
                  ),
                  const SizedBox(height: 16),
                  Center(
                    child: FilledButton.icon(
                      onPressed: () => _addCard(context, ref),
                      icon: const Icon(Icons.add_card_outlined),
                      label: const Text('Add card'),
                    ),
                  ),
                ],
              );
            }
            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                for (final c in list) ...[
                  Card(
                    margin: EdgeInsets.zero,
                    child: ListTile(
                      leading: const Icon(Icons.credit_card),
                      title: Text(c.title),
                      subtitle: Text([
                        if (c.expiry != null) 'Expires ${c.expiry}',
                        if (c.isDefault) 'Default',
                      ].join(' · ')),
                      trailing: PopupMenuButton<String>(
                        onSelected: (v) {
                          switch (v) {
                            case 'pay':
                              _payWith(context, ref, c);
                            case 'default':
                              _setDefault(context, ref, c.id);
                            case 'remove':
                              _remove(context, ref, c);
                          }
                        },
                        itemBuilder: (_) => [
                          const PopupMenuItem(
                              value: 'pay', child: Text('Pay with this card')),
                          if (!c.isDefault)
                            const PopupMenuItem(
                                value: 'default',
                                child: Text('Set as default')),
                          const PopupMenuItem(
                              value: 'remove', child: Text('Remove')),
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
