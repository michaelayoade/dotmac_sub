import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'skeleton.dart';

/// Renders an [AsyncValue] with consistent loading / error (with retry) /
/// data states across the app.
///
/// Stale-while-revalidate: while a refresh or re-listen is in flight and a
/// previous value exists, the cached data stays on screen (with a thin progress
/// bar at the top) instead of flashing back to the loading state. If a
/// background refresh *fails* but we still hold data, the stale data is kept
/// with a quiet "couldn't refresh" banner rather than being replaced by a
/// full-screen error. The loading/error states are only for the *first* load,
/// when there is nothing to show yet.
class AsyncValueView<T> extends StatelessWidget {
  const AsyncValueView({
    super.key,
    required this.value,
    required this.data,
    this.onRetry,
    this.skeleton,
  });

  final AsyncValue<T> value;
  final Widget Function(T data) data;
  final VoidCallback? onRetry;

  /// Placeholder shown on the *first* load (no cached value yet). Defaults to a
  /// centered spinner; pass a [ListSkeleton]/[CardSkeleton] for a shaped one.
  final Widget? skeleton;

  @override
  Widget build(BuildContext context) {
    final refreshing = value.isLoading && value.hasValue;
    final content = value.when(
      skipLoadingOnReload: true,
      skipLoadingOnRefresh: true,
      loading: () =>
          skeleton ?? const Center(child: CircularProgressIndicator()),
      // On a failed *refresh* we still have the last good value: keep showing it
      // with a quiet "couldn't refresh" banner rather than wiping the screen.
      error: (err, _) => value.hasValue
          ? Stack(
              children: [
                data(value.requireValue),
                Positioned(
                  top: 0,
                  left: 0,
                  right: 0,
                  child: StaleBanner(onRetry: onRetry),
                ),
              ],
            )
          : _ErrorState(message: '$err', onRetry: onRetry),
      data: data,
    );

    if (!refreshing) return content;
    return Stack(
      children: [
        content,
        const Positioned(
          top: 0,
          left: 0,
          right: 0,
          child: LinearProgressIndicator(minHeight: 2),
        ),
      ],
    );
  }
}

/// Unobtrusive notice shown over still-valid (stale) data when a refresh failed.
class StaleBanner extends StatelessWidget {
  const StaleBanner({super.key, this.onRetry});

  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Row(
          children: [
            Icon(Icons.cloud_off, size: 16, color: scheme.onSurfaceVariant),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                'Showing saved data — couldn’t refresh.',
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
              ),
            ),
            if (onRetry != null)
              TextButton(
                onPressed: onRetry,
                style: TextButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  minimumSize: const Size(0, 28),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
                child: const Text('Retry'),
              ),
          ],
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.message, this.onRetry});

  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.cloud_off,
              size: 48,
              color: Theme.of(context).colorScheme.error,
            ),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            if (onRetry != null) ...[
              const SizedBox(height: 16),
              FilledButton.tonal(
                onPressed: onRetry,
                child: const Text('Retry'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// A simple centered empty-state placeholder.
class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.message});

  final IconData icon;
  final String message;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(context).colorScheme.outline;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 48, color: muted),
            const SizedBox(height: 12),
            Text(message, style: TextStyle(color: muted)),
          ],
        ),
      ),
    );
  }
}
