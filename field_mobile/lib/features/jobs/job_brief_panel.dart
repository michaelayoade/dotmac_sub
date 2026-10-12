import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'job_brief_models.dart';
import 'job_brief_repository.dart';
import 'job_detail_screen.dart' show uriLauncherProvider;
import 'job_models.dart' show JobLocation;

/// Server-owned instructions, affected audience and requirements for this job.
class JobBriefPanel extends ConsumerStatefulWidget {
  const JobBriefPanel({super.key, required this.jobId});
  final String jobId;
  @override
  ConsumerState<JobBriefPanel> createState() => _JobBriefPanelState();
}

class _JobBriefPanelState extends ConsumerState<JobBriefPanel> {
  var _scope = BriefAudienceScope.current;
  final _extra = <BriefCustomer>[];
  BriefAudience? _lastAudience;
  var _loadingMore = false;
  String? _pageError;

  void _reset() {
    _extra.clear();
    _lastAudience = null;
    _pageError = null;
  }

  @override
  void didUpdateWidget(covariant JobBriefPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.jobId != widget.jobId) _reset();
  }

  @override
  Widget build(BuildContext context) {
    final request = (jobId: widget.jobId, scope: _scope);
    final data = ref.watch(jobBriefProvider(request));
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: data.when(
          loading: () => const Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Job brief'),
              SizedBox(height: 8),
              LinearProgressIndicator(),
            ],
          ),
          error: (_, _) => Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Could not load the job brief'),
              TextButton(
                onPressed: () {
                  setState(_reset);
                  ref.invalidate(jobBriefProvider(request));
                },
                child: const Text('Retry'),
              ),
            ],
          ),
          data: (brief) => _content(brief, request),
        ),
      ),
    );
  }

  Widget _content(JobBrief brief, JobBriefRequest request) {
    final audience = _lastAudience ?? brief.audience;
    final selectedRevision = _scope == BriefAudienceScope.issued
        ? brief.audience?.issued
        : brief.audience?.current;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Job brief', style: Theme.of(context).textTheme.titleMedium),
        if (brief.issue != null) ...[
          const SizedBox(height: 8),
          Text(brief.issue!),
        ],
        if (brief.instructions != null) ...[
          const SizedBox(height: 8),
          Text(brief.instructions!),
        ],
        if (brief.accessNotes != null) ...[
          const SizedBox(height: 8),
          Text('Access: ${brief.accessNotes}'),
        ],
        if (brief.planningGuidance != null)
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            title: const Text('Suggested preparation'),
            subtitle: Text(
              '${brief.planningGuidance!.purpose.replaceAll('_', ' ')} · ${brief.planningGuidance!.technology}',
            ),
            children: [
              const Text(
                'Planning guidance. Completion still requires recorded evidence and sign-off.',
              ),
              for (final evidence in brief.planningGuidance!.evidence)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.list_alt_outlined),
                  title: Text(evidence),
                ),
            ],
          ),
        if (brief.planningGuidanceStatus == 'unavailable_stale_template')
          const Text(
            'Planning guidance is unavailable for the saved template version.',
          ),
        if (brief.tasks.isNotEmpty) ...[
          const Divider(),
          Text(
            'Execution tasks',
            style: Theme.of(context).textTheme.titleSmall,
          ),
          for (final task in brief.tasks)
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text(task.title),
              subtitle: Text(
                [
                  task.status.replaceAll('_', ' '),
                  if (task.instructions != null) task.instructions!,
                ].join('\n'),
              ),
            ),
        ],
        const Divider(),
        Text(
          'Completion requirements',
          style: Theme.of(context).textTheme.titleSmall,
        ),
        if (brief.signoff.evidenceRequired)
          Text('Photos required: ${brief.signoff.minimumPhotos} minimum'),
        if (brief.signoff.customerSignoffRequired)
          const Text('Customer sign-off required'),
        if (brief.signoff.asBuiltRequired)
          const Text('As-built evidence required'),
        if (brief.directions.isNotEmpty) ...[
          const Divider(),
          Text(
            'Directions and assets',
            style: Theme.of(context).textTheme.titleSmall,
          ),
          for (final direction in brief.directions) _direction(direction),
        ],
        if (brief.audience != null) ...[
          const Divider(),
          Text(
            'Affected customers',
            style: Theme.of(context).textTheme.titleSmall,
          ),
          Wrap(
            spacing: 8,
            children: [
              for (final scope in BriefAudienceScope.values)
                ChoiceChip(
                  label: Text(
                    scope == BriefAudienceScope.current
                        ? 'Current scope'
                        : 'Issued scope',
                  ),
                  selected: _scope == scope,
                  onSelected: _loadingMore
                      ? null
                      : (_) => setState(() {
                          _scope = scope;
                          _reset();
                        }),
                ),
            ],
          ),
          Text(
            brief.affectedSubscriptionCount == null
                ? 'Affected audience unknown'
                : '${brief.affectedSubscriptionCount} potentially affected subscriptions',
          ),
          if (brief.audience!.current != null &&
              brief.audience!.current!.id != brief.audience!.issued.id)
            const Text(
              'The outage scope has changed since this job was issued.',
            ),
          for (final customer in [...brief.customers, ..._extra])
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text(customer.name ?? 'Customer details unavailable'),
              subtitle: customer.address == null
                  ? null
                  : Text(customer.address!),
            ),
          if (_pageError != null) Text(_pageError!),
          if (_pageError != null)
            TextButton(
              onPressed: () {
                setState(_reset);
                ref.invalidate(jobBriefProvider(request));
              },
              child: const Text('Reload affected customers'),
            ),
          if (audience?.nextAfter != null &&
              selectedRevision != null &&
              _pageError == null)
            TextButton(
              key: const Key('brief-load-more'),
              onPressed: _loadingMore
                  ? null
                  : () async {
                      setState(() => _loadingMore = true);
                      try {
                        final next = await ref
                            .read(jobBriefRepositoryProvider)
                            .fetch(
                              request,
                              linkId: audience!.linkId,
                              revisionId: selectedRevision.id,
                              after: audience.nextAfter,
                            );
                        if (!mounted ||
                            widget.jobId != request.jobId ||
                            _scope != request.scope) {
                          return;
                        }
                        setState(() {
                          _extra.addAll(next.customers);
                          _lastAudience = next.audience;
                        });
                      } catch (_) {
                        if (mounted &&
                            widget.jobId == request.jobId &&
                            _scope == request.scope) {
                          setState(
                            () => _pageError =
                                'Could not load the next page. Reload the affected customers to use the latest scope.',
                          );
                        }
                      } finally {
                        if (mounted) setState(() => _loadingMore = false);
                      }
                    },
              child: Text(_loadingMore ? 'Loading…' : 'Load more customers'),
            ),
        ],
      ],
    );
  }

  Widget _direction(BriefDirection direction) {
    final uri = JobLocation(
      latitude: direction.latitude,
      longitude: direction.longitude,
      addressText: direction.address,
      source: direction.provenance,
    ).mapsUri;
    return ListTile(
      contentPadding: EdgeInsets.zero,
      title: Text(direction.label),
      subtitle: Text(
        direction.address ??
            (uri == null ? 'Location unavailable' : 'Coordinates on file'),
      ),
      trailing: uri == null
          ? null
          : TextButton.icon(
              onPressed: () => ref.read(uriLauncherProvider)(uri),
              icon: const Icon(Icons.navigation_outlined),
              label: const Text('Navigate'),
            ),
    );
  }
}
