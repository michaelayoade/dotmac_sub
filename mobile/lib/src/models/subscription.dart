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
    this.downloadMbps,
    this.uploadMbps,
    this.login,
    this.ipv4Address,
    this.ipv6Address,
    this.macAddress,
    this.startAt,
    this.endAt,
    this.nextBillingAt,
    this.serverExpiresAt,
    this.serverIsExpired,
    this.hasServerExpiry = false,
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
  final int? downloadMbps; // provisioned line rate (offer)
  final int? uploadMbps;
  final String? login;
  final String? ipv4Address;
  final String? ipv6Address;
  final String? macAddress;
  final DateTime? startAt;
  final DateTime? endAt;
  final DateTime? nextBillingAt;

  /// Server-computed authoritative expiry (catalog.py `expires_at`/`is_expired`):
  /// the backend is the source of truth for when a service genuinely lapses, so
  /// the client doesn't have to guess from billing dates. [hasServerExpiry] is
  /// false against older backends / offline cache, where we fall back to local
  /// mode-aware logic.
  final DateTime? serverExpiresAt;
  final bool? serverIsExpired;
  final bool hasServerExpiry;

  bool get isActive => status == 'active';
  bool get isPrepaid => billingMode == 'prepaid';

  /// Statuses that are operationally relevant to the customer right now.
  /// Terminal/historical ones (disabled, canceled, expired, hidden, archived)
  /// stay out of the dashboard switcher, banners and service counts.
  static const currentStatuses = {
    'pending',
    'active',
    'blocked',
    'suspended',
    'stopped',
  };

  bool get isCurrent => currentStatuses.contains(status);

  /// Out of service for a reason the customer can fix by paying:
  /// blocked = non-payment block, suspended = generic suspension.
  bool get needsPayment => status == 'blocked' || status == 'suspended';

  String get displayName =>
      offerName ?? serviceDescription ?? 'Subscription ${id.substring(0, 8)}';

  /// Provisioned line rate as "↓100 ↑50 Mbps" (download / upload), or null when
  /// the offer carries no speeds.
  String? get speedSummary {
    if (downloadMbps == null && uploadMbps == null) return null;
    final d = downloadMbps?.toString() ?? '—';
    final u = uploadMbps?.toString() ?? '—';
    return '↓$d ↑$u Mbps';
  }

  /// "Business · fiber" style plan descriptor, when available.
  String? get planType {
    final parts = [offerServiceType, offerAccessType]
        .where((e) => e != null && e.isNotEmpty)
        .toList();
    return parts.isEmpty ? null : parts.join(' · ');
  }

  /// Whether the service has a date-based expiry at all. Postpaid bills in
  /// arrears — it does NOT lapse on [nextBillingAt] (that's just the next
  /// invoice date), so postpaid has no expiry unless a contract [endAt] is set.
  /// Only prepaid validity (or an explicit contract end) is a real expiry.
  bool get hasExpiry => endAt != null || isPrepaid;

  /// When the service lapses, or null when it has none. Prefer the server's
  /// authoritative value; fall back to local mode-aware logic when the backend
  /// didn't supply it (older API / offline cache). Note: postpaid and healthy
  /// prepaid have no date expiry — the real prepaid lapse (low balance → grace)
  /// comes from GET /me/service-status, not from next_billing_at.
  DateTime? get expiresAt => hasServerExpiry
      ? serverExpiresAt
      : (hasExpiry ? (endAt ?? nextBillingAt) : null);

  /// Whole days until [expiresAt]; negative if already past, null when there is
  /// no date-based expiry (postpaid) or it's unknown.
  int? get daysUntilExpiry {
    final e = expiresAt;
    if (e == null) return null;
    return e.difference(DateTime.now()).inDays;
  }

  /// Genuinely lapsed: a past expiry on a service that is NOT active. An active
  /// service is never "expired" — `status` is the source of truth, and a
  /// momentarily-stale billing date (e.g. a prepaid validity date the runner
  /// hasn't advanced yet) must not override a running service.
  bool get isExpired {
    if (hasServerExpiry) return serverIsExpired ?? false;
    if (isActive) return false;
    final d = daysUntilExpiry;
    return d != null && d < 0;
  }

  /// Within the 3-day renewal-nudge window and not already past. False for
  /// services without a date-based expiry (postpaid).
  bool get expiresSoon {
    final d = daysUntilExpiry;
    return d != null && d >= 0 && d <= 3;
  }

  factory Subscription.fromJson(Map<String, dynamic> json) {
    final offer = json['offer'];
    String? offerField(String key) =>
        offer is Map ? offer[key] as String? : null;
    int? offerInt(String key) =>
        offer is Map ? (offer[key] as num?)?.toInt() : null;
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
      downloadMbps: offerInt('speed_download_mbps'),
      uploadMbps: offerInt('speed_upload_mbps'),
      login: json['login'] as String?,
      ipv4Address: json['ipv4_address'] as String?,
      ipv6Address: json['ipv6_address'] as String?,
      macAddress: json['mac_address'] as String?,
      startAt: _toDate(json['start_at']),
      endAt: _toDate(json['end_at']),
      nextBillingAt: _toDate(json['next_billing_at']),
      serverExpiresAt: _toDate(json['expires_at']),
      serverIsExpired: json['is_expired'] as bool?,
      hasServerExpiry: json.containsKey('is_expired'),
    );
  }
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
