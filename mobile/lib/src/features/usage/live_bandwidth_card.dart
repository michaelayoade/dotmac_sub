import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';

/// Real-time throughput section. Off by default (on-demand): tapping Start opens
/// a live stream of the subscriber's current download/upload from
/// `/me/bandwidth/stats`; Stop ends the polling. Replaces the old connection-
/// banner "Go live" toggle with a visible, self-contained section.
class LiveBandwidthCard extends ConsumerStatefulWidget {
  const LiveBandwidthCard({super.key});

  @override
  ConsumerState<LiveBandwidthCard> createState() => _LiveBandwidthCardState();
}

class _LiveBandwidthCardState extends ConsumerState<LiveBandwidthCard> {
  Timer? _autoStop;
  static const _idleLimit = Duration(minutes: 5);

  @override
  void dispose() {
    _autoStop?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final enabled = ref.watch(liveBandwidthEnabledProvider);
    // Auto-stop the 15s polling after a few idle minutes to spare battery/data;
    // restarts on the next Start tap.
    ref.listen<bool>(liveBandwidthEnabledProvider, (prev, next) {
      _autoStop?.cancel();
      if (next) {
        _autoStop = Timer(_idleLimit, () {
          if (mounted) {
            ref.read(liveBandwidthEnabledProvider.notifier).state = false;
          }
        });
      }
    });
    final live = ref.watch(liveBandwidthProvider);
    final v = live.asData?.value;
    // "Measuring…" only until the first reading returns. Once it does, show the
    // value even when it's 0 (idle) — otherwise an idle line looks stuck.
    final waiting = enabled && v == null;

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
                    Icon(
                      Icons.speed,
                      size: 18,
                      color: theme.colorScheme.primary,
                    ),
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
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              )
            else ...[
              Row(
                children: [
                  Expanded(
                    child: _Meter(
                      // Customer perspective: downloading = their device
                      // receives (RX).
                      label: 'Download (RX)',
                      icon: Icons.south,
                      bps: v?.downloadBps,
                      waiting: waiting,
                      color: theme.colorScheme.primary,
                    ),
                  ),
                  Expanded(
                    child: _Meter(
                      // Customer perspective: uploading = their device
                      // transmits (TX).
                      label: 'Upload (TX)',
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
                  'Peak (last hour)  ↑ ${Fmt.bps(v?.peakUploadBps)}   ↓ ${Fmt.bps(v?.peakDownloadBps)}',
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
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
                    Text(
                      'Measuring…',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
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
            Text(
              label,
              style: theme.textTheme.labelMedium?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
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
