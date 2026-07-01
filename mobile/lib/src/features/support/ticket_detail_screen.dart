import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../config/env.dart';
import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../models/ticket.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/attachment_picker.dart';
import '../../widgets/status_chip.dart';

class TicketDetailScreen extends ConsumerStatefulWidget {
  const TicketDetailScreen({super.key, required this.ticketId});

  final String ticketId;

  @override
  ConsumerState<TicketDetailScreen> createState() => _TicketDetailScreenState();
}

class _TicketDetailScreenState extends ConsumerState<TicketDetailScreen> {
  final _reply = TextEditingController();
  bool _sending = false;
  List<PickedAttachment> _attachments = [];

  @override
  void dispose() {
    _reply.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final body = _reply.text.trim();
    // A reply needs either text or at least one attachment.
    if (body.isEmpty && _attachments.isEmpty) return;
    setState(() => _sending = true);
    try {
      await ref.read(supportRepositoryProvider).addComment(
            widget.ticketId,
            body,
            attachmentPaths: _attachments.isEmpty
                ? null
                : [for (final a in _attachments) a.path],
          );
      _reply.clear();
      setState(() => _attachments = []);
      ref.invalidate(ticketCommentsProvider(widget.ticketId));
      ref.invalidate(ticketProvider(widget.ticketId));
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  /// Compact "attach" entry for the composer (the inline strip only renders once
  /// something is picked). Delegates to [AttachmentPicker.pickInto] so the
  /// camera/gallery sheet and the ≤5 files / ≤5 MB validation live in one place.
  Future<void> _openAttachSheet() async {
    final next = await AttachmentPicker.pickInto(context, _attachments);
    if (next != null && mounted) setState(() => _attachments = next);
  }

  @override
  Widget build(BuildContext context) {
    final ticket = ref.watch(ticketProvider(widget.ticketId));
    final comments = ref.watch(ticketCommentsProvider(widget.ticketId));

    return Scaffold(
      appBar: AppBar(title: const Text('Ticket')),
      body: Column(
        children: [
          Expanded(
            child: AsyncValueView(
              value: ticket,
              onRetry: () => ref.invalidate(ticketProvider(widget.ticketId)),
              data: (t) => ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(t.title,
                            style: Theme.of(context).textTheme.titleLarge),
                      ),
                      StatusChip.forTicket(t.status),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '${t.number ?? t.id.substring(0, 8)} · ${t.priority} priority · ${Fmt.date(t.createdAt)}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 16),
                  if (t.description != null && t.description!.isNotEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(t.description!),
                            if (t.attachments.isNotEmpty) ...[
                              const SizedBox(height: 12),
                              _AttachmentStrip(attachments: t.attachments),
                            ],
                          ],
                        ),
                      ),
                    ),
                  const SizedBox(height: 16),
                  Text('Conversation',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  comments.when(
                    loading: () => const Padding(
                      padding: EdgeInsets.all(24),
                      child: Center(child: CircularProgressIndicator()),
                    ),
                    error: (e, _) => Text('Could not load replies: $e'),
                    data: (page) {
                      final visible =
                          page.items.where((c) => !c.isInternal).toList();
                      if (visible.isEmpty) {
                        return const Padding(
                          padding: EdgeInsets.symmetric(vertical: 16),
                          child: Text('No replies yet.'),
                        );
                      }
                      return Column(
                        children: [
                          for (final c in visible)
                            Card(
                              child: Padding(
                                padding:
                                    const EdgeInsets.fromLTRB(16, 12, 16, 12),
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    if (c.body.isNotEmpty) Text(c.body),
                                    if (c.attachments.isNotEmpty) ...[
                                      const SizedBox(height: 8),
                                      _AttachmentStrip(
                                          attachments: c.attachments),
                                    ],
                                    const SizedBox(height: 4),
                                    Text(
                                      Fmt.dateTime(c.createdAt),
                                      style:
                                          Theme.of(context).textTheme.bodySmall,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                        ],
                      );
                    },
                  ),
                ],
              ),
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (_attachments.isNotEmpty) ...[
                    AttachmentPicker(
                      attachments: _attachments,
                      enabled: !_sending,
                      onChanged: (a) => setState(() => _attachments = a),
                    ),
                    const SizedBox(height: 8),
                  ],
                  Row(
                    children: [
                      IconButton(
                        tooltip: 'Attach photo',
                        onPressed: _sending ? null : _openAttachSheet,
                        icon: const Icon(Icons.attach_file),
                      ),
                      Expanded(
                        child: TextField(
                          controller: _reply,
                          minLines: 1,
                          maxLines: 4,
                          decoration: const InputDecoration(
                            hintText: 'Write a reply…',
                            isDense: true,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton.filled(
                        tooltip: 'Send reply',
                        onPressed: _sending ? null : _send,
                        icon: _sending
                            ? const SizedBox(
                                height: 18,
                                width: 18,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2))
                            : const Icon(Icons.send),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Renders uploaded attachments as a horizontal row of thumbnails (images) /
/// file chips (PDFs/other). Tapping an image opens it full-screen; tapping a
/// file chip opens it in the device's default viewer/browser.
class _AttachmentStrip extends StatelessWidget {
  const _AttachmentStrip({required this.attachments});

  final List<TicketAttachment> attachments;

  Future<void> _openExternal(BuildContext context, TicketAttachment a) async {
    final raw = a.url;
    if (raw == null) return;
    final messenger = ScaffoldMessenger.of(context);
    final uri = Uri.parse(Env.resolveUrl(raw));
    final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!ok) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Could not open this attachment.')),
      );
    }
  }

  void _openImage(BuildContext context, TicketAttachment a) {
    final url = a.url;
    if (url == null) return;
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => Scaffold(
          backgroundColor: Colors.black,
          appBar: AppBar(
            backgroundColor: Colors.black,
            foregroundColor: Colors.white,
            title: Text(a.filename, overflow: TextOverflow.ellipsis),
          ),
          body: Center(
            child: InteractiveViewer(
              child: Image.network(
                Env.resolveUrl(url),
                fit: BoxFit.contain,
                errorBuilder: (_, __, ___) => const Icon(
                  Icons.broken_image_outlined,
                  color: Colors.white54,
                  size: 48,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final a in attachments)
          if (a.isImage && a.url != null)
            InkWell(
              onTap: () => _openImage(context, a),
              borderRadius: BorderRadius.circular(8),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.network(
                  Env.resolveUrl(a.url!),
                  width: 72,
                  height: 72,
                  fit: BoxFit.cover,
                  errorBuilder: (_, __, ___) => Container(
                    width: 72,
                    height: 72,
                    color: scheme.surfaceContainerHighest,
                    child: const Icon(Icons.broken_image_outlined),
                  ),
                ),
              ),
            )
          else
            ActionChip(
              avatar: Icon(
                a.isPdf
                    ? Icons.picture_as_pdf_outlined
                    : Icons.insert_drive_file_outlined,
                size: 18,
              ),
              label: Text(
                a.filename,
                overflow: TextOverflow.ellipsis,
              ),
              onPressed: a.url == null ? null : () => _openExternal(context, a),
            ),
      ],
    );
  }
}
