import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/session.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

class SessionsScreen extends ConsumerWidget {
  const SessionsScreen({super.key});

  Future<void> _revoke(BuildContext context, WidgetRef ref, String id) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(authRepositoryProvider).revokeSession(id);
      ref.invalidate(sessionsProvider);
      messenger.showSnackBar(const SnackBar(content: Text('Session revoked')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<void> _revokeOthers(BuildContext context, WidgetRef ref) async {
    final messenger = ScaffoldMessenger.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Sign out other sessions?'),
        content:
            const Text('This signs you out everywhere except this device.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Sign out')),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await ref.read(authRepositoryProvider).revokeOtherSessions();
      ref.invalidate(sessionsProvider);
      messenger.showSnackBar(
          const SnackBar(content: Text('Other sessions signed out')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sessions = ref.watch(sessionsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Active sessions')),
      body: AsyncValueView(
        value: sessions,
        onRetry: () => ref.invalidate(sessionsProvider),
        data: (list) {
          final hasOthers = list.any((s) => !s.isCurrent);
          return RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(sessionsProvider);
              await ref.read(sessionsProvider.future);
            },
            child: ListView(
              padding: const EdgeInsets.all(12),
              children: [
                for (final s in list)
                  _SessionCard(
                    session: s,
                    onRevoke:
                        s.isCurrent ? null : () => _revoke(context, ref, s.id),
                  ),
                if (hasOthers) ...[
                  const SizedBox(height: 12),
                  FilledButton.tonalIcon(
                    style: FilledButton.styleFrom(
                      backgroundColor:
                          Theme.of(context).colorScheme.errorContainer,
                      foregroundColor:
                          Theme.of(context).colorScheme.onErrorContainer,
                    ),
                    icon: const Icon(Icons.logout),
                    label: const Text('Sign out all other sessions'),
                    onPressed: () => _revokeOthers(context, ref),
                  ),
                ],
              ],
            ),
          );
        },
      ),
    );
  }
}

class _SessionCard extends StatelessWidget {
  const _SessionCard({required this.session, this.onRevoke});
  final AuthSessionInfo session;
  final VoidCallback? onRevoke;

  @override
  Widget build(BuildContext context) {
    final s = session;
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Icon(
          s.isCurrent ? Icons.smartphone : Icons.devices_other,
          color: s.isCurrent ? Theme.of(context).colorScheme.primary : null,
        ),
        title: Row(
          children: [
            Flexible(child: Text(s.deviceLabel)),
            if (s.isCurrent) ...[
              const SizedBox(width: 8),
              const _Badge('This device'),
            ],
          ],
        ),
        subtitle: Text(
          '${s.ipAddress ?? 'unknown IP'}\n'
          'Last active ${Fmt.dateTime(s.lastSeenAt ?? s.createdAt)}',
        ),
        isThreeLine: true,
        trailing: onRevoke == null
            ? null
            : IconButton(
                tooltip: 'Revoke',
                icon: const Icon(Icons.close),
                onPressed: onRevoke,
              ),
      ),
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge(this.label);
  final String label;
  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: scheme.primaryContainer,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(label,
          style: TextStyle(
              fontSize: 11,
              color: scheme.onPrimaryContainer,
              fontWeight: FontWeight.w600)),
    );
  }
}
