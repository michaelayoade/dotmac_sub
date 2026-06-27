import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../models/chat.dart';
import '../../providers/chat_controller.dart';

/// Standalone live-chat screen (its own Scaffold + back) — used for deep links
/// / push-notification taps (`/chat`, `/reseller/chat`). In-app, the Support
/// tab embeds [ChatView] directly so chat stays in the Support window.
class ChatScreen extends ConsumerWidget {
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
  Widget build(BuildContext context, WidgetRef ref) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Support chat'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () =>
              context.canPop() ? context.pop() : context.go(fallbackRoute),
        ),
      ),
      body: ChatView(sessionEndpoint: sessionEndpoint),
    );
  }
}

/// The chat UI body (message log + composer) without any Scaffold/AppBar, so it
/// can be embedded inside another screen (the Support tab) or wrapped by
/// [ChatScreen] for standalone use. Owns its own scroll/input; the conversation
/// itself lives in the kept-alive [ChatController].
class ChatView extends ConsumerStatefulWidget {
  const ChatView({super.key, this.sessionEndpoint = '/me/chat/session'});

  final String sessionEndpoint;

  @override
  ConsumerState<ChatView> createState() => _ChatViewState();
}

class _ChatViewState extends ConsumerState<ChatView> {
  final _input = TextEditingController();
  final _scroll = ScrollController();

  String get _endpoint => widget.sessionEndpoint;

  @override
  void initState() {
    super.initState();
    // Viewing the chat clears the unread badge + marks read.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        ref.read(chatControllerProvider(_endpoint).notifier).markViewed();
      }
    });
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.jumpTo(_scroll.position.maxScrollExtent);
      }
    });
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    _input.clear();
    try {
      await ref.read(chatControllerProvider(_endpoint).notifier).send(text);
      _scrollToBottom();
    } catch (_) {
      // The message stays in the log as a tappable "failed" bubble, so the
      // draft isn't restored (that would duplicate it). A brief snackbar nudges.
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content: Text('Message failed to send — tap it to retry.')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(chatControllerProvider(_endpoint));

    // Auto-scroll when the conversation grows or the agent starts typing.
    ref.listen<ChatState>(chatControllerProvider(_endpoint), (prev, next) {
      final grew = (prev?.messages.length ?? 0) != next.messages.length;
      final startedTyping = (prev?.agentTyping ?? false) != next.agentTyping;
      if (grew || startedTyping) _scrollToBottom();
      // While the chat is on screen, keep it marked read (no badge buildup).
      if (grew && next.unread > 0) {
        ref.read(chatControllerProvider(_endpoint).notifier).markViewed();
      }
    });

    return state.loading
        ? const Center(child: CircularProgressIndicator())
        : state.error != null
            ? _ErrorView(
                message: state.error!,
                onRetry: () => ref
                    .read(chatControllerProvider(_endpoint).notifier)
                    .retry(),
              )
            : Column(
                children: [
                  if (!state.connected) _ReconnectingBar(),
                  Expanded(child: _buildLog(state)),
                  const Divider(height: 1),
                  _buildComposer(state),
                ],
              );
  }

  Widget _buildLog(ChatState state) {
    if (state.messages.isEmpty && !state.agentTyping) {
      return const Center(child: Text('Say hello — how can we help?'));
    }
    // The typing indicator rides as one extra trailing row when an agent is
    // composing.
    final count = state.messages.length + (state.agentTyping ? 1 : 0);
    final lastMineIdx = _lastMineIndex(state.messages);
    return ListView.builder(
      controller: _scroll,
      padding: const EdgeInsets.all(12),
      itemCount: count,
      itemBuilder: (context, i) {
        if (i >= state.messages.length) return const _TypingBubble();
        final showSeen = i == lastMineIdx &&
            state.agentReadAt != null &&
            _isSeen(state.messages[i], state.agentReadAt!);
        return _bubble(state.messages[i], showSeen: showSeen);
      },
    );
  }

  static int _lastMineIndex(List<ChatMessage> messages) {
    for (var i = messages.length - 1; i >= 0; i--) {
      if (!messages[i].fromAgent) return i;
    }
    return -1;
  }

  static bool _isSeen(ChatMessage m, DateTime readAt) {
    final created = m.createdAt;
    return created == null || !readAt.isBefore(created);
  }

  Widget _bubble(ChatMessage m, {bool showSeen = false}) {
    final theme = Theme.of(context);
    final mine = !m.fromAgent;
    final bubble = Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      constraints:
          BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.70),
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
    );

    final meta = <Widget>[
      if (m.createdAt != null)
        Text(
          TimeOfDay.fromDateTime(m.createdAt!.toLocal()).format(context),
          style: theme.textTheme.labelSmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
      if (showSeen) ...[
        const SizedBox(width: 6),
        Icon(Icons.done_all, size: 13, color: theme.colorScheme.primary),
        const SizedBox(width: 2),
        Text('Seen',
            style: theme.textTheme.labelSmall
                ?.copyWith(color: theme.colorScheme.primary)),
      ],
      if (mine && m.status == MessageStatus.sending) ...[
        const SizedBox(width: 6),
        const SizedBox(
            width: 9,
            height: 9,
            child: CircularProgressIndicator(strokeWidth: 1.4)),
        const SizedBox(width: 3),
        Text('Sending…',
            style: theme.textTheme.labelSmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
      if (mine && m.status == MessageStatus.failed) ...[
        const SizedBox(width: 6),
        Icon(Icons.error_outline, size: 13, color: theme.colorScheme.error),
        const SizedBox(width: 2),
        Text('Failed — tap to retry',
            style: theme.textTheme.labelSmall
                ?.copyWith(color: theme.colorScheme.error)),
      ],
    ];

    final column = Column(
      crossAxisAlignment:
          mine ? CrossAxisAlignment.end : CrossAxisAlignment.start,
      children: [
        bubble,
        if (meta.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 1, left: 4, right: 4),
            child: Row(mainAxisSize: MainAxisSize.min, children: meta),
          ),
      ],
    );

    if (mine) {
      // A failed message is tappable to retry.
      final aligned = Align(alignment: Alignment.centerRight, child: column);
      if (m.status == MessageStatus.failed) {
        return GestureDetector(
          onTap: () => ref
              .read(chatControllerProvider(_endpoint).notifier)
              .retryFailed(m),
          child: aligned,
        );
      }
      return aligned;
    }
    // Agent rows lead with the agent's avatar.
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _AgentAvatar(name: m.authorName, url: m.authorAvatar),
        const SizedBox(width: 8),
        Flexible(child: column),
      ],
    );
  }

  Widget _buildComposer(ChatState state) {
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
                onChanged: (v) {
                  if (v.trim().isNotEmpty) {
                    ref
                        .read(chatControllerProvider(_endpoint).notifier)
                        .notifyTyping();
                  }
                },
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
              onPressed: state.sending ? null : _send,
              icon: const Icon(Icons.send),
            ),
          ],
        ),
      ),
    );
  }
}

class _AgentAvatar extends StatelessWidget {
  const _AgentAvatar({this.name, this.url});

  final String? name;
  final String? url;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final initial = (name != null && name!.trim().isNotEmpty)
        ? name!.trim()[0].toUpperCase()
        : null;
    return CircleAvatar(
      radius: 14,
      backgroundColor: theme.colorScheme.secondaryContainer,
      foregroundImage: (url != null) ? NetworkImage(url!) : null,
      child: (url == null)
          ? (initial != null
              ? Text(initial,
                  style: theme.textTheme.labelSmall
                      ?.copyWith(color: theme.colorScheme.onSecondaryContainer))
              : Icon(Icons.support_agent,
                  size: 16, color: theme.colorScheme.onSecondaryContainer))
          : null,
    );
  }
}

/// Animated "agent is typing" indicator shown as an inbound bubble.
class _TypingBubble extends StatefulWidget {
  const _TypingBubble();

  @override
  State<_TypingBubble> createState() => _TypingBubbleState();
}

class _TypingBubbleState extends State<_TypingBubble>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  )..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _AgentAvatar(),
        const SizedBox(width: 8),
        Container(
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(12),
          ),
          child: AnimatedBuilder(
            animation: _c,
            builder: (context, _) {
              return Row(
                mainAxisSize: MainAxisSize.min,
                children: List.generate(3, (i) {
                  final t = (_c.value + i * 0.2) % 1.0;
                  final opacity =
                      0.3 + 0.7 * (1 - (t - 0.5).abs() * 2).clamp(0, 1);
                  return Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 2),
                    child: Opacity(
                      opacity: opacity.toDouble(),
                      child: CircleAvatar(
                        radius: 3,
                        backgroundColor: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  );
                }),
              );
            },
          ),
        ),
      ],
    );
  }
}

/// Thin banner shown while the realtime socket is down (reconnecting). Messages
/// still send over REST; this just signals delivery may be briefly delayed.
class _ReconnectingBar extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      color: theme.colorScheme.surfaceContainerHighest,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          SizedBox(
            width: 11,
            height: 11,
            child: CircularProgressIndicator(
                strokeWidth: 1.6, color: theme.colorScheme.onSurfaceVariant),
          ),
          const SizedBox(width: 8),
          Text('Reconnecting…',
              style: theme.textTheme.labelMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 12),
            FilledButton.tonal(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}
