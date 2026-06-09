import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/subscription.dart';
import '../../providers/data_providers.dart';
import '../../widgets/skeleton.dart';

/// Resolves the `:id` path parameter of a `/service/...` route into a
/// [Subscription] for the drill-down screens. The originating screen passes
/// the object via the route's `extra` (no lookup needed); on a deep link or
/// process restore we fall back to the subscriptions cache.
class ServiceRoute extends ConsumerWidget {
  const ServiceRoute({
    super.key,
    required this.id,
    this.initial,
    required this.builder,
  });

  final String id;
  final Subscription? initial;
  final Widget Function(Subscription service) builder;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final passed = initial;
    if (passed != null && passed.id == id) return builder(passed);

    final subs = ref.watch(subscriptionsProvider);
    if (subs.hasValue) {
      for (final s in subs.requireValue.items) {
        if (s.id == id) return builder(s);
      }
      return Scaffold(
        appBar: AppBar(title: const Text('Service')),
        body: const Center(child: Text('Service not found.')),
      );
    }
    return Scaffold(
      appBar: AppBar(title: const Text('Service')),
      body: subs.isLoading
          ? const Padding(padding: EdgeInsets.all(16), child: CardSkeleton())
          : const Center(
              child: Text('Couldn’t load this service. Please try again.'),
            ),
    );
  }
}
