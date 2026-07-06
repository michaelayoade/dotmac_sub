/// Mirrors the customer-safe payload from the outage classifier P4 surface
/// (GET /me/connection-status, backed by app/services/topology/connection_status.py).
///
/// Answers "what's wrong with my connection?" — the per-customer last-mile
/// verdict, with area-outage blame suppression already applied server-side.
/// Customer-safe by construction: it carries NO node names, signal values, or
/// verdict internals. When [areaOutage] is true the server has deliberately
/// dropped [advice] (don't tell 200 people on a cut splitter to reboot), so the
/// UI must not re-introduce self-blame advice in that case.
enum ConnectionHealth {
  connected,
  trouble,
  outage,
  unknown;

  static ConnectionHealth fromWire(String? v) {
    switch (v) {
      case 'connected':
        return ConnectionHealth.connected;
      case 'trouble':
        return ConnectionHealth.trouble;
      case 'outage':
        return ConnectionHealth.outage;
      default:
        return ConnectionHealth.unknown;
    }
  }
}

class ConnectionStatus {
  const ConnectionStatus({
    required this.state,
    required this.headline,
    required this.message,
    required this.areaOutage,
    this.advice,
    this.medium,
    this.checkedAt,
  });

  final ConnectionHealth state;
  final String headline;
  final String message;

  /// The one action for the customer to take, or null when there's nothing for
  /// them to do (we're fixing it, or an area outage suppresses self-blame).
  final String? advice;

  /// fiber | wireless | unknown | null — the customer's own access medium.
  final String? medium;

  /// True when this customer sits under a known area outage; the UI shows the
  /// reassuring "we're on it" treatment and never self-blame advice.
  final bool areaOutage;

  /// When the status was computed (server clock), or null for the calm
  /// no-active-service fallback.
  final DateTime? checkedAt;

  bool get isConnected => state == ConnectionHealth.connected;

  factory ConnectionStatus.fromJson(Map<String, dynamic> json) {
    return ConnectionStatus(
      state: ConnectionHealth.fromWire(json['state'] as String?),
      headline: json['headline'] as String? ?? 'Connection status',
      message: json['message'] as String? ?? '',
      advice: json['advice'] as String?,
      medium: json['medium'] as String?,
      areaOutage: json['area_outage'] as bool? ?? false,
      checkedAt: _toDate(json['checked_at']),
    );
  }
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
