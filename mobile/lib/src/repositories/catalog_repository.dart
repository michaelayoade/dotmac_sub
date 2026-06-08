import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/plan_change.dart';
import '../models/subscription.dart';

/// Wraps the catalog subscription endpoints (app/api/catalog.py).
class CatalogRepository {
  CatalogRepository(this.dio);

  final Dio dio;

  /// GET /me/subscriptions — the signed-in subscriber's own services.
  Future<Page<Subscription>> subscriptions({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data =
        await guard(() => dio.get('/me/subscriptions', queryParameters: {
              if (status != null) 'status': status,
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(data as Map<String, dynamic>, Subscription.fromJson);
  }

  /// GET /subscriptions/{id}
  Future<Subscription> subscription(String id) async {
    final data = await guard(() => dio.get('/subscriptions/$id'));
    return Subscription.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/subscriptions/{id}/plan-change — plans the customer can switch to.
  Future<PlanChangeOptions> planChangeOptions(String subscriptionId) async {
    final data = await guard(
        () => dio.get('/me/subscriptions/$subscriptionId/plan-change'));
    return PlanChangeOptions.fromJson(data as Map<String, dynamic>);
  }

  /// GET …/plan-change/quote — prorated quote for one target offer.
  Future<PlanChangeQuote> planChangeQuote(
      String subscriptionId, String offerId) async {
    final data = await guard(() => dio.get(
          '/me/subscriptions/$subscriptionId/plan-change/quote',
          queryParameters: {'offer_id': offerId},
        ));
    return PlanChangeQuote.fromJson((data as Map).cast<String, dynamic>());
  }

  /// POST …/plan-change — submit a plan-change request.
  Future<void> submitPlanChange(
    String subscriptionId, {
    required String offerId,
    required String effectiveDate, // YYYY-MM-DD
    String? notes,
  }) async {
    await guard(() => dio.post(
          '/me/subscriptions/$subscriptionId/plan-change',
          data: {
            'offer_id': offerId,
            'effective_date': effectiveDate,
            if (notes != null) 'notes': notes,
          },
        ));
  }
}
