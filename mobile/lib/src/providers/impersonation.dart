import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../config/env.dart';
import '../core/data_scope.dart';
import '../core/messenger.dart';
import '../router/app_router.dart';
import 'auth_controller.dart';
import 'data_providers.dart';

/// Active reseller "view as customer" session, if any.
class ImpersonationState {
  ImpersonationState({
    required this.customerName,
    required this.accountId,
    required this.expiresAt,
  });

  final String customerName;
  final String accountId;
  final DateTime? expiresAt;
}

/// Drives the customer-mode override: while active, every API call carries
/// the short-lived read-only customer token (see ApiClient.impersonationToken)
/// and the shell shows a persistent banner with an explicit exit.
class ImpersonationController extends Notifier<ImpersonationState?> {
  @override
  ImpersonationState? build() => null;

  /// The reseller's own cache partition, parked while a "view as" session is
  /// running so [stop] can put it back without an async storage read.
  MobileDataScope? _ownScope;

  Future<ImpersonationState> start(String accountId) async {
    final grant =
        await ref.read(resellerRepositoryProvider).impersonate(accountId);
    ref.read(apiClientProvider).impersonationToken = grant.accessToken;
    // A view-as session reads a DIFFERENT principal's data over the same
    // transport. Invalidating the in-memory providers (below) is not enough —
    // the on-disk cache would otherwise hand the customer's bodies back to the
    // reseller after the grant lapses, or vice versa. Give it its own scope,
    // so the two partitions cannot address each other at all.
    final cache = ref.read(responseCacheProvider);
    _ownScope = cache.scope;
    cache.useScope(MobileDataScope(
      tenant: Env.apiBaseUrl,
      principal: 'view-as:${grant.accountId}',
    ));
    final s = ImpersonationState(
      customerName: grant.customerName,
      accountId: grant.accountId,
      expiresAt: grant.expiresAt,
    );
    state = s;
    _refreshCustomerData();
    return s;
  }

  void stop() {
    ref.read(apiClientProvider).impersonationToken = null;
    final restore = _ownScope;
    if (restore != null) {
      ref.read(responseCacheProvider).useScope(restore);
      _ownScope = null;
    }
    state = null;
    _refreshCustomerData();
  }

  /// The short-lived "view as" grant lapsed mid-session (a request returned 401
  /// while impersonating). Clear it, route back to the reseller area, and tell
  /// the user — never fail silently. Idempotent: a no-op once already cleared,
  /// so concurrent 401s don't double-notify.
  void expire() {
    if (state == null) return;
    stop();
    rootNavigatorKey.currentContext?.go('/reseller');
    ref.read(scaffoldMessengerKeyProvider).currentState?.showSnackBar(
          const SnackBar(
            content: Text(
              'View-as session expired — returned to your reseller account.',
            ),
          ),
        );
  }

  /// Cached customer-scope data must not leak across identities.
  void _refreshCustomerData() {
    ref.invalidate(subscriptionsProvider);
    ref.invalidate(accountHealthProvider);
    ref.invalidate(invoicesProvider);
    ref.invalidate(quotaBucketsProvider);
    ref.invalidate(accountingSessionsProvider);
    ref.invalidate(usageSummaryProvider);
    ref.invalidate(notificationsProvider);
    ref.invalidate(addonsProvider);
  }
}

final impersonationProvider =
    NotifierProvider<ImpersonationController, ImpersonationState?>(
  ImpersonationController.new,
);
