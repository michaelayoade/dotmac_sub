import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/usage.dart';

/// Fair-Usage status card for the Usage tab. Shown when the caller is throttled
/// or blocked: explains the limit in plain language and offers a restore CTA.
///
/// Public (not `_`-prefixed) so it can be widget-tested in isolation.
class FupCard extends StatelessWidget {
  const FupCard({super.key, required this.fup});
  final FupStatus fup;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final blocked = fup.isBlocked;
    final accent = blocked ? scheme.error : scheme.tertiary;
    final title = blocked ? 'Service paused' : 'Speed reduced';

    return Card(
      color: accent.withValues(alpha: 0.10),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(blocked ? Icons.block : Icons.speed, color: accent),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: theme.textTheme.titleMedium?.copyWith(
                          color: accent, fontWeight: FontWeight.w600)),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              fup.summary ??
                  (blocked
                      ? 'Your fair-usage limit has been reached.'
                      : 'Speed reduced — fair-usage limit reached.'),
              style: theme.textTheme.bodyMedium,
            ),
            if (fup.resetsAt != null) ...[
              const SizedBox(height: 4),
              Text('Resets ${Fmt.date(fup.resetsAt!)}',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: scheme.outline)),
            ],
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: FilledButton.tonalIcon(
                onPressed: () => context.push('/topup'),
                icon: const Icon(Icons.add_card_outlined, size: 18),
                label: const Text('Top up to restore'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
