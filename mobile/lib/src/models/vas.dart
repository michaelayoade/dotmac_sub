/// Mirrors Vas* schemas from app/schemas/vas.py (Phase 2 — bill payments).
class VasVariation {
  VasVariation({required this.code, required this.name, this.amount});

  final String code;
  final String name;
  final double? amount;

  factory VasVariation.fromJson(Map<String, dynamic> json) => VasVariation(
        code: json['code'] as String,
        name: json['name'] as String? ?? json['code'] as String,
        amount: json['amount'] != null
            ? double.tryParse(json['amount'].toString())
            : null,
      );
}

class VasService {
  VasService({
    required this.serviceId,
    required this.name,
    required this.identifierLabel,
    required this.requiresVerify,
    this.minAmount,
    this.maxAmount,
    this.variations = const [],
  });

  final String serviceId;
  final String name;
  final String identifierLabel;
  final bool requiresVerify;
  final double? minAmount;
  final double? maxAmount;
  final List<VasVariation> variations;

  factory VasService.fromJson(Map<String, dynamic> json) => VasService(
        serviceId: json['service_id'] as String,
        name: json['name'] as String? ?? '',
        identifierLabel: json['identifier_label'] as String? ?? 'Phone number',
        requiresVerify: json['requires_verify'] as bool? ?? false,
        minAmount: json['min_amount'] != null
            ? double.tryParse(json['min_amount'].toString())
            : null,
        maxAmount: json['max_amount'] != null
            ? double.tryParse(json['max_amount'].toString())
            : null,
        variations: [
          for (final item in (json['variations'] as List? ?? const []))
            VasVariation.fromJson(item as Map<String, dynamic>),
        ],
      );
}

class VasCategory {
  VasCategory({required this.category, this.services = const []});

  final String category;
  final List<VasService> services;

  String get label => category
      .split('-')
      .map((w) => w.isEmpty ? w : '${w[0].toUpperCase()}${w.substring(1)}')
      .join(' ');

  factory VasCategory.fromJson(Map<String, dynamic> json) => VasCategory(
        category: json['category'] as String,
        services: [
          for (final item in (json['services'] as List? ?? const []))
            VasService.fromJson(item as Map<String, dynamic>),
        ],
      );
}

class VasTransaction {
  VasTransaction({
    required this.id,
    required this.status,
    required this.identifier,
    required this.amount,
    this.serviceName,
    this.token,
    this.error,
    this.createdAt,
  });

  final String id;
  final String status;
  final String identifier;
  final double amount;
  final String? serviceName;
  final String? token;
  final String? error;
  final DateTime? createdAt;

  bool get isDelivered => status == 'delivered';
  bool get isRefunded => status == 'refunded' || status == 'failed';
  bool get isProcessing =>
      status == 'submitted' || status == 'debited' || status == 'pending';

  factory VasTransaction.fromJson(Map<String, dynamic> json) => VasTransaction(
        id: json['id'].toString(),
        status: json['status'] as String? ?? 'pending',
        identifier: json['identifier'] as String? ?? '',
        amount: double.tryParse(json['amount'].toString()) ?? 0,
        serviceName: json['service_name'] as String?,
        token: json['token'] as String?,
        error: json['error'] as String?,
        createdAt: json['created_at'] != null
            ? DateTime.tryParse(json['created_at'] as String)
            : null,
      );
}
