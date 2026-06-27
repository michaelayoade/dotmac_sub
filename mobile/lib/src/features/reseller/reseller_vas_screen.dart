import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/vas.dart';
import '../../providers/data_providers.dart';
import '../../repositories/reseller_repository.dart';

/// Reseller VAS: float wallet, sell-for-customer, sales history.
/// Hidden unless the server flag is on (wallet endpoint 404 -> null).
class ResellerVasScreen extends ConsumerStatefulWidget {
  const ResellerVasScreen({super.key});

  @override
  ConsumerState<ResellerVasScreen> createState() => _ResellerVasScreenState();
}

class _ResellerVasScreenState extends ConsumerState<ResellerVasScreen> {
  Map<String, dynamic>? _wallet;
  List<VasCategory> _catalog = const [];
  List<Map<String, dynamic>> _sales = const [];
  bool _loading = true;
  Object? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final repo = ref.read(resellerRepositoryProvider);
      final wallet = await repo.vasWalletOrNull();
      if (wallet != null) {
        _catalog = await repo.vasCatalog();
        _sales = await repo.vasSales();
      }
      if (mounted) setState(() => _wallet = wallet);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  double get _balance =>
      double.tryParse((_wallet?['balance'] ?? '0').toString()) ?? 0;

  Future<void> _sell(VasService service) async {
    final sold = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      builder: (context) => _SellSheet(service: service),
    );
    if (sold == null || !mounted) return;
    final status = (sold['status'] ?? '').toString();
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(status == 'delivered'
            ? 'Delivered — commission credits shortly.'
            : status == 'refunded' || status == 'failed'
                ? 'Not delivered — float refunded.'
                : 'Processing — we keep checking automatically.')));
    _load();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Airtime & bills'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          tooltip: 'Back',
          onPressed: () =>
              context.canPop() ? context.pop() : context.go('/reseller'),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(
                  child: Text(_error is ApiException
                      ? (_error as ApiException).message
                      : 'Failed to load'))
              : _wallet == null
                  ? const Center(
                      child: Text('Airtime & bills are not available yet.'))
                  : RefreshIndicator(
                      onRefresh: _load,
                      child: ListView(
                        padding: const EdgeInsets.all(16),
                        children: [
                          Card(
                            child: ListTile(
                              leading: const Icon(Icons.wallet_outlined),
                              title: Text(Fmt.money(_balance, 'NGN')),
                              subtitle:
                                  const Text('Float — fund via the web portal'),
                            ),
                          ),
                          const SizedBox(height: 16),
                          for (final category in _catalog) ...[
                            Text(category.label,
                                style: theme.textTheme.titleMedium),
                            const SizedBox(height: 8),
                            for (final service in category.services)
                              Card(
                                child: ListTile(
                                  title: Text(service.name),
                                  trailing: const Icon(Icons.chevron_right),
                                  onTap: () => _sell(service),
                                ),
                              ),
                            const SizedBox(height: 8),
                          ],
                          if (_sales.isNotEmpty) ...[
                            const SizedBox(height: 8),
                            Text('Recent sales',
                                style: theme.textTheme.titleMedium),
                            for (final sale in _sales.take(15))
                              ListTile(
                                dense: true,
                                contentPadding: EdgeInsets.zero,
                                title: Text(
                                    '${sale['service_name'] ?? 'Sale'} · ${sale['identifier'] ?? ''}'),
                                subtitle: sale['commission_amount'] != null
                                    ? Text(
                                        'Commission ₦${sale['commission_amount']}')
                                    : null,
                                trailing: Text(
                                  (sale['status'] ?? '')
                                      .toString()
                                      .toUpperCase(),
                                  style: TextStyle(
                                    fontSize: 10,
                                    fontWeight: FontWeight.w700,
                                    color: sale['status'] == 'delivered'
                                        ? context.semantic.success
                                        : context.semantic.warning,
                                  ),
                                ),
                              ),
                          ],
                        ],
                      ),
                    ),
    );
  }
}

class _SellSheet extends ConsumerStatefulWidget {
  const _SellSheet({required this.service});

  final VasService service;

  @override
  ConsumerState<_SellSheet> createState() => _SellSheetState();
}

class _SellSheetState extends ConsumerState<_SellSheet> {
  final _identifier = TextEditingController();
  final _amount = TextEditingController();
  VasVariation? _variation;
  bool _busy = false;
  String? _verifiedName;
  String? _error;

  @override
  void dispose() {
    _identifier.dispose();
    _amount.dispose();
    super.dispose();
  }

  Future<void> _verify() async {
    setState(() => _error = null);
    try {
      final name = await ref.read(resellerRepositoryProvider).vasVerify(
            serviceId: widget.service.serviceId,
            identifier: _identifier.text.trim(),
          );
      if (mounted) setState(() => _verifiedName = name ?? 'Verified');
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  Future<void> _submit() async {
    final amount = _variation?.amount ??
        double.tryParse(_amount.text.trim().replaceAll(',', ''));
    if (_identifier.text.trim().isEmpty || amount == null || amount <= 0) {
      setState(() => _error = 'Customer number and amount are required.');
      return;
    }
    if (widget.service.requiresVerify && _verifiedName == null) {
      setState(() => _error = 'Check the customer name before selling.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final sold = await ref.read(resellerRepositoryProvider).vasSell(
            serviceId: widget.service.serviceId,
            identifier: _identifier.text.trim(),
            variationCode: _variation?.code,
            amount: _variation?.amount == null ? amount : null,
          );
      if (mounted) Navigator.of(context).pop(sold);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = e.message;
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
            Text('Sell ${service.name}',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 12),
            TextField(
              controller: _identifier,
              keyboardType: TextInputType.phone,
              decoration: InputDecoration(
                labelText: '${service.identifierLabel} *',
                border: const OutlineInputBorder(),
                suffixIcon: service.requiresVerify
                    ? TextButton(onPressed: _verify, child: const Text('Check'))
                    : null,
              ),
            ),
            if (_verifiedName != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(_verifiedName!,
                    style: TextStyle(
                        color: context.semantic.success,
                        fontWeight: FontWeight.w600)),
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
                  : const Text('Sell from float'),
            ),
          ],
        ),
      ),
    );
  }
}
