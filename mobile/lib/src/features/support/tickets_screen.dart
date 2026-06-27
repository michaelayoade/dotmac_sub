import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import 'chat_screen.dart';

/// Support tab: a Tickets | Live chat segment that switches in-place. Selecting
/// Live chat embeds [ChatView] right here — the chat stays in the Support
/// window (no separate screen / back button).
class TicketsScreen extends ConsumerStatefulWidget {
  const TicketsScreen({super.key});

  @override
  ConsumerState<TicketsScreen> createState() => _TicketsScreenState();
}

class _TicketsScreenState extends ConsumerState<TicketsScreen> {
  bool _chat = false;

  @override
  Widget build(BuildContext context) {
    final tickets = ref.watch(ticketsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Support')),
      floatingActionButton: _chat
          ? null
          : FloatingActionButton.extended(
              onPressed: () => context.go('/support/new'),
              icon: const Icon(Icons.add),
              label: const Text('New ticket'),
            ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 8),
            child: SegmentedButton<bool>(
              segments: const [
                ButtonSegment(
                  value: false,
                  icon: Icon(Icons.confirmation_number_outlined),
                  label: Text('Tickets'),
                ),
                ButtonSegment(
                  value: true,
                  icon: Icon(Icons.forum_outlined),
                  label: Text('Live chat'),
                ),
              ],
              selected: {_chat},
              onSelectionChanged: (s) => setState(() => _chat = s.first),
            ),
          ),
          Expanded(
            child: _chat ? const ChatView() : _ticketList(tickets),
          ),
        ],
      ),
    );
  }

  Widget _ticketList(AsyncValue<dynamic> tickets) {
    return RefreshIndicator(
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
    );
  }
}
