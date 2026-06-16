import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

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
  List<PickedAttachment> _attachments = [];

  static const _priorities = ['low', 'normal', 'high', 'urgent'];

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
          );
      ref.invalidate(ticketsProvider);
      if (!mounted) return;
      context.go('/support/${ticket.id}');
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
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
              onChanged: (v) => setState(() => _priority = v ?? 'normal'),
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _description,
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
              onChanged: (a) => setState(() => _attachments = a),
            ),
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _submitting ? null : _submit,
              child: _submitting
                  ? const SizedBox(
                      height: 20,
                      width: 20,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Submit ticket'),
            ),
          ],
        ),
      ),
    );
  }
}
