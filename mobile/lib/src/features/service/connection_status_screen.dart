import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/connection_status.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';

/// "What's wrong with my connection?" — the outage classifier's per-customer
/// verdict (GET /me/connection-status). Shows the state, a plain-language
/// explanation, and the ONE action to take (when there is one). When the
/// customer is under a known area outage the server suppresses self-blame
/// advice and we show the reassuring "we're on it" treatment.
class ConnectionStatusScreen extends ConsumerWidget {
  const ConnectionStatusScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final status = ref.watch(connectionStatusProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Connection status')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(connectionStatusProvider);
          await ref.read(connectionStatusProvider.future);
        },
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            AsyncValueView<ConnectionStatus>(
              value: status,
              onRetry: () => ref.invalidate(connectionStatusProvider),
              skeleton: const CardSkeleton(height: 180),
              data: (s) => _ConnectionCard(status: s),
            ),
          ],
        ),
      ),
    );
  }
}

class _ConnectionCard extends StatelessWidget {
  const _ConnectionCard({required this.status});

  final ConnectionStatus status;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visual = statusPresentationVisual(context, status.statusPresentation);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(visual.icon, color: visual.color, size: 32),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        status.headline,
                        style: theme.textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Text(status.message, style: theme.textTheme.bodyMedium),
                if (status.areaOutage) ...[
                  const SizedBox(height: 12),
                  _AreaOutageNote(color: visual.color),
                ],
              ],
            ),
          ),
        ),
        // Server suppresses `advice` under an area outage; the guard is belt-
        // and-suspenders so the UI never self-blames during a known outage.
        if (status.advice != null && !status.areaOutage) ...[
          const SizedBox(height: 12),
          Card(
            color: theme.colorScheme.surfaceContainerHighest,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.lightbulb_outline,
                      color: theme.colorScheme.primary, size: 22),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      status.advice!,
                      style: theme.textTheme.bodyMedium,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
        const SizedBox(height: 12),
        Text(
          _checkedLabel(status.checkedAt),
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
      ],
    );
  }
}

class _AreaOutageNote extends StatelessWidget {
  const _AreaOutageNote({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Icon(Icons.info_outline, color: color, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              "This is a known outage in your area — you don't need to do "
              'anything.',
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
                  ?.copyWith(color: color, fontWeight: FontWeight.w600),
            ),
          ),
        ],
      ),
    );
  }
}

/// "Checked just now / 5 min ago / 2 h ago" — pull-to-refresh recomputes it.
String _checkedLabel(DateTime? at) {
  if (at == null) return 'Pull down to refresh';
  final d = DateTime.now().difference(at);
  if (d.inSeconds < 60) return 'Checked just now';
  if (d.inMinutes < 60) return 'Checked ${d.inMinutes} min ago';
  if (d.inHours < 24) return 'Checked ${d.inHours} h ago';
  return 'Checked ${d.inDays} d ago';
}
