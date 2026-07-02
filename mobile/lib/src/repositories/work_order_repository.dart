import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/technician_location.dart';
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

  /// GET /me/work-orders/{id}/technician-location — live position (poll while
  /// the visit is in progress). Returns available=false when it should hide.
  Future<TechnicianLocation> technicianLocation(String workOrderId) async {
    final data = await guard(
      () => dio.get('/me/work-orders/$workOrderId/technician-location'),
    );
    return TechnicianLocation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/work-orders/{id}/rate-technician — rate after completion.
  Future<TechnicianRatingResult> rateTechnician(
    String workOrderId, {
    required int rating,
    String? comment,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/work-orders/$workOrderId/rate-technician',
        data: {
          'rating': rating,
          if (comment != null && comment.isNotEmpty) 'comment': comment,
        },
      ),
    );
    return TechnicianRatingResult.fromJson(data as Map<String, dynamic>);
  }
}
