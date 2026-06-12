import 'package:flutter/material.dart';

/// Lightweight, dependency-free shimmer. Wrap a tree of [SkeletonBox]
/// placeholders in a [Shimmer] to animate a sweeping highlight across them while
/// real content loads — far less jarring than a bare spinner.
class Shimmer extends StatefulWidget {
  const Shimmer({super.key, required this.child});

  final Widget child;

  @override
  State<Shimmer> createState() => _ShimmerState();
}

class _ShimmerState extends State<Shimmer> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final base = scheme.surfaceContainerHighest;
    final highlight =
        Color.alphaBlend(scheme.onSurface.withValues(alpha: 0.08), base);
    return AnimatedBuilder(
      animation: _controller,
      child: widget.child,
      builder: (context, child) {
        return ShaderMask(
          blendMode: BlendMode.srcATop,
          shaderCallback: (bounds) {
            // Sweep the highlight band left → right across the bounds.
            final dx = bounds.width * (2 * _controller.value - 1);
            return LinearGradient(
              begin: Alignment.centerLeft,
              end: Alignment.centerRight,
              colors: [base, highlight, base],
              stops: const [0.35, 0.5, 0.65],
              transform: _SlidingGradient(dx),
            ).createShader(bounds);
          },
          child: child,
        );
      },
    );
  }
}

class _SlidingGradient extends GradientTransform {
  const _SlidingGradient(this.dx);

  final double dx;

  @override
  Matrix4 transform(Rect bounds, {TextDirection? textDirection}) =>
      Matrix4.translationValues(dx, 0, 0);
}

/// A single rounded placeholder block. Colour is supplied by [Shimmer]'s shader,
/// so any opaque fill works here.
class SkeletonBox extends StatelessWidget {
  const SkeletonBox({
    super.key,
    this.width,
    this.height = 14,
    this.radius = 8,
  });

  final double? width;
  final double height;
  final double radius;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(radius),
      ),
    );
  }
}

/// A shimmering list of card-shaped rows, shaped like a typical list/detail
/// screen. Drop in as the `skeleton` of an `AsyncValueView` for list screens.
class ListSkeleton extends StatelessWidget {
  const ListSkeleton({super.key, this.rows = 6, this.hasLeading = false});

  final int rows;
  final bool hasLeading;

  @override
  Widget build(BuildContext context) {
    return Shimmer(
      child: ListView.separated(
        padding: const EdgeInsets.all(12),
        physics: const NeverScrollableScrollPhysics(),
        itemCount: rows,
        separatorBuilder: (_, __) => const SizedBox(height: 8),
        itemBuilder: (_, __) => _RowSkeleton(hasLeading: hasLeading),
      ),
    );
  }
}

class _RowSkeleton extends StatelessWidget {
  const _RowSkeleton({required this.hasLeading});

  final bool hasLeading;

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            if (hasLeading) ...[
              const SkeletonBox(width: 36, height: 36, radius: 18),
              const SizedBox(width: 12),
            ],
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SkeletonBox(width: 140),
                  SizedBox(height: 8),
                  SkeletonBox(width: 90, height: 12),
                ],
              ),
            ),
            const SizedBox(width: 12),
            const SkeletonBox(width: 56, height: 12),
          ],
        ),
      ),
    );
  }
}

/// A single shimmering card (e.g. the dashboard "Current service" placeholder).
class CardSkeleton extends StatelessWidget {
  const CardSkeleton({super.key, this.height = 88});

  final double height;

  @override
  Widget build(BuildContext context) {
    return Shimmer(
      child: Card(
        margin: EdgeInsets.zero,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const SkeletonBox(width: 160),
              const SizedBox(height: 10),
              const SkeletonBox(width: 100, height: 12),
              SizedBox(height: height * 0.2 + 8),
              const SkeletonBox(width: double.infinity, height: 12),
            ],
          ),
        ),
      ),
    );
  }
}
