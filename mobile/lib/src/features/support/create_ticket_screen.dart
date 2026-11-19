import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../core/api_exception.dart';
import '../../providers/data_providers.dart';
import '../../widgets/attachment_picker.dart';

class CreateTicketScreen extends ConsumerStatefulWidget {
  const CreateTicketScreen({super.key});

  @override
  ConsumerState<CreateTicketScreen> createState() => _CreateTicketScreenState();
}

class _CreateTicketScreenState extends ConsumerState<CreateTicketScreen> {
  final _formKey = GlobalKey<FormState>();
  final _title = TextEditingController();
  final _description = TextEditingController();
  String _priority = 'normal';
  bool _submitting = false;
  String _submissionId = const Uuid().v4();
  bool _duplicateOverride = false;
  List<PickedAttachment> _attachments = [];

  static const _priorities = ['low', 'normal', 'high', 'urgent'];

  void _draftChanged() {
    _submissionId = const Uuid().v4();
    _duplicateOverride = false;
  }

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _submitting = true);
    try {
      final ticket = await ref.read(supportRepositoryProvider).createTicket(
            title: _title.text.trim(),
            description: _description.text.trim().isEmpty
                ? null
                : _description.text.trim(),
            priority: _priority,
            attachmentPaths: _attachments.isEmpty
                ? null
                : [for (final a in _attachments) a.path],
            submissionId: _submissionId,
            duplicateOverride: _duplicateOverride,
          );
      ref.invalidate(ticketsProvider);
      if (!mounted) return;
      context.go('/support/${ticket.id}');
    } on ApiException catch (e) {
      if (mounted) {
        if (e.code == 'ticket_duplicate_warning' && e.statusCode == 409) {
          setState(() => _submitting = false);
          final matches = e.details?['matches'];
          final confirmed = await showDialog<bool>(
            context: context,
            builder: (dialogContext) => AlertDialog(
              title: const Text('Similar open tickets'),
              content: SizedBox(
                width: double.maxFinite,
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    const Text(
                      'Review an existing ticket or create a separate one.',
                    ),
                    if (matches is List)
                      for (final item in matches)
                        if (item is Map)
                          ListTile(
                            title: Text((item['title'] ?? 'Ticket').toString()),
                            subtitle: Text(
                              (item['reference'] ?? '').toString(),
                            ),
                            onTap: () {
                              Navigator.pop(dialogContext, false);
                              if (mounted) {
                                context.go('/support/${item['ticket_id']}');
                              }
                            },
                          ),
                  ],
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.pop(dialogContext, false),
                  child: const Text('Cancel'),
                ),
                FilledButton(
                  onPressed: () => Navigator.pop(dialogContext, true),
                  child: const Text('Create anyway'),
                ),
              ],
            ),
          );
          if (confirmed == true && mounted) {
            _duplicateOverride = true;
            await _submit();
          }
          return;
        }
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(e.message)));
        setState(() => _submitting = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('New ticket')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            TextFormField(
              controller: _title,
              onChanged: (_) => _draftChanged(),
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                labelText: 'Subject',
                hintText: 'Brief summary of the issue',
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 16),
            DropdownButtonFormField<String>(
              initialValue: _priority,
              decoration: const InputDecoration(labelText: 'Priority'),
              items: [
                for (final p in _priorities)
                  DropdownMenuItem(value: p, child: Text(p)),
              ],
              onChanged: (v) => setState(() {
                _priority = v ?? 'normal';
                _draftChanged();
              }),
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _description,
              onChanged: (_) => _draftChanged(),
              minLines: 4,
              maxLines: 10,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                labelText: 'Description',
                alignLabelWithHint: true,
              ),
            ),
            const SizedBox(height: 16),
            AttachmentPicker(
              attachments: _attachments,
              enabled: !_submitting,
              onChanged: (a) => setState(() {
                _attachments = a;
                _draftChanged();
              }),
            ),
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _submitting ? null : _submit,
              child: _submitting
                  ? const SizedBox(
                      height: 20,
                      width: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('Submit ticket'),
            ),
          ],
        ),
      ),
    );
  }
}
