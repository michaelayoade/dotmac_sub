import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/usage.dart';
import '../../providers/data_providers.dart';
import '../../widgets/skeleton.dart';

/// Look-back windows for the speed chart, in hours.
const _ranges = <int, String>{
  1: '1h',
  6: '6h',
  24: '24h',
  168: '7d',
  720: '30d'
};

String _fmtBps(double bps) {
  const k = 1000.0, m = 1e6, g = 1e9;
  if (bps >= g) return '${(bps / g).toStringAsFixed(1)} Gbps';
  if (bps >= m) return '${(bps / m).toStringAsFixed(bps >= 1e7 ? 0 : 1)} Mbps';
  if (bps >= k) return '${(bps / k).toStringAsFixed(0)} Kbps';
  return '${bps.toStringAsFixed(0)} bps';
}

/// Actual download/upload speed over time, from the bandwidth pipeline
/// (Postgres <24h, VictoriaMetrics older — so it reaches as far back as VM
/// retention). Mirrors the web "Bandwidth Speed History" chart.
class SpeedHistoryCard extends ConsumerWidget {
  const SpeedHistoryCard({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final hours = ref.watch(speedRangeHoursProvider);
    final series = ref.watch(bandwidthSeriesProvider(hours));
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Speed', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            _RangeChips(
              selected: hours,
              onSelect: (h) =>
                  ref.read(speedRangeHoursProvider.notifier).state = h,
            ),
            const SizedBox(height: 12),
            // Degrade gracefully: a 403/unavailable connection shows a muted
            // note (this account may have no live-bandwidth mapping), not an
            // alarming error.
            series.when(
              loading: () => const CardSkeleton(height: 180),
              error: (_, __) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 24),
                child: Center(
                  child: Text(
                    'Speed data isn\'t available for this connection.',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.outline),
                  ),
                ),
              ),
              data: (pts) => _SpeedBody(points: pts, hours: hours),
            ),
          ],
        ),
      ),
    );
  }
}

class _RangeChips extends StatelessWidget {
  const _RangeChips({required this.selected, required this.onSelect});
  final int selected;
  final ValueChanged<int> onSelect;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final e in _ranges.entries)
          ChoiceChip(
            label: Text(e.value),
            selected: selected == e.key,
            onSelected: (_) => onSelect(e.key),
          ),
      ],
    );
  }
}

class _SpeedBody extends StatelessWidget {
  const _SpeedBody({required this.points, required this.hours});
  final List<BandwidthPoint> points;
  final int hours;

  String _xLabel(DateTime d) => hours <= 24
      ? '${d.hour}:${d.minute.toString().padLeft(2, '0')}'
      : '${d.day}/${d.month}';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (points.length < 2) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 24),
        child: Center(
          child: Text('No speed data for this range',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.outline)),
        ),
      );
    }

    final peakDown =
        points.fold<double>(0, (a, p) => p.downloadBps > a ? p.downloadBps : a);
    final peakUp =
        points.fold<double>(0, (a, p) => p.uploadBps > a ? p.uploadBps : a);
    final latest = points.last;
    final maxBps = peakDown > peakUp ? peakDown : peakUp;

    const k = 1000.0, m = 1e6, g = 1e9;
    final (double div, String unit) = maxBps >= g
        ? (g, 'Gbps')
        : maxBps >= m
            ? (m, 'Mbps')
            : maxBps >= k
                ? (k, 'Kbps')
                : (1, 'bps');
    final maxY = maxBps <= 0 ? 1.0 : (maxBps / div) * 1.25;
    final labelStep = (points.length / 5).ceil().clamp(1, 99999);
    final download = theme.colorScheme.primary;
    final upload = theme.colorScheme.tertiary;

    List<FlSpot> spots(double Function(BandwidthPoint) sel) => [
          for (var i = 0; i < points.length; i++)
            FlSpot(i.toDouble(), sel(points[i]) / div)
        ];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
                child:
                    _Stat(label: 'Now ↓', value: _fmtBps(latest.downloadBps))),
            Expanded(child: _Stat(label: 'Peak ↓', value: _fmtBps(peakDown))),
            Expanded(child: _Stat(label: 'Peak ↑', value: _fmtBps(peakUp))),
          ],
        ),
        const SizedBox(height: 16),
        Text('Speed ($unit)', style: theme.textTheme.titleSmall),
        const SizedBox(height: 12),
        SizedBox(
          height: 180,
          child: LineChart(
            LineChartData(
              minY: 0,
              maxY: maxY,
              gridData: const FlGridData(show: true, drawVerticalLine: false),
              borderData: FlBorderData(show: false),
              titlesData: FlTitlesData(
                topTitles:
                    const AxisTitles(sideTitles: SideTitles(showTitles: false)),
                rightTitles:
                    const AxisTitles(sideTitles: SideTitles(showTitles: false)),
                leftTitles: AxisTitles(
                  sideTitles: SideTitles(
                    showTitles: true,
                    reservedSize: 36,
                    getTitlesWidget: (v, _) => Text(
                      v >= 100 ? v.toStringAsFixed(0) : v.toStringAsFixed(1),
                      style: theme.textTheme.labelSmall,
                    ),
                  ),
                ),
                bottomTitles: AxisTitles(
                  sideTitles: SideTitles(
                    showTitles: true,
                    reservedSize: 24,
                    getTitlesWidget: (v, _) {
                      final i = v.toInt();
                      if (i < 0 || i >= points.length || i % labelStep != 0) {
                        return const SizedBox.shrink();
                      }
                      return Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(_xLabel(points[i].at),
                            style: theme.textTheme.labelSmall),
                      );
                    },
                  ),
                ),
              ),
              lineBarsData: [
                LineChartBarData(
                  spots: spots((p) => p.downloadBps),
                  isCurved: true,
                  barWidth: 2,
                  color: download,
                  dotData: const FlDotData(show: false),
                ),
                LineChartBarData(
                  spots: spots((p) => p.uploadBps),
                  isCurved: true,
                  barWidth: 2,
                  color: upload,
                  dotData: const FlDotData(show: false),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            _LegendDot(color: download, label: 'Download'),
            const SizedBox(width: 16),
            _LegendDot(color: upload, label: 'Upload'),
          ],
        ),
      ],
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(value, style: theme.textTheme.titleMedium),
        const SizedBox(height: 2),
        Text(label,
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.outline)),
      ],
    );
  }
}

class _LegendDot extends StatelessWidget {
  const _LegendDot({required this.color, required this.label});
  final Color color;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 10,
          height: 10,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
        const SizedBox(width: 6),
        Text(label, style: Theme.of(context).textTheme.bodySmall),
      ],
    );
  }
}
