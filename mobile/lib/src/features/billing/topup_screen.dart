import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/payment_errors.dart';
import '../../models/payment_method.dart';
import '../../models/topup.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import 'payment_webview_screen.dart';
import 'transfer_proofs_screen.dart';

/// Prepaid account top-up: pick/enter an amount, complete the provider checkout
/// in a WebView, then verify and credit the account.
class TopUpScreen extends ConsumerStatefulWidget {
  const TopUpScreen({super.key, this.saveCardInitial = false});

  /// When true (e.g. launched from "Add card"), the Paystack "Save this card"
  /// toggle starts ON so a top-up doubles as saving a card.
  final bool saveCardInitial;

  @override
  ConsumerState<TopUpScreen> createState() => _TopUpScreenState();
}

class _TopUpScreenState extends ConsumerState<TopUpScreen> {
  TopupPage? _page;
  Object? _loadError;
  bool _loadingPage = true;

  final _custom = TextEditingController();
  bool _busy = false;
  late bool _saveCard = widget.saveCardInitial;

  /// The selected pay method, encoded as one of:
  ///  - `card:<id>`   — charge a saved card server-side (one-tap)
  ///  - `gw:<type>`   — new card via a gateway ('paystack'/'flutterwave')
  ///  - `transfer`    — direct bank transfer + upload receipt
  /// Null until the page + saved cards load and a default is chosen.
  String? _selection;

  /// Pull the saved-card id out of a `card:<id>` selection (else null).
  String? get _selectedCardId =>
      _selection != null && _selection!.startsWith('card:')
      ? _selection!.substring('card:'.length)
      : null;

  /// The gateway type for a `gw:<type>` selection (else null).
  String? get _selectedGateway =>
      _selection != null && _selection!.startsWith('gw:')
      ? _selection!.substring('gw:'.length)
      : null;

  bool get _isTransfer => _selection == 'transfer';

  @override
  void initState() {
    super.initState();
    _loadPage();
  }

  @override
  void dispose() {
    _custom.dispose();
    super.dispose();
  }

  Future<void> _loadPage() async {
    setState(() {
      _loadingPage = true;
      _loadError = null;
    });
    try {
      final page = await ref.read(billingRepositoryProvider).topupPage();
      if (mounted) {
        setState(() {
          _page = page;
          // Default to the configured online gateway (listed first by the API).
          _selection ??= page.providers.isNotEmpty
              ? 'gw:${page.providers.first.providerType}'
              : 'gw:${page.providerType}';
        });
      }
    } catch (e) {
      if (mounted) setState(() => _loadError = e);
    } finally {
      if (mounted) setState(() => _loadingPage = false);
    }
  }

  int? get _amount => int.tryParse(_custom.text.trim().replaceAll(',', ''));

  bool get _amountValid {
    final page = _page;
    final amount = _amount;
    return page != null &&
        amount != null &&
        amount >= page.minAmount &&
        amount <= page.maxAmount;
  }

  Future<void> _submit() async {
    final page = _page!;
    final amount = _amount;
    final messenger = ScaffoldMessenger.of(context);
    final router = GoRouter.of(context);
    if (amount == null || amount < page.minAmount || amount > page.maxAmount) {
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            'Enter an amount between ${Fmt.money(page.minAmount, page.currency)} '
            'and ${Fmt.money(page.maxAmount, page.currency)}',
          ),
        ),
      );
      return;
    }

    // Bank transfer: show the account(s) + collect the receipt; staff verify
    // and credit the wallet. No gateway / verify round-trip here.
    if (_isTransfer) {
      final ok = await showSubmitProofSheet(
        context,
        initialAmount: amount.toString(),
        accounts: page.bankTransfer.accounts,
        instructions: page.bankTransfer.instructions,
      );
      if (ok == true && mounted) {
        ref.invalidate(paymentProofsProvider);
        messenger.showSnackBar(
          const SnackBar(
            content: Text(
              'Receipt submitted — we will verify it and credit your account.',
            ),
          ),
        );
      }
      return;
    }

    setState(() => _busy = true);
    try {
      final cardId = _selectedCardId;
      final initiation = await ref
          .read(billingRepositoryProvider)
          .initiateTopup(
            amount,
            provider: cardId == null ? _selectedGateway : null,
            paymentMethodId: cardId,
            // One key per attempt makes a saved-card charge safe against a
            // Dio retry; the button busy-guard covers double-taps.
            idempotencyKey: cardId == null
                ? null
                : 'topup-${DateTime.now().microsecondsSinceEpoch}-'
                      '${Random().nextInt(0x7fffffff)}',
          );
      if (!mounted) return;

      String reference;
      if (initiation.charged) {
        // Saved card was charged server-side — skip the gateway webview.
        reference = initiation.paymentReference;
      } else {
        final ref0 = await router.push<String>(
          '/pay',
          extra: CheckoutArgs.topup(initiation),
        );
        if (ref0 == null) return; // cancelled
        reference = ref0;
      }

      final result = await ref
          .read(billingRepositoryProvider)
          .verifyTopup(
            reference,
            // "Save this card" only applies to a brand-new Paystack card.
            saveCard:
                cardId == null && _selectedGateway == 'paystack' && _saveCard,
          );
      // Top-up credits the wallet — refresh balance + ledger + invoices.
      ref.invalidate(invoicesProvider);
      ref.invalidate(balanceProvider);
      ref.invalidate(ledgerProvider);
      ref.invalidate(paymentMethodsProvider);
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            result.availableBalance != null
                ? 'Topped up — balance ${Fmt.money(result.availableBalance!, page.currency)}'
                : 'Top-up of ${Fmt.money(result.amount, page.currency)} received',
          ),
        ),
      );
      await _loadPage();
    } on ApiException catch (e) {
      if (mounted) showPaymentError(context, e, onRetry: _submit);
    } catch (e) {
      if (mounted) showPaymentError(context, e, onRetry: _submit);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Top up'),
        // Pushed entries pop back automatically; a deep link / cold start
        // has no stack, so fall back to the dashboard instead of a dead end.
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          tooltip: 'Back',
          onPressed: () =>
              context.canPop() ? context.pop() : context.go('/dashboard'),
        ),
      ),
      body: _loadingPage
          ? const Center(child: CircularProgressIndicator())
          : _page == null
          ? AsyncValueView(
              value: AsyncValue<void>.error(
                _loadError ?? 'error',
                StackTrace.empty,
              ),
              data: (_) => const SizedBox.shrink(),
              onRetry: _loadPage,
            )
          : _form(_page!),
    );
  }

  String _payLabel(TopupPage page) {
    if (_isTransfer) {
      return _amount == null
          ? 'Pay by transfer'
          : 'Pay ${Fmt.money(_amount!, page.currency)} by transfer';
    }
    final verb = _selectedCardId == null ? 'Top up' : 'Pay';
    return _amount == null
        ? verb
        : '$verb ${Fmt.money(_amount!, page.currency)}';
  }

  Widget _methodTile({
    required String value,
    required IconData icon,
    required String title,
    String? subtitle,
  }) {
    final theme = Theme.of(context);
    final selected = _selection == value;
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: Icon(icon),
      title: Text(title),
      subtitle: subtitle == null ? null : Text(subtitle),
      trailing: selected
          ? Icon(Icons.check_circle, color: theme.colorScheme.primary)
          : const Icon(Icons.radio_button_unchecked),
      selected: selected,
      onTap: _busy ? null : () => setState(() => _selection = value),
    );
  }

  Widget _form(TopupPage page) {
    final theme = Theme.of(context);
    final savedCards =
        ref.watch(paymentMethodsProvider).asData?.value ?? const <SavedCard>[];
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (page.prepaidBalance != null)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Current balance'),
                  const SizedBox(height: 4),
                  Text(
                    Fmt.money(page.prepaidBalance!, page.currency),
                    style: theme.textTheme.headlineSmall?.copyWith(
                      color: page.prepaidBalance! < 0
                          ? theme.colorScheme.error
                          : theme.colorScheme.primary,
                    ),
                  ),
                ],
              ),
            ),
          ),
        const SizedBox(height: 24),
        Text('Enter an amount', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        TextField(
          controller: _custom,
          autofocus: true,
          keyboardType: TextInputType.number,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly],
          onChanged: (_) => setState(() {}),
          decoration: InputDecoration(
            labelText: 'Amount',
            prefixText: '${page.currency} ',
            helperText:
                '${Fmt.money(page.minAmount, page.currency)} – ${Fmt.money(page.maxAmount, page.currency)}',
          ),
        ),
        // --- Pay with: saved card (one-tap), an online gateway, or transfer ---
        const SizedBox(height: 8),
        Text('Pay with', style: theme.textTheme.titleSmall),
        for (final c in savedCards)
          _methodTile(
            value: 'card:${c.id}',
            icon: Icons.credit_card,
            title: c.label ?? '${c.brand ?? 'Card'} •••• ${c.last4 ?? ''}',
            subtitle: (c.expiresMonth != null && c.expiresYear != null)
                ? 'Expires '
                      '${c.expiresMonth!.toString().padLeft(2, '0')}/${c.expiresYear}'
                : null,
          ),
        for (final p in page.providers)
          _methodTile(
            value: 'gw:${p.providerType}',
            icon: Icons.add_card_outlined,
            title: p.label,
          ),
        if (page.bankTransfer.hasAccounts)
          _methodTile(
            value: 'transfer',
            icon: Icons.account_balance_outlined,
            title: 'Bank transfer',
            subtitle: 'Show account details and upload your receipt',
          ),
        // "Save this card" only matters for a brand-new Paystack card.
        if (_selectedGateway == 'paystack')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Save this card'),
            subtitle: const Text('Use it for faster payments and autopay'),
            value: _saveCard,
            onChanged: _busy ? null : (v) => setState(() => _saveCard = v),
          ),
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: _busy || !_amountValid || _selection == null
              ? null
              : _submit,
          icon: _busy
              ? const SizedBox(
                  height: 18,
                  width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : Icon(
                  _isTransfer
                      ? Icons.account_balance_outlined
                      : _selectedCardId == null
                      ? Icons.add_card_outlined
                      : Icons.bolt_outlined,
                ),
          label: Text(_payLabel(page)),
        ),
      ],
    );
  }
}
