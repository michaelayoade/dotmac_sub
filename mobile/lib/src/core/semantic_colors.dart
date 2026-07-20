import 'package:flutter/material.dart';

import '../config/env.dart';

/// Branding-owned semantic colors that adapt to light and dark themes.
///
/// Domain DTOs choose a semantic role. They never choose a concrete color.
@immutable
class SemanticColors extends ThemeExtension<SemanticColors> {
  const SemanticColors({
    required this.success,
    required this.onSuccess,
    required this.info,
    required this.onInfo,
    required this.warning,
    required this.onWarning,
    required this.negative,
    required this.onNegative,
    required this.neutral,
    required this.onNeutral,
  });

  final Color success;
  final Color onSuccess;
  final Color info;
  final Color onInfo;
  final Color warning;
  final Color onWarning;
  final Color negative;
  final Color onNegative;
  final Color neutral;
  final Color onNeutral;

  static final light = SemanticColors(
    success: Brand.semanticPositiveColor,
    onSuccess: _contrastForeground(Brand.semanticPositiveColor),
    info: Brand.semanticInfoColor,
    onInfo: _contrastForeground(Brand.semanticInfoColor),
    warning: Brand.semanticWarningColor,
    onWarning: _contrastForeground(Brand.semanticWarningColor),
    negative: Brand.semanticNegativeColor,
    onNegative: _contrastForeground(Brand.semanticNegativeColor),
    neutral: Brand.semanticNeutralColor,
    onNeutral: _contrastForeground(Brand.semanticNeutralColor),
  );

  static final dark = SemanticColors(
    success: _darkForeground(Brand.semanticPositiveColor),
    onSuccess:
        _contrastForeground(_darkForeground(Brand.semanticPositiveColor)),
    info: _darkForeground(Brand.semanticInfoColor),
    onInfo: _contrastForeground(_darkForeground(Brand.semanticInfoColor)),
    warning: _darkForeground(Brand.semanticWarningColor),
    onWarning: _contrastForeground(_darkForeground(Brand.semanticWarningColor)),
    negative: _darkForeground(Brand.semanticNegativeColor),
    onNegative:
        _contrastForeground(_darkForeground(Brand.semanticNegativeColor)),
    neutral: _darkForeground(Brand.semanticNeutralColor),
    onNeutral: _contrastForeground(_darkForeground(Brand.semanticNeutralColor)),
  );

  @override
  SemanticColors copyWith({
    Color? success,
    Color? onSuccess,
    Color? info,
    Color? onInfo,
    Color? warning,
    Color? onWarning,
    Color? negative,
    Color? onNegative,
    Color? neutral,
    Color? onNeutral,
  }) =>
      SemanticColors(
        success: success ?? this.success,
        onSuccess: onSuccess ?? this.onSuccess,
        info: info ?? this.info,
        onInfo: onInfo ?? this.onInfo,
        warning: warning ?? this.warning,
        onWarning: onWarning ?? this.onWarning,
        negative: negative ?? this.negative,
        onNegative: onNegative ?? this.onNegative,
        neutral: neutral ?? this.neutral,
        onNeutral: onNeutral ?? this.onNeutral,
      );

  @override
  SemanticColors lerp(ThemeExtension<SemanticColors>? other, double t) {
    if (other is! SemanticColors) return this;
    return SemanticColors(
      success: Color.lerp(success, other.success, t)!,
      onSuccess: Color.lerp(onSuccess, other.onSuccess, t)!,
      info: Color.lerp(info, other.info, t)!,
      onInfo: Color.lerp(onInfo, other.onInfo, t)!,
      warning: Color.lerp(warning, other.warning, t)!,
      onWarning: Color.lerp(onWarning, other.onWarning, t)!,
      negative: Color.lerp(negative, other.negative, t)!,
      onNegative: Color.lerp(onNegative, other.onNegative, t)!,
      neutral: Color.lerp(neutral, other.neutral, t)!,
      onNeutral: Color.lerp(onNeutral, other.onNeutral, t)!,
    );
  }
}

Color _darkForeground(Color color) => Color.lerp(color, Colors.white, 0.52)!;

Color _contrastForeground(Color color) =>
    color.computeLuminance() > 0.45 ? Colors.black : Colors.white;

extension SemanticColorsX on BuildContext {
  /// Theme-aware semantic colors. Falls back to the light set if the
  /// extension isn't registered (shouldn't happen).
  SemanticColors get semantic =>
      Theme.of(this).extension<SemanticColors>() ?? SemanticColors.light;
}
