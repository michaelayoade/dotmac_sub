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

/// Open the full notification in a bottom sheet. Tapping a card here, not
/// jumping straight to another screen, so the whole message is always
/// readable — long bodies scroll, and a related screen (if any) is an
/// explicit button rather than the side effect of a tap.
void _showNotificationDetail(
    BuildContext context, AppNotification n, String? route) {
  showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (sheetContext) {
      final theme = Theme.of(sheetContext);
      final body = n.body?.trim() ?? '';
      return SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(n.title, style: theme.textTheme.titleMedium),
              const SizedBox(height: 4),
              Text(
                '${n.channel} · ${Fmt.dateTime(n.createdAt)}',
                style: theme.textTheme.labelSmall
                    ?.copyWith(color: theme.colorScheme.outline),
              ),
              if (body.isNotEmpty) ...[
                const SizedBox(height: 16),
                Flexible(
                  child: SingleChildScrollView(
                    child: Text(body, style: theme.textTheme.bodyMedium),
                  ),
                ),
              ],
              if (route != null) ...[
                const SizedBox(height: 20),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: () {
                      Navigator.of(sheetContext).pop();
                      context.go(route);
                    },
                    icon: const Icon(Icons.open_in_new, size: 18),
                    label: Text('Open ${_sectionLabel(route)}'),
                  ),
                ),
              ],
            ],
          ),
        ),
      );
    },
  );
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
              notifications.asData!.value.items
                  .any((n) => !readIds.contains(n.id)))
            TextButton(
              onPressed: () =>
                  ref.read(readNotificationsProvider.notifier).markAllRead(
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
                  // Tap opens the full message in a sheet (marking it read);
                  // any related screen is an explicit button inside it.
                  onTap: () {
                    ref.read(readNotificationsProvider.notifier).markRead(n.id);
                    _showNotificationDetail(context, n, route);
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
    required this.onTap,
  });
  final AppNotification n;
  final bool unread;
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
    final body = n.body?.trim() ?? '';
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              CircleAvatar(
                radius: 18,
                backgroundColor: theme.colorScheme.secondaryContainer,
                child: Icon(_icon,
                    size: 18, color: theme.colorScheme.onSecondaryContainer),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(n.title,
                        style: theme.textTheme.titleSmall?.copyWith(
                          fontWeight:
                              unread ? FontWeight.w700 : FontWeight.w400,
                        ),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis),
                    if (body.isNotEmpty) ...[
                      const SizedBox(height: 4),
                      // Preview only — a tap opens the full text in a sheet.
                      Text(body,
                          style: theme.textTheme.bodySmall,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis),
                    ],
                    const SizedBox(height: 6),
                    Text(
                      '${n.channel} · ${Fmt.dateTime(n.createdAt)}',
                      style: theme.textTheme.labelSmall
                          ?.copyWith(color: theme.colorScheme.outline),
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
              Padding(
                padding: const EdgeInsets.only(left: 4),
                child: Icon(Icons.chevron_right,
                    size: 18, color: theme.colorScheme.outline),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
