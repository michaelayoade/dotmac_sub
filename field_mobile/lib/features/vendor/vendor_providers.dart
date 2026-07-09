import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';

class VendorProject {
  const VendorProject({required this.id, required this.status, this.notes});

  final String id;
  final String status;
  final String? notes;

  factory VendorProject.fromJson(Map<String, dynamic> json) => VendorProject(
        id: json['id'] as String,
        status: json['status'] as String? ?? 'unknown',
        notes: json['notes'] as String?,
      );
}

/// A stage of a job's lifecycle (quote / as-built / billing). Any field may be
/// null until the crew reaches that stage.
class VendorStageState {
  const VendorStageState({this.status, this.label});

  final String? status;

  /// A short human line for the chip subtitle (e.g. total, invoice no.).
  final String? label;

  bool get isPresent => status != null;
}

/// Per-job lifecycle: bid → approval → as-built → payment. Mirrors the backend
/// VendorProjectLifecycle bundle (#123).
class VendorLifecycle {
  const VendorLifecycle({this.quote, this.asBuilt, this.billing});

  final VendorStageState? quote;
  final VendorStageState? asBuilt;
  final VendorStageState? billing;

  static String? _money(num? total, String? currency) {
    if (total == null) return null;
    final amount = total.toStringAsFixed(0);
    return currency != null ? '$currency $amount' : amount;
  }

  factory VendorLifecycle.fromJson(Map<String, dynamic> json) {
    final quote = (json['quote'] as Map?)?.cast<String, dynamic>();
    final asBuilt = (json['as_built'] as Map?)?.cast<String, dynamic>();
    final billing = (json['billing'] as Map?)?.cast<String, dynamic>();
    return VendorLifecycle(
      quote: quote == null
          ? null
          : VendorStageState(
              status: quote['status'] as String?,
              label: _money(quote['total'] as num?, quote['currency'] as String?),
            ),
      asBuilt: asBuilt == null
          ? null
          : VendorStageState(status: asBuilt['status'] as String?),
      billing: billing == null
          ? null
          : VendorStageState(
              status: billing['status'] as String?,
              label: (billing['erp_synced'] as bool? ?? false)
                  ? 'Synced to ERP'
                  : _money(billing['total'] as num?, billing['currency'] as String?),
            ),
    );
  }
}

/// Who to call and where to go — the site bundle from the project detail (#122).
class VendorSite {
  const VendorSite({
    this.name,
    this.phone,
    this.email,
    this.addressText,
    this.accessNotes,
  });

  final String? name;
  final String? phone;
  final String? email;
  final String? addressText;
  final String? accessNotes;

  bool get hasContact => (name != null && name!.isNotEmpty) || phone != null;

  factory VendorSite.fromJson(Map<String, dynamic> json) => VendorSite(
        name: json['name'] as String?,
        phone: json['phone'] as String?,
        email: json['email'] as String?,
        addressText: json['address_text'] as String?,
        accessNotes: json['access_notes'] as String?,
      );
}

/// A list row: the project plus its lifecycle state.
class VendorProjectListItem {
  const VendorProjectListItem({required this.project, this.lifecycle});

  final VendorProject project;
  final VendorLifecycle? lifecycle;

  factory VendorProjectListItem.fromJson(Map<String, dynamic> json) => VendorProjectListItem(
        project: VendorProject.fromJson((json['project'] as Map).cast<String, dynamic>()),
        lifecycle: json['lifecycle'] != null
            ? VendorLifecycle.fromJson((json['lifecycle'] as Map).cast<String, dynamic>())
            : null,
      );
}

class AsBuiltSubmission {
  const AsBuiltSubmission({required this.id, required this.status, this.actualLengthMeters, this.reviewNotes});

  final String id;
  final String status;
  final double? actualLengthMeters;
  final String? reviewNotes;

  factory AsBuiltSubmission.fromJson(Map<String, dynamic> json) => AsBuiltSubmission(
        id: json['id'] as String,
        status: json['status'] as String? ?? 'submitted',
        actualLengthMeters: (json['actual_length_meters'] as num?)?.toDouble(),
        reviewNotes: json['review_notes'] as String?,
      );
}

/// A quote (bid) the crew is preparing or has submitted.
class VendorQuote {
  const VendorQuote({required this.id, required this.status, this.total, this.currency});

  final String id;
  final String status;
  final num? total;
  final String? currency;

  bool get isEditable => status == 'draft' || status == 'revision_requested';

  factory VendorQuote.fromJson(Map<String, dynamic> json) => VendorQuote(
        id: json['id'] as String,
        status: json['status'] as String? ?? 'draft',
        total: json['total'] as num?,
        currency: json['currency'] as String?,
      );
}

/// A saved line on a quote (as returned by the API, with its id + amount).
class QuoteLine {
  const QuoteLine({required this.id, this.description, this.quantity, this.unitPrice, this.amount});

  final String id;
  final String? description;
  final num? quantity;
  final num? unitPrice;
  final num? amount;

  factory QuoteLine.fromJson(Map<String, dynamic> json) => QuoteLine(
        id: json['id'] as String,
        description: json['description'] as String?,
        quantity: json['quantity'] as num?,
        unitPrice: json['unit_price'] as num?,
        amount: json['amount'] as num?,
      );
}

/// A proposed-route revision attached to a quote — the map half of the bid.
class ProposedRoute {
  const ProposedRoute({required this.id, required this.revisionNumber, required this.status});

  final String id;
  final int revisionNumber;
  final String status;

  factory ProposedRoute.fromJson(Map<String, dynamic> json) => ProposedRoute(
        id: json['id'] as String,
        revisionNumber: (json['revision_number'] as num?)?.toInt() ?? 1,
        status: json['status'] as String? ?? 'draft',
      );
}

class VendorQuoteDetail {
  const VendorQuoteDetail({required this.quote, this.lineItems = const [], this.proposedRoutes = const []});

  final VendorQuote quote;
  final List<QuoteLine> lineItems;
  final List<ProposedRoute> proposedRoutes;

  factory VendorQuoteDetail.fromJson(Map<String, dynamic> json) => VendorQuoteDetail(
        quote: VendorQuote.fromJson((json['quote'] as Map).cast<String, dynamic>()),
        lineItems: ((json['line_items'] as List?) ?? [])
            .cast<Map>()
            .map((i) => QuoteLine.fromJson(i.cast<String, dynamic>()))
            .toList(),
        proposedRoutes: ((json['proposed_routes'] as List?) ?? [])
            .cast<Map>()
            .map((r) => ProposedRoute.fromJson(r.cast<String, dynamic>()))
            .toList(),
      );
}

class VendorProjectDetail {
  const VendorProjectDetail({
    required this.project,
    this.site,
    this.lifecycle,
    this.submissions = const [],
    this.rejectedForResubmission,
  });

  final VendorProject project;
  final VendorSite? site;
  final VendorLifecycle? lifecycle;
  final List<AsBuiltSubmission> submissions;

  /// Set when the latest submission was rejected: the capture flow pre-fills
  /// from it so the crew fixes rather than restarts.
  final AsBuiltSubmission? rejectedForResubmission;

  factory VendorProjectDetail.fromJson(Map<String, dynamic> json) => VendorProjectDetail(
        project: VendorProject.fromJson((json['project'] as Map).cast<String, dynamic>()),
        site: json['site'] != null
            ? VendorSite.fromJson((json['site'] as Map).cast<String, dynamic>())
            : null,
        lifecycle: json['lifecycle'] != null
            ? VendorLifecycle.fromJson((json['lifecycle'] as Map).cast<String, dynamic>())
            : null,
        submissions: ((json['submissions'] as List?) ?? [])
            .cast<Map>()
            .map((s) => AsBuiltSubmission.fromJson(s.cast<String, dynamic>()))
            .toList(),
        rejectedForResubmission: json['rejected_for_resubmission'] != null
            ? AsBuiltSubmission.fromJson(
                (json['rejected_for_resubmission'] as Map).cast<String, dynamic>())
            : null,
      );
}

/// A material/labour line on an as-built submission. Mirrors the backend
/// AsBuiltLineItemInput; the crew captures what was actually installed so the
/// reviewer can reconcile against the quote.
class AsBuiltLineItem {
  const AsBuiltLineItem({
    this.itemType,
    this.description,
    this.cableType,
    this.fiberCount,
    this.spliceCount,
    this.quantity = 1,
    this.unitPrice = 0,
  });

  final String? itemType;
  final String? description;
  final String? cableType;
  final int? fiberCount;
  final int? spliceCount;
  final num quantity;
  final num unitPrice;

  Map<String, dynamic> toJson() => {
        'item_type': ?itemType,
        'description': ?description,
        'cable_type': ?cableType,
        'fiber_count': ?fiberCount,
        'splice_count': ?spliceCount,
        'quantity': quantity,
        'unit_price': unitPrice,
      };
}

/// As-built variation types — matches the backend VariationType enum.
const asBuiltVariationTypes = <String>[
  'scope_change',
  'route_deviation',
  'material_change',
  'additional_work',
  'reduction',
];

class VendorRepository {
  VendorRepository(this._ref);

  final Ref _ref;

  Future<List<VendorProjectListItem>> fetchProjects() async {
    final response = await _ref.read(apiClientProvider).dio.get('/api/v1/field/projects');
    final items = (response.data['items'] as List).cast<Map>();
    return items.map((item) => VendorProjectListItem.fromJson(item.cast<String, dynamic>())).toList();
  }

  Future<VendorProjectDetail> fetchDetail(String projectId) async {
    final response = await _ref.read(apiClientProvider).dio.get('/api/v1/field/projects/$projectId');
    return VendorProjectDetail.fromJson((response.data as Map).cast<String, dynamic>());
  }

  /// Queue an as-built submission through the offline outbox.
  Future<String> submitAsBuilt({
    required String projectId,
    required Map<String, dynamic> geojson,
    required double actualLengthMeters,
    String? variationReason,
    String? variationType,
    List<AsBuiltLineItem> lineItems = const [],
  }) async {
    final clientRef = const Uuid().v4();
    await _ref.read(syncServiceProvider).enqueue(
      kind: 'as_built',
      clientRef: clientRef,
      payload: {
        'project_id': projectId,
        'geojson': geojson,
        'actual_length_meters': double.parse(actualLengthMeters.toStringAsFixed(1)),
        'variation_reason': ?variationReason,
        'variation_type': ?variationType,
        if (lineItems.isNotEmpty) 'line_items': [for (final item in lineItems) item.toJson()],
      },
    );
    await _ref.read(syncServiceProvider).flushOutbox();
    return clientRef;
  }

  // --- Quoting (bid). Online round-trips: the backend guards state, and the
  // crew needs the server-assigned quote id before adding lines / submitting. ---

  Dio get _dio => _ref.read(apiClientProvider).dio;

  /// Open (or resume) this project's draft quote — the bid entry point.
  Future<VendorQuote> openQuoteDraft(String projectId) async {
    final response = await _dio.post('/api/v1/field/projects/$projectId/quote');
    return VendorQuote.fromJson((response.data as Map).cast<String, dynamic>());
  }

  Future<VendorQuoteDetail> fetchQuote(String quoteId) async {
    final response = await _dio.get('/api/v1/field/quotes/$quoteId');
    return VendorQuoteDetail.fromJson((response.data as Map).cast<String, dynamic>());
  }

  /// Queue a bid line item through the offline outbox so it survives a
  /// connectivity drop mid-bid; the unique client_ref makes the retried POST
  /// idempotent server-side (no duplicate line). Flushes immediately when online.
  Future<void> addQuoteLineItem(String quoteId, AsBuiltLineItem item) async {
    final clientRef = const Uuid().v4();
    await _ref.read(syncServiceProvider).enqueue(
      kind: 'quote_line_item',
      clientRef: clientRef,
      payload: {'quote_id': quoteId, 'client_ref': clientRef, ...item.toJson()},
    );
    await _ref.read(syncServiceProvider).flushOutbox();
  }

  Future<void> removeQuoteLineItem(String quoteId, String lineItemId) async {
    await _dio.delete('/api/v1/field/quotes/$quoteId/line-items/$lineItemId');
  }

  /// Attach the proposed route (drawn/walked on the map) and submit it.
  Future<void> addProposedRoute(String quoteId, Map<String, dynamic> geojson, double lengthMeters) async {
    await _dio.post(
      '/api/v1/field/quotes/$quoteId/proposed-route',
      data: {'geojson': geojson, 'length_meters': double.parse(lengthMeters.toStringAsFixed(1))},
    );
  }

  Future<VendorQuote> submitQuote(String quoteId) async {
    final response = await _dio.post('/api/v1/field/quotes/$quoteId/submit');
    return VendorQuote.fromJson((response.data as Map).cast<String, dynamic>());
  }
}

final vendorRepositoryProvider = Provider<VendorRepository>(VendorRepository.new);

final vendorProjectsProvider =
    FutureProvider<List<VendorProjectListItem>>((ref) => ref.watch(vendorRepositoryProvider).fetchProjects());

final vendorProjectDetailProvider = FutureProvider.family<VendorProjectDetail, String>(
  (ref, projectId) => ref.watch(vendorRepositoryProvider).fetchDetail(projectId),
);

/// Opens/resumes the project's draft quote then loads its detail — the quote
/// screen watches this so a pull-to-refresh re-reads server state.
final vendorProjectQuoteProvider = FutureProvider.family<VendorQuoteDetail, String>((ref, projectId) async {
  final repo = ref.watch(vendorRepositoryProvider);
  final quote = await repo.openQuoteDraft(projectId);
  return repo.fetchQuote(quote.id);
});
