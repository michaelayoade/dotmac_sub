import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../models/chat.dart';
import '../../providers/data_providers.dart';
import '../../repositories/chat_repository.dart';

/// Live chat with support. Brokers a session, loads history, and polls for new
/// messages while foregrounded; background delivery arrives via FCM push.
class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({
    super.key,
    this.sessionEndpoint = '/me/chat/session',
    this.fallbackRoute = '/support',
  });

  /// Broker path — `/me/chat/session` (customer) or `/reseller/chat/session`.
  final String sessionEndpoint;

  /// Route to use when the screen was reached without a navigation stack.
  final String fallbackRoute;

  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen>
    with WidgetsBindingObserver {
  ChatSession? _session;
  List<ChatMessage> _messages = const [];
  final _input = TextEditingController();
  final _scroll = ScrollController();
  Timer? _poll;
  bool _loading = true;
  bool _sending = false;
  String? _error;

  ChatRepository get _repo => ref.read(chatRepositoryProvider);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _start();
  }

  @override
  void dispose() {
    _poll?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Pause polling in the background; resume (and refresh once) on return.
    if (state == AppLifecycleState.resumed) {
      _startPolling();
      _refresh();
    } else if (state == AppLifecycleState.paused) {
      _poll?.cancel();
    }
  }

  Future<void> _start() async {
    try {
      final session = await _repo.openSession(endpoint: widget.sessionEndpoint);
      final history = await _repo.history(session);
      if (!mounted) return;
      setState(() {
        _session = session;
        _messages = history;
        _loading = false;
      });
      _scrollToBottom();
      unawaited(_repo.markRead(session));
      _startPolling();
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Chat is unavailable right now. Please try again later.';
      });
    }
  }

  void _startPolling() {
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refresh());
  }

  Future<void> _refresh() async {
    final session = _session;
    if (session == null) return;
    try {
      final history = await _repo.history(session);
      if (!mounted) return;
      final grew = history.length != _messages.length;
      setState(() => _messages = history);
      if (grew) {
        _scrollToBottom();
        unawaited(_repo.markRead(session));
      }
    } catch (_) {
      // Transient; next tick retries.
    }
  }

  Future<void> _send() async {
    final session = _session;
    final text = _input.text.trim();
    if (session == null || text.isEmpty || _sending) return;
    setState(() => _sending = true);
    _input.clear();
    try {
      final msg = await _repo.send(session, text);
      if (!mounted) return;
      setState(() => _messages = [..._messages, msg]);
      _scrollToBottom();
    } catch (e) {
      if (!mounted) return;
      _input.text = text;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Message failed to send.')),
      );
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.jumpTo(_scroll.position.maxScrollExtent);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Support chat'),
        // Explicit back: works whether the screen was pushed (pop) or reached
        // via go() with no stack (fall back to the Support tab).
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.canPop()
              ? context.pop()
              : context.go(widget.fallbackRoute),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(
                  child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(_error!, textAlign: TextAlign.center)))
              : Column(
                  children: [
                    Expanded(child: _buildLog()),
                    const Divider(height: 1),
                    _buildComposer(),
                  ],
                ),
    );
  }

  Widget _buildLog() {
    if (_messages.isEmpty) {
      return const Center(child: Text('Say hello — how can we help?'));
    }
    return ListView.builder(
      controller: _scroll,
      padding: const EdgeInsets.all(12),
      itemCount: _messages.length,
      itemBuilder: (context, i) => _bubble(_messages[i]),
    );
  }

  Widget _bubble(ChatMessage m) {
    final theme = Theme.of(context);
    final mine = !m.fromAgent;
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        constraints:
            BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.78),
        decoration: BoxDecoration(
          color: mine
              ? theme.colorScheme.primary
              : theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          crossAxisAlignment:
              mine ? CrossAxisAlignment.end : CrossAxisAlignment.start,
          children: [
            if (!mine && m.authorName != null)
              Text(m.authorName!,
                  style: theme.textTheme.labelSmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            Text(
              m.body,
              style: TextStyle(
                color: mine
                    ? theme.colorScheme.onPrimary
                    : theme.colorScheme.onSurface,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildComposer() {
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: Row(
          children: [
            Expanded(
              child: TextField(
                controller: _input,
                minLines: 1,
                maxLines: 4,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _send(),
                decoration: const InputDecoration(
                  hintText: 'Type a message…',
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
            const SizedBox(width: 8),
            IconButton.filled(
              onPressed: _sending ? null : _send,
              icon: const Icon(Icons.send),
            ),
          ],
        ),
      ),
    );
  }
}
