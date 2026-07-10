import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../../app/theme.dart';
import '../../../app/widgets/status_pill.dart';
import '../job_models.dart';

/// The primary work unit on Today/Jobs. A big, gloved-thumb card: a status
/// stripe down the left, the scheduled window and a [StatusPill] up top, the
/// job title, and a quiet meta footer. Completed jobs read as done, not gone.
class JobCard extends StatelessWidget {
  const JobCard({super.key, required this.job, this.onTap});

  final JobSummary job;
  final VoidCallback? onTap;

  String get _window {
    final start = job.scheduledStart?.toLocal();
    final end = job.scheduledEnd?.toLocal();
    if (start == null) return 'No time set';
    final f = DateFormat.Hm();
    return end == null
        ? f.format(start)
        : '${f.format(start)} – ${f.format(end)}';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final statusColor = AppColors.status(job.status);
    final done = job.status == 'completed';
    final line = isDark ? AppColors.lineDark : AppColors.lineLight;
    final faint = isDark ? AppColors.inkFaintDark : AppColors.inkFaint;

    final duration = job.estimatedDurationMinutes;

    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: IntrinsicHeight(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Container(width: 4, color: statusColor),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(15, 13, 15, 13),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            done ? Icons.check_circle : Icons.schedule_rounded,
                            size: 15,
                            color: done ? AppColors.status('completed') : faint,
                          ),
                          const SizedBox(width: 7),
                          Text(
                            _window,
                            style: TextStyle(
                              fontFamily: 'Outfit',
                              fontSize: 14,
                              fontWeight: FontWeight.w700,
                              color: done
                                  ? faint
                                  : (isDark
                                        ? AppColors.inkDark
                                        : AppColors.ink),
                              fontFeatures: const [
                                FontFeature.tabularFigures(),
                              ],
                            ),
                          ),
                          const Spacer(),
                          StatusPill(job.status),
                        ],
                      ),
                      const SizedBox(height: AppSpace.sm),
                      Text(
                        job.title,
                        style: theme.textTheme.titleMedium?.copyWith(
                          color: done
                              ? faint
                              : (isDark ? AppColors.inkDark : AppColors.ink),
                        ),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                      ),
                      const SizedBox(height: AppSpace.md),
                      Container(height: 1, color: line),
                      const SizedBox(height: AppSpace.md - 2),
                      Row(
                        children: [
                          _Meta(
                            icon: Icons.build_rounded,
                            label: _titleCase(job.workType),
                            color: faint,
                          ),
                          if (duration != null) ...[
                            const SizedBox(width: AppSpace.lg),
                            _Meta(
                              icon: Icons.timelapse_rounded,
                              label: '~$duration min',
                              color: faint,
                            ),
                          ],
                          const Spacer(),
                          Text(
                            'Open →',
                            style: TextStyle(
                              fontFamily: 'PlusJakartaSans',
                              fontSize: 12.5,
                              fontWeight: FontWeight.w700,
                              color: AppColors.primaryDeep,
                            ),
                          ),
                        ],
                      ),
                    ],
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

String _titleCase(String s) => s.isEmpty
    ? s
    : s
          .split('_')
          .map((w) => w.isEmpty ? w : '${w[0].toUpperCase()}${w.substring(1)}')
          .join(' ');

class _Meta extends StatelessWidget {
  const _Meta({required this.icon, required this.label, required this.color});

  final IconData icon;
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 14, color: color),
        const SizedBox(width: 6),
        Text(
          label,
          style: TextStyle(
            fontFamily: 'PlusJakartaSans',
            fontSize: 12.5,
            fontWeight: FontWeight.w600,
            color: color,
          ),
        ),
      ],
    );
  }
}
