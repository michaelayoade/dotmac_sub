import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../models/usage.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

class UsageScreen extends ConsumerWidget {
  const UsageScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final buckets = ref.watch(quotaBucketsProvider);
    final sessions = ref.watch(accountingSessionsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Usage')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(quotaBucketsProvider);
          ref.invalidate(accountingSessionsProvider);
          await Future.wait([
            ref.read(quotaBucketsProvider.future),
            ref.read(accountingSessionsProvider.future),
          ]);
        },
        // Sessions are the primary signal here (this ISP meters via RADIUS
        // accounting, not quota buckets); quota cards show when present.
        child: AsyncValueView(
          value: sessions,
          onRetry: () => ref.invalidate(accountingSessionsProvider),
          data: (sessionPage) {
            final list = sessionPage.items;
            final quotaList = buckets.asData?.value ?? const <QuotaBucket>[];

            if (list.isEmpty && quotaList.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 120),
                  EmptyState(
                    icon: Icons.data_usage_outlined,
                    message: 'No usage recorded yet.',
                  ),
                ],
              );
            }

            final download =
                list.fold<int>(0, (s, e) => s + (e.outputOctets ?? 0));
            final upload =
                list.fold<int>(0, (s, e) => s + (e.inputOctets ?? 0));

            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (list.isNotEmpty) ...[
                  _UsageHistoryCard(sessions: list),
                  const SizedBox(height: 12),
                ],
                if (list.isNotEmpty)
                  _TotalsCard(download: download, upload: upload),
                for (final b in quotaList) ...[
                  const SizedBox(height: 12),
                  _QuotaCard(bucket: b),
                ],
                if (list.isNotEmpty) ...[
                  const SizedBox(height: 20),
                  Text('Recent sessions',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  for (final s in list) _SessionTile(session: s),
                ],
              ],
            );
          },
        ),
      ),
    );
  }
}

/// Daily data-usage history as a stacked bar chart (download + upload), built
/// from the RADIUS accounting sessions (most recent 14 days with data).
class _UsageHistoryCard extends StatelessWidget {
  const _UsageHistoryCard({required this.sessions});
  final List<AccountingSession> sessions;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final down = <DateTime, int>{};
    final up = <DateTime, int>{};
    for (final s in sessions) {
      final d = s.sessionStart;
      if (d == null) continue;
      final day = DateTime(d.year, d.month, d.day);
      down[day] = (down[day] ?? 0) + (s.outputOctets ?? 0);
      up[day] = (up[day] ?? 0) + (s.inputOctets ?? 0);
    }
    final days = {...down.keys, ...up.keys}.toList()..sort();
    final recent = days.length > 14 ? days.sublist(days.length - 14) : days;
    if (recent.isEmpty) return const SizedBox.shrink();

    const gb = 1024 * 1024 * 1024;
    double toGb(int v) => v / gb;
    final maxGb = recent
        .map((d) => toGb((down[d] ?? 0) + (up[d] ?? 0)))
        .fold<double>(0, (a, b) => a > b ? a : b);

    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(8, 16, 16, 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Text('Usage history (GB/day)',
                  style: theme.textTheme.titleSmall),
            ),
            const SizedBox(height: 16),
            SizedBox(
              height: 180,
              child: BarChart(
                BarChartData(
                  maxY: maxGb <= 0 ? 1 : maxGb * 1.25,
                  alignment: BarChartAlignment.spaceAround,
                  gridData:
                      const FlGridData(show: true, drawVerticalLine: false),
                  borderData: FlBorderData(show: false),
                  titlesData: FlTitlesData(
                    topTitles: const AxisTitles(
                        sideTitles: SideTitles(showTitles: false)),
                    rightTitles: const AxisTitles(
                        sideTitles: SideTitles(showTitles: false)),
                    leftTitles: AxisTitles(
                      sideTitles: SideTitles(
                        showTitles: true,
                        reservedSize: 30,
                        getTitlesWidget: (v, _) => Text(
                          v >= 100
                              ? v.toStringAsFixed(0)
                              : v.toStringAsFixed(1),
                          style: theme.textTheme.labelSmall,
                        ),
                      ),
                    ),
                    bottomTitles: AxisTitles(
                      sideTitles: SideTitles(
                        showTitles: true,
                        getTitlesWidget: (v, _) {
                          final i = v.toInt();
                          if (i < 0 || i >= recent.length) {
                            return const SizedBox.shrink();
                          }
                          final d = recent[i];
                          return Padding(
                            padding: const EdgeInsets.only(top: 4),
                            child: Text('${d.day}/${d.month}',
                                style: theme.textTheme.labelSmall),
                          );
                        },
                      ),
                    ),
                  ),
                  barGroups: [
                    for (var i = 0; i < recent.length; i++)
                      BarChartGroupData(x: i, barRods: [
                        BarChartRodData(
                          toY: toGb(
                              (down[recent[i]] ?? 0) + (up[recent[i]] ?? 0)),
                          width: 14,
                          borderRadius: BorderRadius.circular(3),
                          rodStackItems: [
                            BarChartRodStackItem(0, toGb(down[recent[i]] ?? 0),
                                theme.colorScheme.primary),
                            BarChartRodStackItem(
                                toGb(down[recent[i]] ?? 0),
                                toGb((down[recent[i]] ?? 0) +
                                    (up[recent[i]] ?? 0)),
                                theme.colorScheme.tertiary),
                          ],
                        ),
                      ]),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Row(
                children: [
                  _Legend(color: theme.colorScheme.primary, label: 'Download'),
                  const SizedBox(width: 16),
                  _Legend(color: theme.colorScheme.tertiary, label: 'Upload'),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Legend extends StatelessWidget {
  const _Legend({required this.color, required this.label});
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
            decoration: BoxDecoration(
                color: color, borderRadius: BorderRadius.circular(2))),
        const SizedBox(width: 6),
        Text(label, style: Theme.of(context).textTheme.labelSmall),
      ],
    );
  }
}

class _TotalsCard extends StatelessWidget {
  const _TotalsCard({required this.download, required this.upload});
  final int download;
  final int upload;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Total data used', style: theme.textTheme.bodyMedium),
            const SizedBox(height: 4),
            Text(Fmt.bytes(download + upload),
                style: theme.textTheme.headlineMedium),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: _Meter(
                    icon: Icons.south_rounded,
                    label: 'Download',
                    value: Fmt.bytes(download),
                    color: theme.colorScheme.primary,
                  ),
                ),
                Expanded(
                  child: _Meter(
                    icon: Icons.north_rounded,
                    label: 'Upload',
                    value: Fmt.bytes(upload),
                    color: theme.colorScheme.tertiary,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _Meter extends StatelessWidget {
  const _Meter({
    required this.icon,
    required this.label,
    required this.value,
    required this.color,
  });
  final IconData icon;
  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 8),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label, style: Theme.of(context).textTheme.bodySmall),
            Text(value, style: const TextStyle(fontWeight: FontWeight.w600)),
          ],
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
          '↓ ${Fmt.bytes(s.outputOctets ?? 0)}  ↑ ${Fmt.bytes(s.inputOctets ?? 0)}\n'
          '${Fmt.dateTime(s.sessionStart)}${s.isActive ? ' · active' : ''}',
        ),
        isThreeLine: true,
      ),
    );
  }
}

class _QuotaCard extends StatelessWidget {
  const _QuotaCard({required this.bucket});
  final QuotaBucket bucket;

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
                Text('${Fmt.date(b.periodStart)} – ${Fmt.date(b.periodEnd)}',
                    style: theme.textTheme.bodySmall),
                if (b.isUnlimited)
                  Text('Unlimited',
                      style: theme.textTheme.labelMedium
                          ?.copyWith(color: theme.colorScheme.primary)),
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
          ],
        ),
      ),
    );
  }
}
