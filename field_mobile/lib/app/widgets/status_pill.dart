import 'package:flutter/material.dart';

import '../status_presentation.dart';
import '../theme.dart';

/// Status shown three ways at once — dot + colour + label — so it survives
/// sunlight and colour-blindness. Colour comes from the shared status ramp.
class StatusPill extends StatelessWidget {
  const StatusPill(this.presentation, {super.key, this.compact = false});

  final StatusPresentation presentation;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final color = AppColors.statusTone(context, presentation.tone);
    final icon = switch (presentation.icon) {
      'check' => Icons.check_circle_outline,
      'clock' => Icons.schedule,
      'alert' => Icons.warning_amber_rounded,
      'x' => Icons.cancel_outlined,
      'minus' => Icons.remove_circle_outline,
      'archive' => Icons.archive_outlined,
      _ => Icons.info_outline,
    };
    return Container(
      padding: EdgeInsets.symmetric(horizontal: compact ? 8 : 9, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.13),
        borderRadius: BorderRadius.circular(AppRadii.pill),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 13, color: color),
          const SizedBox(width: 6),
          Text(
            presentation.label.toUpperCase(),
            style: const TextStyle(
              fontFamily: 'PlusJakartaSans',
              fontSize: 10.5,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ).copyWith(color: color),
          ),
        ],
      ),
    );
  }
}
