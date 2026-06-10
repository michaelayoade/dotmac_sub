import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../providers/impersonation.dart';
import '../../widgets/async_value_view.dart';

/// A reseller's drill-down into one managed customer account: profile,
/// subscriptions and invoices. All data is scoped server-side to the reseller
/// (404 for accounts that aren't theirs).
class ResellerAccountScreen extends ConsumerWidget {
  const ResellerAccountScreen({
    super.key,
    required this.accountId,
    this.title,
  });

  final String accountId;
  final String? title;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = ref.watch(resellerAccountProvider(accountId));
    final invoices = ref.watch(resellerAccountInvoicesProvider(accountId));
    final tickets = ref.watch(resellerAccountTicketsProvider(accountId));

    return Scaffold(
      appBar: AppBar(title: Text(title ?? 'Account'), actions: [
        IconButton(
          tooltip: 'View as customer (read-only)',
          icon: const Icon(Icons.supervisor_account_outlined),
          onPressed: () async {
            final messenger = ScaffoldMessenger.of(context);
            try {
              await ref.read(impersonationProvider.notifier).start(accountId);
              if (context.mounted) context.go('/dashboard');
            } catch (_) {
              messenger.showSnackBar(const SnackBar(
                  content: Text('Could not start customer view.')));
            }
          },
        ),
      ]),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerAccountProvider(accountId));
          ref.invalidate(resellerAccountInvoicesProvider(accountId));
          await ref.read(resellerAccountProvider(accountId).future);
        },
        child: AsyncValueView<ResellerAccountDetail>(
          value: detail,
          onRetry: () => ref.invalidate(resellerAccountProvider(accountId)),
          data: (account) => ListView(
            padding: const EdgeInsets.all(12),
            children: [
              _ProfileCard(account: account),
              const SizedBox(height: 16),
              Text('Subscriptions',
                  style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 8),
              if (account.subscriptions.isEmpty)
                const Card(
                  child: ListTile(title: Text('No subscriptions')),
                )
              else
                for (final s in account.subscriptions)
                  Card(
                    margin: const EdgeInsets.only(bottom: 8),
                    child: ListTile(
                      title: Text(s.offerName),
                      subtitle: Text('Since ${Fmt.date(s.startDate)}'),
                      trailing: _StatusChip(status: s.status),
                    ),
                  ),
              const SizedBox(height: 16),
              Text('Invoices', style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 8),
              invoices.when(
                loading: () => const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: CircularProgressIndicator()),
                ),
                error: (_, __) => const Card(
                  child: ListTile(title: Text('Could not load invoices')),
                ),
                data: (items) => items.isEmpty
                    ? const Card(child: ListTile(title: Text('No invoices')))
                    : Column(
                        children: [
                          for (final i in items) _InvoiceTile(invoice: i)
                        ],
                      ),
              ),
              const SizedBox(height: 16),
              Text('Support tickets',
                  style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 8),
              tickets.when(
                loading: () => const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: CircularProgressIndicator()),
                ),
                error: (_, __) => const Card(
                  child: ListTile(title: Text('Could not load tickets')),
                ),
                data: (page) => !page.crmAvailable
                    ? const Card(
                        child: ListTile(
                            title: Text('Ticket system unavailable right now')),
                      )
                    : page.items.isEmpty
                        ? const Card(child: ListTile(title: Text('No tickets')))
                        : Column(
                            children: [
                              for (final t in page.items) _TicketTile(ticket: t)
                            ],
                          ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ProfileCard extends StatelessWidget {
  const _ProfileCard({required this.account});

  final ResellerAccountDetail account;

  @override
  Widget build(BuildContext context) {
    final name = account.subscriberName.isEmpty
        ? (account.accountNumber ?? account.id)
        : account.subscriberName;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(name,
                      style: Theme.of(context).textTheme.titleMedium),
                ),
                _StatusChip(status: account.status),
              ],
            ),
            const SizedBox(height: 8),
            if (account.accountNumber != null)
              _Row(label: 'Account', value: account.accountNumber!),
            if (account.email != null)
              _Row(label: 'Email', value: account.email!),
            if (account.phone != null)
              _Row(label: 'Phone', value: account.phone!),
            const Divider(),
            _Row(
              label: 'Open balance',
              value: Fmt.money(account.openBalance, 'NGN'),
              emphasise: account.openBalance > 0,
            ),
          ],
        ),
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row(
      {required this.label, required this.value, this.emphasise = false});

  final String label;
  final String value;
  final bool emphasise;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: theme.textTheme.bodySmall),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              value,
              textAlign: TextAlign.right,
              style: emphasise
                  ? theme.textTheme.bodyMedium
                      ?.copyWith(color: theme.colorScheme.error)
                  : theme.textTheme.bodyMedium,
            ),
          ),
        ],
      ),
    );
  }
}

class _InvoiceTile extends StatelessWidget {
  const _InvoiceTile({required this.invoice});

  final ResellerInvoiceSummary invoice;

  @override
  Widget build(BuildContext context) {
    final label = invoice.invoiceNumber ?? invoice.id;
    final theme = Theme.of(context);
    // A plain Row instead of ListTile: the trailing budget is too tight for
    // "NGN 200,000.00 due" and overflows.
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(label, maxLines: 1, overflow: TextOverflow.ellipsis),
                  const SizedBox(height: 2),
                  Text(
                    'Due ${Fmt.date(invoice.dueDate)} · ${invoice.status}',
                    style: theme.textTheme.bodySmall,
                  ),
                ],
              ),
            ),
            const SizedBox(width: 12),
            Column(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(Fmt.money(invoice.totalAmount, 'NGN')),
                if (invoice.balanceDue > 0)
                  Text(
                    '${Fmt.money(invoice.balanceDue, 'NGN')} due',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.error),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.status});

  final String status;

  @override
  Widget build(BuildContext context) {
    final active = status.toLowerCase() == 'active';
    final color = active ? Colors.green : Colors.grey;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(status, style: TextStyle(color: color, fontSize: 12)),
    );
  }
}

class _TicketTile extends StatelessWidget {
  const _TicketTile({required this.ticket});

  final ResellerTicket ticket;

  @override
  Widget build(BuildContext context) {
    final t = ticket;
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        dense: true,
        leading: Icon(
          t.isOpen
              ? Icons.confirmation_number_outlined
              : Icons.check_circle_outline,
          color: t.isOpen ? theme.colorScheme.primary : null,
        ),
        title: Text(t.subject, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text([
          if (t.status != null) t.status!.replaceAll('_', ' '),
          if (t.createdAt != null) Fmt.date(t.createdAt!),
        ].join(' · ')),
        trailing: t.priority == null
            ? null
            : Text(t.priority!, style: theme.textTheme.labelSmall),
      ),
    );
  }
}
