import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/subscription.dart';
import '../../widgets/status_chip.dart';

/// Full detail for one service: plan, connection (IP/login/MAC), validity
/// (start, expiry, days left) and billing mode. For prepaid services a Top-up
/// entry point is shown.
class ServiceDetailScreen extends StatelessWidget {
  const ServiceDetailScreen({super.key, required this.service});

  final Subscription service;

  @override
  Widget build(BuildContext context) {
    final s = service;
    return Scaffold(
      appBar: AppBar(title: const Text('Service')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  s.displayName,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
              StatusChip.forSubscription(s.status),
            ],
          ),
          if (s.planType != null) ...[
            const SizedBox(height: 4),
            Text(
              s.planType!,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: Theme.of(context).colorScheme.outline,
              ),
            ),
          ],
          const SizedBox(height: 16),
          _ExpiryCard(service: s),
          const SizedBox(height: 12),
          _Section(
            title: 'Connection',
            rows: [
              _Row('IPv4 address', s.ipv4Address, copyable: true),
              if (s.ipv6Address != null)
                _Row('IPv6 address', s.ipv6Address, copyable: true),
              if (s.login != null) _Row('Login', s.login),
              if (s.macAddress != null) _Row('MAC address', s.macAddress),
            ],
          ),
          const SizedBox(height: 12),
          _Section(
            title: 'Plan',
            rows: [
              _Row('Billing', s.isPrepaid ? 'Prepaid' : 'Postpaid'),
              _Row('Started', Fmt.date(s.startAt)),
              _Row('Expires', Fmt.date(s.expiresAt)),
            ],
          ),
          const SizedBox(height: 24),
          OutlinedButton.icon(
            onPressed: () =>
                context.push('/service/${s.id}/change-plan', extra: s),
            icon: const Icon(Icons.swap_horiz),
            label: const Text('Change plan'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () => context.push('/service/${s.id}/addons', extra: s),
            icon: const Icon(Icons.add_box_outlined),
            label: const Text('Add-ons'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () =>
                context.push('/service/${s.id}/buy-data', extra: s),
            icon: const Icon(Icons.data_usage),
            label: const Text('Buy data'),
          ),
          if (s.isPrepaid) ...[
            const SizedBox(height: 12),
            FilledButton.icon(
              onPressed: () => context.push('/topup'),
              icon: const Icon(Icons.add_card_outlined),
              label: const Text('Top up'),
            ),
          ],
        ],
      ),
    );
  }
}

class _ExpiryCard extends StatelessWidget {
  const _ExpiryCard({required this.service});
  final Subscription service;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final days = service.daysUntilExpiry;
    final (color, label) = switch (days) {
      null => (scheme.outline, 'Validity unknown'),
      < 0 => (scheme.error, 'Expired ${-days} day${days == -1 ? '' : 's'} ago'),
      0 => (scheme.error, 'Expires today'),
      <= 3 => (Colors.orange.shade800, '$days day${days == 1 ? '' : 's'} left'),
      _ => (Colors.green.shade700, '$days days left'),
    };
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Icon(Icons.schedule, color: color),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    label,
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      color: color,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  Text(
                    'Valid until ${Fmt.date(service.expiresAt)}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.rows});
  final String title;
  final List<Widget> rows;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: 4),
          child: Text(title, style: Theme.of(context).textTheme.titleSmall),
        ),
        Card(
          child: Column(
            children: [
              for (var i = 0; i < rows.length; i++) ...[
                if (i > 0) const Divider(height: 1),
                rows[i],
              ],
            ],
          ),
        ),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  const _Row(this.label, this.value, {this.copyable = false});
  final String label;
  final String? value;
  final bool copyable;

  @override
  Widget build(BuildContext context) {
    final v = value ?? '—';
    return ListTile(
      dense: true,
      title: Text(label),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Flexible(
            child: Text(
              v,
              textAlign: TextAlign.end,
              style: TextStyle(color: Theme.of(context).colorScheme.outline),
            ),
          ),
          if (copyable && value != null)
            IconButton(
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.copy, size: 16),
              tooltip: 'Copy',
              onPressed: () {
                Clipboard.setData(ClipboardData(text: value!));
                ScaffoldMessenger.of(
                  context,
                ).showSnackBar(SnackBar(content: Text('$label copied')));
              },
            ),
        ],
      ),
    );
  }
}
