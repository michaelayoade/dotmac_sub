/// Transport-neutral status meaning supplied by the server.
///
/// Field workflow state stays in the raw value. The backend owns labels and
/// state-to-tone decisions; Flutter owns only Material rendering tokens.
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

  factory StatusPresentation.fromJsonOrFallback(
    Object? json,
    String fallbackValue,
  ) {
    if (json is Map) {
      return StatusPresentation.fromJson(json.cast<String, dynamic>());
    }
    return StatusPresentation.neutralFallback(fallbackValue);
  }

  /// Compatibility for cached/older payloads. Preserve the raw value and a
  /// readable label without recreating domain-specific semantic policy.
  factory StatusPresentation.neutralFallback(String value) {
    final normalized = value.trim().isEmpty ? 'unknown' : value.trim();
    final words = normalized.replaceAll('_', ' ');
    return StatusPresentation(
      value: normalized,
      label: words.isEmpty
          ? 'Unknown'
          : '${words[0].toUpperCase()}${words.substring(1)}',
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
