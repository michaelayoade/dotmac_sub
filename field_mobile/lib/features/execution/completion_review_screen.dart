import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../jobs/jobs_providers.dart';
import '../jobs/job_attachment.dart';
import 'completion_review_models.dart';
import 'completion_review_drafts.dart';
import 'completion_review_repository.dart';
import 'completion_wizard.dart';
import 'execution_controller.dart';

/// Review is an independent server fact, never derived from execution status.
class CompletionReviewPanel extends ConsumerWidget {
  const CompletionReviewPanel({super.key, required this.jobId});
  final String jobId;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final preview = ref.watch(completionReviewPreviewProvider(jobId));
    final draft = ref.watch(completionReviewDraftProvider(jobId)).valueOrNull;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Completion evidence and last staff review',
              style: Theme.of(context).textTheme.titleSmall,
            ),
            const SizedBox(height: 8),
            const Text(
              'Execution completion and supervisor acceptance are separate. Recorded reviews do not confirm current engineering acceptance.',
            ),
            const SizedBox(height: 8),
            if (draft != null)
              OutlinedButton(
                onPressed: () async {
                  await Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) => CompletionReviewScreen(
                        jobId: jobId,
                        preview: draft.intent.preview,
                      ),
                    ),
                  );
                  ref.invalidate(completionReviewDraftProvider(jobId));
                  ref.invalidate(completionReviewPreviewProvider(jobId));
                },
                child: const Text('Resume saved completion evidence'),
              ),
            preview.when(
              loading: () =>
                  const Text('Loading review criteria and review state…'),
              error: (error, _) => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    error is CompletionReviewRefusal
                        ? error.message
                        : 'Review information unavailable.',
                  ),
                  TextButton(
                    onPressed: () =>
                        ref.invalidate(completionReviewPreviewProvider(jobId)),
                    child: const Text('Reload review information'),
                  ),
                ],
              ),
              data: (value) => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    value.state.label,
                    key: const Key('completion-review-state'),
                  ),
                  if (!value.submissionAvailable)
                    Text(
                      value.submissionUnavailableReason ??
                          'Evidence submission is unavailable.',
                    ),
                  OutlinedButton(
                    onPressed: () async {
                      await Navigator.of(context).push<void>(
                        MaterialPageRoute(
                          builder: (_) => CompletionReviewScreen(
                            jobId: jobId,
                            preview: value,
                          ),
                        ),
                      );
                      ref.invalidate(completionReviewPreviewProvider(jobId));
                      ref.invalidate(completionReviewDraftProvider(jobId));
                    },
                    child: Text(
                      value.submissionAvailable
                          ? 'Record review criteria evidence'
                          : 'View review criteria',
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class CompletionReviewScreen extends ConsumerStatefulWidget {
  const CompletionReviewScreen({
    super.key,
    required this.jobId,
    required this.preview,
  });
  final String jobId;
  final CompletionReviewPreview preview;
  @override
  ConsumerState<CompletionReviewScreen> createState() =>
      _CompletionReviewScreenState();
}

class _CompletionReviewScreenState
    extends ConsumerState<CompletionReviewScreen> {
  late CompletionReviewPreview _preview;
  late final CompletionReviewDraftStore _draftStore;
  String _intentId = const Uuid().v4();
  Future<void> _draftWrites = Future<void>.value();
  bool _restoring = true;
  bool _restored = false;
  final _observations = <String, TextEditingController>{};
  final _selectedAttachments = <String, Set<String>>{};
  final _reason = TextEditingController();
  CompletionEvidenceIntent? _intent;
  CompletionEvidenceReceipt? _receipt;
  bool _busy = false;
  bool _refused = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _preview = widget.preview;
    _draftStore = ref.read(completionReviewDraftStoreProvider);
    _initializeInputs();
    _reason.addListener(_queueDraft);
    Future.microtask(_restoreDraft);
  }

  void _initializeInputs() {
    for (final controller in _observations.values) {
      controller.dispose();
    }
    _observations.clear();
    _selectedAttachments.clear();
    for (final criterion in _preview.criteria) {
      _observations[criterion.id] = TextEditingController()
        ..addListener(_queueDraft);
      _selectedAttachments[criterion.id] = <String>{};
    }
  }

  CompletionEvidenceIntent _currentIntent() =>
      _intent ??
      CompletionEvidenceIntent(
        intentId: _intentId,
        reason: _reason.text,
        preview: _preview,
        observations: [
          for (final criterion in _preview.criteria)
            CompletionCriterionObservation(
              criterionId: criterion.id,
              observation: _observations[criterion.id]!.text,
              attachmentIds: List.unmodifiable(
                _selectedAttachments[criterion.id]!,
              ),
            ),
        ],
      );

  void _queueDraft() {
    if (_restoring || _receipt != null) return;
    final draft = CompletionEvidenceDraft(
      intent: _currentIntent(),
      attempted: _intent != null,
      refused: _refused,
    );
    _draftWrites = _draftWrites
        .catchError((Object _) {})
        .then((_) => _draftStore.save(widget.jobId, draft));
    _draftWrites.catchError((Object _) {
      if (mounted) {
        setState(
          () => _error =
              'Could not save completion evidence on this device. Keep this form open and retry saving.',
        );
      }
    });
  }

  Future<void> _restoreDraft() async {
    try {
      final draft = await _draftStore.load(widget.jobId);
      if (!mounted) return;
      if (draft != null) {
        _preview = draft.intent.preview;
        _intentId = draft.intent.intentId;
        _initializeInputs();
        _reason.text = draft.intent.reason;
        for (final observation in draft.intent.observations) {
          _observations[observation.criterionId]!.text =
              observation.observation;
          _selectedAttachments[observation.criterionId]!.addAll(
            observation.attachmentIds,
          );
        }
        _intent = draft.attempted ? draft.intent : null;
        _refused = draft.refused;
        _restored = true;
      }
      setState(() => _restoring = false);
      _queueDraft();
    } catch (_) {
      if (mounted) {
        setState(() {
          _error =
              'Saved completion evidence could not be restored. Reload review criteria to discard it explicitly.';
          _refused = true;
          _restoring = false;
        });
      }
    }
  }

  @override
  void dispose() {
    for (final controller in _observations.values) {
      controller.dispose();
    }
    _reason.dispose();
    super.dispose();
  }

  bool get _editable =>
      !_busy &&
      !_restoring &&
      !_refused &&
      _intent == null &&
      _receipt == null &&
      _preview.submissionAvailable;

  Future<void> _reload() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final preview = await ref
          .read(completionReviewRepositoryProvider)
          .preview(widget.jobId);
      await _draftWrites.catchError((Object _) {});
      await _draftStore.delete(widget.jobId);
      if (!mounted) return;
      _restoring = true;
      setState(() {
        _preview = preview;
        _initializeInputs();
        _reason.clear();
        _intent = null;
        _intentId = const Uuid().v4();
        _restored = false;
        _receipt = null;
        _refused = false;
        _restoring = false;
      });
      _queueDraft();
      await _draftWrites;
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = error is CompletionReviewRefusal
              ? error.message
              : 'Could not reload review criteria. Try again.';
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _capturePhoto() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(photoCaptureProvider)
          .recoverLost(workOrderId: widget.jobId);
      await ref.read(photoCaptureProvider).capture(workOrderId: widget.jobId);
      await ref.read(syncServiceProvider).flushPhotos();
      ref.invalidate(jobDetailProvider(widget.jobId));
      await ref.read(jobDetailProvider(widget.jobId).future);
    } catch (_) {
      if (mounted) {
        setState(
          () => _error =
              'Photo upload is incomplete. Uploaded job attachments can be selected after refresh.',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() async {
    if (_busy || _restoring || _refused || _receipt != null) return;
    if (_intent == null) {
      if (_reason.text.trim().isEmpty ||
          _observations.values.any((c) => c.text.trim().isEmpty)) {
        setState(
          () => _error =
              'Add an observation for every enrolled criterion and a submission reason.',
        );
        return;
      }
      _intent = CompletionEvidenceIntent(
        intentId: _intentId,
        reason: _reason.text.trim(),
        preview: _preview,
        observations: [
          for (final criterion in _preview.criteria)
            CompletionCriterionObservation(
              criterionId: criterion.id,
              observation: _observations[criterion.id]!.text.trim(),
              attachmentIds: List.unmodifiable(
                _selectedAttachments[criterion.id]!,
              ),
            ),
        ],
      );
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      _queueDraft();
      await _draftWrites;
    } catch (_) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error =
              'Could not save this intent. Submission was not sent. Retry saving before sending.';
        });
      }
      return;
    }
    try {
      final receipt = await ref
          .read(completionReviewRepositoryProvider)
          .submit(widget.jobId, _intent!);
      if (!mounted) return;
      setState(() => _receipt = receipt);
      try {
        await _draftWrites;
        await _draftStore.delete(widget.jobId);
        ref.invalidate(completionReviewDraftProvider(widget.jobId));
      } catch (_) {
        if (mounted) {
          setState(
            () => _error =
                'Evidence was received, but its local draft could not be removed. Reopening may require retrying the same intent to recover its receipt.',
          );
        }
      }
      ref.invalidate(completionReviewPreviewProvider(widget.jobId));
    } on CompletionReviewRefusal catch (error) {
      if (mounted) {
        setState(() {
          _refused = true;
          _error =
              '${error.message}\n${error.code}\nReload review criteria before a new submission.';
        });
        _queueDraft();
      }
    } catch (_) {
      if (mounted) {
        setState(
          () => _error =
              'Delivery could not be confirmed. Retry sends the same intent and evidence.',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final detail = ref.watch(jobDetailProvider(widget.jobId));
    final attachments =
        detail.valueOrNull?.attachments ?? const <JobAttachment>[];
    return Scaffold(
      appBar: AppBar(title: const Text('Completion review evidence')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            _receipt?.state.label ?? _preview.state.label,
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const Text(
            'Submitting observations requests supervisor review. Execution completion remains a separate job action.',
          ),
          const SizedBox(height: 8),
          if (_restoring) const Text('Restoring saved evidence…'),
          if (_restored)
            const Text(
              'Saved evidence restored with its original intent and tokens. Nothing was sent automatically.',
            ),
          if (_refused)
            const Text(
              'Submission is blocked. Explicitly reload review criteria before starting a new intent.',
            ),
          Text(
            _preview.enrollmentId == null
                ? 'Completion review is not configured for this job.'
                : 'Review version: ${_preview.generation}',
          ),
          if (!_preview.submissionAvailable)
            Text(
              _preview.submissionUnavailableReason ??
                  'Evidence submission is unavailable.',
            ),
          if (_receipt != null) ...[
            const SizedBox(height: 12),
            const Text('Evidence received by the server.'),
            SelectableText(
              'Submission: ${_receipt!.submissionId ?? 'Unavailable'}',
            ),
          ],
          for (final criterion in _preview.criteria)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                      criterion.statement,
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    Text('Verification: ${criterion.verificationMethod}'),
                    TextField(
                      key: Key('criterion-observation-${criterion.id}'),
                      controller: _observations[criterion.id],
                      enabled: _editable,
                      maxLines: 3,
                      decoration: const InputDecoration(
                        labelText: 'Observed result',
                      ),
                    ),
                    const SizedBox(height: 8),
                    const Text('Link uploaded job evidence'),
                    for (final attachment in attachments)
                      CheckboxListTile(
                        contentPadding: EdgeInsets.zero,
                        title: Text(attachment.fileName),
                        subtitle: Text(attachment.id),
                        value: _selectedAttachments[criterion.id]!.contains(
                          attachment.id,
                        ),
                        onChanged: !_editable
                            ? null
                            : (selected) => setState(() {
                                final ids = _selectedAttachments[criterion.id]!;
                                if (selected == true) {
                                  ids.add(attachment.id);
                                } else {
                                  ids.remove(attachment.id);
                                }
                                _queueDraft();
                              }),
                      ),
                  ],
                ),
              ),
            ),
          detail.when(
            data: (_) => const SizedBox.shrink(),
            loading: () => const Text('Loading uploaded job attachments…'),
            error: (_, _) => const Text(
              'Job attachments unavailable. Refresh before selecting evidence.',
            ),
          ),
          OutlinedButton.icon(
            onPressed: _editable ? _capturePhoto : null,
            icon: const Icon(Icons.photo_camera_outlined),
            label: const Text('Add and upload job photo'),
          ),
          TextButton(
            onPressed: _busy
                ? null
                : () => ref.invalidate(jobDetailProvider(widget.jobId)),
            child: const Text('Refresh uploaded attachments'),
          ),
          TextField(
            controller: _reason,
            enabled: _editable,
            maxLines: 2,
            decoration: const InputDecoration(labelText: 'Submission reason'),
          ),
          if (_intent != null) SelectableText('Intent: ${_intent!.intentId}'),
          if (_error != null)
            Text(
              _error!,
              key: const Key('completion-review-error'),
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          const SizedBox(height: 12),
          FilledButton(
            key: const Key('submit-completion-evidence'),
            onPressed:
                _busy ||
                    _restoring ||
                    _refused ||
                    _receipt != null ||
                    !_preview.submissionAvailable
                ? null
                : _submit,
            child: Text(
              _busy
                  ? 'Working…'
                  : _intent == null
                  ? 'Confirm and submit evidence'
                  : 'Retry same submission',
            ),
          ),
          const Text(
            'Discard and reload removes the saved evidence and starts a new intent using current review criteria and review tokens.',
          ),
          OutlinedButton(
            key: const Key('reload-completion-review'),
            onPressed: _busy || _restoring ? null : _reload,
            child: const Text('Discard and reload review criteria'),
          ),
        ],
      ),
    );
  }
}
