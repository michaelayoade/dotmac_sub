import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/payment_method.dart';
import '../models/quote.dart';
import '../models/reseller.dart';
import '../models/reseller_crm.dart';
import '../models/page.dart';

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
    String? status,
    String orderBy = 'created_at',
    String orderDir = 'desc',
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get('/reseller/accounts', queryParameters: {
        if (search != null && search.isNotEmpty) 'search': search,
        if (status != null && status.isNotEmpty) 'status': status,
        'order_by': orderBy,
        'order_dir': orderDir,
        'limit': limit,
        'offset': offset,
      }),
    );
    return Page.fromJson(
        data as Map<String, dynamic>, ResellerAccount.fromJson);
  }

  /// GET /reseller/revenue — 12-month paid revenue + outstanding totals.
  Future<ResellerRevenue> revenue() async {
    final data = await guard(() => dio.get('/reseller/revenue'));
    return ResellerRevenue.fromJson(data as Map<String, dynamic>);
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

  /// GET /reseller/billing — consolidated statement.
  Future<ResellerBillingSummary> billing() async {
    final data = await guard(() => dio.get('/reseller/billing'));
    return ResellerBillingSummary.fromJson(data as Map<String, dynamic>);
  }

  /// POST /reseller/billing/pay/intent — start a consolidated payment.
  /// Optionally charge a saved card ([paymentMethodId]) and/or save the card
  /// used for this charge ([saveCard]) for future payments.
  Future<ResellerPayIntent> payIntent(
    String amount, {
    String? paymentMethodId,
    bool saveCard = false,
  }) async {
    final data =
        await guard(() => dio.post('/reseller/billing/pay/intent', data: {
              'amount': amount,
              if (paymentMethodId != null) 'payment_method_id': paymentMethodId,
              if (saveCard) 'save_card': true,
            }));
    return ResellerPayIntent.fromJson(data as Map<String, dynamic>);
  }

  /// POST /reseller/billing/pay/verify — confirm + record the charge.
  Future<void> payVerify(String reference) async {
    await guard(() => dio
        .post('/reseller/billing/pay/verify', data: {'reference': reference}));
  }

  /// POST /payment-proofs/reseller/consolidated — upload a bulk bank-transfer
  /// receipt. [amount] is the net cash sent; [grossAmount]/[whtRate] capture
  /// any withholding tax. Verified by staff, then credited to the account.
  Future<void> submitConsolidatedProof({
    required String amount,
    String? grossAmount,
    String? whtRate,
    String? bankName,
    String? reference,
    required String filePath,
    required String fileName,
  }) async {
    final form = FormData.fromMap({
      'amount': amount,
      if (grossAmount != null && grossAmount.isNotEmpty)
        'gross_amount': grossAmount,
      if (whtRate != null && whtRate.isNotEmpty) 'wht_rate': whtRate,
      if (bankName != null && bankName.isNotEmpty) 'bank_name': bankName,
      if (reference != null && reference.isNotEmpty) 'reference': reference,
      'file': await MultipartFile.fromFile(filePath, filename: fileName),
    });
    await guard(
        () => dio.post('/payment-proofs/reseller/consolidated', data: form));
  }

  /// GET /reseller/payment-methods — the reseller's saved cards.
  Future<List<SavedCard>> paymentMethods() async {
    final data = await guard(() => dio.get('/reseller/payment-methods'));
    return (data as List)
        .cast<Map<String, dynamic>>()
        .map(SavedCard.fromJson)
        .toList();
  }

  /// POST /reseller/payment-methods/{id}/default — make a card the default.
  Future<void> setDefaultCard(String id) async {
    await guard(() => dio.post('/reseller/payment-methods/$id/default'));
  }

  /// DELETE /reseller/payment-methods/{id} — remove a saved card.
  Future<void> removeCard(String id) async {
    await guard(() => dio.delete('/reseller/payment-methods/$id'));
  }

  /// GET /reseller/fiber-map — fiber plant GeoJSON for the coverage map.
  Future<ResellerFiberMap> fiberMap() async {
    final data = await guard(() => dio.get('/reseller/fiber-map'));
    return ResellerFiberMap.fromJson(data as Map<String, dynamic>);
  }

  /// GET /reseller/service-requests — my submitted requests.
  Future<List<ResellerServiceRequest>> serviceRequests() async {
    final data = await guard(() => dio.get('/reseller/service-requests'));
    final items = (data as Map<String, dynamic>)['items'] as List? ?? const [];
    return items
        .cast<Map<String, dynamic>>()
        .map(ResellerServiceRequest.fromJson)
        .toList();
  }

  /// POST /reseller/service-requests — submit a new-service request.
  Future<ResellerServiceRequest> createServiceRequest({
    String? subscriberId,
    String? contactName,
    String? contactPhone,
    String? contactEmail,
    String? address,
    double? latitude,
    double? longitude,
    String? notes,
  }) async {
    final data =
        await guard(() => dio.post('/reseller/service-requests', data: {
              if (subscriberId != null) 'subscriber_id': subscriberId,
              if (contactName != null && contactName.isNotEmpty)
                'contact_name': contactName,
              if (contactPhone != null && contactPhone.isNotEmpty)
                'contact_phone': contactPhone,
              if (contactEmail != null && contactEmail.isNotEmpty)
                'contact_email': contactEmail,
              if (address != null && address.isNotEmpty) 'address': address,
              if (latitude != null) 'latitude': latitude,
              if (longitude != null) 'longitude': longitude,
              if (notes != null && notes.isNotEmpty) 'notes': notes,
            }));
    return ResellerServiceRequest.fromJson(data as Map<String, dynamic>);
  }

  /// GET /reseller/accounts/{id}/tickets — CRM tickets for a managed account.
  Future<ResellerTicketsPage> accountTickets(String accountId) async {
    final data =
        await guard(() => dio.get('/reseller/accounts/$accountId/tickets'));
    return ResellerTicketsPage.fromJson(data as Map<String, dynamic>);
  }

  /// POST /reseller/accounts/{id}/impersonate — short-lived read-only
  /// customer token for "view as customer".
  Future<ResellerImpersonationGrant> impersonate(String accountId) async {
    final data = await guard(
        () => dio.post('/reseller/accounts/$accountId/impersonate'));
    return ResellerImpersonationGrant.fromJson(data as Map<String, dynamic>);
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

  // ── Sales/Quotes across the reseller's customers (B3) ──────────────────────

  /// GET /reseller/quotes — self-serve quotes for every managed customer.
  Future<List<ResellerQuote>> quotes() async {
    final data = await guard(() => dio.get('/reseller/quotes'));
    return parseResellerList(
        data as Map<String, dynamic>, 'quotes', ResellerQuote.fromJson);
  }

  /// GET /reseller/projects — installations across managed customers.
  Future<List<ResellerProject>> projects() async {
    final data = await guard(() => dio.get('/reseller/projects'));
    return parseResellerList(
        data as Map<String, dynamic>, 'projects', ResellerProject.fromJson);
  }

  /// GET /reseller/work-orders — field-service visits across managed customers.
  Future<List<ResellerWorkOrder>> workOrders() async {
    final data = await guard(() => dio.get('/reseller/work-orders'));
    return parseResellerList(data as Map<String, dynamic>, 'work_orders',
        ResellerWorkOrder.fromJson);
  }

  /// POST /reseller/accounts/{id}/quote-request — request a map-pinned quote on
  /// a managed customer's behalf (404 if the account isn't the reseller's).
  Future<Quote> requestQuoteForAccount(
    String accountId, {
    required double latitude,
    required double longitude,
    String? address,
    String? region,
    String? note,
  }) async {
    final data = await guard(
      () => dio.post('/reseller/accounts/$accountId/quote-request', data: {
        'latitude': latitude,
        'longitude': longitude,
        if (address != null && address.isNotEmpty) 'address': address,
        if (region != null && region.isNotEmpty) 'region': region,
        if (note != null && note.isNotEmpty) 'note': note,
      }),
    );
    return Quote.fromJson(data as Map<String, dynamic>);
  }
}
