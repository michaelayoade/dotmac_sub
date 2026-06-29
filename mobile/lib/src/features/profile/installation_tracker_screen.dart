import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/project.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Installation Progress — the customer's install lifecycle (stage timeline +
/// progress %), served from the sub's local project mirror.
class InstallationTrackerScreen extends ConsumerWidget {
  const InstallationTrackerScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projects = ref.watch(projectsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Installation Progress')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(projectsProvider);
          await ref.read(projectsProvider.future);
        },
        child: AsyncValueView<ProjectsSummary>(
          value: projects,
          onRetry: () => ref.invalidate(projectsProvider),
          data: (summary) {
            if (summary.projects.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 80),
                  Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 32),
                      child: Text(
                        'No installations in progress. Once your order is '
                        'scheduled, live progress shows here.',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ),
                ],
              );
            }
            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                for (final p in summary.projects) _ProjectCard(project: p),
              ],
            );
          },
        ),
      ),
    );
  }
}

class _ProjectCard extends StatelessWidget {
  const _ProjectCard({required this.project});

  final ProjectItem project;

  static const _statusColors = {
    'active': Colors.blue,
    'on_hold': Colors.orange,
    'completed': Colors.green,
    'canceled': Colors.red,
  };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = _statusColors[project.status] ?? Colors.grey;
    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    project.name,
                    style: theme.textTheme.titleMedium
                        ?.copyWith(fontWeight: FontWeight.bold),
                  ),
                ),
                Chip(
                  label: Text(
                    project.status.replaceAll('_', ' '),
                    style: const TextStyle(fontSize: 12),
                  ),
                  backgroundColor: color.withValues(alpha: 0.15),
                  side: BorderSide.none,
                  visualDensity: VisualDensity.compact,
                  materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              ],
            ),
            if (project.customerAddress != null)
              Text(project.customerAddress!, style: theme.textTheme.bodySmall),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: Text(
                    project.currentStage ??
                        (project.status == 'completed'
                            ? 'Completed'
                            : 'In progress'),
                    style: theme.textTheme.bodyMedium,
                  ),
                ),
                Text(
                  '${project.progressPct}%',
                  style: theme.textTheme.titleSmall
                      ?.copyWith(fontWeight: FontWeight.bold),
                ),
              ],
            ),
            const SizedBox(height: 6),
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: LinearProgressIndicator(
                value: project.progressPct / 100,
                minHeight: 8,
              ),
            ),
            if (project.stages.isNotEmpty) ...[
              const SizedBox(height: 16),
              for (final s in project.stages) _StageRow(stage: s),
            ],
          ],
        ),
      ),
    );
  }
}

class _StageRow extends StatelessWidget {
  const _StageRow({required this.stage});

  final ProjectStage stage;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final done = stage.status == 'done';
    final inProgress = stage.status == 'in_progress';
    final IconData icon = done
        ? Icons.check_circle
        : (inProgress ? Icons.radio_button_checked : Icons.circle_outlined);
    final Color color =
        done ? Colors.green : (inProgress ? Colors.blue : Colors.grey);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 18, color: color),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              stage.title,
              style: theme.textTheme.bodyMedium?.copyWith(
                color: stage.status == 'pending' ? theme.disabledColor : null,
                fontWeight: inProgress ? FontWeight.w600 : null,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
