/// Mirrors SubscriptionRead from app/schemas/catalog.py.
///
/// Note: the backend serialises `subscriber_id` as `account_id`
/// (serialization_alias), so we read `account_id` here.
class Subscription {
  Subscription({
    required this.id,
    required this.accountId,
    required this.offerId,
    required this.status,
    required this.billingMode,
    this.serviceDescription,
    this.offerName,
    this.offerServiceType,
    this.offerAccessType,
    this.login,
    this.ipv4Address,
    this.ipv6Address,
    this.macAddress,
    this.startAt,
    this.endAt,
    this.nextBillingAt,
  });

  final String id;
  final String accountId;
  final String offerId;
  final String status;
  final String billingMode; // prepaid | postpaid
  final String? serviceDescription;
  final String? offerName;
  final String? offerServiceType; // business | residential | ...
  final String? offerAccessType; // fiber | wireless | ...
  final String? login;
  final String? ipv4Address;
  final String? ipv6Address;
  final String? macAddress;
  final DateTime? startAt;
  final DateTime? endAt;
  final DateTime? nextBillingAt;

  bool get isActive => status == 'active';
  bool get isPrepaid => billingMode == 'prepaid';

  String get displayName =>
      offerName ?? serviceDescription ?? 'Subscription ${id.substring(0, 8)}';

  /// "Business · fiber" style plan descriptor, when available.
  String? get planType {
    final parts = [offerServiceType, offerAccessType]
        .where((e) => e != null && e.isNotEmpty)
        .toList();
    return parts.isEmpty ? null : parts.join(' · ');
  }

  /// When the service lapses: explicit end date, else the next billing date
  /// (for prepaid this is effectively the expiry).
  DateTime? get expiresAt => endAt ?? nextBillingAt;

  /// Whole days until [expiresAt]; negative if already expired, null if unknown.
  int? get daysUntilExpiry {
    final e = expiresAt;
    if (e == null) return null;
    return e.difference(DateTime.now()).inDays;
  }

  factory Subscription.fromJson(Map<String, dynamic> json) {
    final offer = json['offer'];
    String? offerField(String key) =>
        offer is Map ? offer[key] as String? : null;
    return Subscription(
      id: json['id'].toString(),
      accountId: (json['account_id'] ?? json['subscriber_id']).toString(),
      offerId: json['offer_id'].toString(),
      status: json['status'] as String? ?? 'pending',
      billingMode: json['billing_mode'] as String? ?? 'prepaid',
      serviceDescription: json['service_description'] as String?,
      offerName: offerField('name'),
      offerServiceType: offerField('service_type'),
      offerAccessType: offerField('access_type'),
      login: json['login'] as String?,
      ipv4Address: json['ipv4_address'] as String?,
      ipv6Address: json['ipv6_address'] as String?,
      macAddress: json['mac_address'] as String?,
      startAt: _toDate(json['start_at']),
      endAt: _toDate(json['end_at']),
      nextBillingAt: _toDate(json['next_billing_at']),
    );
  }
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
