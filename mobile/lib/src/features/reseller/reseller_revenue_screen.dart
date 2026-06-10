import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Revenue report for the reseller portal: paid/outstanding totals and a
/// 12-month paid-revenue chart (GET /reseller/revenue).
class ResellerRevenueScreen extends ConsumerWidget {
  const ResellerRevenueScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final revenue = ref.watch(resellerRevenueProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Revenue')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerRevenueProvider);
          await ref.read(resellerRevenueProvider.future);
        },
        child: AsyncValueView<ResellerRevenue>(
          value: revenue,
          onRetry: () => ref.invalidate(resellerRevenueProvider),
          data: (r) => ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Row(
                children: [
                  Expanded(
                    child: _Kpi(
                      label: 'Total paid',
                      value: Fmt.moneyCompact(r.totalPaid, 'NGN'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _Kpi(
                      label: 'Outstanding',
                      value: Fmt.moneyCompact(r.totalOutstanding, 'NGN'),
                      highlight: r.totalOutstanding > 0,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _Kpi(label: 'Accounts', value: '${r.accountCount}'),
                  ),
                ],
              ),
              const SizedBox(height: 16),
              if (r.monthly.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 32),
                  child: Center(child: Text('No paid invoices yet.')),
                )
              else ...[
                Text('Paid revenue — last ${r.monthly.length} months',
                    style: Theme.of(context).textTheme.titleSmall),
                const SizedBox(height: 16),
                _RevenueChart(monthly: r.monthly),
                const SizedBox(height: 16),
                for (final m in r.monthly.reversed)
                  ListTile(
                    dense: true,
                    title: Text(m.label),
                    subtitle:
                        Text('${m.count} invoice${m.count == 1 ? '' : 's'}'),
                    trailing: Text(
                      Fmt.money(m.total, 'NGN'),
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _Kpi extends StatelessWidget {
  const _Kpi(
      {required this.label, required this.value, this.highlight = false});

  final String label;
  final String value;
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(
                value,
                maxLines: 1,
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                  color: highlight ? theme.colorScheme.error : null,
                ),
              ),
            ),
            const SizedBox(height: 4),
            Text(label, style: theme.textTheme.bodySmall),
          ],
        ),
      ),
    );
  }
}

class _RevenueChart extends StatelessWidget {
  const _RevenueChart({required this.monthly});

  final List<ResellerRevenueMonth> monthly;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final maxTotal =
        monthly.fold<double>(0, (a, m) => m.total > a ? m.total : a);
    final maxY = maxTotal <= 0 ? 1.0 : maxTotal * 1.25;
    final labelStep = (monthly.length / 6).ceil().clamp(1, 12);

    return SizedBox(
      height: 200,
      child: BarChart(
        BarChartData(
          maxY: maxY,
          alignment: BarChartAlignment.spaceAround,
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
                reservedSize: 44,
                getTitlesWidget: (v, _) => Text(
                  Fmt.moneyCompact(v, 'NGN'),
                  style: theme.textTheme.labelSmall,
                ),
              ),
            ),
            bottomTitles: AxisTitles(
              sideTitles: SideTitles(
                showTitles: true,
                reservedSize: 28,
                getTitlesWidget: (v, _) {
                  final i = v.toInt();
                  if (i < 0 || i >= monthly.length || i % labelStep != 0) {
                    return const SizedBox.shrink();
                  }
                  return Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(monthly[i].label,
                        style: theme.textTheme.labelSmall),
                  );
                },
              ),
            ),
          ),
          barGroups: [
            for (var i = 0; i < monthly.length; i++)
              BarChartGroupData(x: i, barRods: [
                BarChartRodData(
                  toY: monthly[i].total,
                  width: monthly.length > 8 ? 10 : 16,
                  borderRadius: BorderRadius.circular(3),
                  color: theme.colorScheme.primary,
                ),
              ]),
          ],
        ),
      ),
    );
  }
}
