import 'package:flutter/material.dart';

import '../core/semantic_colors.dart';

/// Small coloured label used for invoice/ticket/subscription statuses.
class StatusChip extends StatelessWidget {
  const StatusChip(this.label, {super.key, this.tone = StatusTone.neutral});

  final String label;
  final StatusTone tone;

  factory StatusChip.forInvoice(String status) {
    final tone = switch (status) {
      'paid' => StatusTone.positive,
      'overdue' || 'void' => StatusTone.negative,
      'issued' || 'open' => StatusTone.warning,
      _ => StatusTone.neutral,
    };
    return StatusChip(status, tone: tone);
  }

  factory StatusChip.forTicket(String status) {
    final tone = switch (status) {
      'resolved' || 'closed' => StatusTone.positive,
      'open' || 'new' => StatusTone.warning,
      'pending' || 'on_hold' => StatusTone.neutral,
      _ => StatusTone.neutral,
    };
    return StatusChip(status, tone: tone);
  }

  factory StatusChip.forSubscription(String status) {
    final tone = switch (status) {
      'active' => StatusTone.positive,
      'suspended' || 'canceled' => StatusTone.negative,
      'pending' => StatusTone.warning,
      _ => StatusTone.neutral,
    };
    return StatusChip(status, tone: tone);
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final semantic = context.semantic;
    // Tonal pill: a translucent fill of the foreground hue. Because the
    // semantic tokens (and colorScheme.error) already adapt to brightness,
    // this reads correctly in both light and dark mode.
    final (Color bg, Color fg) = switch (tone) {
      StatusTone.positive => (
          semantic.success.withValues(alpha: 0.15),
          semantic.success
        ),
      StatusTone.negative => (
          scheme.error.withValues(alpha: 0.15),
          scheme.error
        ),
      StatusTone.warning => (
          semantic.warning.withValues(alpha: 0.15),
          semantic.warning
        ),
      StatusTone.neutral => (
          scheme.surfaceContainerHighest,
          scheme.onSurfaceVariant
        ),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        label.replaceAll('_', ' '),
        style: TextStyle(color: fg, fontSize: 12, fontWeight: FontWeight.w600),
      ),
    );
  }
}

enum StatusTone { positive, negative, warning, neutral }
