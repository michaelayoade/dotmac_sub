import 'package:flutter/material.dart';

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
    final (bg, fg) = switch (tone) {
      StatusTone.positive => (Colors.green.shade100, Colors.green.shade900),
      StatusTone.negative => (Colors.red.shade100, Colors.red.shade900),
      StatusTone.warning => (Colors.orange.shade100, Colors.orange.shade900),
      StatusTone.neutral => (
          Colors.blueGrey.shade100,
          Colors.blueGrey.shade900
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
