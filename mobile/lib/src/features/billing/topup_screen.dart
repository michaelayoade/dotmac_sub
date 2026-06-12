import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/topup.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import 'payment_webview_screen.dart';

/// Prepaid account top-up: pick/enter an amount, complete the provider checkout
/// in a WebView, then verify and credit the account.
class TopUpScreen extends ConsumerStatefulWidget {
  const TopUpScreen({super.key});

  @override
  ConsumerState<TopUpScreen> createState() => _TopUpScreenState();
}

class _TopUpScreenState extends ConsumerState<TopUpScreen> {
  TopupPage? _page;
  Object? _loadError;
  bool _loadingPage = true;

  int? _selected;
  final _custom = TextEditingController();
  bool _busy = false;
  bool _saveCard = false;

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
      if (mounted) setState(() => _page = page);
    } catch (e) {
      if (mounted) setState(() => _loadError = e);
    } finally {
      if (mounted) setState(() => _loadingPage = false);
    }
  }

  int? get _amount =>
      _selected ?? int.tryParse(_custom.text.trim().replaceAll(',', ''));

  Future<void> _submit() async {
    final page = _page!;
    final amount = _amount;
    final messenger = ScaffoldMessenger.of(context);
    final router = GoRouter.of(context);
    if (amount == null || amount < page.minAmount || amount > page.maxAmount) {
      messenger.showSnackBar(SnackBar(
        content: Text(
            'Enter an amount between ${Fmt.money(page.minAmount, page.currency)} '
            'and ${Fmt.money(page.maxAmount, page.currency)}'),
      ));
      return;
    }
    setState(() => _busy = true);
    try {
      final initiation =
          await ref.read(billingRepositoryProvider).initiateTopup(amount);
      if (!mounted) return;
      final reference = await router.push<String>(
        '/pay',
        extra: CheckoutArgs.topup(initiation),
      );
      if (reference == null) return; // cancelled

      final result = await ref
          .read(billingRepositoryProvider)
          .verifyTopup(reference, saveCard: _saveCard);
      // Top-up credits the wallet — refresh balance + ledger + invoices.
      ref.invalidate(invoicesProvider);
      ref.invalidate(balanceProvider);
      ref.invalidate(ledgerProvider);
      messenger.showSnackBar(SnackBar(
        content: Text(result.availableBalance != null
            ? 'Topped up — balance ${Fmt.money(result.availableBalance!, page.currency)}'
            : 'Top-up of ${Fmt.money(result.amount, page.currency)} received'),
      ));
      await _loadPage();
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Top-up failed: $e')));
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
          onPressed: () =>
              context.canPop() ? context.pop() : context.go('/dashboard'),
        ),
      ),
      body: _loadingPage
          ? const Center(child: CircularProgressIndicator())
          : _page == null
              ? AsyncValueView(
                  value: AsyncValue<void>.error(
                      _loadError ?? 'error', StackTrace.empty),
                  data: (_) => const SizedBox.shrink(),
                  onRetry: _loadPage,
                )
              : _form(_page!),
    );
  }

  Widget _form(TopupPage page) {
    final theme = Theme.of(context);
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
        const SizedBox(height: 16),
        Text('Choose an amount', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final amt in page.presetAmounts)
              ChoiceChip(
                label: Text(Fmt.money(amt, page.currency)),
                selected: _selected == amt,
                onSelected: (_) => setState(() {
                  _selected = amt;
                  _custom.clear();
                }),
              ),
          ],
        ),
        const SizedBox(height: 16),
        TextField(
          controller: _custom,
          keyboardType: TextInputType.number,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly],
          onChanged: (_) => setState(() => _selected = null),
          decoration: InputDecoration(
            labelText: 'Or enter an amount',
            prefixText: '${page.currency} ',
            helperText:
                '${Fmt.money(page.minAmount, page.currency)} – ${Fmt.money(page.maxAmount, page.currency)}',
          ),
        ),
        // Saved cards are Paystack-only — the reusable authorization captured
        // on a successful charge is what powers saved cards and autopay.
        if (page.providerType == 'paystack')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Save this card'),
            subtitle: const Text('Use it for faster payments and autopay'),
            value: _saveCard,
            onChanged: _busy ? null : (v) => setState(() => _saveCard = v),
          ),
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: _busy || _amount == null ? null : _submit,
          icon: _busy
              ? const SizedBox(
                  height: 18,
                  width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.add_card_outlined),
          label: Text(_amount == null
              ? 'Top up'
              : 'Top up ${Fmt.money(_amount!, page.currency)}'),
        ),
      ],
    );
  }
}
