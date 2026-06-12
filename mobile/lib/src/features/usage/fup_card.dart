import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/usage.dart';

/// Fair-Usage status card. Three states:
///  - blocked / throttled: explains the enforcement, when it lifts
///    ("Throttled until <reset>"), and offers remedy CTAs;
///  - approaching: pre-warns before enforcement with the remaining headroom.
///
/// Remedy CTAs are contextual: "Buy data" appears only when the plan actually
/// sells data bundles ([canBuyData]); "Upgrade plan" routes to the existing
/// change-plan flow. Both need [serviceId].
///
/// Public (not `_`-prefixed) so it can be widget-tested in isolation.
class FupCard extends StatelessWidget {
  const FupCard({
    super.key,
    required this.fup,
    this.serviceId,
    this.canBuyData = false,
  });

  final FupStatus fup;
  final String? serviceId;
  final bool canBuyData;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final blocked = fup.isBlocked;
    final approaching = fup.isApproaching;
    final accent = blocked
        ? scheme.error
        : approaching
            ? scheme.secondary
            : scheme.tertiary;
    final title = blocked
        ? 'Service paused'
        : approaching
            ? 'Approaching your limit'
            : 'Speed reduced';
    final icon = blocked
        ? Icons.block
        : approaching
            ? Icons.data_usage
            : Icons.speed;

    final fallback = blocked
        ? 'Your fair-usage limit has been reached.'
        : approaching
            ? 'You are close to your fair-usage limit.'
            : 'Speed reduced — fair-usage limit reached.';

    return Card(
      color: accent.withValues(alpha: 0.10),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: accent),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: theme.textTheme.titleMedium?.copyWith(
                          color: accent, fontWeight: FontWeight.w600)),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(fup.summary ?? fallback, style: theme.textTheme.bodyMedium),
            if (approaching && fup.gbUntilThrottle != null) ...[
              const SizedBox(height: 8),
              LinearProgressIndicator(
                value: (fup.usageRatio ?? 0).clamp(0.0, 1.0),
                minHeight: 8,
                borderRadius: BorderRadius.circular(4),
                color: accent,
              ),
            ],
            if (fup.needsAttention && fup.resetsAt != null) ...[
              const SizedBox(height: 4),
              Text(
                '${blocked ? 'Paused' : 'Throttled'} until '
                '${Fmt.date(fup.resetsAt!)}',
                style:
                    theme.textTheme.bodySmall?.copyWith(color: scheme.outline),
              ),
            ],
            if (serviceId != null) ...[
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (canBuyData)
                    FilledButton.tonalIcon(
                      onPressed: () =>
                          context.push('/service/$serviceId/buy-data'),
                      icon: const Icon(Icons.add_chart_outlined, size: 18),
                      label: Text(fup.needsAttention
                          ? 'Buy data to restore'
                          : 'Buy data'),
                    ),
                  OutlinedButton.icon(
                    onPressed: () =>
                        context.push('/service/$serviceId/change-plan'),
                    icon: const Icon(Icons.upgrade, size: 18),
                    label: const Text('Upgrade plan'),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
