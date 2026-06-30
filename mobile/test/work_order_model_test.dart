import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/models/work_order.dart';

void main() {
  group('WorkOrdersSummary.fromJson', () {
    test('parses work orders with technician, schedule and ETA', () {
      final summary = WorkOrdersSummary.fromJson({
        'total': 1,
        'upcoming': 1,
        'work_orders': [
          {
            'id': 'wo1',
            'title': 'Fault repair — no signal',
            'status': 'dispatched',
            'work_type': 'repair',
            'technician_name': 'Ade Tech',
            'technician_phone': '+2348000000000',
            'scheduled_start': '2026-06-30T09:00:00+00:00',
            'estimated_arrival_at': '2026-06-30T09:30:00+00:00',
            'estimated_duration_minutes': 60,
          },
        ],
      });

      expect(summary.total, 1);
      expect(summary.upcoming, 1);
      final w = summary.workOrders.single;
      expect(w.title, 'Fault repair — no signal');
      expect(w.status, 'dispatched');
      expect(w.technicianName, 'Ade Tech');
      expect(w.estimatedDurationMinutes, 60);
      expect(w.scheduledStart, isNotNull);
      expect(w.estimatedArrivalAt, isNotNull);
    });

    test('tolerates an empty payload', () {
      final summary = WorkOrdersSummary.fromJson({});
      expect(summary.workOrders, isEmpty);
      expect(summary.total, 0);
      expect(summary.upcoming, 0);
    });
  });
}
