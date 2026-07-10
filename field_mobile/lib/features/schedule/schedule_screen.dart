import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../jobs/job_models.dart';
import '../jobs/jobs_providers.dart';
import '../jobs/widgets/job_card.dart';

class ScheduleScreen extends ConsumerWidget {
  const ScheduleScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final jobs = ref.watch(allAssignedJobsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Schedule')),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(allAssignedJobsProvider),
        child: jobs.when(
          data: (list) {
            final items = list.jobs;
            if (items.isEmpty) {
              return ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: [
                  if (list.fromCache)
                    const Padding(
                      padding: EdgeInsets.all(16),
                      child: _OfflineBanner(),
                    ),
                  const SizedBox(height: 160),
                  const Center(child: Text('No assigned work yet')),
                ],
              );
            }
            final groups = _groupJobsByDay(items);
            return ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: [
                if (list.fromCache) const _OfflineBanner(),
                for (final group in groups) ...[
                  Padding(
                    padding: const EdgeInsets.only(top: 8, bottom: 8),
                    child: Text(
                      _dayLabel(group.day),
                      style: Theme.of(context).textTheme.titleSmall?.copyWith(
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.5,
                      ),
                    ),
                  ),
                  for (final job in group.jobs)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: JobCard(
                        job: job,
                        onTap: () => context.push('/jobs/${job.id}'),
                      ),
                    ),
                ],
              ],
            );
          },
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [
              SizedBox(height: 160),
              Center(
                child: Text('Could not load your schedule — pull to retry'),
              ),
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
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        key: const Key('schedule-offline-banner'),
        children: [
          const Icon(Icons.cloud_off_outlined, size: 16),
          const SizedBox(width: 8),
          Text(
            'Offline — showing saved schedule',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}

class _JobDayGroup {
  const _JobDayGroup(this.day, this.jobs);

  final DateTime? day;
  final List<JobSummary> jobs;
}

List<_JobDayGroup> _groupJobsByDay(List<JobSummary> jobs) {
  final sorted = [...jobs]
    ..sort((a, b) {
      final aDate = a.scheduledStart;
      final bDate = b.scheduledStart;
      if (aDate == null && bDate == null) return a.title.compareTo(b.title);
      if (aDate == null) return 1;
      if (bDate == null) return -1;
      return aDate.compareTo(bDate);
    });
  final groups = <DateTime?, List<JobSummary>>{};
  for (final job in sorted) {
    final scheduled = job.scheduledStart?.toLocal();
    final day = scheduled == null
        ? null
        : DateTime(scheduled.year, scheduled.month, scheduled.day);
    groups.putIfAbsent(day, () => []).add(job);
  }
  return [
    for (final entry in groups.entries) _JobDayGroup(entry.key, entry.value),
  ];
}

String _dayLabel(DateTime? day) {
  if (day == null) return 'Unscheduled';
  return DateFormat('EEEE, d MMM').format(day);
}
