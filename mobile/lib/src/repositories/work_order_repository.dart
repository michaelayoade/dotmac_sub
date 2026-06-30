import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/work_order.dart';

/// Wraps the self-scoped field-service endpoint (app/api/me.py,
/// /me/work-orders). Reads come from the sub's local work-order mirror.
class WorkOrderRepository {
  WorkOrderRepository(this.dio);

  final Dio dio;

  /// GET /me/work-orders — technician, schedule, ETA, status.
  Future<WorkOrdersSummary> summary() async {
    final data = await guard(() => dio.get('/me/work-orders'));
    return WorkOrdersSummary.fromJson(data as Map<String, dynamic>);
  }
}
