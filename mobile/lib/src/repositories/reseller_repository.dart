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

  /// GET /reseller/profile — organization profile + MFA state.
  Future<ResellerProfile> profile() async {
    final data = await guard(() => dio.get('/reseller/profile'));
    return ResellerProfile.fromJson(data as Map<String, dynamic>);
  }

  /// PATCH /reseller/profile — update contact details.
  Future<ResellerProfile> updateProfile({
    String? contactEmail,
    String? contactPhone,
    String? notes,
  }) async {
    final data = await guard(() => dio.patch('/reseller/profile', data: {
          if (contactEmail != null) 'contact_email': contactEmail,
          if (contactPhone != null) 'contact_phone': contactPhone,
          if (notes != null) 'notes': notes,
        }));
    return ResellerProfile.fromJson(data as Map<String, dynamic>);
  }

  /// POST /reseller/profile/mfa/setup — begin TOTP enrollment.
  Future<ResellerMfaSetup> mfaSetup() async {
    final data = await guard(() => dio.post('/reseller/profile/mfa/setup'));
    return ResellerMfaSetup.fromJson(data as Map<String, dynamic>);
  }

  /// POST /reseller/profile/mfa/confirm — verify the first code.
  Future<void> mfaConfirm(
      {required String methodId, required String code}) async {
    await guard(() => dio.post('/reseller/profile/mfa/confirm',
        data: {'method_id': methodId, 'code': code}));
  }

  /// GET /reseller/accounts/{id}/tickets — CRM tickets for a managed account.
  Future<ResellerTicketsPage> accountTickets(String accountId) async {
    final data =
        await guard(() => dio.get('/reseller/accounts/$accountId/tickets'));
    return ResellerTicketsPage.fromJson(data as Map<String, dynamic>);
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
