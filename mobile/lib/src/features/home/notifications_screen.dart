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

  if (has(['invoice', 'payment', 'billing', 'suspend', 'overdue', 'charge'])) {
    return '/billing';
  }
  if (has(['ticket', 'support'])) return '/support';
  if (has(['usage', 'quota', 'data', 'cap'])) return '/usage';
  return null;
}

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
                  hasAction: route != null,
                  onTap: () {
                    ref.read(readNotificationsProvider.notifier).markRead(n.id);
                    if (route != null) context.go(route);
                  },
                );
              },
            );
          },
        ),
      ),
    );
  }
}

class _NotificationCard extends StatelessWidget {
  const _NotificationCard({
    required this.n,
    required this.unread,
    required this.hasAction,
    required this.onTap,
  });
  final AppNotification n;
  final bool unread;

  /// Whether tapping opens a related screen (vs. just marking read).
  final bool hasAction;
  final VoidCallback onTap;

  IconData get _icon => switch (n.channel) {
    'email' => Icons.mail_outline,
    'sms' => Icons.sms_outlined,
    'push' => Icons.notifications_active_outlined,
    'webhook' => Icons.webhook_outlined,
    _ => Icons.notifications_none_outlined,
  };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: (unread || hasAction) ? onTap : null,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
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
                        fontWeight: unread ? FontWeight.w700 : FontWeight.w400,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                    if (n.body != null && n.body!.trim().isNotEmpty) ...[
                      const SizedBox(height: 4),
                      Text(
                        n.body!.trim(),
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
              if (unread)
                Container(
                  margin: const EdgeInsets.only(left: 8, top: 4),
                  width: 9,
                  height: 9,
                  decoration: BoxDecoration(
                    color: theme.colorScheme.primary,
                    shape: BoxShape.circle,
                  ),
                ),
              if (hasAction)
                Padding(
                  padding: const EdgeInsets.only(left: 4),
                  child: Icon(
                    Icons.chevron_right,
                    size: 18,
                    color: theme.colorScheme.outline,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
