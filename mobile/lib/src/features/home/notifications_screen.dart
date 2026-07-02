import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/notification.dart';
import '../../providers/data_providers.dart';
import '../../providers/read_notifications.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';

/// Best-effort deep-link target for a notification, derived from its
/// event type / category / subject (the API carries no explicit resource link).
/// Returns null when there's nothing actionable to open.
String? notificationRoute(AppNotification n) {
  final hay = '${n.eventType ?? ''} ${n.category ?? ''} ${n.subject ?? ''}'
      .toLowerCase();
  bool has(List<String> words) => words.any(hay.contains);

  if (has([
    'message.outbound',
    'message_outbound',
    'message_new',
    'support message',
    'new message',
    'agent replied',
    'agent message',
    'live chat',
    'chat',
    'crm',
  ])) {
    return '/support/chat';
  }
  if (has(['invoice', 'payment', 'billing', 'suspend', 'overdue', 'charge'])) {
    return '/billing';
  }
  if (has(['ticket', 'support'])) return '/support';
  if (has(['usage', 'quota', 'data', 'cap'])) return '/usage';
  return null;
}

String _sectionLabel(String route) => switch (route) {
  '/support/chat' => 'chat',
  '/billing' => 'billing',
  '/support' => 'support',
  '/usage' => 'usage',
  _ => 'details',
};

class NotificationsScreen extends ConsumerWidget {
  const NotificationsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final notifications = ref.watch(notificationsProvider);
    final readIds = ref.watch(readNotificationsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Notifications'),
        actions: [
          if (notifications.asData != null &&
              notifications.asData!.value.items.any(
                (n) => !readIds.contains(n.id),
              ))
            TextButton(
              onPressed: () => ref
                  .read(readNotificationsProvider.notifier)
                  .markAllRead(
                    notifications.asData!.value.items.map((n) => n.id),
                  ),
              child: const Text('Mark all read'),
            ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(notificationsProvider);
          final page = await ref.read(notificationsProvider.future);
          // Keep the persisted read-set bounded to the current inbox.
          await ref
              .read(readNotificationsProvider.notifier)
              .prune(page.items.map((n) => n.id));
        },
        child: AsyncValueView(
          value: notifications,
          onRetry: () => ref.invalidate(notificationsProvider),
          skeleton: const ListSkeleton(hasLeading: true),
          data: (page) {
            if (page.items.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 120),
                  EmptyState(
                    icon: Icons.notifications_none_outlined,
                    message: 'No notifications yet.',
                  ),
                ],
              );
            }
            return ListView.separated(
              padding: const EdgeInsets.all(12),
              itemCount: page.items.length,
              separatorBuilder: (_, __) => const SizedBox(height: 8),
              itemBuilder: (_, i) {
                final n = page.items[i];
                final route = notificationRoute(n);
                return _NotificationCard(
                  n: n,
                  unread: !readIds.contains(n.id),
                  onMarkRead: () {
                    ref.read(readNotificationsProvider.notifier).markRead(n.id);
                  },
                  onOpen: route == null ? null : () => context.go(route),
                  openLabel: route == null ? null : _sectionLabel(route),
                );
              },
            );
          },
        ),
      ),
    );
  }
}

class _NotificationCard extends StatefulWidget {
  const _NotificationCard({
    required this.n,
    required this.unread,
    required this.onMarkRead,
    this.onOpen,
    this.openLabel,
  });
  final AppNotification n;
  final bool unread;
  final VoidCallback onMarkRead;
  final VoidCallback? onOpen;
  final String? openLabel;

  @override
  State<_NotificationCard> createState() => _NotificationCardState();
}

class _NotificationCardState extends State<_NotificationCard> {
  bool _expanded = false;

  IconData get _icon => switch (widget.n.channel) {
    'email' => Icons.mail_outline,
    'sms' => Icons.sms_outlined,
    'push' => Icons.notifications_active_outlined,
    'webhook' => Icons.webhook_outlined,
    _ => Icons.notifications_none_outlined,
  };

  void _toggle() {
    widget.onMarkRead();
    setState(() => _expanded = !_expanded);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final n = widget.n;
    final body = n.body?.trim();
    final hasBody = body != null && body.isNotEmpty;
    final hasAction = widget.onOpen != null;

    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: _toggle,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  CircleAvatar(
                    radius: 18,
                    backgroundColor: theme.colorScheme.secondaryContainer,
                    child: Icon(
                      _icon,
                      size: 18,
                      color: theme.colorScheme.onSecondaryContainer,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          n.title,
                          style: theme.textTheme.titleSmall?.copyWith(
                            fontWeight: widget.unread
                                ? FontWeight.w700
                                : FontWeight.w400,
                          ),
                          maxLines: _expanded ? null : 2,
                          overflow: _expanded
                              ? TextOverflow.visible
                              : TextOverflow.ellipsis,
                        ),
                        if (hasBody && !_expanded) ...[
                          const SizedBox(height: 4),
                          Text(
                            body,
                            style: theme.textTheme.bodySmall,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ],
                        const SizedBox(height: 6),
                        Text(
                          '${n.channel} · ${Fmt.dateTime(n.createdAt)}',
                          style: theme.textTheme.labelSmall?.copyWith(
                            color: theme.colorScheme.outline,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (widget.unread)
                    Container(
                      margin: const EdgeInsets.only(left: 8, top: 4),
                      width: 9,
                      height: 9,
                      decoration: BoxDecoration(
                        color: theme.colorScheme.primary,
                        shape: BoxShape.circle,
                      ),
                    ),
                  Padding(
                    padding: const EdgeInsets.only(left: 4),
                    child: Icon(
                      _expanded ? Icons.expand_less : Icons.expand_more,
                      size: 20,
                      color: theme.colorScheme.outline,
                    ),
                  ),
                ],
              ),
              AnimatedCrossFade(
                firstChild: const SizedBox.shrink(),
                secondChild: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const Divider(height: 24),
                    Text(
                      hasBody ? body : n.title,
                      style: theme.textTheme.bodyMedium,
                    ),
                    if (hasAction) ...[
                      const SizedBox(height: 8),
                      Align(
                        alignment: Alignment.centerRight,
                        child: TextButton.icon(
                          onPressed: widget.onOpen,
                          icon: const Icon(Icons.open_in_new),
                          label: Text('Open ${widget.openLabel ?? 'details'}'),
                        ),
                      ),
                    ],
                  ],
                ),
                crossFadeState: _expanded
                    ? CrossFadeState.showSecond
                    : CrossFadeState.showFirst,
                duration: const Duration(milliseconds: 180),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
