import 'package:flutter/material.dart';

/// Theme-aware semantic colors (success / warning) that adapt to light & dark.
/// Material's [ColorScheme] only ships error — these fill the gap so status
/// UI (active/success = green, caution = amber) is never a hardcoded `Colors.*`
/// that clashes in dark mode. Use via `context.semantic.success`, etc.
@immutable
class SemanticColors extends ThemeExtension<SemanticColors> {
  const SemanticColors({
    required this.success,
    required this.onSuccess,
    required this.warning,
    required this.onWarning,
  });

  final Color success;
  final Color onSuccess;
  final Color warning;
  final Color onWarning;

  static const light = SemanticColors(
    success: Color(0xFF2E7D32),
    onSuccess: Colors.white,
    warning: Color(0xFFB26A00),
    onWarning: Colors.white,
  );

  static const dark = SemanticColors(
    success: Color(0xFF81C784),
    onSuccess: Color(0xFF00350D),
    warning: Color(0xFFFFB74D),
    onWarning: Color(0xFF3A2A00),
  );

  @override
  SemanticColors copyWith({
    Color? success,
    Color? onSuccess,
    Color? warning,
    Color? onWarning,
  }) => SemanticColors(
    success: success ?? this.success,
    onSuccess: onSuccess ?? this.onSuccess,
    warning: warning ?? this.warning,
    onWarning: onWarning ?? this.onWarning,
  );

  @override
  SemanticColors lerp(ThemeExtension<SemanticColors>? other, double t) {
    if (other is! SemanticColors) return this;
    return SemanticColors(
      success: Color.lerp(success, other.success, t)!,
      onSuccess: Color.lerp(onSuccess, other.onSuccess, t)!,
      warning: Color.lerp(warning, other.warning, t)!,
      onWarning: Color.lerp(onWarning, other.onWarning, t)!,
    );
  }
}

extension SemanticColorsX on BuildContext {
  /// Theme-aware success/warning colors. Falls back to the light set if the
  /// extension isn't registered (shouldn't happen).
  SemanticColors get semantic =>
      Theme.of(this).extension<SemanticColors>() ?? SemanticColors.light;
}
