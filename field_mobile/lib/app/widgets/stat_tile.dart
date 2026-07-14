import 'package:flutter/material.dart';

import '../theme.dart';

/// One number from the day's summary. The [highlighted] tile is the teal-filled
/// hero (e.g. "Assigned"); the rest are quiet surface tiles.
class StatTile extends StatelessWidget {
  const StatTile({
    super.key,
    required this.value,
    required this.label,
    this.unit,
    this.highlighted = false,
  });

  final String value;
  final String label;
  final String? unit;
  final bool highlighted;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final valueColor = highlighted
        ? Colors.white
        : (isDark ? AppColors.inkDark : AppColors.ink);
    final labelColor = highlighted
        ? Colors.white.withValues(alpha: 0.85)
        : (isDark ? AppColors.inkSoftDark : AppColors.inkSoft);

    return Container(
      padding: const EdgeInsets.fromLTRB(13, 12, 12, 11),
      decoration: BoxDecoration(
        gradient: highlighted
            ? LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [AppColors.primary, AppColors.primaryDeep],
              )
            : null,
        color: highlighted
            ? null
            : (isDark ? AppColors.surfaceDark : AppColors.surfaceLight),
        borderRadius: BorderRadius.circular(AppRadii.tile),
        border: highlighted
            ? null
            : Border.all(
                color: isDark ? AppColors.lineDark : AppColors.lineLight,
              ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text.rich(
            TextSpan(
              text: value,
              children: [
                if (unit != null)
                  TextSpan(text: unit, style: const TextStyle(fontSize: 13)),
              ],
            ),
            style: TextStyle(
              fontFamily: 'Outfit',
              fontSize: 26,
              height: 1,
              fontWeight: FontWeight.w800,
              color: valueColor,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
          const SizedBox(height: 5),
          Text(
            label,
            style: TextStyle(
              fontFamily: 'PlusJakartaSans',
              fontSize: 11,
              fontWeight: FontWeight.w600,
              color: labelColor,
            ),
          ),
        ],
      ),
    );
  }
}
