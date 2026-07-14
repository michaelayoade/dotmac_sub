import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../jobs/job_models.dart';
import 'completion_state.dart';
import 'execution_controller.dart';
import 'signature_pad.dart';

/// Camera abstraction: returns true when a photo was captured and queued.
/// The device build overrides this with PhotoQueue.captureForJob at
/// bootstrap; the default no-op keeps headless environments safe.
typedef PhotoCapture =
    Future<bool> Function({String? workOrderId, String? installationProjectId});

final photoCaptureProvider = Provider<PhotoCapture>(
  (ref) =>
      ({workOrderId, installationProjectId}) async => false,
);

/// Queues a rendered customer signature as a kind=signature attachment.
/// Overridden at bootstrap with PhotoQueue.enqueueImageBytes; no-op in tests.
typedef SignatureSink =
    Future<void> Function({
      required String workOrderId,
      required Uint8List png,
    });

final signatureSinkProvider = Provider<SignatureSink>(
  (ref) => ({required workOrderId, required png}) async {},
);

/// Canvas size the signature is rendered at for upload.
const _signatureCanvas = Size(800, 220);

class CompletionWizard extends ConsumerStatefulWidget {
  const CompletionWizard({
    super.key,
    required this.jobId,
    this.requirements = JobCompletionRequirements.safeFallback,
    this.existingPhotoCount = 0,
    this.hasExistingSignature = false,
  });

  final String jobId;
  final JobCompletionRequirements requirements;
  final int existingPhotoCount;
  final bool hasExistingSignature;

  @override
  ConsumerState<CompletionWizard> createState() => _CompletionWizardState();
}

class _CompletionWizardState extends ConsumerState<CompletionWizard> {
  int _step = 0;
  late CompletionState _completion;
  final _signature = SignaturePadController();
  final _signerName = TextEditingController();
  final _fallbackReason = TextEditingController();
  final _serial = TextEditingController();
  final _summary = TextEditingController();

  @override
  void initState() {
    super.initState();
    _completion = CompletionState(
      requirements: widget.requirements,
      photoCount: widget.existingPhotoCount,
      hasSignature: widget.hasExistingSignature,
    );
  }

  void _update(CompletionState Function(CompletionState) change) {
    setState(() => _completion = change(_completion));
  }

  @override
  void dispose() {
    _signerName.dispose();
    _fallbackReason.dispose();
    _serial.dispose();
    _summary.dispose();
    super.dispose();
  }

  Future<void> _finish() async {
    final completion = _completion;
    final controller = ref.read(executionControllerProvider.notifier);
    final sync = ref.read(syncServiceProvider);

    // Render a newly drawn signature and queue it before the transition when
    // the current server policy requires sign-off evidence.
    if (completion.hasSignature && _signature.hasInk) {
      final png = await _signature.toPng(_signatureCanvas);
      await ref.read(signatureSinkProvider)(
        workOrderId: widget.jobId,
        png: png,
      );
    }
    if (_serial.text.trim().isNotEmpty) {
      // Record the installed ONT through the dedicated equipment endpoint,
      // which links it to the subscriber + work order (not a free-text note).
      await sync.enqueue(
        kind: 'equipment',
        clientRef: 'equip-${DateTime.now().microsecondsSinceEpoch}',
        payload: {
          'work_order_id': widget.jobId,
          'serial_number': _serial.text.trim(),
        },
      );
    }
    // Push evidence (photos + signature) up first so the complete transition
    // never races ahead of its attachments. Offline, the photos-before-outbox
    // ordering in flushAll preserves this on reconnect.
    await sync.flushPhotos();
    await controller.transition(
      widget.jobId,
      'complete',
      payload: completion.transitionPayload,
    );
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Job completed — will sync when online')),
      );
      Navigator.of(context).pop(true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final completion = _completion;

    return Scaffold(
      appBar: AppBar(title: Text('Complete job — step ${_step + 1} of 3')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: switch (_step) {
          0 => _ChecklistStep(
            done: completion.checklistDone,
            onChanged: (value) =>
                _update((s) => s.copyWith(checklistDone: value)),
          ),
          1 => _EvidenceStep(
            photoCount: completion.photoCount,
            minimumPhotoCount: completion.requirements.minimumPhotoCount,
            summary: _summary,
            onAddPhoto: () async {
              final captured = await ref.read(photoCaptureProvider)(
                workOrderId: widget.jobId,
              );
              if (captured) {
                _update((s) => s.copyWith(photoCount: s.photoCount + 1));
              }
            },
          ),
          _ => _SignOffStep(
            required: completion.requirements.customerSignoffRequired,
            fallbackAllowed:
                completion.requirements.signatureUnavailableReasonAllowed,
            signature: _signature,
            signerName: _signerName,
            fallbackReason: _fallbackReason,
            serial: _serial,
            onSigned: () => _update(
              (s) => s.copyWith(
                hasSignature: widget.hasExistingSignature || _signature.hasInk,
              ),
            ),
            onFallbackChanged: (value) =>
                _update((s) => s.copyWith(signatureUnavailableReason: value)),
            onSignerChanged: (value) =>
                _update((s) => s.copyWith(signerName: value)),
          ),
        },
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (_step == 2 && !completion.canComplete)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(
                    completion.blockers.join(' · '),
                    style: Theme.of(context).textTheme.bodySmall,
                    textAlign: TextAlign.center,
                  ),
                ),
              Row(
                children: [
                  if (_step > 0)
                    Expanded(
                      child: OutlinedButton(
                        onPressed: () => setState(() => _step--),
                        child: const Text('Back'),
                      ),
                    ),
                  if (_step > 0) const SizedBox(width: 12),
                  Expanded(
                    flex: 2,
                    child: _step < 2
                        ? FilledButton(
                            key: const Key('wizard-next'),
                            onPressed: () {
                              setState(() {
                                _completion = _completion.copyWith(
                                  summary: _summary.text,
                                );
                                _step++;
                              });
                            },
                            child: const Text('Continue'),
                          )
                        : FilledButton(
                            key: const Key('wizard-finish'),
                            onPressed: completion.canComplete ? _finish : null,
                            child: const Text('Finish job'),
                          ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ChecklistStep extends StatelessWidget {
  const _ChecklistStep({required this.done, required this.onChanged});

  final bool done;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return ListView(
      children: [
        Text(
          'Quality checklist',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        CheckboxListTile(
          key: const Key('checklist-confirm'),
          value: done,
          onChanged: (value) => onChanged(value ?? false),
          title: const Text(
            'Work completed to spec, site tidy, customer informed',
          ),
          subtitle: const Text('Recommended quality confirmation'),
        ),
      ],
    );
  }
}

class _EvidenceStep extends StatelessWidget {
  const _EvidenceStep({
    required this.photoCount,
    required this.minimumPhotoCount,
    required this.onAddPhoto,
    required this.summary,
  });

  final int photoCount;
  final int minimumPhotoCount;
  final Future<void> Function() onAddPhoto;
  final TextEditingController summary;

  @override
  Widget build(BuildContext context) {
    return ListView(
      children: [
        Text('Evidence', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        Text(
          minimumPhotoCount > 0
              ? 'Photos: $photoCount · required: $minimumPhotoCount'
              : 'Photos: $photoCount · optional',
          key: const Key('photo-count'),
        ),
        const SizedBox(height: 8),
        OutlinedButton.icon(
          key: const Key('add-photo'),
          onPressed: onAddPhoto,
          icon: const Icon(Icons.photo_camera_outlined),
          label: const Text('Add photo'),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: summary,
          decoration: const InputDecoration(labelText: 'Work summary'),
          maxLines: 4,
        ),
      ],
    );
  }
}

class _SignOffStep extends StatelessWidget {
  const _SignOffStep({
    required this.required,
    required this.fallbackAllowed,
    required this.signature,
    required this.signerName,
    required this.fallbackReason,
    required this.serial,
    required this.onSigned,
    required this.onFallbackChanged,
    required this.onSignerChanged,
  });

  final bool required;
  final bool fallbackAllowed;
  final SignaturePadController signature;
  final TextEditingController signerName;
  final TextEditingController fallbackReason;
  final TextEditingController serial;
  final VoidCallback onSigned;
  final ValueChanged<String> onFallbackChanged;
  final ValueChanged<String> onSignerChanged;

  @override
  Widget build(BuildContext context) {
    return ListView(
      children: [
        Text(
          required ? 'Customer sign-off' : 'Customer sign-off (optional)',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        SignaturePad(controller: signature, onChanged: onSigned),
        const SizedBox(height: 8),
        TextField(
          controller: signerName,
          decoration: const InputDecoration(labelText: 'Signer name'),
          onChanged: onSignerChanged,
        ),
        if (fallbackAllowed) ...[
          const SizedBox(height: 16),
          TextField(
            key: const Key('fallback-reason'),
            controller: fallbackReason,
            decoration: const InputDecoration(
              labelText: 'Signature unavailable? Explain why',
              helperText: 'e.g. customer absent — photo of premises attached',
            ),
            onChanged: onFallbackChanged,
          ),
        ],
        const SizedBox(height: 16),
        TextField(
          key: const Key('equipment-serial'),
          controller: serial,
          decoration: const InputDecoration(
            labelText: 'Installed ONT serial (optional)',
          ),
        ),
      ],
    );
  }
}
