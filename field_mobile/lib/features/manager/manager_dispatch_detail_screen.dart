import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../../app/theme.dart';
import '../../app/widgets/status_pill.dart';
import 'manager_providers.dart';

class ManagerDispatchDetailScreen extends ConsumerWidget {
  const ManagerDispatchDetailScreen({
    super.key,
    required this.jobId,
    this.assignedToPersonId,
  });

  final String jobId;
  final String? assignedToPersonId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final profile = ref.watch(managerProfileProvider);

    return profile.when(
      loading: () => const Scaffold(
        appBar: _DispatchDetailAppBar(),
        body: Center(child: CircularProgressIndicator()),
      ),
      error: (_, _) => const _DispatchAccessDenied(),
      data: (manager) {
        if (manager?.isManager != true || manager?.canViewDispatch != true) {
          return const _DispatchAccessDenied();
        }
        return _ManagerDispatchDetailBody(
          jobId: jobId,
          assignedToPersonId: assignedToPersonId,
        );
      },
    );
  }
}

class _ManagerDispatchDetailBody extends ConsumerWidget {
  const _ManagerDispatchDetailBody({
    required this.jobId,
    required this.assignedToPersonId,
  });

  final String jobId;
  final String? assignedToPersonId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final request = (jobId: jobId, assignedToPersonId: assignedToPersonId);
    final job = ref.watch(managerJobProvider(request));

    void refresh() {
      ref
        ..invalidate(managerJobsProvider)
        ..invalidate(managerJobProvider(request));
    }

    return Scaffold(
      appBar: const _DispatchDetailAppBar(),
      body: job.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => _DispatchDetailMessage(
          icon: Icons.cloud_off_outlined,
          title: 'Could not load this dispatch',
          message: 'Check your connection and try again.',
          actionLabel: 'Retry',
          onAction: refresh,
        ),
        data: (item) => item == null
            ? _DispatchDetailMessage(
                icon: Icons.assignment_late_outlined,
                title: 'Dispatch no longer available',
                message:
                    'This work order is no longer in the open dispatch queue.',
                actionLabel: 'Refresh',
                onAction: refresh,
              )
            : RefreshIndicator(
                onRefresh: () async {
                  refresh();
                  await ref.read(managerJobProvider(request).future);
                },
                child: _DispatchDetailList(job: item),
              ),
      ),
    );
  }
}

class _DispatchDetailList extends StatelessWidget {
  const _DispatchDetailList({required this.job});

  final ManagerJob job;

  @override
  Widget build(BuildContext context) {
    final description = job.description?.trim();
    final hasSiteContext =
        _hasText(job.subscriberLabel) ||
        _hasText(job.addressText) ||
        (job.latitude != null && job.longitude != null);

    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.all(AppSpace.lg),
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Text(
                job.title,
                style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                  fontWeight: FontWeight.w800,
                ),
              ),
            ),
            const SizedBox(width: AppSpace.md),
            StatusPill(job.statusPresentation),
          ],
        ),
        const SizedBox(height: AppSpace.lg),
        _DetailCard(
          title: 'Dispatch overview',
          children: [
            _DetailRow(
              icon: Icons.build_outlined,
              label: 'Work type',
              value: _displayValue(job.workType),
            ),
            _DetailRow(
              icon: Icons.flag_outlined,
              label: 'Priority',
              value: _displayValue(job.priority),
            ),
            _DetailRow(
              icon: Icons.schedule_outlined,
              label: 'Starts',
              value: _formatDateTime(job.scheduledStart) ?? 'Not scheduled',
            ),
            _DetailRow(
              icon: Icons.event_available_outlined,
              label: 'Ends',
              value: _formatDateTime(job.scheduledEnd) ?? 'Not scheduled',
              isLast: true,
            ),
          ],
        ),
        const SizedBox(height: AppSpace.md),
        _DetailCard(
          title: 'Assignment',
          children: [
            _DetailRow(
              icon: job.assignedToLabel == null
                  ? Icons.person_off_outlined
                  : Icons.engineering_outlined,
              label: 'Technician',
              value: job.assignedToLabel ?? 'Unassigned',
              isLast: true,
            ),
          ],
        ),
        if (hasSiteContext) ...[
          const SizedBox(height: AppSpace.md),
          _DetailCard(
            title: 'Customer and site',
            children: [
              if (_hasText(job.subscriberLabel))
                _DetailRow(
                  icon: Icons.account_circle_outlined,
                  label: 'Subscriber',
                  value: job.subscriberLabel!,
                  isLast:
                      !_hasText(job.addressText) &&
                      (job.latitude == null || job.longitude == null),
                ),
              if (_hasText(job.addressText))
                _DetailRow(
                  icon: Icons.place_outlined,
                  label: 'Address',
                  value: job.addressText!,
                  isLast: job.latitude == null || job.longitude == null,
                ),
              if (job.latitude != null && job.longitude != null)
                _DetailRow(
                  icon: Icons.pin_drop_outlined,
                  label: 'Coordinates',
                  value:
                      '${job.latitude!.toStringAsFixed(6)}, '
                      '${job.longitude!.toStringAsFixed(6)}',
                  isLast: true,
                ),
            ],
          ),
        ],
        if (description != null && description.isNotEmpty) ...[
          const SizedBox(height: AppSpace.md),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(AppSpace.lg),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Scope of work',
                    style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: AppSpace.sm),
                  Text(description),
                ],
              ),
            ),
          ),
        ],
        const SizedBox(height: AppSpace.xxl),
      ],
    );
  }
}

class _DetailCard extends StatelessWidget {
  const _DetailCard({required this.title, required this.children});

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(AppSpace.lg),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              title,
              style: Theme.of(
                context,
              ).textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: AppSpace.sm),
            ...children,
          ],
        ),
      ),
    );
  }
}

class _DetailRow extends StatelessWidget {
  const _DetailRow({
    required this.icon,
    required this.label,
    required this.value,
    this.isLast = false,
  });

  final IconData icon;
  final String label;
  final String value;
  final bool isLast;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(bottom: isLast ? 0 : AppSpace.md),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 20, color: AppColors.subdued(context)),
          const SizedBox(width: AppSpace.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: AppColors.subdued(context),
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: AppSpace.xs),
                Text(value),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _DispatchDetailMessage extends StatelessWidget {
  const _DispatchDetailMessage({
    required this.icon,
    required this.title,
    required this.message,
    required this.actionLabel,
    required this.onAction,
  });

  final IconData icon;
  final String title;
  final String message;
  final String actionLabel;
  final VoidCallback onAction;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(AppSpace.xxl),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 40, color: AppColors.subdued(context)),
            const SizedBox(height: AppSpace.md),
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: AppSpace.sm),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: AppSpace.lg),
            OutlinedButton(onPressed: onAction, child: Text(actionLabel)),
          ],
        ),
      ),
    );
  }
}

class _DispatchDetailAppBar extends StatelessWidget
    implements PreferredSizeWidget {
  const _DispatchDetailAppBar();

  @override
  Size get preferredSize => const Size.fromHeight(kToolbarHeight);

  @override
  Widget build(BuildContext context) {
    return AppBar(title: const Text('Dispatch details'));
  }
}

class _DispatchAccessDenied extends StatelessWidget {
  const _DispatchAccessDenied();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      appBar: _DispatchDetailAppBar(),
      body: Center(
        child: Padding(
          padding: EdgeInsets.all(AppSpace.xxl),
          child: Text(
            'You do not have permission to view dispatch details.',
            textAlign: TextAlign.center,
          ),
        ),
      ),
    );
  }
}

bool _hasText(String? value) => value != null && value.trim().isNotEmpty;

String _displayValue(String value) {
  final normalized = value.trim().replaceAll('_', ' ');
  if (normalized.isEmpty) return 'Unknown';
  return '${normalized[0].toUpperCase()}${normalized.substring(1)}';
}

String? _formatDateTime(DateTime? value) =>
    value == null ? null : DateFormat('d MMM y, HH:mm').format(value.toLocal());
