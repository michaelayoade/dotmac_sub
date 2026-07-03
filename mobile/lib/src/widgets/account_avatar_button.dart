import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../config/env.dart';
import '../providers/auth_controller.dart';

/// Circular account button for app-bar `actions`. Shows the customer's avatar
/// (or initials) and opens the Account screen (`/profile`).
///
/// Account lives in the header — beside the notification bell — rather than a
/// bottom-nav tab: it's low-frequency identity/settings, so it shouldn't
/// compete with the recurring-job tabs (Home · Service · Billing · Help).
class AccountAvatarButton extends ConsumerWidget {
  const AccountAvatarButton({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(currentUserProvider);
    final scheme = Theme.of(context).colorScheme;
    final avatarUrl = me?.avatarUrl;
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: IconButton(
        tooltip: 'Account',
        padding: EdgeInsets.zero,
        constraints: const BoxConstraints(minWidth: 44, minHeight: 44),
        onPressed: () => context.push('/profile'),
        icon: CircleAvatar(
          radius: 16,
          backgroundColor: scheme.primaryContainer,
          foregroundColor: scheme.onPrimaryContainer,
          backgroundImage: avatarUrl != null
              ? NetworkImage(Env.resolveUrl(avatarUrl))
              : null,
          child: avatarUrl == null
              ? Text(
                  me?.initials ?? '',
                  style: const TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                  ),
                )
              : null,
        ),
      ),
    );
  }
}
