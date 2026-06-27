import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';

/// Real-time throughput section. Off by default (on-demand): tapping Start opens
/// a live stream of the subscriber's current download/upload from
/// `/me/bandwidth/stats`; Stop ends the polling. Replaces the old connection-
/// banner "Go live" toggle with a visible, self-contained section.
class LiveBandwidthCard extends ConsumerWidget {
  const LiveBandwidthCard({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final enabled = ref.watch(liveBandwidthEnabledProvider);
    final live = ref.watch(liveBandwidthProvider);
    final v = live.asData?.value;
    final waiting = enabled && (live.isLoading || !(v?.hasSignal ?? false));

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Row(
                  children: [
                    Icon(Icons.speed,
                        size: 18, color: theme.colorScheme.primary),
                    const SizedBox(width: 6),
                    Text('Live bandwidth', style: theme.textTheme.titleMedium),
                  ],
                ),
                FilledButton.tonalIcon(
                  onPressed: () => ref
                      .read(liveBandwidthEnabledProvider.notifier)
                      .update((on) => !on),
                  icon: Icon(enabled ? Icons.stop : Icons.play_arrow, size: 18),
                  label: Text(enabled ? 'Stop' : 'Start'),
                ),
              ],
            ),
            const SizedBox(height: 16),
            if (!enabled)
              Text(
                'Tap Start to measure your connection speed in real time.',
                style: theme.textTheme.bodyMedium
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              )
            else ...[
              Row(
                children: [
                  Expanded(
                    child: _Meter(
                      label: 'Download',
                      icon: Icons.south,
                      bps: v?.downloadBps,
                      waiting: waiting,
                      color: theme.colorScheme.primary,
                    ),
                  ),
                  Expanded(
                    child: _Meter(
                      label: 'Upload',
                      icon: Icons.north,
                      bps: v?.uploadBps,
                      waiting: waiting,
                      color: theme.colorScheme.tertiary,
                    ),
                  ),
                ],
              ),
              if ((v?.peakDownloadBps ?? 0) > 0 ||
                  (v?.peakUploadBps ?? 0) > 0) ...[
                const SizedBox(height: 12),
                Text(
                  'Peak (last hour)  ↓ ${Fmt.bps(v?.peakDownloadBps)}   ↑ ${Fmt.bps(v?.peakUploadBps)}',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                ),
              ],
              if (waiting) ...[
                const SizedBox(height: 12),
                Row(
                  children: [
                    const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                    const SizedBox(width: 8),
                    Text('Measuring…',
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant)),
                  ],
                ),
              ],
            ],
          ],
        ),
      ),
    );
  }
}

class _Meter extends StatelessWidget {
  const _Meter({
    required this.label,
    required this.icon,
    required this.bps,
    required this.waiting,
    required this.color,
  });

  final String label;
  final IconData icon;
  final double? bps;
  final bool waiting;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(icon, size: 14, color: color),
            const SizedBox(width: 4),
            Text(label,
                style: theme.textTheme.labelMedium
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ],
        ),
        const SizedBox(height: 4),
        Text(
          waiting ? '—' : Fmt.bps(bps),
          style: theme.textTheme.headlineSmall?.copyWith(color: color),
        ),
      ],
    );
  }
}
