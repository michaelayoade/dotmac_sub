import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../app/theme.dart';
import '../execution/completion_wizard.dart';
import '../execution/execution_controller.dart';
import 'job_models.dart';
import 'jobs_providers.dart';
import 'location_pin_screen.dart';

/// Launcher abstraction so widget tests assert the URI without opening apps.
typedef UriLauncher = Future<bool> Function(Uri uri);

final uriLauncherProvider = Provider<UriLauncher>((ref) => launchUrl);

class JobDetailScreen extends ConsumerWidget {
  const JobDetailScreen({super.key, required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = ref.watch(jobDetailProvider(jobId));

    return detail.when(
      data: (data) => _JobDetailView(detail: data),
      loading: () =>
          const Scaffold(body: Center(child: CircularProgressIndicator())),
      error: (error, _) => Scaffold(
        appBar: AppBar(),
        body: const Center(child: Text('Could not load this job')),
      ),
    );
  }
}

class _JobDetailView extends ConsumerStatefulWidget {
  const _JobDetailView({required this.detail});

  final JobDetail detail;

  @override
  ConsumerState<_JobDetailView> createState() => _JobDetailViewState();
}

class _JobDetailViewState extends ConsumerState<_JobDetailView> {
  late List<Map<String, dynamic>> _notes;
  final _noteController = TextEditingController();
  final _noteComposerKey = GlobalKey();
  final _noteFocusNode = FocusNode();
  bool _isAddingNote = false;
  bool _isSavingNote = false;
  bool _isInternalNote = true;
  String _noteError = '';
  JobDestination? _activeDestination;

  @override
  void initState() {
    super.initState();
    _notes = [...widget.detail.notes];
  }

  @override
  void didUpdateWidget(covariant _JobDetailView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.detail != widget.detail) {
      _notes = [...widget.detail.notes];
    }
  }

  @override
  void dispose() {
    _noteController.dispose();
    _noteFocusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final detail = widget.detail;
    final job = detail.job;
    final actions = workActionsFor(job.status);
    final travelActions = actions
        .where((action) => action == 'en_route' || action == 'arrived')
        .toList();
    final executionActions = actions
        .where((action) => action != 'en_route' && action != 'arrived')
        .toList();
    final statusColor = AppColors.status(job.status);

    return Scaffold(
      appBar: AppBar(
        title: Text(job.workType.toUpperCase()),
        actions: [
          IconButton(
            tooltip: 'Request materials',
            onPressed: () => context.push(
              '/materials/new?workOrderId=${Uri.encodeComponent(job.id)}'
              '&workOrderLabel=${Uri.encodeComponent(job.title)}',
            ),
            icon: const Icon(Icons.inventory_2_outlined),
          ),
          IconButton(
            tooltip: 'Request expense',
            onPressed: () => context.push(
              '/expenses/new?workOrderId=${Uri.encodeComponent(job.id)}'
              '&workOrderLabel=${Uri.encodeComponent(job.title)}',
            ),
            icon: const Icon(Icons.receipt_long_outlined),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: Row(
                children: [
                  Icon(Icons.circle, size: 10, color: statusColor),
                  const SizedBox(width: 6),
                  Text(statusLabel(job.status)),
                ],
              ),
            ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        key: const Key('add-note-action'),
        onPressed: _isAddingNote ? null : _openNoteComposer,
        icon: const Icon(Icons.note_add_outlined),
        label: const Text('Add note'),
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              job.title,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
            ),
            if (detail.ticketRef != null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  'Ticket ${detail.ticketRef}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            const SizedBox(height: 16),
            _LocationCard(jobId: job.id, location: detail.location),
            if (detail.customer != null) ...[
              const SizedBox(height: 12),
              _CustomerCard(customer: detail.customer!),
            ],
            if (travelActions.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Wrap(
                    spacing: 10,
                    runSpacing: 10,
                    children: [
                      for (final action in travelActions)
                        OutlinedButton.icon(
                          key: Key('work-action-$action'),
                          onPressed: () => _runWorkAction(job.id, action),
                          icon: Icon(
                            action == 'en_route'
                                ? Icons.navigation_outlined
                                : Icons.place_outlined,
                          ),
                          label: Text(actionLabel(action)),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            if (job.description != null && job.description!.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Scope of work',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      Text(job.description!),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.materialRequests.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Material requests',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final request in detail.materialRequests)
                        _MaterialRequestListTile(request: request),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.history.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'History',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final item in detail.history)
                        _HistoryTile(item: item),
                    ],
                  ),
                ),
              ),
            ],
            if (_isAddingNote) ...[
              const SizedBox(height: 12),
              Card(
                key: _noteComposerKey,
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        'Add note',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        key: const Key('note-body-field'),
                        controller: _noteController,
                        focusNode: _noteFocusNode,
                        minLines: 4,
                        maxLines: 6,
                        textInputAction: TextInputAction.newline,
                        decoration: InputDecoration(
                          hintText: 'What happened on site?',
                          errorText: _noteError.isEmpty ? null : _noteError,
                        ),
                      ),
                      const SizedBox(height: 12),
                      CheckboxListTile(
                        key: const Key('internal-note-checkbox'),
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Internal note'),
                        subtitle: Text(
                          _isInternalNote
                              ? 'Visible to staff only'
                              : 'External note for customer-facing history',
                        ),
                        value: _isInternalNote,
                        onChanged: _isSavingNote
                            ? null
                            : (value) => setState(
                                () => _isInternalNote = value ?? true,
                              ),
                      ),
                      const SizedBox(height: 12),
                      OverflowBar(
                        alignment: MainAxisAlignment.end,
                        spacing: 8,
                        children: [
                          TextButton(
                            onPressed: _isSavingNote
                                ? null
                                : () => setState(() {
                                    _isAddingNote = false;
                                    _isInternalNote = true;
                                    _noteError = '';
                                    _noteFocusNode.unfocus();
                                    _noteController.clear();
                                  }),
                            child: const Text('Cancel'),
                          ),
                          FilledButton(
                            key: const Key('save-note-action'),
                            onPressed: _isSavingNote
                                ? null
                                : () => _saveInlineNote(job.id),
                            child: _isSavingNote
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                    ),
                                  )
                                : const Text('Save'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.materials.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Materials',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final material in detail.materials)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                          child: Row(
                            children: [
                              Expanded(
                                child: Text(
                                  material['item_name'] as String? ?? 'Item',
                                ),
                              ),
                              Text('×${material['quantity']}'),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            if (_notes.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Notes',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final note in _notes)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                          child: _NoteTile(note: note),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            const SizedBox(height: 96),
          ],
        ),
      ),
      bottomNavigationBar: executionActions.isEmpty
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    for (final action in executionActions) ...[
                      FilledButton(
                        key: Key('work-action-$action'),
                        onPressed: () => _runWorkAction(job.id, action),
                        child: Text(actionLabel(action)),
                      ),
                      if (action != executionActions.last)
                        const SizedBox(height: 8),
                    ],
                    TextButton(
                      key: const Key('unable-action'),
                      onPressed: () =>
                          promptUnableToComplete(context, ref, job.id),
                      child: const Text("Can't complete this job"),
                    ),
                  ],
                ),
              ),
            ),
    );
  }

  void _openNoteComposer() {
    setState(() {
      _isAddingNote = true;
      _isInternalNote = true;
      _noteError = '';
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final composerContext = _noteComposerKey.currentContext;
      if (composerContext != null) {
        Scrollable.ensureVisible(
          composerContext,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOutCubic,
          alignment: 0.1,
        );
      }
      _noteFocusNode.requestFocus();
    });
  }

  Future<void> _runWorkAction(String jobId, String action) async {
    if (action == 'complete') {
      await Navigator.of(
        context,
      ).push(MaterialPageRoute(builder: (_) => CompletionWizard(jobId: jobId)));
    } else if (action == 'en_route') {
      final destination = await _pickDestination(jobId);
      if (destination == null) return;
      _activeDestination = destination;
      await ref
          .read(executionControllerProvider.notifier)
          .transition(
            jobId,
            action,
            payload: destination.toTransitionPayload(),
          );
    } else if (action == 'arrived') {
      final destination = _activeDestination ?? await _pickDestination(jobId);
      if (destination == null) return;
      _activeDestination = destination;
      await ref
          .read(executionControllerProvider.notifier)
          .transition(
            jobId,
            action,
            payload: destination.toTransitionPayload(),
          );
    } else {
      await ref
          .read(executionControllerProvider.notifier)
          .transition(jobId, action);
    }
    if (!mounted) return;
    ref.invalidate(jobDetailProvider(jobId));
  }

  Future<JobDestination?> _pickDestination(String jobId) async {
    List<JobDestination> destinations;
    try {
      destinations = await ref
          .read(jobsRepositoryProvider)
          .fetchDestinations(jobId);
    } catch (_) {
      destinations = const [
        JobDestination(destinationType: 'customer', label: 'Customer site'),
        JobDestination(destinationType: 'other', label: 'Other location'),
      ];
    }
    if (!mounted) return null;
    return showModalBottomSheet<JobDestination>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text(
                'Select destination',
                style: TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            for (final destination in destinations)
              ListTile(
                key: Key(
                  'destination-${destination.destinationType}-${destination.destinationId ?? destination.label}',
                ),
                leading: Icon(_destinationIcon(destination.destinationType)),
                title: Text(destination.label),
                subtitle:
                    destination.addressText == null ||
                        destination.addressText!.isEmpty
                    ? Text(destination.destinationType.replaceAll('_', ' '))
                    : Text(destination.addressText!),
                onTap: () => Navigator.of(sheetContext).pop(destination),
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _saveInlineNote(String jobId) async {
    final body = _noteController.text.trim();
    if (body.isEmpty) {
      setState(() => _noteError = 'Enter a note');
      return;
    }
    setState(() {
      _isSavingNote = true;
      _noteError = '';
    });
    try {
      final clientRef = await ref
          .read(executionControllerProvider.notifier)
          .addNote(jobId, body, isInternal: _isInternalNote);
      if (!mounted) return;
      final isInternal = _isInternalNote;
      _noteController.clear();
      setState(() {
        _isAddingNote = false;
        _isSavingNote = false;
        _isInternalNote = true;
      });
      _addLocalNote(clientRef, body, isInternal: isInternal);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Note saved')));
      unawaited(_refreshJobDetail(jobId));
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _isSavingNote = false;
        _noteError = 'Could not save note';
      });
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Could not save note')));
    }
  }

  void _addLocalNote(
    String clientRef,
    String body, {
    required bool isInternal,
  }) {
    setState(() {
      _notes = [
        {
          'id': clientRef,
          'body': body,
          'is_internal': isInternal,
          'author_name': 'You',
          'created_at': DateTime.now().toUtc().toIso8601String(),
        },
        ..._notes,
      ];
    });
  }

  Future<void> _refreshJobDetail(String jobId) async {
    try {
      final detail = await ref.read(jobsRepositoryProvider).fetchDetail(jobId);
      if (!mounted) return;
      setState(() => _notes = [...detail.notes]);
    } catch (_) {
      // The note is saved/queued; a refresh problem should not show as save failure.
    }
  }
}

IconData _destinationIcon(String type) {
  return switch (type) {
    'customer' => Icons.home_outlined,
    'cabinet' || 'fdh' => Icons.dns_outlined,
    'closure' || 'splice_closure' => Icons.hub_outlined,
    'pop' || 'olt' => Icons.router_outlined,
    _ => Icons.place_outlined,
  };
}

String _noteBody(Map<String, dynamic> note) {
  for (final key in ['body', 'text', 'comment', 'note']) {
    final value = note[key];
    if (value is String && value.trim().isNotEmpty) return value;
  }
  return '';
}

class _NoteTile extends StatelessWidget {
  const _NoteTile({required this.note});

  final Map<String, dynamic> note;

  @override
  Widget build(BuildContext context) {
    final meta = _noteMeta(note);
    final isInternal = note['is_internal'];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (isInternal is bool)
          Align(
            alignment: Alignment.centerLeft,
            child: Chip(
              visualDensity: VisualDensity.compact,
              label: Text(isInternal ? 'Internal' : 'External'),
            ),
          ),
        if (meta != null)
          Text(
            meta,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        Text(_noteBody(note)),
      ],
    );
  }
}

class _MaterialRequestListTile extends StatelessWidget {
  const _MaterialRequestListTile({required this.request});

  final Map<String, dynamic> request;

  @override
  Widget build(BuildContext context) {
    final id = request['id']?.toString();
    final number = request['number']?.toString();
    final status = request['status']?.toString().replaceAll('_', ' ');
    final items = request['items'];
    final itemCount = items is List ? items.length : 0;
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: const Icon(Icons.assignment_outlined),
      title: Text(
        number == null || number.isEmpty ? 'Material request' : number,
      ),
      subtitle: Text(
        [
          if (status != null && status.isNotEmpty) status,
          '$itemCount item${itemCount == 1 ? '' : 's'}',
        ].join(' · '),
      ),
      trailing: id == null ? null : const Icon(Icons.chevron_right),
      onTap: id == null ? null : () => context.push('/materials/$id'),
    );
  }
}

class _HistoryTile extends StatelessWidget {
  const _HistoryTile({required this.item});

  final Map<String, dynamic> item;

  @override
  Widget build(BuildContext context) {
    final type = item['type']?.toString() ?? 'activity';
    final title = item['title']?.toString() ?? 'Activity';
    final description = item['description']?.toString();
    final actor = item['actor_name']?.toString();
    final status = item['status']?.toString().replaceAll('_', ' ');
    final occurredAt = item['occurred_at']?.toString();
    final isInternal = item['is_internal'];
    final meta = [
      if (actor != null && actor.isNotEmpty) actor,
      if (status != null && status.isNotEmpty) status,
      if (occurredAt != null && occurredAt.isNotEmpty) occurredAt,
    ].join(' · ');

    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: Icon(_historyIcon(type)),
      title: Row(
        children: [
          Expanded(child: Text(title)),
          if (isInternal is bool)
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Chip(
                visualDensity: VisualDensity.compact,
                label: Text(isInternal ? 'Internal' : 'External'),
              ),
            ),
        ],
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (description != null && description.isNotEmpty) Text(description),
          if (meta.isNotEmpty)
            Text(
              meta,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
        ],
      ),
    );
  }
}

IconData _historyIcon(String type) {
  return switch (type) {
    'note' => Icons.sticky_note_2_outlined,
    'material_request' => Icons.assignment_outlined,
    'work_event' => Icons.timeline_outlined,
    'worklog' => Icons.timer_outlined,
    'attachment' => Icons.attach_file,
    _ => Icons.history,
  };
}

String? _noteMeta(Map<String, dynamic> note) {
  final author = _noteString(note, const [
    'author_name',
    'author',
    'created_by_name',
    'created_by',
  ]);
  final createdAt = _noteString(note, const ['created_at', 'createdAt']);
  if (author == null && createdAt == null) return null;
  if (author != null && createdAt != null) return '$author · $createdAt';
  return author ?? createdAt;
}

String? _noteString(Map<String, dynamic> note, List<String> keys) {
  for (final key in keys) {
    final value = note[key];
    if (value is String && value.trim().isNotEmpty) return value.trim();
  }
  return null;
}

/// Field outcomes for a visit that can't be completed. Keys mirror the backend
/// ``unable_to_complete`` reasons; labels are tech-facing.
const List<({String key, String label})> kUnableReasons = [
  (key: 'customer_absent', label: 'Customer not home'),
  (key: 'no_access', label: 'Could not access site'),
  (key: 'site_not_ready', label: 'Site not ready'),
  (key: 'needs_parts', label: 'Missing parts/materials'),
  (key: 'unsafe', label: 'Unsafe conditions'),
  (key: 'other', label: 'Other'),
];

/// Ask why the job can't be completed, then record the failed visit (which
/// cancels the job server-side with the chosen reason).
Future<void> promptUnableToComplete(
  BuildContext context,
  WidgetRef ref,
  String jobId,
) async {
  final reason = await showModalBottomSheet<String>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Padding(
            padding: EdgeInsets.all(16),
            child: Text(
              "Why can't this job be completed?",
              style: TextStyle(fontWeight: FontWeight.w600),
            ),
          ),
          for (final reasonOption in kUnableReasons)
            ListTile(
              key: Key('reason-${reasonOption.key}'),
              title: Text(reasonOption.label),
              onTap: () => Navigator.of(sheetContext).pop(reasonOption.key),
            ),
        ],
      ),
    ),
  );
  if (reason == null) return;
  if (!context.mounted) return;
  await ref
      .read(executionControllerProvider.notifier)
      .unableToComplete(jobId, reason: reason);
  if (!context.mounted) return;
  ref.invalidate(jobDetailProvider(jobId));
  Navigator.of(context).maybePop();
}

class _LocationCard extends ConsumerWidget {
  const _LocationCard({required this.jobId, required this.location});

  final String jobId;
  final JobLocation location;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final uri = location.mapsUri;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.place_outlined, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(location.addressText ?? 'No address on file'),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: [
                if (uri != null)
                  OutlinedButton.icon(
                    key: const Key('navigate-button'),
                    onPressed: () => ref.read(uriLauncherProvider)(uri),
                    icon: const Icon(Icons.navigation_outlined),
                    label: const Text('Navigate'),
                  ),
                OutlinedButton.icon(
                  key: const Key('edit-location-button'),
                  onPressed: () async {
                    final changed = await Navigator.of(context).push<bool>(
                      MaterialPageRoute(
                        builder: (_) => LocationPinScreen(
                          jobId: jobId,
                          initialLocation: location,
                        ),
                      ),
                    );
                    if (context.mounted && changed == true) {
                      ref.invalidate(jobDetailProvider(jobId));
                    }
                  },
                  icon: const Icon(Icons.push_pin_outlined),
                  label: Text(
                    location.hasCoordinates ? 'Edit pin' : 'Pin location',
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _CustomerCard extends ConsumerWidget {
  const _CustomerCard({required this.customer});

  final JobCustomer customer;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            CircleAvatar(
              child: Text((customer.name ?? '?').substring(0, 1).toUpperCase()),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    customer.name ?? 'Customer',
                    style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  if (customer.servicePlan != null)
                    Text(
                      customer.servicePlan!,
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                ],
              ),
            ),
            if (customer.phone != null)
              IconButton(
                key: const Key('call-button'),
                onPressed: () => ref.read(uriLauncherProvider)(
                  Uri.parse('tel:${customer.phone}'),
                ),
                icon: const Icon(Icons.call_outlined),
                tooltip: 'Call customer',
              ),
          ],
        ),
      ),
    );
  }
}
