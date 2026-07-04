import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';
import '../../widgets/account_avatar_button.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import '../../providers/chat_controller.dart';
import '../profile/work_orders_screen.dart';
import 'chat_screen.dart';

/// Support tab: a Tickets | Live chat segment that switches in-place. Selecting
/// Live chat embeds [ChatView] right here — the chat stays in the Support
/// window (no separate screen / back button).
class TicketsScreen extends ConsumerStatefulWidget {
  const TicketsScreen({super.key});

  @override
  ConsumerState<TicketsScreen> createState() => _TicketsScreenState();
}

/// Which sub-view the Help tab is showing.
enum _HelpView { tickets, chat, visits }

class _TicketsScreenState extends ConsumerState<TicketsScreen> {
  _HelpView _view = _HelpView.tickets;

  @override
  Widget build(BuildContext context) {
    final tickets = ref.watch(ticketsProvider);
    // Unread agent replies (badge on the Live chat segment). Watching this opens
    // the kept-alive chat session while on Support, so replies are tracked even
    // before the user taps into the chat.
    final unread = ref.watch(
      chatControllerProvider('/me/chat/session').select((s) => s.unread),
    );

    return Scaffold(
      appBar: AppBar(
        title: const Text('Help'),
        actions: const [AccountAvatarButton()],
      ),
      floatingActionButton: _view != _HelpView.tickets
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
            child: SegmentedButton<_HelpView>(
              showSelectedIcon: false,
              segments: [
                const ButtonSegment(
                  value: _HelpView.tickets,
                  icon: Icon(Icons.confirmation_number_outlined),
                  label: Text('Tickets'),
                ),
                ButtonSegment(
                  value: _HelpView.chat,
                  icon: const Icon(Icons.forum_outlined),
                  label: unread > 0
                      ? Badge(
                          label: Text('$unread'),
                          child: const Text('Chat'),
                        )
                      : const Text('Chat'),
                ),
                const ButtonSegment(
                  value: _HelpView.visits,
                  icon: Icon(Icons.engineering_outlined),
                  label: Text('Visits'),
                ),
              ],
              selected: {_view},
              onSelectionChanged: (s) => setState(() => _view = s.first),
            ),
          ),
          Expanded(
            child: switch (_view) {
              _HelpView.tickets => _ticketList(tickets),
              _HelpView.chat => const ChatView(),
              _HelpView.visits => const WorkOrdersView(),
            },
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
