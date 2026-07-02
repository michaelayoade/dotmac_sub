import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/work_order.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Technician Visits — the customer's field-service work orders (status,
/// schedule, ETA, technician), served from the sub's local work-order mirror.
class WorkOrdersScreen extends ConsumerWidget {
  const WorkOrdersScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final workOrders = ref.watch(workOrdersProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Technician Visits')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(workOrdersProvider);
          await ref.read(workOrdersProvider.future);
        },
        child: AsyncValueView<WorkOrdersSummary>(
          value: workOrders,
          onRetry: () => ref.invalidate(workOrdersProvider),
          data: (summary) {
            if (summary.workOrders.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 80),
                  Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 32),
                      child: Text(
                        'No technician visits scheduled. When a visit is booked '
                        "you'll see the schedule and technician here.",
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ),
                ],
              );
            }
            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                for (final w in summary.workOrders)
                  _WorkOrderCard(workOrder: w),
              ],
            );
          },
        ),
      ),
    );
  }
}

class _WorkOrderCard extends StatelessWidget {
  const _WorkOrderCard({required this.workOrder});

  final WorkOrderItem workOrder;

  static const _statusColors = {
    'scheduled': Colors.blue,
    'dispatched': Colors.indigo,
    'in_progress': Colors.orange,
    'completed': Colors.green,
    'canceled': Colors.red,
  };

  static String _fmt(DateTime? dt) {
    if (dt == null) return '';
    final d =
        '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}';
    final t =
        '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    return '$d $t';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = _statusColors[workOrder.status] ?? Colors.grey;
    final terminal =
        workOrder.status == 'completed' || workOrder.status == 'canceled';
    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    workOrder.title,
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
                Chip(
                  label: Text(
                    workOrder.status.replaceAll('_', ' '),
                    style: const TextStyle(fontSize: 12),
                  ),
                  backgroundColor: color.withValues(alpha: 0.15),
                  side: BorderSide.none,
                  visualDensity: VisualDensity.compact,
                  materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (workOrder.scheduledStart != null)
              _row(
                theme,
                Icons.event,
                'Scheduled',
                _fmt(workOrder.scheduledStart),
              ),
            if (workOrder.estimatedArrivalAt != null && !terminal)
              _row(
                theme,
                Icons.schedule,
                'Est. arrival',
                _fmt(workOrder.estimatedArrivalAt),
              ),
            if (workOrder.technicianName != null)
              _row(
                theme,
                Icons.engineering,
                'Technician',
                workOrder.technicianName!,
              ),
            if (workOrder.completedAt != null)
              _row(
                theme,
                Icons.check_circle,
                'Completed',
                _fmt(workOrder.completedAt),
              ),
          ],
        ),
      ),
    );
  }

  Widget _row(ThemeData theme, IconData icon, String label, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 16, color: theme.colorScheme.outline),
          const SizedBox(width: 8),
          Text('$label: ', style: theme.textTheme.bodySmall),
          Expanded(
            child: Text(
              value,
              style: theme.textTheme.bodyMedium?.copyWith(
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
