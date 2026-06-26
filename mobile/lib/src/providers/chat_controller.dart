import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/chat.dart';
import '../repositories/chat_repository.dart';
import 'data_providers.dart';

/// Immutable snapshot of one live-chat conversation.
class ChatState {
  const ChatState({
    this.session,
    this.messages = const [],
    this.loading = true,
    this.sending = false,
    this.error,
    this.agentTyping = false,
    this.agentReadAt,
  });

  final ChatSession? session;
  final List<ChatMessage> messages;
  final bool loading;
  final bool sending;
  final String? error;

  /// True while a support agent is composing (Layer B: pushed over the CRM
  /// widget channel). Stays false until that wiring is live.
  final bool agentTyping;

  /// Watermark of the latest of OUR messages an agent has read — drives the
  /// "Seen" receipt. Null until the agent reads (Layer B).
  final DateTime? agentReadAt;

  ChatState copyWith({
    ChatSession? session,
    List<ChatMessage>? messages,
    bool? loading,
    bool? sending,
    Object? error = _unset,
    bool? agentTyping,
    Object? agentReadAt = _unset,
  }) {
    return ChatState(
      session: session ?? this.session,
      messages: messages ?? this.messages,
      loading: loading ?? this.loading,
      sending: sending ?? this.sending,
      error: identical(error, _unset) ? this.error : error as String?,
      agentTyping: agentTyping ?? this.agentTyping,
      agentReadAt: identical(agentReadAt, _unset)
          ? this.agentReadAt
          : agentReadAt as DateTime?,
    );
  }

  static const _unset = Object();
}

/// Owns one chat conversation for the lifetime of the app session. Kept ALIVE
/// (a plain, non-autoDispose Notifier) so navigating away from the chat screen
/// — or switching bottom-nav tabs — does not tear the conversation down: the
/// session, history, and polling all survive. Polling runs while the app is
/// foregrounded and pauses in the background (where FCM push takes over).
///
/// Keyed by the broker endpoint so the customer (`/me/chat/session`) and
/// reseller (`/reseller/chat/session`) conversations are independent.
class ChatController extends FamilyNotifier<ChatState, String>
    with WidgetsBindingObserver {
  Timer? _poll;
  bool _starting = false;

  ChatRepository get _repo => ref.read(chatRepositoryProvider);

  @override
  ChatState build(String endpoint) {
    WidgetsBinding.instance.addObserver(this);
    ref.onDispose(() {
      _poll?.cancel();
      WidgetsBinding.instance.removeObserver(this);
    });
    // Broker + load history on first build; subsequent screen mounts reuse the
    // already-running conversation.
    scheduleMicrotask(_start);
    return const ChatState();
  }

  @override
  // Not named `state` to avoid shadowing the Notifier's `state` property.
  // ignore: avoid_renaming_method_parameters
  void didChangeAppLifecycleState(AppLifecycleState lifecycle) {
    if (lifecycle == AppLifecycleState.resumed) {
      _startPolling();
      unawaited(_refresh());
    } else if (lifecycle == AppLifecycleState.paused) {
      _poll?.cancel();
    }
  }

  Future<void> _start() async {
    if (_starting || state.session != null) return;
    _starting = true;
    try {
      final session = await _repo.openSession(endpoint: arg);
      final history = await _repo.history(session);
      state = state.copyWith(
        session: session,
        messages: history,
        loading: false,
        error: null,
        agentReadAt: _readWatermark(history),
      );
      unawaited(_repo.markRead(session));
      _startPolling();
    } catch (_) {
      state = state.copyWith(
        loading: false,
        error: 'Chat is unavailable right now. Please try again later.',
      );
    } finally {
      _starting = false;
    }
  }

  /// Re-broker after a failure (used by the screen's retry action).
  Future<void> retry() async {
    state = state.copyWith(loading: true, error: null);
    await _start();
  }

  void _startPolling() {
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refresh());
  }

  Future<void> _refresh() async {
    final session = state.session;
    if (session == null) return;
    try {
      final history = await _repo.history(session);
      final grew = history.length != state.messages.length;
      state = state.copyWith(
        messages: history,
        agentReadAt: _readWatermark(history) ?? state.agentReadAt,
      );
      if (grew) unawaited(_repo.markRead(session));
    } catch (_) {
      // Transient; the next tick retries.
    }
  }

  /// Latest time an agent read one of OUR messages — the "Seen" watermark.
  static DateTime? _readWatermark(List<ChatMessage> messages) {
    DateTime? latest;
    for (final m in messages) {
      if (m.fromAgent) continue;
      final r = m.readAt;
      if (r != null && (latest == null || r.isAfter(latest))) latest = r;
    }
    return latest;
  }

  Future<void> send(String text) async {
    final session = state.session;
    final body = text.trim();
    if (session == null || body.isEmpty || state.sending) return;
    state = state.copyWith(sending: true);
    try {
      final msg = await _repo.send(session, body);
      state = state.copyWith(
        messages: [...state.messages, msg],
        sending: false,
      );
    } catch (_) {
      state = state.copyWith(sending: false);
      rethrow; // the screen surfaces a snackbar and restores the draft
    }
  }
}

/// Persistent per-endpoint chat conversation. Not autoDispose: the conversation
/// outlives any single screen so leaving Support never ends the chat.
final chatControllerProvider =
    NotifierProvider.family<ChatController, ChatState, String>(
  ChatController.new,
);
