/// Transport-neutral status meaning supplied by the server.
///
/// The backend owns labels and state-to-tone decisions. Flutter only maps the
/// semantic tone to Material colors and the icon key to a platform icon.
class StatusPresentation {
  const StatusPresentation({
    required this.value,
    required this.label,
    required this.tone,
    required this.icon,
  });

  final String value;
  final String label;
  final StatusTone tone;
  final String icon;

  factory StatusPresentation.fromJson(Map<String, dynamic> json) =>
      StatusPresentation(
        value: json['value'] as String? ?? 'unknown',
        label: json['label'] as String? ?? 'Unknown',
        tone: StatusTone.fromWire(json['tone'] as String?),
        icon: json['icon'] as String? ?? 'info',
      );

  /// Compatibility for older/offline payloads: preserve the value and label,
  /// but stay visually neutral instead of recreating domain-specific policy.
  factory StatusPresentation.neutralFallback(String value) {
    final normalized = value.trim().isEmpty ? 'unknown' : value.trim();
    final words = normalized.replaceAll('_', ' ');
    final label = words
        .split(RegExp(r'\s+'))
        .where((word) => word.isNotEmpty)
        .map((word) => '${word[0].toUpperCase()}${word.substring(1)}')
        .join(' ');
    return StatusPresentation(
      value: normalized,
      label: label.isEmpty ? 'Unknown' : label,
      tone: StatusTone.neutral,
      icon: 'info',
    );
  }
}

enum StatusTone {
  positive,
  info,
  warning,
  negative,
  neutral;

  factory StatusTone.fromWire(String? value) => switch (value) {
        'positive' => StatusTone.positive,
        'info' => StatusTone.info,
        'warning' => StatusTone.warning,
        'negative' => StatusTone.negative,
        _ => StatusTone.neutral,
      };
}
