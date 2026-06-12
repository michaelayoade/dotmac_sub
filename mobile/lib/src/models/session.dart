/// Mirrors SessionInfoResponse from app/schemas/auth_flow.py.
class AuthSessionInfo {
  AuthSessionInfo({
    required this.id,
    required this.status,
    required this.isCurrent,
    this.ipAddress,
    this.userAgent,
    this.createdAt,
    this.lastSeenAt,
    this.expiresAt,
  });

  final String id;
  final String status;
  final bool isCurrent;
  final String? ipAddress;
  final String? userAgent;
  final DateTime? createdAt;
  final DateTime? lastSeenAt;
  final DateTime? expiresAt;

  /// A short, human-friendly device descriptor parsed from the user agent.
  String get deviceLabel {
    final ua = userAgent ?? '';
    if (ua.isEmpty) return 'Unknown device';
    if (ua.contains('Dart')) return 'Mobile app';
    final match = RegExp(
      r'(iPhone|iPad|Android|Macintosh|Windows|Linux|CrOS)',
    ).firstMatch(ua);
    final os = match?.group(1);
    String browser = '';
    if (ua.contains('Edg')) {
      browser = 'Edge';
    } else if (ua.contains('Chrome')) {
      browser = 'Chrome';
    } else if (ua.contains('Firefox')) {
      browser = 'Firefox';
    } else if (ua.contains('Safari')) {
      browser = 'Safari';
    }
    return [os, browser].where((e) => e != null && e.isNotEmpty).join(' · ');
  }

  factory AuthSessionInfo.fromJson(Map<String, dynamic> json) =>
      AuthSessionInfo(
        id: json['id'].toString(),
        status: json['status'] as String? ?? 'active',
        isCurrent: json['is_current'] as bool? ?? false,
        ipAddress: json['ip_address'] as String?,
        userAgent: json['user_agent'] as String?,
        createdAt: _toDate(json['created_at']),
        lastSeenAt: _toDate(json['last_seen_at']),
        expiresAt: _toDate(json['expires_at']),
      );
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
