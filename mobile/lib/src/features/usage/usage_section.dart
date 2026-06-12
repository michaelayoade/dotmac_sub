import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../models/usage.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';

/// The usage half of the Service tab: period chips, windowed total + chart,
/// and the recent RADIUS sessions list. Extracted from the former Usage tab.
class UsageSection extends StatelessWidget {
  const UsageSection({
    super.key,
    required this.period,
    required this.summary,
    required this.sessions,
    required this.onSelectPeriod,
    required this.onRetry,
  });

  final String period;
  final AsyncValue<UsageSummary> summary;
  final List<AccountingSession> sessions;
  final ValueChanged<String> onSelectPeriod;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Usage', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        _PeriodChips(selected: period, onSelect: onSelectPeriod),
        const SizedBox(height: 12),
        AsyncValueView(
          value: summary,
          onRetry: onRetry,
          skeleton: const CardSkeleton(height: 160),
          data: (s) => _WindowSummaryCard(summary: s),
        ),
        if (sessions.isNotEmpty) ...[
          const SizedBox(height: 20),
          Text(
            'Recent sessions',
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 8),
          for (final s in sessions) _SessionTile(session: s),
        ],
      ],
    );
  }
}

/// Quota bar for one bucket, with the plan's FUP terms underneath (visible
/// while healthy, not just once capped) and the running overage cost when the
/// customer is past their allowance on a metered plan.
class QuotaCard extends StatelessWidget {
  const QuotaCard({super.key, required this.bucket, this.policyLine});

  final QuotaBucket bucket;
  final String? policyLine;

  @override
  Widget build(BuildContext context) {
    final b = bucket;
    final theme = Theme.of(context);
    final fraction = b.usedFraction;
    final overLimit = b.overageGb > 0;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  '${Fmt.date(b.periodStart)} – ${Fmt.date(b.periodEnd)}',
                  style: theme.textTheme.bodySmall,
                ),
                if (b.isUnlimited)
                  Text(
                    'Unlimited',
                    style: theme.textTheme.labelMedium?.copyWith(
                      color: theme.colorScheme.primary,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              b.isUnlimited
                  ? Fmt.gb(b.usedGb)
                  : '${Fmt.gb(b.usedGb)} / ${Fmt.gb(b.allowanceGb ?? 0)}',
              style: theme.textTheme.headlineSmall,
            ),
            const SizedBox(height: 12),
            LinearProgressIndicator(
              value: fraction,
              minHeight: 10,
              borderRadius: BorderRadius.circular(5),
              color: overLimit ? theme.colorScheme.error : null,
            ),
            if (!b.isUnlimited) ...[
              const SizedBox(height: 8),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    overLimit
                        ? '${Fmt.gb(b.overageGb)} over'
                        : '${Fmt.gb(b.remainingGb ?? 0)} left',
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: overLimit
                          ? theme.colorScheme.error
                          : theme.colorScheme.outline,
                    ),
                  ),
                  if (b.topupGb > 0)
                    Text(
                      '+${Fmt.gb(b.topupGb)} top-up',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.primary,
                      ),
                    ),
                ],
              ),
            ],
            if (overLimit && b.overageAmount != null) ...[
              const SizedBox(height: 6),
              Text(
                'In overage — ${Fmt.money(b.overageAmount!, 'NGN')} so far',
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.error,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
            if (policyLine != null) ...[
              const SizedBox(height: 8),
              Row(
                children: [
                  Icon(
                    Icons.info_outline,
                    size: 14,
                    color: theme.colorScheme.outline,
                  ),
                  const SizedBox(width: 4),
                  Expanded(
                    child: Text(
                      policyLine!,
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.outline,
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

String _periodLabel(String p) => switch (p) {
  'hour' => 'Past hour',
  'today' => 'Today',
  'week' => 'This week',
  'cycle' => 'This billing cycle',
  _ => 'All time',
};

/// Sourcing note so a throughput-estimated total isn't mistaken for billing.
String _sourceNote(UsageSummary s) {
  if (s.isAuthoritative) {
    return s.totalSource == 'quota' ? 'Rated billing usage' : 'Metered total';
  }
  return 'Estimated from live throughput';
}

class _PeriodChips extends StatelessWidget {
  const _PeriodChips({required this.selected, required this.onSelect});
  final String selected;
  final ValueChanged<String> onSelect;

  static const _periods = <String, String>{
    'hour': 'Hour',
    'today': 'Today',
    'week': 'Week',
    'cycle': 'Cycle',
    'all': 'All',
  };

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final e in _periods.entries)
          ChoiceChip(
            label: Text(e.value),
            selected: selected == e.key,
            onSelected: (_) => onSelect(e.key),
          ),
      ],
    );
  }
}

class _WindowSummaryCard extends StatelessWidget {
  const _WindowSummaryCard({required this.summary});
  final UsageSummary summary;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final hasChart = summary.bucket != null && summary.series.isNotEmpty;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _periodLabel(summary.period),
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 4),
            Text(
              Fmt.bytes(summary.totalBytes),
              style: theme.textTheme.headlineMedium,
            ),
            const SizedBox(height: 2),
            Text(
              _sourceNote(summary),
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
            if (hasChart) ...[
              const SizedBox(height: 16),
              _UsageBarChart(series: summary.series, bucket: summary.bucket!),
            ],
          ],
        ),
      ),
    );
  }
}

class _UsageBarChart extends StatelessWidget {
  const _UsageBarChart({required this.series, required this.bucket});
  final List<UsageSeriesPoint> series;
  final String bucket;

  String _xLabel(DateTime d) => switch (bucket) {
    'minute' => '${d.hour}:${d.minute.toString().padLeft(2, '0')}',
    'hour' => '${d.hour}:00',
    _ => '${d.day}/${d.month}',
  };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final maxBytes = series.fold<int>(0, (a, b) => b.bytes > a ? b.bytes : a);

    const kb = 1 << 10, mb = 1 << 20, gb = 1 << 30;
    double div;
    String unit;
    if (maxBytes >= gb) {
      div = gb.toDouble();
      unit = 'GB';
    } else if (maxBytes >= mb) {
      div = mb.toDouble();
      unit = 'MB';
    } else if (maxBytes >= kb) {
      div = kb.toDouble();
      unit = 'KB';
    } else {
      div = 1;
      unit = 'B';
    }
    double toUnit(int b) => b / div;
    final maxY = maxBytes <= 0 ? 1.0 : toUnit(maxBytes) * 1.25;
    final labelStep = (series.length / 6).ceil().clamp(1, 9999);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Usage ($unit)', style: theme.textTheme.titleSmall),
        const SizedBox(height: 16),
        SizedBox(
          height: 180,
          child: BarChart(
            BarChartData(
              maxY: maxY,
              alignment: BarChartAlignment.spaceAround,
              gridData: const FlGridData(show: true, drawVerticalLine: false),
              borderData: FlBorderData(show: false),
              titlesData: FlTitlesData(
                topTitles: const AxisTitles(
                  sideTitles: SideTitles(showTitles: false),
                ),
                rightTitles: const AxisTitles(
                  sideTitles: SideTitles(showTitles: false),
                ),
                leftTitles: AxisTitles(
                  sideTitles: SideTitles(
                    showTitles: true,
                    reservedSize: 32,
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
                      if (i < 0 || i >= series.length || i % labelStep != 0) {
                        return const SizedBox.shrink();
                      }
                      return Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(
                          _xLabel(series[i].bucketStart),
                          style: theme.textTheme.labelSmall,
                        ),
                      );
                    },
                  ),
                ),
              ),
              barGroups: [
                for (var i = 0; i < series.length; i++)
                  BarChartGroupData(
                    x: i,
                    barRods: [
                      BarChartRodData(
                        toY: toUnit(series[i].bytes),
                        width: series.length > 20 ? 6 : 12,
                        borderRadius: BorderRadius.circular(3),
                        color: theme.colorScheme.primary,
                      ),
                    ],
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _SessionTile extends StatelessWidget {
  const _SessionTile({required this.session});
  final AccountingSession session;

  @override
  Widget build(BuildContext context) {
    final s = session;
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Icon(
          s.isActive ? Icons.cloud_sync_outlined : Icons.cloud_done_outlined,
          color: s.isActive ? Theme.of(context).colorScheme.primary : null,
        ),
        title: Text(Fmt.bytes(s.totalOctets)),
        subtitle: Text(
          '↓ ${Fmt.bytes(s.outputOctets ?? 0)}  ↑ ${Fmt.bytes(s.inputOctets ?? 0)}'
          '${s.framedIpAddress != null ? '  ·  ${s.framedIpAddress}' : ''}\n'
          '${Fmt.dateTime(s.sessionStart)}'
          '${s.isActive ? ' · active · seen ${Fmt.dateTime(s.lastSeenAt)}' : ''}',
        ),
        isThreeLine: true,
      ),
    );
  }
}
