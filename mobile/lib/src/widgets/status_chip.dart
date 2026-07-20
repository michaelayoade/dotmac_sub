import 'package:flutter/material.dart';

import '../core/semantic_colors.dart';
import '../models/status_presentation.dart';

/// Platform-native rendering for the server's transport-neutral semantics.
({Color color, IconData icon}) statusPresentationVisual(
  BuildContext context,
  StatusPresentation presentation,
) {
  final semantic = context.semantic;
  final color = switch (presentation.tone) {
    StatusTone.positive => semantic.success,
    StatusTone.negative => semantic.negative,
    StatusTone.warning => semantic.warning,
    StatusTone.info => semantic.info,
    StatusTone.neutral => semantic.neutral,
  };
  return (
    color: color,
    icon: _statusIcon(presentation.icon) ?? Icons.info_outline,
  );
}

IconData? _statusIcon(String? icon) => switch (icon) {
      'check' => Icons.check_circle_outline,
      'info' => Icons.info_outline,
      'clock' => Icons.schedule,
      'alert' => Icons.warning_amber_rounded,
      'x' => Icons.cancel_outlined,
      'minus' => Icons.remove_circle_outline,
      'archive' => Icons.archive_outlined,
      _ => null,
    };

/// Small semantic status label rendered from server-owned presentation data.
class StatusChip extends StatelessWidget {
  const StatusChip(
    this.label, {
    super.key,
    this.tone = StatusTone.neutral,
    this.icon,
  });

  final String label;
  final StatusTone tone;
  final String? icon;

  factory StatusChip.fromPresentation(StatusPresentation presentation) =>
      StatusChip(
        presentation.label,
        tone: presentation.tone,
        icon: presentation.icon,
      );

  @override
  Widget build(BuildContext context) {
    final semantic = context.semantic;
    // Tonal pill: every hue is resolved by the branding theme extension.
    final (Color bg, Color fg) = switch (tone) {
      StatusTone.positive => (
          semantic.success.withValues(alpha: 0.15),
          semantic.success
        ),
      StatusTone.negative => (
          semantic.negative.withValues(alpha: 0.15),
          semantic.negative
        ),
      StatusTone.warning => (
          semantic.warning.withValues(alpha: 0.15),
          semantic.warning
        ),
      StatusTone.info => (semantic.info.withValues(alpha: 0.15), semantic.info),
      StatusTone.neutral => (
          semantic.neutral.withValues(alpha: 0.15),
          semantic.neutral
        ),
    };
    final iconData = _statusIcon(icon);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (iconData != null) ...[
            Icon(iconData, size: 13, color: fg),
            const SizedBox(width: 4),
          ],
          Text(
            label.replaceAll('_', ' '),
            style:
                TextStyle(color: fg, fontSize: 12, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}
