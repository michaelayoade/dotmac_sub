import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/token_store.dart';
import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import '../jobs/jobs_providers.dart';
import 'vendor_profile_provider.dart';

/// Live counts from the offline queues.
final pendingOutboxProvider = StreamProvider<List<OutboxEntry>>((ref) {
  final db = ref.watch(syncServiceProvider).db;
  return (db.select(
    db.outboxEntries,
  )..where((row) => row.status.equals('pending'))).watch();
});

final conflictOutboxProvider = StreamProvider<List<OutboxEntry>>((ref) {
  final db = ref.watch(syncServiceProvider).db;
  return (db.select(
    db.outboxEntries,
  )..where((row) => row.status.equals('conflict'))).watch();
});

final pendingPhotosProvider = StreamProvider<int>((ref) {
  final db = ref.watch(syncServiceProvider).db;
  return (db.select(db.pendingPhotos)
        ..where((row) => row.uploaded.equals(false)))
      .watch()
      .map((rows) => rows.length);
});

class ProfileScreen extends ConsumerWidget {
  const ProfileScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    final me = auth is Authenticated && auth.mode == LoginMode.vendor
        ? null
        : ref.watch(meProvider);
    final vendorMe = auth is Authenticated && auth.mode == LoginMode.vendor
        ? ref.watch(vendorProfileProvider)
        : null;
    final pendingState = ref.watch(pendingOutboxProvider);
    final conflictState = ref.watch(conflictOutboxProvider);
    final photosState = ref.watch(pendingPhotosProvider);
    final queueUnavailable =
        pendingState.hasError || conflictState.hasError || photosState.hasError;
    final queuesReady =
        pendingState.asData != null &&
        conflictState.asData != null &&
        photosState.asData != null;
    final pending = pendingState.asData?.value;
    final conflicts = conflictState.asData?.value;
    final pendingPhotos = photosState.asData?.value;

    return Scaffold(
      appBar: AppBar(title: const Text('Profile')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (vendorMe != null)
            vendorMe.when(
              data: (data) => Card(
                child: ListTile(
                  leading: CircleAvatar(
                    child: Text(data.name.isEmpty ? '?' : data.name[0]),
                  ),
                  title: Text(data.name),
                  subtitle:
                      [
                        data.email,
                        data.vendorName,
                        data.vendorRole,
                      ].whereType<String>().any((value) => value.isNotEmpty)
                      ? Text(
                          [data.email, data.vendorName, data.vendorRole]
                              .whereType<String>()
                              .where((value) => value.isNotEmpty)
                              .join(' · '),
                        )
                      : null,
                ),
              ),
              loading: () => const Card(
                child: ListTile(
                  leading: CircularProgressIndicator(),
                  title: Text('Loading profile…'),
                ),
              ),
              error: (_, _) => Card(
                child: ListTile(
                  leading: const Icon(Icons.error_outline),
                  title: const Text('Could not load your profile'),
                  subtitle: const Text('Check your connection and try again.'),
                  trailing: TextButton(
                    key: const Key('retry-vendor-profile'),
                    onPressed: () => ref.invalidate(vendorProfileProvider),
                    child: const Text('Retry'),
                  ),
                ),
              ),
            )
          else if (me != null)
            me.when(
              data: (data) => Card(
                child: ListTile(
                  leading: CircleAvatar(
                    child: Text(data.name.isEmpty ? '?' : data.name[0]),
                  ),
                  title: Text(data.name),
                  subtitle: Text(
                    '${data.openJobs} open · ${data.completedToday} done today',
                  ),
                ),
              ),
              loading: () => const SizedBox(height: 72),
              error: (_, _) => const SizedBox.shrink(),
            ),
          const SizedBox(height: 16),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Sync', style: Theme.of(context).textTheme.titleSmall),
                  const SizedBox(height: 8),
                  if (queueUnavailable) ...[
                    const Text('Sync status unavailable'),
                    const Text('Could not read the local queues. Try again.'),
                    TextButton(
                      key: const Key('retry-sync-status'),
                      onPressed: () {
                        ref.invalidate(pendingOutboxProvider);
                        ref.invalidate(conflictOutboxProvider);
                        ref.invalidate(pendingPhotosProvider);
                      },
                      child: const Text('Retry'),
                    ),
                  ] else if (!queuesReady)
                    const Text('Loading sync status…')
                  else
                    Text(
                      '${pending!.length} queued actions · $pendingPhotos queued photos',
                      key: const Key('sync-counts'),
                    ),
                  if (!queueUnavailable &&
                      queuesReady &&
                      conflicts != null &&
                      conflicts.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Text(
                        '${conflicts.length} need review',
                        key: const Key('conflict-count'),
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                        ),
                      ),
                    ),
                  if (!queueUnavailable && queuesReady) ...[
                    const SizedBox(height: 12),
                    OutlinedButton.icon(
                      key: const Key('sync-now'),
                      icon: const Icon(Icons.sync),
                      label: const Text('Sync now'),
                      onPressed: () async {
                        final sync = ref.read(syncServiceProvider);
                        await sync.flushAll();
                      },
                    ),
                  ],
                ],
              ),
            ),
          ),
          if (!queueUnavailable &&
              queuesReady &&
              conflicts != null &&
              conflicts.isNotEmpty) ...[
            const SizedBox(height: 16),
            Text(
              'Needs review',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 4),
            Text(
              'These actions were rejected because the job changed on the server. '
              'Review with dispatch, then discard.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 8),
            for (final entry in conflicts)
              Card(
                child: ListTile(
                  leading: const Icon(Icons.warning_amber_outlined),
                  title: Text(entry.kind),
                  subtitle: Text(entry.lastError ?? 'Rejected by the server'),
                  trailing: IconButton(
                    key: Key('discard-${entry.clientRef}'),
                    icon: const Icon(Icons.delete_outline),
                    tooltip: 'Discard',
                    onPressed: () async {
                      final confirmed = await showDialog<bool>(
                        context: context,
                        builder: (context) => AlertDialog(
                          title: const Text('Discard this action?'),
                          content: const Text(
                            'It was rejected by the server and cannot be retried.',
                          ),
                          actions: [
                            TextButton(
                              onPressed: () => Navigator.pop(context, false),
                              child: const Text('Keep'),
                            ),
                            FilledButton(
                              onPressed: () => Navigator.pop(context, true),
                              child: const Text('Discard'),
                            ),
                          ],
                        ),
                      );
                      if (confirmed == true) {
                        final db = ref.read(syncServiceProvider).db;
                        await (db.delete(
                          db.outboxEntries,
                        )..where((row) => row.seq.equals(entry.seq))).go();
                      }
                    },
                  ),
                ),
              ),
          ],
          const SizedBox(height: 24),
          OutlinedButton.icon(
            key: const Key('logout'),
            icon: const Icon(Icons.logout),
            label: const Text('Sign out'),
            onPressed: () => ref.read(authControllerProvider.notifier).logout(),
          ),
        ],
      ),
    );
  }
}
