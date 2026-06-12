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
      appBar: AppBar(title: const Text('Support')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => context.go('/support/new'),
        icon: const Icon(Icons.add),
        label: const Text('New ticket'),
      ),
      body: RefreshIndicator(
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
                    title: Text(
                      t.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
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
    );
  }
}
