import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Full managed-accounts list: server-side search, status filters
/// (overdue / suspended), sort, and infinite scroll — the home screen only
/// shows the first few accounts, which doesn't scale to thousands.
class ResellerAccountsScreen extends ConsumerStatefulWidget {
  const ResellerAccountsScreen({super.key});

  @override
  ConsumerState<ResellerAccountsScreen> createState() =>
      _ResellerAccountsScreenState();
}

class _ResellerAccountsScreenState
    extends ConsumerState<ResellerAccountsScreen> {
  static const _pageSize = 50;

  final _scroll = ScrollController();
  final _searchController = TextEditingController();
  Timer? _debounce;

  String _search = '';
  String? _status; // null | 'overdue' | 'suspended'
  String _orderBy = 'created_at';

  final List<ResellerAccount> _items = [];
  int _total = 0;
  bool _loading = false;
  bool _initialLoad = true;
  String? _error;

  bool get _hasMore => _items.length < _total;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.extentAfter < 400 && !_loading && _hasMore) {
        _fetch();
      }
    });
    _fetch(reset: true);
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _scroll.dispose();
    _searchController.dispose();
    super.dispose();
  }

  Future<void> _fetch({bool reset = false}) async {
    if (_loading) return;
    setState(() {
      _loading = true;
      if (reset) {
        _initialLoad = _items.isEmpty;
        _error = null;
      }
    });
    try {
      final page = await ref
          .read(resellerRepositoryProvider)
          .accounts(
            search: _search,
            status: _status,
            orderBy: _orderBy,
            orderDir: _orderBy == 'name' ? 'asc' : 'desc',
            limit: _pageSize,
            offset: reset ? 0 : _items.length,
          );
      if (!mounted) return;
      setState(() {
        if (reset) _items.clear();
        _items.addAll(page.items);
        _total = page.count;
        _initialLoad = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = 'Could not load accounts.';
        _initialLoad = false;
      });
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _onSearchChanged(String value) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), () {
      _search = value;
      _fetch(reset: true);
    });
  }

  void _setStatus(String? status) {
    if (_status == status) return;
    setState(() => _status = status);
    _fetch(reset: true);
  }

  void _setOrder(String orderBy) {
    if (_orderBy == orderBy) return;
    setState(() => _orderBy = orderBy);
    _fetch(reset: true);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_total > 0 ? 'Accounts ($_total)' : 'Accounts'),
        actions: [
          PopupMenuButton<String>(
            tooltip: 'Sort',
            icon: const Icon(Icons.sort),
            initialValue: _orderBy,
            onSelected: _setOrder,
            itemBuilder: (_) => const [
              PopupMenuItem(value: 'created_at', child: Text('Newest')),
              PopupMenuItem(value: 'balance', child: Text('Open balance')),
              PopupMenuItem(value: 'overdue', child: Text('Overdue invoices')),
              PopupMenuItem(value: 'name', child: Text('Name')),
            ],
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
            child: TextField(
              controller: _searchController,
              onChanged: _onSearchChanged,
              decoration: InputDecoration(
                hintText: 'Search name, account or phone',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: _searchController.text.isEmpty
                    ? null
                    : IconButton(
                        icon: const Icon(Icons.clear),
                        onPressed: () {
                          _searchController.clear();
                          _onSearchChanged('');
                        },
                      ),
                isDense: true,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: Row(
              children: [
                FilterChip(
                  label: const Text('All'),
                  selected: _status == null,
                  onSelected: (_) => _setStatus(null),
                ),
                const SizedBox(width: 8),
                FilterChip(
                  label: const Text('Overdue'),
                  selected: _status == 'overdue',
                  onSelected: (_) => _setStatus('overdue'),
                ),
                const SizedBox(width: 8),
                FilterChip(
                  label: const Text('Suspended'),
                  selected: _status == 'suspended',
                  onSelected: (_) => _setStatus('suspended'),
                ),
              ],
            ),
          ),
          Expanded(child: _buildList()),
        ],
      ),
    );
  }

  Widget _buildList() {
    if (_initialLoad && _loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _items.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () => _fetch(reset: true),
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    if (_items.isEmpty) {
      return const EmptyState(
        icon: Icons.people_outline,
        message: 'No accounts match.',
      );
    }
    return RefreshIndicator(
      onRefresh: () => _fetch(reset: true),
      child: ListView.builder(
        controller: _scroll,
        padding: const EdgeInsets.all(12),
        itemCount: _items.length + (_hasMore ? 1 : 0),
        itemBuilder: (context, index) {
          if (index >= _items.length) {
            return const Padding(
              padding: EdgeInsets.symmetric(vertical: 16),
              child: Center(child: CircularProgressIndicator()),
            );
          }
          return _AccountRow(account: _items[index]);
        },
      ),
    );
  }
}

class _AccountRow extends StatelessWidget {
  const _AccountRow({required this.account});

  final ResellerAccount account;

  @override
  Widget build(BuildContext context) {
    final title = account.subscriberName.isEmpty
        ? (account.accountNumber ?? account.id)
        : account.subscriberName;
    final lastPaid = account.lastPaymentAt == null
        ? ''
        : ' · paid ${Fmt.date(account.lastPaymentAt)}';
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        onTap: () =>
            context.push('/reseller/accounts/${account.id}', extra: title),
        title: Text(title),
        subtitle: Text(
          '${account.status} · ${account.openInvoices} open$lastPaid',
        ),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(Fmt.moneyCompact(account.openBalance, 'NGN')),
            const Icon(Icons.chevron_right),
          ],
        ),
      ),
    );
  }
}
