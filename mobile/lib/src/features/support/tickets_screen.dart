import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';

class TicketsScreen extends ConsumerWidget {
  const TicketsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final tickets = ref.watch(ticketsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Support'),
        actions: [
          IconButton(
            tooltip: 'Live chat',
            icon: const Icon(Icons.chat_bubble_outline),
            onPressed: () => context.push('/support/chat'),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => context.go('/support/new'),
        icon: const Icon(Icons.add),
        label: const Text('New ticket'),
      ),
      body: Column(
        children: [
          // Prominent live-chat entry (the AppBar icon alone was too easy to
          // miss). Opens the CRM-backed live chat.
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
            child: Card(
              margin: EdgeInsets.zero,
              color: Theme.of(context).colorScheme.primaryContainer,
              child: ListTile(
                leading: Icon(
                  Icons.forum_outlined,
                  color: Theme.of(context).colorScheme.onPrimaryContainer,
                ),
                title: Text(
                  'Live chat',
                  style: TextStyle(
                    fontWeight: FontWeight.w700,
                    color: Theme.of(context).colorScheme.onPrimaryContainer,
                  ),
                ),
                subtitle: Text(
                  'Chat with our support team now',
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.onPrimaryContainer,
                  ),
                ),
                trailing: Icon(
                  Icons.chevron_right,
                  color: Theme.of(context).colorScheme.onPrimaryContainer,
                ),
                onTap: () => context.push('/support/chat'),
              ),
            ),
          ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: () async {
                ref.invalidate(ticketsProvider);
                await ref.read(ticketsProvider.future);
              },
              child: AsyncValueView(
                value: tickets,
                onRetry: () => ref.invalidate(ticketsProvider),
                skeleton: const ListSkeleton(),
                data: (page) {
                  if (page.items.isEmpty) {
                    return ListView(
                      children: const [
                        SizedBox(height: 120),
                        EmptyState(
                          icon: Icons.support_agent_outlined,
                          message: 'No support tickets yet.',
                        ),
                      ],
                    );
                  }
                  return ListView.separated(
                    padding: const EdgeInsets.all(12),
                    itemCount: page.items.length,
                    separatorBuilder: (_, __) => const SizedBox(height: 8),
                    itemBuilder: (_, i) {
                      final t = page.items[i];
                      return Card(
                        margin: EdgeInsets.zero,
                        child: ListTile(
                          title: Text(t.title,
                              maxLines: 1, overflow: TextOverflow.ellipsis),
                          subtitle: Text(
                            '${t.number ?? t.id.substring(0, 8)} · ${Fmt.date(t.createdAt)}',
                          ),
                          trailing: StatusChip.forTicket(t.status),
                          onTap: () => context.go('/support/${t.id}'),
                        ),
                      );
                    },
                  );
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}
