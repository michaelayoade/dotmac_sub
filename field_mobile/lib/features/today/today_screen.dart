import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../jobs/jobs_providers.dart';
import '../jobs/widgets/job_card.dart';
import '../location/location_tracking_controller.dart';
import '../profile/profile_screen.dart';

const _filters = <(String?, String)>[
  (null, 'All'),
  ('dispatched', 'Assigned'),
  ('in_progress', 'Active'),
  ('completed', 'Done'),
];

class TodayScreen extends ConsumerWidget {
  const TodayScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(meProvider);
    final jobs = ref.watch(todayJobsProvider);
    final filter = ref.watch(jobsFilterProvider);

    return Scaffold(
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(meProvider);
            ref.invalidate(todayJobsProvider);
          },
          child: CustomScrollView(
            physics: const AlwaysScrollableScrollPhysics(),
            slivers: [
              SliverPadding(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
                sliver: SliverToBoxAdapter(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      me.when(
                        data: (data) => Text(
                          'Hello, ${data.name.split(' ').first}',
                          style: Theme.of(context).textTheme.headlineSmall
                              ?.copyWith(fontWeight: FontWeight.w700),
                        ),
                        loading: () => const SizedBox(height: 32),
                        error: (_, _) => const SizedBox(height: 32),
                      ),
                      const SizedBox(height: 12),
                      me.when(
                        data: (data) => Row(
                          children: [
                            _MetricTile(
                              value: '${data.openJobs}',
                              label: 'open',
                            ),
                            const SizedBox(width: 12),
                            _MetricTile(
                              value: '${data.completedToday}',
                              label: 'done today',
                            ),
                          ],
                        ),
                        loading: () => const SizedBox.shrink(),
                        error: (_, _) => const SizedBox.shrink(),
                      ),
                      const SizedBox(height: 12),
                      const SyncStatusBar(),
                      const SizedBox(height: 12),
                      const LocationSharingControls(),
                      if (jobs.value?.fromCache ?? false)
                        const _OfflineBanner(),
                      const SizedBox(height: 16),
                      SingleChildScrollView(
                        scrollDirection: Axis.horizontal,
                        child: Row(
                          children: [
                            for (final (value, label) in _filters) ...[
                              FilterChip(
                                label: Text(label),
                                selected: filter == value,
                                onSelected: (_) =>
                                    ref
                                            .read(jobsFilterProvider.notifier)
                                            .state =
                                        value,
                              ),
                              const SizedBox(width: 8),
                            ],
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              jobs.when(
                data: (list) => list.jobs.isEmpty
                    ? const SliverFillRemaining(
                        hasScrollBody: false,
                        child: Center(child: Text('No jobs in this view')),
                      )
                    : SliverPadding(
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                        sliver: SliverList.separated(
                          itemCount: list.jobs.length,
                          separatorBuilder: (_, _) =>
                              const SizedBox(height: 12),
                          itemBuilder: (context, index) {
                            final job = list.jobs[index];
                            return JobCard(
                              job: job,
                              onTap: () => context.push('/jobs/${job.id}'),
                            );
                          },
                        ),
                      ),
                loading: () => const SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(child: CircularProgressIndicator()),
                ),
                error: (error, _) => SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(
                        'Could not load jobs — pull to retry',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MetricTile extends StatelessWidget {
  const _MetricTile({required this.value, required this.label});

  final String value;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Card(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 14),
          child: Column(
            children: [
              Text(
                value,
                style: Theme.of(context).textTheme.headlineMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
              Text(label, style: Theme.of(context).textTheme.bodySmall),
            ],
          ),
        ),
      ),
    );
  }
}

/// Compact sync state: queued work + items needing review, tap → Profile.
/// Calm by design — amber for attention, never red.
class SyncStatusBar extends ConsumerWidget {
  const SyncStatusBar({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final pending = ref.watch(pendingOutboxProvider).value?.length ?? 0;
    final photos = ref.watch(pendingPhotosProvider).value ?? 0;
    final conflicts = ref.watch(conflictOutboxProvider).value?.length ?? 0;
    final queued = pending + photos;
    if (queued == 0 && conflicts == 0) return const SizedBox.shrink();

    final amber = const Color(0xFFF59E0B);
    final parts = <String>[
      if (queued > 0) '$queued queued',
      if (conflicts > 0) '$conflicts need review',
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: InkWell(
        key: const Key('sync-status-bar'),
        onTap: () => context.go('/profile'),
        borderRadius: BorderRadius.circular(8),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: amber.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: [
              Icon(Icons.sync, size: 16, color: amber),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  parts.join(' · '),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const Icon(Icons.chevron_right, size: 16),
            ],
          ),
        ),
      ),
    );
  }
}

class _OfflineBanner extends StatelessWidget {
  const _OfflineBanner();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Row(
        key: const Key('offline-banner'),
        children: [
          const Icon(Icons.cloud_off_outlined, size: 16),
          const SizedBox(width: 8),
          Text(
            'Offline — showing saved jobs',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
