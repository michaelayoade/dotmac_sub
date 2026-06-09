import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/reseller.dart';

/// Wraps the reseller endpoints (app/api/reseller.py, mounted at /api/v1).
/// Every call is scoped server-side to the authenticated reseller.
class ResellerRepository {
  ResellerRepository(this.dio);

  final Dio dio;

  /// GET /reseller/dashboard — KPIs plus a first page of managed accounts.
  Future<ResellerDashboard> dashboard({int limit = 10, int offset = 0}) async {
    final data = await guard(
      () => dio.get('/reseller/dashboard', queryParameters: {
        'limit': limit,
        'offset': offset,
      }),
    );
    return ResellerDashboard.fromJson(data as Map<String, dynamic>);
  }

  /// GET /reseller/accounts — the reseller's managed customer accounts.
  Future<Page<ResellerAccount>> accounts({
    String? search,
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get('/reseller/accounts', queryParameters: {
        if (search != null && search.isNotEmpty) 'search': search,
        'limit': limit,
        'offset': offset,
      }),
    );
    return Page.fromJson(
        data as Map<String, dynamic>, ResellerAccount.fromJson);
  }

  /// GET /reseller/accounts/{id} — one managed account (404 if not owned).
  Future<ResellerAccountDetail> account(String accountId) async {
    final data = await guard(() => dio.get('/reseller/accounts/$accountId'));
    return ResellerAccountDetail.fromJson(data as Map<String, dynamic>);
  }

  /// GET /reseller/accounts/{id}/invoices — invoices for a managed account.
  Future<List<ResellerInvoiceSummary>> accountInvoices(
    String accountId, {
    int limit = 25,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get('/reseller/accounts/$accountId/invoices',
          queryParameters: {'limit': limit, 'offset': offset}),
    );
    final items = (data as Map<String, dynamic>)['items'] as List? ?? const [];
    return items
        .cast<Map<String, dynamic>>()
        .map(ResellerInvoiceSummary.fromJson)
        .toList();
  }
}
