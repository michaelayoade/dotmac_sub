import 'dart:math';

import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/addon.dart';
import '../models/connection_status.dart';
import '../models/page.dart';
import '../models/plan_change.dart';
import '../models/service_status.dart';
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

  /// GET /me/service-status — truthful account + per-service status (balance,
  /// grace/deactivation, dunning). The source of truth for "is my service good
  /// and when does it lapse", instead of guessing from a billing date.
  Future<ServiceStatus> serviceStatus() async {
    final data = await guard(() => dio.get('/me/service-status'));
    return ServiceStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// GET /me/connection-status — the outage classifier's per-customer verdict
  /// ("what's wrong with my connection?") with area-outage blame suppression
  /// already applied server-side. Self-scoped to the caller's active service.
  Future<ConnectionStatus> connectionStatus() async {
    final data = await guard(() => dio.get('/me/connection-status'));
    return ConnectionStatus.fromJson((data as Map).cast<String, dynamic>());
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

  /// GET /me/subscriptions/{id}/add-ons — available and active add-ons.
  Future<AddonsAvailable> addons(String subscriptionId) async {
    final data =
        await guard(() => dio.get('/me/subscriptions/$subscriptionId/add-ons'));
    return AddonsAvailable.fromJson(data as Map<String, dynamic>);
  }

  /// GET …/add-ons/quote — server-owned purchase and exact-debit preview.
  Future<AddonQuote> addonQuote(
      String subscriptionId, String addOnId, int quantity) async {
    final data = await guard(() => dio.get(
          '/me/subscriptions/$subscriptionId/add-ons/quote',
          queryParameters: {'add_on_id': addOnId, 'quantity': quantity},
        ));
    return AddonQuote.fromJson(data as Map<String, dynamic>);
  }

  /// POST …/add-ons — confirm the exact previewed add-on debit. A fresh
  /// idempotency key is built into the request so a transport-level retry
  /// (e.g. the 401-refresh replay) cannot post the prepaid debit twice.
  Future<AddonPurchaseResult> purchaseAddon(String subscriptionId,
      String addOnId, int quantity, String previewFingerprint) async {
    final key = 'addon-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random().nextInt(1 << 32)}';
    final data = await guard(() => dio.post(
          '/me/subscriptions/$subscriptionId/add-ons',
          data: {
            'add_on_id': addOnId,
            'quantity': quantity,
            'preview_fingerprint': previewFingerprint,
            'idempotency_key': key,
          },
        ));
    return AddonPurchaseResult.fromJson(data as Map<String, dynamic>);
  }

  /// DELETE …/add-ons/{id} — cancel an add-on (stops billing next cycle).
  Future<void> cancelAddon(String subscriptionId, String subAddOnId) async {
    await guard(() =>
        dio.delete('/me/subscriptions/$subscriptionId/add-ons/$subAddOnId'));
  }

  /// POST …/plan-change — submit a plan-change request.
  Future<void> submitPlanChange(
    String subscriptionId, {
    required String offerId,
    required String previewFingerprint,
    String? notes,
  }) async {
    final key = 'plan-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random().nextInt(1 << 32)}';
    await guard(() => dio.post(
          '/me/subscriptions/$subscriptionId/plan-change',
          data: {
            'offer_id': offerId,
            'preview_fingerprint': previewFingerprint,
            'idempotency_key': key,
            if (notes != null) 'notes': notes,
          },
        ));
  }
}
