import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../../models/field_visit_chat.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

final fieldVisitChatProvider =
    FutureProvider.autoDispose.family<FieldVisitChat, String>(
  (ref, workOrderId) =>
      ref.watch(workOrderRepositoryProvider).chat(workOrderId),
);

/// The server owns availability and sending eligibility for this exact visit.
class FieldVisitChatScreen extends ConsumerStatefulWidget {
  const FieldVisitChatScreen({super.key, required this.workOrderId});

  final String workOrderId;

  @override
  ConsumerState<FieldVisitChatScreen> createState() =>
      _FieldVisitChatScreenState();
}

class _FieldVisitChatScreenState extends ConsumerState<FieldVisitChatScreen>
    with WidgetsBindingObserver {
  final _draft = TextEditingController();
  Timer? _poll;
  bool _sending = false;
  String? _pendingBody;
  String? _pendingRequestId;
  String? _sendError;
  bool _active = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _poll = Timer.periodic(const Duration(seconds: 20), (_) {
      if (mounted && _active && ModalRoute.of(context)?.isCurrent == true) {
        ref.invalidate(fieldVisitChatProvider(widget.workOrderId));
      }
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _active = state == AppLifecycleState.resumed;
    if (_active) ref.invalidate(fieldVisitChatProvider(widget.workOrderId));
  }

  @override
  void dispose() {
    _poll?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _draft.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final body = _draft.text.trim();
    final thread = ref.read(fieldVisitChatProvider(widget.workOrderId));
    if (_sending ||
        body.isEmpty ||
        thread.hasError ||
        thread.isLoading ||
        thread.valueOrNull?.canSend != true) {
      return;
    }
    if (_pendingBody != body) {
      _pendingBody = body;
      _pendingRequestId = const Uuid().v4();
    }
    setState(() {
      _sending = true;
      _sendError = null;
    });
    try {
      await ref.read(workOrderRepositoryProvider).sendChatMessage(
          widget.workOrderId,
          body: body,
          requestId: _pendingRequestId);
      if (!mounted) return;
      _draft.clear();
      _pendingBody = null;
      _pendingRequestId = null;
      ref.invalidate(fieldVisitChatProvider(widget.workOrderId));
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _sendError = 'Could not confirm your message. Refresh the conversation '
            'before sending again. Your draft is saved here.';
      });
      ref.invalidate(fieldVisitChatProvider(widget.workOrderId));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final thread = ref.watch(fieldVisitChatProvider(widget.workOrderId));
    final value = thread.valueOrNull;
    final canSend = !thread.hasError &&
        !thread.isLoading &&
        value?.available == true &&
        value?.canSend == true;
    return Scaffold(
      appBar: AppBar(
        title: Text(value?.canSend == false
            ? 'Visit conversation'
            : 'Message technician'),
        actions: [
          IconButton(
            tooltip: 'Refresh conversation',
            onPressed: () =>
                ref.invalidate(fieldVisitChatProvider(widget.workOrderId)),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: AsyncValueView<FieldVisitChat>(
              value: thread,
              onRetry: () =>
                  ref.invalidate(fieldVisitChatProvider(widget.workOrderId)),
              data: (chat) {
                if (!chat.available) {
                  return Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(
                        chat.reason == 'not_departed'
                            ? 'You can message your technician once they set off.'
                            : 'Messaging is unavailable for this visit.',
                      ),
                    ),
                  );
                }
                return ListView(
                  padding: const EdgeInsets.all(16),
                  children: [
                    if (!chat.canSend)
                      const Text(
                        'This visit’s chat is closed. '
                        'Your conversation remains available below.',
                      ),
                    if (chat.messages.isEmpty) const Text('No messages yet.'),
                    for (final message in chat.messages)
                      Align(
                        alignment: message.fromCustomer
                            ? Alignment.centerRight
                            : Alignment.centerLeft,
                        child: Card(
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  message.fromCustomer
                                      ? 'You'
                                      : message.authorName ??
                                          chat.technicianName ??
                                          'Technician',
                                  style: Theme.of(
                                    context,
                                  ).textTheme.labelMedium,
                                ),
                                Text(message.body),
                              ],
                            ),
                          ),
                        ),
                      ),
                  ],
                );
              },
            ),
          ),
          if (_sendError != null)
            Padding(
              padding: const EdgeInsets.all(12),
              child: Text(_sendError!),
            ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Expanded(
                    child: TextField(
                      controller: _draft,
                      enabled: !_sending,
                      minLines: 1,
                      maxLines: 4,
                      maxLength: 2000,
                      decoration: const InputDecoration(
                        labelText: 'Message',
                        hintText:
                            'Access instructions or a question about this visit',
                      ),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Send message',
                    onPressed: canSend && !_sending ? _send : null,
                    icon: _sending
                        ? const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.send),
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
