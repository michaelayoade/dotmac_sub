import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../execution/completion_wizard.dart' show photoCaptureProvider;
import '../execution/execution_controller.dart';
import '../jobs/job_detail_screen.dart' show uriLauncherProvider;
import 'trace_recorder.dart';
import 'vendor_providers.dart';

class VendorProjectsScreen extends ConsumerWidget {
  const VendorProjectsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projects = ref.watch(vendorProjectsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('My projects')),
      body: projects.when(
        data: (items) => items.isEmpty
            ? const Center(child: Text('No assigned projects'))
            : ListView.separated(
                padding: const EdgeInsets.all(16),
                itemCount: items.length,
                separatorBuilder: (_, _) => const SizedBox(height: 12),
                itemBuilder: (context, index) {
                  final item = items[index];
                  final project = item.project;
                  return Card(
                    child: ListTile(
                      title: Text('Project ${project.id.substring(0, 8)}'),
                      subtitle: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (project.notes != null &&
                              project.notes!.isNotEmpty)
                            Text(project.notes!),
                          if (item.lifecycle != null) ...[
                            const SizedBox(height: 6),
                            VendorLifecycleChips(lifecycle: item.lifecycle!),
                          ],
                        ],
                      ),
                      isThreeLine: item.lifecycle != null,
                      trailing: Chip(
                        label: Text(project.status.replaceAll('_', ' ')),
                        backgroundColor: AppColors.status(
                          project.status,
                        ).withValues(alpha: 0.15),
                      ),
                      onTap: () => Navigator.of(context).push(
                        MaterialPageRoute(
                          builder: (_) =>
                              VendorProjectDetailScreen(projectId: project.id),
                        ),
                      ),
                    ),
                  );
                },
              ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => const Center(child: Text('Could not load projects')),
      ),
    );
  }
}

class VendorProjectDetailScreen extends ConsumerWidget {
  const VendorProjectDetailScreen({super.key, required this.projectId});

  final String projectId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = ref.watch(vendorProjectDetailProvider(projectId));
    return Scaffold(
      appBar: AppBar(
        title: const Text('Project'),
        actions: [
          IconButton(
            key: const Key('open-quote'),
            tooltip: 'Prepare bid',
            icon: const Icon(Icons.request_quote_outlined),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(
                builder: (_) => VendorQuoteScreen(projectId: projectId),
              ),
            ),
          ),
        ],
      ),
      body: detail.when(
        data: (data) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (data.site != null && data.site!.hasContact) ...[
              VendorSiteCard(site: data.site!),
              const SizedBox(height: 12),
            ],
            if (data.lifecycle != null) ...[
              VendorLifecycleChips(lifecycle: data.lifecycle!),
              const SizedBox(height: 12),
            ],
            if (data.rejectedForResubmission != null)
              Card(
                color: Theme.of(context).colorScheme.errorContainer,
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Resubmission needed',
                        style: Theme.of(context).textTheme.titleSmall?.copyWith(
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      if (data.rejectedForResubmission!.reviewNotes !=
                          null) ...[
                        const SizedBox(height: 4),
                        Text(data.rejectedForResubmission!.reviewNotes!),
                      ],
                    ],
                  ),
                ),
              ),
            const SizedBox(height: 12),
            Text('Submissions', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (data.submissions.isEmpty) const Text('None yet'),
            for (final submission in data.submissions)
              ListTile(
                dense: true,
                leading: Icon(
                  Icons.route_outlined,
                  color: AppColors.status(submission.status),
                ),
                title: Text(submission.status.replaceAll('_', ' ')),
                subtitle: submission.actualLengthMeters != null
                    ? Text(
                        '${submission.actualLengthMeters!.toStringAsFixed(0)} m',
                      )
                    : null,
              ),
            const SizedBox(height: 96),
          ],
        ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => const Center(child: Text('Could not load project')),
      ),
      bottomNavigationBar: detail.maybeWhen(
        data: (data) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: FilledButton.icon(
              key: const Key('start-capture'),
              icon: const Icon(Icons.route),
              label: Text(
                data.rejectedForResubmission != null
                    ? 'Recapture as-built'
                    : 'Capture as-built',
              ),
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(
                  builder: (_) => AsBuiltCaptureScreen(
                    projectId: projectId,
                    prefillLengthMeters:
                        data.rejectedForResubmission?.actualLengthMeters,
                  ),
                ),
              ),
            ),
          ),
        ),
        orElse: () => null,
      ),
    );
  }
}

class AsBuiltCaptureScreen extends ConsumerStatefulWidget {
  const AsBuiltCaptureScreen({
    super.key,
    required this.projectId,
    this.prefillLengthMeters,
  });

  final String projectId;
  final double? prefillLengthMeters;

  @override
  ConsumerState<AsBuiltCaptureScreen> createState() =>
      _AsBuiltCaptureScreenState();
}

class _AsBuiltCaptureScreenState extends ConsumerState<AsBuiltCaptureScreen> {
  final recorder = TraceRecorder();
  Timer? _sampler;
  bool _submitting = false;

  String? _variationType;
  final List<AsBuiltLineItem> _lineItems = [];
  int _photoCount = 0;

  @override
  void dispose() {
    _sampler?.cancel();
    super.dispose();
  }

  void _toggleRecording() {
    setState(() {
      if (recorder.recording) {
        recorder.stop();
        _sampler?.cancel();
      } else {
        recorder.start();
        _sampler = Timer.periodic(const Duration(seconds: 3), (_) async {
          final point = await ref.read(locationSourceProvider).current();
          if (point != null && mounted) {
            setState(() => recorder.addPoint(point));
          }
        });
      }
    });
  }

  Future<void> _addPhoto() async {
    final captured = await ref.read(photoCaptureProvider)(
      installationProjectId: widget.projectId,
    );
    if (captured && mounted) {
      setState(() => _photoCount++);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Photo queued — uploads when online')),
      );
    }
  }

  Future<void> _addLineItem() async {
    final item = await showModalBottomSheet<AsBuiltLineItem>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _LineItemSheet(),
    );
    if (item != null) setState(() => _lineItems.add(item));
  }

  Future<void> _submit() async {
    setState(() => _submitting = true);
    await ref
        .read(vendorRepositoryProvider)
        .submitAsBuilt(
          projectId: widget.projectId,
          geojson: recorder.toGeoJson(),
          actualLengthMeters: recorder.distanceMeters,
          variationType: _variationType,
          lineItems: _lineItems,
        );
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('As-built submitted — will sync when online'),
        ),
      );
      Navigator.of(context).pop(true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('As-built capture')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (widget.prefillLengthMeters != null)
            Text(
              'Previous submission: ${widget.prefillLengthMeters!.toStringAsFixed(0)} m (rejected)',
              key: const Key('prefill-banner'),
            ),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                children: [
                  Text(
                    '${recorder.points.length} points · ${recorder.distanceMeters.toStringAsFixed(0)} m',
                    key: const Key('trace-pill'),
                    style: theme.textTheme.titleLarge,
                  ),
                  const SizedBox(height: 16),
                  FilledButton.icon(
                    key: const Key('record-toggle'),
                    onPressed: _toggleRecording,
                    icon: Icon(
                      recorder.recording
                          ? Icons.stop
                          : Icons.fiber_manual_record,
                    ),
                    label: Text(
                      recorder.recording
                          ? 'Stop recording'
                          : 'Start walking the route',
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          // Evidence photos — queued to the same offline outbox, uploaded
          // against this installation project.
          OutlinedButton.icon(
            key: const Key('add-photo'),
            onPressed: _addPhoto,
            icon: const Icon(Icons.add_a_photo_outlined),
            label: Text(
              _photoCount == 0 ? 'Add photo' : 'Add photo ($_photoCount)',
            ),
          ),
          const SizedBox(height: 16),
          Text('Variation (optional)', style: theme.textTheme.titleSmall),
          const SizedBox(height: 4),
          DropdownButtonFormField<String?>(
            key: const Key('variation-type'),
            initialValue: _variationType,
            decoration: const InputDecoration(
              border: OutlineInputBorder(),
              isDense: true,
            ),
            items: [
              const DropdownMenuItem(value: null, child: Text('None')),
              for (final v in asBuiltVariationTypes)
                DropdownMenuItem(value: v, child: Text(v.replaceAll('_', ' '))),
            ],
            onChanged: (v) => setState(() => _variationType = v),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: Text('Line items', style: theme.textTheme.titleSmall),
              ),
              TextButton.icon(
                key: const Key('add-line-item'),
                onPressed: _addLineItem,
                icon: const Icon(Icons.add),
                label: const Text('Add'),
              ),
            ],
          ),
          if (_lineItems.isEmpty)
            Text('None', style: theme.textTheme.bodySmall)
          else
            for (var i = 0; i < _lineItems.length; i++)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text(
                  _lineItems[i].description ?? _lineItems[i].itemType ?? 'Item',
                ),
                subtitle: Text(
                  '${_lineItems[i].quantity} × ${_lineItems[i].unitPrice}',
                ),
                trailing: IconButton(
                  icon: const Icon(Icons.close),
                  onPressed: () => setState(() => _lineItems.removeAt(i)),
                ),
              ),
          const SizedBox(height: 24),
        ],
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: FilledButton(
            key: const Key('submit-asbuilt'),
            onPressed:
                !recorder.recording && recorder.hasUsableTrace && !_submitting
                ? _submit
                : null,
            child: const Text('Review & submit'),
          ),
        ),
      ),
    );
  }
}

/// Bottom-sheet form for one as-built line item.
class _LineItemSheet extends StatefulWidget {
  const _LineItemSheet();

  @override
  State<_LineItemSheet> createState() => _LineItemSheetState();
}

class _LineItemSheetState extends State<_LineItemSheet> {
  final _description = TextEditingController();
  final _itemType = TextEditingController();
  final _quantity = TextEditingController(text: '1');
  final _unitPrice = TextEditingController(text: '0');

  @override
  void dispose() {
    _description.dispose();
    _itemType.dispose();
    _quantity.dispose();
    _unitPrice.dispose();
    super.dispose();
  }

  void _save() {
    final qty = num.tryParse(_quantity.text.trim()) ?? 1;
    Navigator.of(context).pop(
      AsBuiltLineItem(
        description: _description.text.trim().isEmpty
            ? null
            : _description.text.trim(),
        itemType: _itemType.text.trim().isEmpty ? null : _itemType.text.trim(),
        quantity: qty < 1 ? 1 : qty,
        unitPrice: num.tryParse(_unitPrice.text.trim()) ?? 0,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        left: 16,
        right: 16,
        top: 16,
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Add line item', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          TextField(
            key: const Key('li-description'),
            controller: _description,
            decoration: const InputDecoration(
              labelText: 'Description',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _itemType,
            decoration: const InputDecoration(
              labelText: 'Item type (optional)',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const Key('li-quantity'),
                  controller: _quantity,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(
                    labelText: 'Qty',
                    border: OutlineInputBorder(),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: TextField(
                  key: const Key('li-unit-price'),
                  controller: _unitPrice,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(
                    labelText: 'Unit price',
                    border: OutlineInputBorder(),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          FilledButton(
            key: const Key('li-save'),
            onPressed: _save,
            child: const Text('Add'),
          ),
        ],
      ),
    );
  }
}

/// "Who to call, where to go" — the site bundle on a vendor project (#122).
class VendorSiteCard extends ConsumerWidget {
  const VendorSiteCard({super.key, required this.site});

  final VendorSite site;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Site contact',
              style: theme.textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (site.name != null && site.name!.isNotEmpty)
                        Text(site.name!),
                      if (site.addressText != null &&
                          site.addressText!.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(
                          site.addressText!,
                          style: theme.textTheme.bodySmall,
                        ),
                      ],
                    ],
                  ),
                ),
                if (site.phone != null)
                  IconButton(
                    key: const Key('vendor-call-button'),
                    tooltip: 'Call site contact',
                    icon: const Icon(Icons.call_outlined),
                    onPressed: () => ref.read(uriLauncherProvider)(
                      Uri.parse('tel:${site.phone}'),
                    ),
                  ),
              ],
            ),
            if (site.accessNotes != null && site.accessNotes!.isNotEmpty) ...[
              const SizedBox(height: 8),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: theme.colorScheme.secondaryContainer.withValues(
                    alpha: 0.4,
                  ),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(Icons.vpn_key_outlined, size: 16),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        site.accessNotes!,
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Chips for the bid → approval → as-built → payment lifecycle (#123). Only the
/// stages the crew has reached are shown.
class VendorLifecycleChips extends StatelessWidget {
  const VendorLifecycleChips({super.key, required this.lifecycle});

  final VendorLifecycle lifecycle;

  static Widget? _chip(String prefix, VendorStageState? stage) {
    if (stage == null || !stage.isPresent) return null;
    final status = stage.status!;
    final text = stage.label != null
        ? '$prefix: ${status.replaceAll('_', ' ')} · ${stage.label}'
        : '$prefix: ${status.replaceAll('_', ' ')}';
    return Chip(
      label: Text(text),
      visualDensity: VisualDensity.compact,
      backgroundColor: AppColors.status(status).withValues(alpha: 0.15),
    );
  }

  @override
  Widget build(BuildContext context) {
    final chips = <Widget?>[
      _chip('Quote', lifecycle.quote),
      _chip('As-built', lifecycle.asBuilt),
      _chip('Billing', lifecycle.billing),
    ].whereType<Widget>().toList();
    if (chips.isEmpty) return const SizedBox.shrink();
    return Wrap(spacing: 6, runSpacing: 6, children: chips);
  }
}

/// The bid: line items + a proposed route on the map, then submit. Online —
/// the crew needs the server-assigned quote id before adding lines / submitting.
class VendorQuoteScreen extends ConsumerWidget {
  const VendorQuoteScreen({super.key, required this.projectId});

  final String projectId;

  Future<void> _addLineItem(
    BuildContext context,
    WidgetRef ref,
    String quoteId,
  ) async {
    final item = await showModalBottomSheet<AsBuiltLineItem>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _LineItemSheet(),
    );
    if (item == null) return;
    await ref.read(vendorRepositoryProvider).addQuoteLineItem(quoteId, item);
    if (!context.mounted) return;
    ref.invalidate(vendorProjectQuoteProvider(projectId));
  }

  Future<void> _removeLineItem(
    WidgetRef ref,
    String quoteId,
    String lineItemId,
  ) async {
    await ref
        .read(vendorRepositoryProvider)
        .removeQuoteLineItem(quoteId, lineItemId);
    ref.invalidate(vendorProjectQuoteProvider(projectId));
  }

  Future<void> _captureRoute(
    BuildContext context,
    WidgetRef ref,
    String quoteId,
  ) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => _ProposedRouteCaptureScreen(quoteId: quoteId),
      ),
    );
    if (context.mounted && saved == true) {
      ref.invalidate(vendorProjectQuoteProvider(projectId));
    }
  }

  Future<void> _submit(
    BuildContext context,
    WidgetRef ref,
    String quoteId,
  ) async {
    try {
      await ref.read(vendorRepositoryProvider).submitQuote(quoteId);
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Bid submitted')));
        Navigator.of(context).pop();
      }
    } catch (_) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Could not submit — add a line item and try again'),
          ),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = ref.watch(vendorProjectQuoteProvider(projectId));
    return Scaffold(
      appBar: AppBar(title: const Text('Prepare bid')),
      body: detail.when(
        data: (data) {
          final quote = data.quote;
          final editable = quote.isEditable;
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      quote.total != null
                          ? '${quote.currency ?? ''} ${quote.total}'.trim()
                          : 'Draft bid',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  Chip(
                    label: Text(quote.status.replaceAll('_', ' ')),
                    backgroundColor: AppColors.status(
                      quote.status,
                    ).withValues(alpha: 0.15),
                  ),
                ],
              ),
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'Line items',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
                  if (editable)
                    TextButton.icon(
                      key: const Key('quote-add-line-item'),
                      onPressed: () => _addLineItem(context, ref, quote.id),
                      icon: const Icon(Icons.add),
                      label: const Text('Add'),
                    ),
                ],
              ),
              if (data.lineItems.isEmpty)
                Text('None yet', style: Theme.of(context).textTheme.bodySmall)
              else
                for (final line in data.lineItems)
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    title: Text(line.description ?? 'Item'),
                    subtitle: Text(
                      '${line.quantity ?? 1} × ${line.unitPrice ?? 0}',
                    ),
                    trailing: editable
                        ? Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Text('${line.amount ?? 0}'),
                              IconButton(
                                key: Key('remove-line-${line.id}'),
                                icon: const Icon(Icons.close, size: 18),
                                tooltip: 'Remove',
                                onPressed: () =>
                                    _removeLineItem(ref, quote.id, line.id),
                              ),
                            ],
                          )
                        : Text('${line.amount ?? 0}'),
                  ),
              const SizedBox(height: 16),
              Text(
                'Proposed route',
                style: Theme.of(context).textTheme.titleSmall,
              ),
              const SizedBox(height: 4),
              if (data.proposedRoutes.isEmpty)
                Text(
                  'Not attached yet',
                  style: Theme.of(context).textTheme.bodySmall,
                )
              else
                for (final route in data.proposedRoutes)
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(
                      Icons.route_outlined,
                      color: AppColors.status(route.status),
                    ),
                    title: Text('Revision ${route.revisionNumber}'),
                    trailing: Text(route.status.replaceAll('_', ' ')),
                  ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                key: const Key('quote-capture-route'),
                onPressed: editable
                    ? () => _captureRoute(context, ref, quote.id)
                    : null,
                icon: const Icon(Icons.route_outlined),
                label: Text(
                  data.proposedRoutes.isEmpty
                      ? 'Capture proposed route'
                      : 'Capture new revision',
                ),
              ),
              const SizedBox(height: 24),
            ],
          );
        },
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => const Center(child: Text('Could not load the bid')),
      ),
      bottomNavigationBar: detail.maybeWhen(
        data: (data) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: FilledButton(
              key: const Key('submit-quote'),
              onPressed: data.quote.isEditable && data.lineItems.isNotEmpty
                  ? () => _submit(context, ref, data.quote.id)
                  : null,
              child: const Text('Submit bid'),
            ),
          ),
        ),
        orElse: () => null,
      ),
    );
  }
}

/// Walk/draw the proposed route, then attach + submit it to the quote.
class _ProposedRouteCaptureScreen extends ConsumerStatefulWidget {
  const _ProposedRouteCaptureScreen({required this.quoteId});

  final String quoteId;

  @override
  ConsumerState<_ProposedRouteCaptureScreen> createState() =>
      _ProposedRouteCaptureScreenState();
}

class _ProposedRouteCaptureScreenState
    extends ConsumerState<_ProposedRouteCaptureScreen> {
  final recorder = TraceRecorder();
  Timer? _sampler;
  bool _saving = false;

  @override
  void dispose() {
    _sampler?.cancel();
    super.dispose();
  }

  void _toggleRecording() {
    setState(() {
      if (recorder.recording) {
        recorder.stop();
        _sampler?.cancel();
      } else {
        recorder.start();
        _sampler = Timer.periodic(const Duration(seconds: 3), (_) async {
          final point = await ref.read(locationSourceProvider).current();
          if (point != null && mounted) {
            setState(() => recorder.addPoint(point));
          }
        });
      }
    });
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await ref
          .read(vendorRepositoryProvider)
          .addProposedRoute(
            widget.quoteId,
            recorder.toGeoJson(),
            recorder.distanceMeters,
          );
      if (mounted) {
        Navigator.of(context).pop(true);
      }
    } catch (_) {
      if (mounted) {
        setState(() => _saving = false);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not save the route')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Proposed route')),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              '${recorder.points.length} points · ${recorder.distanceMeters.toStringAsFixed(0)} m',
              key: const Key('proposed-trace-pill'),
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 16),
            FilledButton.icon(
              key: const Key('proposed-record-toggle'),
              onPressed: _toggleRecording,
              icon: Icon(
                recorder.recording ? Icons.stop : Icons.fiber_manual_record,
              ),
              label: Text(
                recorder.recording
                    ? 'Stop recording'
                    : 'Start walking the route',
              ),
            ),
          ],
        ),
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: FilledButton(
            key: const Key('save-proposed-route'),
            onPressed:
                !recorder.recording && recorder.hasUsableTrace && !_saving
                ? _save
                : null,
            child: const Text('Attach to bid'),
          ),
        ),
      ),
    );
  }
}
