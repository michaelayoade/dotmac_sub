import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../../app/theme.dart';
import '../job_models.dart';

class JobCard extends StatelessWidget {
  const JobCard({super.key, required this.job, this.onTap});

  final JobSummary job;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final typeColor = AppColors.workType(job.workType);
    final statusColor = AppColors.status(job.status);
    final time = job.scheduledStart != null
        ? DateFormat.Hm().format(job.scheduledStart!.toLocal())
        : '—';

    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: IntrinsicHeight(
          child: Row(
            children: [
              Container(width: 5, color: typeColor),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Text(
                            time,
                            style: Theme.of(context).textTheme.labelLarge,
                          ),
                          const SizedBox(width: 8),
                          Text(
                            job.workType.toUpperCase(),
                            style: Theme.of(context).textTheme.labelSmall
                                ?.copyWith(
                                  color: typeColor,
                                  fontWeight: FontWeight.w700,
                                  letterSpacing: 1,
                                ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 6),
                      Text(
                        job.title,
                        style: Theme.of(context).textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                      ),
                      const SizedBox(height: 8),
                      Row(
                        children: [
                          Icon(Icons.circle, size: 10, color: statusColor),
                          const SizedBox(width: 6),
                          Text(
                            statusLabel(job.status),
                            style: Theme.of(
                              context,
                            ).textTheme.bodySmall?.copyWith(color: statusColor),
                          ),
                          if (job.estimatedDurationMinutes != null) ...[
                            const Spacer(),
                            Text(
                              '~${job.estimatedDurationMinutes} min',
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                          ],
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
